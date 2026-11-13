"""
PulseMeasurementService unit tesztek — SUP01-ARCH-001/PULSE-002..011.
MockDMM/MockLoad-dal, instrumentum-időzített chunk-acquisition szimulációval.
"""
from __future__ import annotations

import pytest

from Prog.src.exceptions import InstrumentInvalidReading
from Prog.src.pulse_measurement_service import (
    PulseMeasurementService,
    _parse_csv_floats,
)
from Prog.src.safety import SafetyManager
from Prog.tests.mock_drivers.fake_clock import (
    FakeMonotonicClock,
    InstrumentLatencyClock,
    bind_pulse_instrument_latency,
)
from Prog.tests.mock_drivers.mock_dmm import MockDMM
from Prog.tests.mock_drivers.mock_load import MockLoad


def _pulse_safety(cell_count: int = 6) -> SafetyManager:
    # Önálló mock-profil; az éles PMS kötelező safety-függőségét adjuk át.
    from Prog.src.battery_profile import BatteryProfile
    from Prog.src.safety import PsuMode
    profile = BatteryProfile(
        battery_name="TEST", manufacturer="FIAMM", model="FG20721",
        nominal_voltage_V=2.0 * cell_count, cell_count=cell_count,
        nominal_capacity_Ah=7.2, i10_discharge_current_A=0.67,
    )
    return SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)


def make_service(
    terminate_voltage_V: float = 10.8,
    chunk_sample_count: int = 5,
    sample_interval_s: float = 0.2,
    time_source=None,
) -> tuple[PulseMeasurementService, MockLoad, MockDMM]:
    load = MockLoad()
    dmm_v = MockDMM()
    # Alapértelmezésben "ideális, holtidő nélküli hardver" szimuláció — minden
    # chunk valós ideje pontosan a deklarált mintaszám × intervallum. Egyes,
    # kifejezetten a valós-idő-korrekciót/driftet tesztelő esetek saját
    # FakeMonotonicClock-ot adnak át.
    clock = time_source or FakeMonotonicClock(step_s=chunk_sample_count * sample_interval_s)
    service = PulseMeasurementService(
        load, dmm_v, terminate_voltage_V,
        chunk_sample_count=chunk_sample_count,
        sample_interval_s=sample_interval_s,
        time_source=clock,
        safety=_pulse_safety(),
        max_voltage_V=14.55,
        max_load_power_W=SafetyManager.max_load_power_W,
    )
    return service, load, dmm_v


def _run_all_chunks(service: PulseMeasurementService, max_chunks: int = 60) -> None:
    n = 0
    while service.step_chunk() and n < max_chunks:
        n += 1


class TestBeginPulse:
    def test_begin_pulse_reads_baseline_voltage(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        started = service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        assert started is True
        assert dmm_v.called("read_voltage()")

    def test_begin_pulse_turns_on_load_in_cc_mode(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        assert load.called("set_mode_cc()")
        assert load.called("set_current(1.4)")
        assert load.input_commanded_on is True

    def test_begin_pulse_skips_when_already_below_terminate(self):
        service, load, dmm_v = make_service(terminate_voltage_V=11.4)
        dmm_v.voltage_V = 11.0
        started = service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        assert started is False
        assert load.input_commanded_on is False

    def test_begin_pulse_skipped_result_is_skipped_low_voltage(self):
        service, load, dmm_v = make_service(terminate_voltage_V=11.4)
        dmm_v.voltage_V = 11.0
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        result = service.finalize()
        assert result.started is False
        assert result.skipped_low_voltage is True
        assert result.results == {}

    def test_begin_pulse_attempts_input_off_when_load_energizing_fails(self):
        """[Kör 20 Batch E14, HW20-07, DFAM-016] `begin_pulse()` korábban
        az EGYETLEN Load-energizáló ág volt a fájlban, ami hiba esetén NEM
        kísérelte meg az `input_off()`-ot -- csak `_restore_acquisition_
        nplc()`-t hívott, majd újra dobta a kivételt. Ez a teszt a fix
        ELŐTTI kódon bukna: `load.calls` NEM tartalmazna `"input_off()"`-ot,
        és az eredeti kivétel is csak akkor propagál helyesen, ha a
        best-effort `input_off()` sosem nyeli el (itt sikeresen fut, tehát
        nem is nyelhetné el)."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5

        def _raise(*a, **kw):
            raise RuntimeError("simulated load communication failure")

        load.input_on = _raise

        with pytest.raises(RuntimeError, match="simulated load communication failure"):
            service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)

        assert "input_off()" in load.calls

    def test_begin_pulse_propagates_original_exception_even_if_input_off_also_fails(self):
        """Counter-test: a best-effort `input_off()` SOSEM fedheti el az
        eredeti hibát -- ha MAGA az `input_off()` is hibázik (pl. ugyanaz
        a soros port már megszakadt), az EREDETI kivételnek kell
        propagálnia, nem az `input_off()` másodikjának (rules/10-safety.md
        #3: "egy OFF-hiba ne akadályozza a másikat" -- itt az energizálási
        hiba ELSŐBBSÉGE a takarítási hiba felett)."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5

        def _raise_input_on(*a, **kw):
            raise RuntimeError("original load communication failure")

        def _raise_input_off(*a, **kw):
            raise RuntimeError("input_off ALSO failed")

        load.input_on = _raise_input_on
        load.input_off = _raise_input_off

        with pytest.raises(RuntimeError, match="original load communication failure"):
            service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)


class TestKor20BatchE14ParseCsvFloatsValueErrorHandling:
    """[Kör 20 Batch E14, HW20-05, DFAM-002+DFAM-001] `_parse_csv_floats()`
    korábban egy bare `float(x)`-et hívott -- a device_dmm.py::
    _parse_float() referencia-mintájával ellentétben NEM fogta el a
    ValueError-t (egy csonka/torzult SCPI-válasz nem numerikus tokenje). A
    hívók KIZÁRÓLAG InstrumentInvalidReading-et kapnak el (az aktív-
    impulzus Load-OFF láncot hajtó kivétel-osztály) -- egy ValueError ezt a
    láncot elkerülve, kivételként propagált volna."""

    def test_malformed_token_raises_instrument_invalid_reading_when_reject_invalid(self):
        """Ez a teszt a fix ELŐTTI kódon `ValueError`-t dobna, NEM
        `InstrumentInvalidReading`-et -- a hívó Load-OFF láncát elkerülve."""
        with pytest.raises(InstrumentInvalidReading):
            _parse_csv_floats("1.5,GARBLED,2.5", reject_invalid=True)

    def test_malformed_token_skipped_when_reject_invalid_false(self):
        """A `reject_invalid=False` best-effort úton (l. a függvény
        docstringje) egy értelmezhetetlen token egyszerű kihagyása a
        helyes, meglévő fail-soft viselkedés -- ugyanaz a minta, mint a
        NaN/Inf/overload esetén."""
        result = _parse_csv_floats("1.5,GARBLED,2.5", reject_invalid=False)
        assert result == [1.5, 2.5]

    def test_well_formed_values_unaffected(self):
        """Counter-test: a fix nem szűkítheti a jól formázott bemeneteket."""
        result = _parse_csv_floats("1.5,2.5,-3.25", reject_invalid=True)
        assert result == [1.5, 2.5, -3.25]


# R3-B1 (R3-metrology-01, DFAM-001): üres/whitespace CSV-mező. A
# leading/inner/trailing/space/tab esetek mind ugyanazt a "részleges
# szűrés" hibaosztályt fedik, amit a _parse_csv_floats() docstringje a
# szigorú (reject_invalid=True) módban tilt.
_R3B1_EMPTY_TOKEN_BLOCKS = [
    pytest.param(",1.5,2.5", id="leading"),
    pytest.param("1.5,,2.5", id="inner"),
    pytest.param("1.5,2.5,", id="trailing"),
    pytest.param("1.5, ,2.5", id="space"),
    pytest.param("1.5,\t,2.5", id="tab"),
]


class TestR3B1EmptyCsvTokenStrict:
    """[regresszió, R3-B1 (R3-metrology-01), DFAM-001] A _parse_csv_floats()
    az üres/whitespace mezőt korábban mindkét módban csendben kihagyta --
    szigorú módban ez részleges szűrés, ami a chunk hosszára épülő
    időbélyeg-hozzárendelést (t = chunk_end - (n-1-i)*interval) eltolja.
    Szigorú módban a teljes chunk elutasítandó (InstrumentInvalidReading ->
    meglévő Load-OFF lánc), best-effort módban a mező kimarad. A teljesen
    üres válasz ([]) és az érvényes, lezáró karakteres blokk változatlan.
    Unit/mock bizonyíték; a 34465A FETCh? valós válaszformája HIL_PENDING."""

    @pytest.mark.parametrize("raw", _R3B1_EMPTY_TOKEN_BLOCKS)
    def test_strict_rejects(self, raw):
        with pytest.raises(InstrumentInvalidReading):
            _parse_csv_floats(raw, reject_invalid=True)

    @pytest.mark.parametrize("raw", _R3B1_EMPTY_TOKEN_BLOCKS)
    def test_strict_default_rejects(self, raw):
        """A default (reject_invalid=True) is szigorú."""
        with pytest.raises(InstrumentInvalidReading):
            _parse_csv_floats(raw)

    @pytest.mark.parametrize("raw", _R3B1_EMPTY_TOKEN_BLOCKS)
    def test_best_effort_skips(self, raw):
        assert _parse_csv_floats(raw, reject_invalid=False) == [1.5, 2.5]

    @pytest.mark.parametrize("raw", ["", "   ", "\n", "\r\n"])
    @pytest.mark.parametrize("strict", [True, False])
    def test_fully_empty_unchanged(self, raw, strict):
        """KEEP_AS_IS: a teljesen üres válasz [] marad (-> NO_VALID_SAMPLES ág)."""
        assert _parse_csv_floats(raw, reject_invalid=strict) == []

    @pytest.mark.parametrize("raw", ["1.5,2.5\n", "1.5,2.5\r\n", " 1.5 , 2.5 "])
    def test_valid_block_with_terminator(self, raw):
        assert _parse_csv_floats(raw, reject_invalid=True) == [1.5, 2.5]

    def test_empty_token_chunk_turns_load_off_and_raises(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        assert load.input_commanded_on is True
        dmm_v.fetch_timed_acquisition_raw_override = "13.4,13.3,,13.2,13.1"
        with pytest.raises(InstrumentInvalidReading):
            service.step_chunk()
        assert load.input_commanded_on is False

    def test_prior_chunk_energy_preserved(self):
        """A hibás chunk előtti, már mért prefix változatlan marad."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        assert service.step_chunk() is True
        before = service.measured_energy_so_far()
        assert before.reading_count == 1
        dmm_v.fetch_timed_acquisition_raw_override = "13.4,13.3,13.2,13.1,"
        with pytest.raises(InstrumentInvalidReading):
            service.step_chunk()
        assert service.measured_energy_so_far() == before
        assert load.input_commanded_on is False


class TestChunkAcquisition:
    def test_step_chunk_configures_timed_acquisition(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        service.step_chunk()
        assert dmm_v.called("configure_timed_acquisition(sample_count=5, interval_s=0.2)")
        assert dmm_v.called("arm_timed_acquisition()")
        assert dmm_v.called("fetch_timed_acquisition()")

    def test_full_pulse_reaches_target_times_exactly(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        load.current_A = 0.0
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()

        r1 = result.result_at(1.0)
        r10 = result.result_at(10.0)
        r30 = result.result_at(30.0)
        assert r1 is not None and r1.reached
        assert r1.actual_time_s == pytest.approx(1.0)
        assert r1.time_error_s == pytest.approx(0.0)
        assert r10.actual_time_s == pytest.approx(10.0)
        assert r30.actual_time_s == pytest.approx(30.0)

    def test_resistance_computed_from_measured_delta(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()

        r1 = result.result_at(1.0)
        assert r1.delta_voltage_V == pytest.approx(0.3)
        assert r1.delta_current_A == pytest.approx(1.4)
        assert r1.resistance_ohm == pytest.approx(0.3 / 1.4, rel=1e-3)
        assert r1.resistance_mOhm == pytest.approx((0.3 / 1.4) * 1000.0, rel=1e-3)

    def test_load_off_after_full_pulse(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        assert load.input_commanded_on is False

    def test_mean_current_uses_measured_readings(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        load.current_A = 1.35   # mért ≠ setpoint — begin_pulse() UTÁN, mert
                                 # set_current(1.4) felülírná indításkor
        _run_all_chunks(service)
        result = service.finalize()
        assert result.pulse_current_measured_mean_A == pytest.approx(1.35)

    def test_removed_ah_matches_constant_current_integral(self):
        # R1 B8a (F-21, D-B8-8) fixture-korrekció, az elvárás VÁLTOZATLAN:
        # a FakeMonotonicClock minden lekérdezésre egy teljes chunkidőt
        # léptet -- az új OFF-nyugta horgony lekérdezésére is, ami egy nem
        # létező +1 chunk terhelési időt gyártana. Az InstrumentLatencyClock
        # nulla latenciával csak a DMM-fetch-ben lép (ideális hardver).
        clock = InstrumentLatencyClock()
        service, load, dmm_v = make_service(time_source=clock)
        bind_pulse_instrument_latency(clock, dmm_v, load)
        dmm_v.voltage_V = 13.5
        load.current_A = 1.4
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.removed_Ah == pytest.approx(1.4 * 30.0 / 3600.0, rel=1e-3)

    def test_removed_wh_matches_constant_current_voltage_integral(self):
        """Teljes kódbázis review 16. kör (2026-09-04), Metrológia/
        Adatintegritás P2 lelet: a removed_Ah testvérpárja — korábban a
        PulseAcquisitionResult nem exponált Wh-adatot.

        R1 B8a (F-21, D-B8-8) fixture-korrekció, az elvárás VÁLTOZATLAN --
        l. test_removed_ah_matches_constant_current_integral kommentje."""
        clock = InstrumentLatencyClock()
        service, load, dmm_v = make_service(time_source=clock)
        bind_pulse_instrument_latency(clock, dmm_v, load)
        dmm_v.voltage_V = 13.5
        load.current_A = 1.4
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.removed_Wh == pytest.approx(1.4 * 13.5 * 30.0 / 3600.0, rel=1e-3)

    def test_synchronization_quality_is_instrument_timed(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.synchronization_quality == "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"


class TestB8aPhysicalLoadWindow:
    """[regresszió, R1 B8a, F-21 + F-22] Az R(t)-értékelési ablak (a cutoffig
    elfogadott minták) és a fizikai terhelési ablak (ON-nyugta -> OFF-
    nyugta) szétválasztása, valamint a mintaidő-horgony a fetch után, a
    reset ELŐTT. Az óra csak a mockolt műszerhívások valós idejével lép
    (InstrumentLatencyClock): fetch = 5 × 0,2 s, reset 0,3 s, áramolvasás
    0,05 s, Load OFF 0,07 s. A konstans mért pulzusáram (1,3 A) SZÁNDÉKOSAN
    eltér a setpointtól (1,4 A), hogy a setpoint-fallback ne fedje el a
    hibát. Unit/mock -- a fizikai kapcsolási késés HIL_PENDING."""

    I_SET = 1.4
    I_MEAS = 1.3
    RESET_S = 0.3
    MEASURE_S = 0.05
    OFF_S = 0.07

    def _make(self, duration_s, voltage_fn, recovery_duration_s=0.0):
        clock = InstrumentLatencyClock()
        load = MockLoad()
        dmm_v = MockDMM()
        service = PulseMeasurementService(
            load, dmm_v, 11.4,
            chunk_sample_count=5,
            sample_interval_s=0.2,
            target_times_s=(1.0, 2.0),
            time_tolerance_s=0.2,
            time_source=clock,
            recovery_duration_s=recovery_duration_s,
            recovery_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        bind_pulse_instrument_latency(
            clock, dmm_v, load,
            reset_s=self.RESET_S, measure_current_s=self.MEASURE_S, input_off_s=self.OFF_S,
        )
        dmm_v.voltage_V = 13.0
        dmm_v.timed_voltage_fn = voltage_fn
        assert service.begin_pulse(pulse_current_A=self.I_SET, duration_s=duration_s)
        load.current_A = self.I_MEAS  # mért != setpoint (begin_pulse() UTÁN)
        return service, load, dmm_v, clock

    def test_cutoff_in_first_chunk_energy_and_duration_until_load_off_ack(self):
        # chunk 1: fetch vége t=1,0 (horgony) -> minták 0,2/0,4/0,6 (cutoff
        # a 0,6 s-os mintán, a 0,8/1,0 eldobva); reset 1,3; áram 1,35;
        # OFF-nyugta 1,42.
        service, load, _dmm_v, _clock = self._make(
            duration_s=2.0, voltage_fn=lambda t: 13.0 if t < 0.5 else 11.0,
        )
        assert service.step_chunk() is False
        result = service.finalize()

        t_off = 1.0 + self.RESET_S + self.MEASURE_S + self.OFF_S
        assert result.cutoff_during_pulse is True
        assert load.input_commanded_on is False
        assert result.pulse_duration_actual_s == pytest.approx(t_off)
        assert result.removed_Ah == pytest.approx(self.I_MEAS * t_off / 3600.0, rel=1e-9)
        # Wh: a cutoff-minta feszültsége (11,0 V) a legközelebbi minta a
        # [0; 0,6] és a kiterjesztett [0,6; t_off] szakaszhoz is (BECSLÉS).
        assert result.removed_Wh == pytest.approx(
            self.I_MEAS * 11.0 * t_off / 3600.0, rel=1e-9
        )
        # R(t)-hez új minta NEM jött létre, a cutoff utáni minták kizárva:
        assert [round(s.time_from_pulse_start_s, 9) for s in result.samples] == [0.2, 0.4, 0.6]
        assert result.samples[-1].voltage_V == pytest.approx(11.0)
        assert result.result_at(1.0).reached is False
        assert result.result_at(2.0).reached is False
        assert result.pulse_current_degraded is False

    def test_no_cutoff_energy_and_duration_until_load_off_ack(self):
        # chunk 1: fetch 1,0 (horgony), reset 1,3, áram 1,35;
        # chunk 2: fetch 2,35 (horgony), reset 2,65, áram 2,70; OFF 2,77.
        service, load, _dmm_v, _clock = self._make(duration_s=2.0, voltage_fn=lambda t: 12.9)
        assert service.step_chunk() is True
        assert service.step_chunk() is False
        result = service.finalize()

        t_off = 2.0 + 2 * (self.RESET_S + self.MEASURE_S) + self.OFF_S
        assert result.cutoff_during_pulse is False
        assert load.input_commanded_on is False
        assert result.pulse_duration_actual_s == pytest.approx(t_off)
        assert result.removed_Ah == pytest.approx(self.I_MEAS * t_off / 3600.0, rel=1e-9)
        assert result.removed_Wh == pytest.approx(
            self.I_MEAS * 12.9 * t_off / 3600.0, rel=1e-9
        )
        assert result.pulse_current_measured_mean_A == pytest.approx(self.I_MEAS)
        assert len(result.samples) == 10
        assert result.pulse_current_degraded is False

    def test_tail_extended_with_last_measured_current_when_current_varies(self):
        """P3-2 (B8a verifier): a farok [utolsó áramleolvasás, OFF-nyugta]
        az UTOLSÓ mért árammal terjed ki -- nem az elsővel, nem az átlaggal.
        Ehhez chunkonként VÁLTOZÓ mért áram kell (1,3 A -> 0,7 A); konstans
        áramnál a három változat egybeesne."""
        service, load, _dmm_v, _clock = self._make(duration_s=2.0, voltage_fn=lambda t: 12.9)
        assert service.step_chunk() is True  # 1. chunk áramolvasása: 1,3 A
        load.current_A = 0.7
        assert service.step_chunk() is False  # 2. chunk áramolvasása: 0,7 A
        result = service.finalize()

        # Független idővonal (az órát csak a mock-latenciák léptetik):
        # chunk 1: fetch vége 1,00 (1. leolvasás ideje, 1,3 A), reset 1,30,
        # áram 1,35; chunk 2: fetch vége 2,35 (2. leolvasás ideje, 0,7 A),
        # reset 2,65, áram 2,70; OFF-nyugta 2,77.
        # Szakaszonkénti Σ i·dt (minden leolvasás az előző leolvasás óta
        # eltelt szakaszt fedi, a farok az utolsó leolvasás értékével):
        #   [0; 1,00]    1,3 A · 1,00 s = 1,300 As
        #   [1,00; 2,35] 0,7 A · 1,35 s = 0,945 As
        #   [2,35; 2,77] 0,7 A · 0,42 s = 0,294 As  (farok)
        # Az ELSŐ árammal (1,3 A) a farok 0,546 As, az átlaggal (1,0 A)
        # 0,420 As lenne -- mindkettő eltér.
        expected_As = 1.3 * 1.00 + 0.7 * 1.35 + 0.7 * 0.42
        assert result.cutoff_during_pulse is False
        assert result.pulse_current_degraded is False
        assert result.pulse_duration_actual_s == pytest.approx(2.77)
        assert result.pulse_current_measured_mean_A == pytest.approx(1.0)
        assert result.removed_Ah == pytest.approx(expected_As / 3600.0, rel=1e-9)
        assert result.removed_Wh == pytest.approx(expected_As * 12.9 / 3600.0, rel=1e-9)

    def test_sample_times_anchored_at_fetch_end_before_reset(self):
        """F-22: chunkonként az utolsó minta ideje == a fetch visszatérése,
        az első == fetch vége - (n-1)·interval -- a reset 0,3 s-os
        round-tripje NEM tolja el (a régi kódon mindkét chunk minden
        mintája +0,3 s-mal később)."""
        service, _load, _dmm_v, _clock = self._make(duration_s=2.0, voltage_fn=lambda t: 12.9)
        while service.step_chunk():
            pass
        result = service.finalize()
        chunk2_end = 2.0 + self.RESET_S + self.MEASURE_S
        expected = [0.2, 0.4, 0.6, 0.8, 1.0] + [
            chunk2_end - (4 - i) * 0.2 for i in range(5)
        ]
        assert [s.time_from_pulse_start_s for s in result.samples] == pytest.approx(expected)

    def test_recovery_window_excluded_from_energy_and_duration(self):
        service, _load, _dmm_v, clock = self._make(
            duration_s=2.0, voltage_fn=lambda t: 12.9, recovery_duration_s=1.0,
        )
        while service.step_chunk():
            pass
        result = service.finalize()

        t_off = 2.0 + 2 * (self.RESET_S + self.MEASURE_S) + self.OFF_S
        # a recovery-ablak (1,0 s fetch + 0,3 s reset) ténylegesen eltelt...
        assert clock.now - 100.0 == pytest.approx(t_off + 1.0 + self.RESET_S)
        assert len(result.recovery_samples) == 5
        assert all(s.time_from_pulse_start_s > t_off for s in result.recovery_samples)
        # ...de sem az időtartamba, sem az energiába nem kerül.
        assert result.pulse_duration_actual_s == pytest.approx(t_off)
        assert result.removed_Ah == pytest.approx(self.I_MEAS * t_off / 3600.0, rel=1e-9)
        assert result.removed_Wh == pytest.approx(
            self.I_MEAS * 12.9 * t_off / 3600.0, rel=1e-9
        )

    def test_repeated_finalize_is_idempotent(self):
        service, load, _dmm_v, _clock = self._make(duration_s=2.0, voltage_fn=lambda t: 12.9)
        while service.step_chunk():
            pass
        first = service.finalize()
        offs = load.calls.count("input_off()")
        second = service.finalize()
        assert load.calls.count("input_off()") == offs
        assert second.removed_Ah == first.removed_Ah
        assert second.removed_Wh == first.removed_Wh
        assert second.pulse_duration_actual_s == first.pulse_duration_actual_s
        assert second.samples == first.samples
        assert second.results == first.results

    def test_defensive_finalize_off_branch_anchors_load_off_and_is_idempotent(self):
        """finalize() a step_chunk()-ciklus lezárása ELŐTT (1. chunk után a
        2-ből): a védekező ág Load OFF-ja után is OFF-nyugta horgony kell
        (az utolsó mért árammal kiterjesztve); a második finalize() nem ad
        ki újabb OFF-ot és ugyanazt adja."""
        service, load, _dmm_v, _clock = self._make(duration_s=2.0, voltage_fn=lambda t: 12.9)
        assert service.step_chunk() is True
        first = service.finalize()
        t_off = 1.0 + self.RESET_S + self.MEASURE_S + self.OFF_S
        assert load.input_commanded_on is False
        assert load.calls.count("input_off()") == 1
        assert first.pulse_duration_actual_s == pytest.approx(t_off)
        assert first.removed_Ah == pytest.approx(self.I_MEAS * t_off / 3600.0, rel=1e-9)
        second = service.finalize()
        assert load.calls.count("input_off()") == 1
        assert second.removed_Ah == first.removed_Ah
        assert second.removed_Wh == first.removed_Wh
        assert second.pulse_duration_actual_s == first.pulse_duration_actual_s

    def test_degraded_setpoint_fallback_extends_with_recorded_setpoint(self):
        """A MEGLÉVŐ degraded fallback (SUP01-PULSE-008: áramolvasási hiba
        -> setpoint, pulse_current_degraded=True) változatlan; a fizikai
        ablak kiterjesztése a rögzített (setpoint) értékkel történik, a
        degraded jelölés megmarad."""
        service, load, _dmm_v, _clock = self._make(duration_s=2.0, voltage_fn=lambda t: 12.9)
        load.simulate_current_readback_failure = True
        while service.step_chunk():
            pass
        result = service.finalize()
        t_off = 2.0 + 2 * (self.RESET_S + self.MEASURE_S) + self.OFF_S
        assert result.pulse_current_degraded is True
        assert result.pulse_current_measured_mean_A == pytest.approx(self.I_SET)
        assert result.pulse_duration_actual_s == pytest.approx(t_off)
        assert result.removed_Ah == pytest.approx(self.I_SET * t_off / 3600.0, rel=1e-9)


class TestCutoffDuringPulse:
    def test_cutoff_stops_before_full_duration(self):
        service, load, dmm_v = make_service(terminate_voltage_V=11.4)
        dmm_v.voltage_V = 13.0
        dmm_v.timed_voltage_fn = lambda t: 13.0 if t < 1.0 else 11.0
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)

        more = service.step_chunk()

        assert more is False
        assert load.input_commanded_on is False

    def test_cutoff_result_has_no_30s_point(self):
        service, load, dmm_v = make_service(terminate_voltage_V=11.4)
        dmm_v.voltage_V = 13.0
        dmm_v.timed_voltage_fn = lambda t: 11.0
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()

        assert result.cutoff_during_pulse is True
        r30 = result.result_at(30.0)
        assert r30.reached is False
        assert r30.resistance_ohm is None

    def test_cutoff_does_not_iterate_all_chunks(self):
        """A cutoff az ELSŐ chunk után álljon meg, ne a teljes 30 chunkig."""
        service, load, dmm_v = make_service(terminate_voltage_V=11.4)
        dmm_v.voltage_V = 13.0
        dmm_v.timed_voltage_fn = lambda t: 11.0
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)

        chunks_run = 0
        while service.step_chunk():
            chunks_run += 1
        assert chunks_run == 0  # az 1. hívás már False-t adott (cutoff az 1. chunkban)

    def test_mid_chunk_crossing_discards_post_cutoff_samples(self):
        """Review-lelet: a cutoff-ellenőrzés ne csak a chunk UTOLSÓ mintáját
        nézze — egy chunk KÖZEPÉN történő átlépés (nem az utolsó mintán) is
        azonnal érvényteleníti a cutoff UTÁNI mintákat ugyanabban a chunkban."""
        service, load, dmm_v = make_service(terminate_voltage_V=11.4, chunk_sample_count=5)
        dmm_v.voltage_V = 13.0
        # chunk 1 minták (interval 0.2s): t=0.2→13.0, 0.4→13.0, 0.6→11.0(cutoff!), 0.8→10.9, 1.0→10.9
        voltages = {0.2: 13.0, 0.4: 13.0, 0.6: 11.0, 0.8: 10.9, 1.0: 10.9}
        dmm_v.timed_voltage_fn = lambda t: voltages.get(round(t, 6), 10.9)
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)

        more = service.step_chunk()

        assert more is False
        assert load.input_commanded_on is False
        result = service.finalize()
        assert result.cutoff_during_pulse is True
        # A chunkban CSAK a cutoff-ig (0.2, 0.4, 0.6) tartó minták maradtak —
        # a 0.8/1.0-nál mért (fizikailag már cutoff utáni) 10.9V NEM szerepel.
        assert len(result.samples) == 3
        assert result.samples[-1].time_from_pulse_start_s == pytest.approx(0.6)
        assert result.samples[-1].voltage_V == pytest.approx(11.0)


class TestDmmStickyStateReset:
    """Review-lelet (Critical): a Keysight 34465A-n a SAMPle:COUNt/SOURce
    beállítás TARTÓS — minden chunk után vissza kell állítani, különben a
    következő sima read_voltage() (pl. a következő pulse előellenőrzése)
    hibázna."""

    def test_reset_called_after_each_chunk(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)

        service.step_chunk()

        assert dmm_v.called("reset_to_single_sample()")
        assert dmm_v._timed_mode_active is False

    def test_plain_read_voltage_succeeds_after_full_pulse(self):
        """A MockDMM a valós műszer sticky-állapotát modellezi — ha a service
        elfelejtené a reset_to_single_sample()-t hívni, ez a teszt buktatná."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)

        # A pulse befejezése UTÁN egy sima olvasás (pl. a következő pulse
        # begin_pulse() előellenőrzése, vagy a runner _build_sample()-je)
        # nem hibázhat multi-értékű CSV miatt.
        v = dmm_v.read_voltage()
        assert isinstance(v, float)

    def test_plain_read_voltage_succeeds_after_cutoff(self):
        service, load, dmm_v = make_service(terminate_voltage_V=11.4)
        dmm_v.voltage_V = 13.0
        dmm_v.timed_voltage_fn = lambda t: 11.0
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)

        v = dmm_v.read_voltage()
        assert isinstance(v, float)


class TestDegradedCurrent:
    def test_measure_current_failure_sets_degraded(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        load.simulate_current_readback_failure = True
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.pulse_current_degraded is True

    def test_degraded_uses_setpoint_for_all_points(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        load.simulate_current_readback_failure = True
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()

        r1 = result.result_at(1.0)
        assert r1.current_A == pytest.approx(1.4)
        assert result.pulse_current_measured_mean_A == pytest.approx(1.4)


class TestFaultSafety:
    def test_dmm_failure_during_chunk_propagates(self):
        """SUP01-SAFE-002: DMM-hiba a hívóra hárul (a controller emergency_stop-ol)."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        dmm_v.simulate_timed_acquisition_failure = True
        with pytest.raises(Exception):
            service.step_chunk()

    def test_dmm_failure_during_chunk_turns_load_off_before_raising(self):
        """Review-lelet: a DMM configure/arm/fetch hiba NE hagyja a terhelést
        bekapcsolva — a service saját maga is megpróbálja lekapcsolni, nem
        csak a hívó emergency_stop()-jára hagyatkozik."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        assert load.input_commanded_on is True
        dmm_v.simulate_timed_acquisition_failure = True

        with pytest.raises(Exception):
            service.step_chunk()

        assert load.input_commanded_on is False

    def test_dmm_failure_during_chunk_still_resets_sticky_timed_mode(self):
        """[regresszió, review-kör 5, Hardver HW5-01 / Architektúra A-3
        (P1, DFAM-001+DFAM-005)] a DMM sticky SAMP:COUN/SAMP:SOURce
        állapotát korábban CSAK a sikeres ágon állítottuk vissza -- egy
        sikertelen acquisition (pl. VISA-hiccup) UTÁN a MockDMM saját
        sticky-modellje szerint minden további read_voltage()
        InstrumentInvalidReading-et dobott volna, mert reset_to_single_
        sample() sosem futott le a hibaágon."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        dmm_v.simulate_timed_acquisition_failure = True

        with pytest.raises(Exception):
            service.step_chunk()

        dmm_v.simulate_timed_acquisition_failure = False
        # Ha a reset lefutott, egy sima read_voltage() ismét működik --
        # a mai (fix előtti) kódon ez InstrumentInvalidReading-et dobna.
        dmm_v.read_voltage()

    def test_dmm_failure_leaves_no_pending_init_armed_state(self):
        """[regresszió, 2026-09-12, INSTR-DMM-ABORT-001] A FETC? hibája után
        a műszer egy FÜGGŐBEN LÉVŐ, INIT-tel armed mérésben marad; ebből
        kizárólag az `ABORt` hozza ki (34460-70-Manual.txt 5527. sor: "Use
        ABORt to return to idle"). A `reset_to_single_sample()` SAMPle-írásai
        önmagukban NEM trigger-parancsok.

        Élő HIL-bizonyíték (2026-09-12, Keysight 34465A MY60016569): a
        következő szkriptfutás ELSŐ, sima `read_voltage()`-e két, vesszővel
        elválasztott értéket kapott ('+1.29388309E+01,+1.29398106E+01'),
        mert a `SAMP:COUN` az előző futás 2-es értékén maradt."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        dmm_v.simulate_timed_acquisition_failure = True

        with pytest.raises(Exception):
            service.step_chunk()

        assert dmm_v._armed is False, (
            "a műszer 'wait-for-trigger' állapotban maradt egy megszakadt "
            "acquisition után — hiányzik az ABORt"
        )

    def test_characterization_pre_fix_reset_without_abort_stays_stuck(self):
        """[karakterizáció] Ugyanaz a forgatókönyv a JAVÍTÁS ELŐTTI driverrel
        emulálva (`reset_aborts_pending_init=False`, azaz `SAMP:COUN 1` +
        `SAMP:SOUR IMM` ABORt nélkül): a műszer armed marad, a konfigurációs
        írások nem érvényesülnek, és a KÖVETKEZŐ sima olvasás elbukik —
        pontosan a 2026-09-12-i 3. HIL-futás tünete.

        Ez a teszt bizonyítja, hogy a fenti regressziós teszt DISZKRIMINÁLÓ
        (nem tautologikus): a mock modellje a fix nélkül ténylegesen bukna."""
        service, load, dmm_v = make_service()
        dmm_v.reset_aborts_pending_init = False   # fix előtti driver
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        dmm_v.simulate_timed_acquisition_failure = True

        with pytest.raises(Exception):
            service.step_chunk()

        assert dmm_v._armed is True
        dmm_v.simulate_timed_acquisition_failure = False
        with pytest.raises(InstrumentInvalidReading):
            dmm_v.read_voltage()

    def test_load_off_failure_after_dmm_error_raises_chained_exception(self):
        """Ha a DMM ÉS a Load OFF is hibázik, a hívó egyértelmű, láncolt
        diagnosztikát kapjon (nem nyeli el a Load OFF hibáját csendben)."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        dmm_v.simulate_timed_acquisition_failure = True

        def _raise_on_off():
            raise RuntimeError("load comm lost")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="IMPULSE_LOAD_OFF_FAILED"):
            service.step_chunk()


class TestSampleTimerConfigRejected:
    """Governance-v2 review-kör 1 (Metrológia reviewer, CF-04/MET-R1-01,
    INSTR-DMM-SAMPTIM-001): a Load-ág (configure_transient_pulse() után)
    már ellenőrzi az error-queue-t, a DMM-ágon (configure_timed_
    acquisition() után) ez korábban hiányzott. Ha a kért SAMPle:TIMer az
    apertúra alatt van, a 34465A CSENDBEN megnöveli az intervallumot és
    -221-et tesz a hibasorba — enélkül a kód a KÉRT, nem a TÉNYLEGES
    intervallummal számolna visszafelé időbélyeget minden mintára. Ez a
    teszt a JELENLEGI kódon bukik."""

    def test_dmm_config_rejection_raises_and_turns_load_off(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        dmm_v.pending_errors = ['-221,"Settings conflict; cannot meet requested timing"']

        with pytest.raises(RuntimeError, match="DMM_TIMED_ACQUISITION_CONFIG_REJECTED"):
            service.step_chunk()

        assert load.input_commanded_on is False

    def test_silent_samp_tim_mismatch_with_empty_error_queue_raises_and_turns_load_off(self):
        """[regresszió, 2026-09-12, musszer-only HIL-mérés, INSTR-DMM-SAMPTIM-001]
        Valós Keysight 34465A-n (NPLC=10, 0,2 s SAMP:TIM kérés) igazolva: a
        műszer a kért intervallumot CSENDBEN, A HIBASORBA SEMMIT NEM ÍRVA is
        figyelmen kívül hagyhatja (a `SAMP:TIM` az előző értékén marad) — ez
        a `check_error()`-alapú védelmet (fenti teszt) NEM váltja ki. Enélkül
        a readback-ellenőrzés (`_verify_actual_sample_timer_or_raise()`)
        nélkül ez a teszt a RÉGI kódon zölden menne át (a chunk "sikeresen"
        lefutna a KÉRT 0,2 s-mal számolva, miközben a műszer valójában más
        intervallumot alkalmazott) — ez a teszt a REGI kódon bukik."""
        service, load, dmm_v = make_service(sample_interval_s=0.2)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        # A hibasor ÜRES marad (nincs pending_errors) — a műszer mégis más
        # SAMP:TIM-et "alkalmazott", mint amit kértünk (a HIL-en mért 1.0s
        # eset mintájára).
        dmm_v.actual_sample_timer_override_s = 1.0

        with pytest.raises(RuntimeError, match="DMM_TIMED_ACQUISITION_CONFIG_REJECTED"):
            service.step_chunk()

        assert load.input_commanded_on is False

    def test_matching_samp_tim_readback_does_not_raise(self):
        """A readback-ellenőrzés NEM új hamis-pozitív forrás: ha a driver a
        ténylegesen kért intervallumot adja vissza (a normál, sikeres eset —
        ez a MockDMM alapértelmezett viselkedése is), a chunk változatlanul
        lefut."""
        service, load, dmm_v = make_service(sample_interval_s=0.2)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)

        service.step_chunk()

        assert dmm_v.called("get_sample_timer_s()")

    def test_missing_get_sample_timer_s_on_driver_does_not_raise(self, monkeypatch):
        """Ha a driver nem támogatja a `get_sample_timer_s()`-t (pl. egy
        régebbi/eltérő mock), a readback-ellenőrzés csendben kimarad — ez
        NEM új hibaosztály, csak azt jelenti, hogy ez a TOVÁBBI védelmi
        réteg nem elérhető; a meglévő `check_error()`-kapu változatlan.
        `monkeypatch` automatikusan visszaállítja a törölt osztály-attribútumot
        a teszt végén, más tesztek nem szenvednek mellékhatást."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        monkeypatch.delattr(type(dmm_v), "get_sample_timer_s")
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        service.step_chunk()  # nem dobhat


class TestInvalidSampleValueRejected:
    """Governance-v2 review-kör 1 (Metrológia reviewer, CF-09/MET-R1-05,
    DFAM-001): a skalár olvasási utakon (device_dmm.py::_validate_voltage,
    device_load.py::_validate_reading) megvan a NaN/Inf/overload (9.9E37)
    védelem, de az időzített, vektoros (CSV) DMM-blokk parszolása korábban
    ezt megkerülte — egy NaN mintára minden `<=`/`>` összehasonlítás False-t
    ad (a cutoff-detektálás csendben kimarad), egy 9.9E37 overload-kód pedig
    fizikailag lehetetlen, de "OK" minőségű R(t) adatpontként kerülne be.
    CLAUDE.md §6: "aktív energiaátadásnál kritikus readback hibát ... tilos
    csendben ... helyettesíteni." Ez a teszt a JELENLEGI kódon bukik."""

    def test_nan_sample_in_chunk_raises_instead_of_silently_poisoning_cutoff(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        dmm_v.timed_voltage_fn = lambda t: float("nan")

        with pytest.raises(Exception):
            service.step_chunk()

    def test_nan_sample_turns_load_off_before_raising(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        assert load.input_commanded_on is True
        dmm_v.timed_voltage_fn = lambda t: float("nan")

        with pytest.raises(Exception):
            service.step_chunk()

        assert load.input_commanded_on is False

    def test_overload_sample_in_chunk_raises(self):
        """9.9E37 — a Keysight overload-kódja — fizikailag lehetetlen
        feszültségként NEM válhat csendben elfogadott mintává."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        dmm_v.timed_voltage_fn = lambda t: 9.9e37

        with pytest.raises(Exception):
            service.step_chunk()


class TestInvalidResistanceNotSilentlyZeroed:
    """Review-lelet: ha DeltaI <= 0 egy ELÉRT ponton, ez NEM válhat csendben
    0.0 mOhm-má minősítés nélkül."""

    def test_zero_delta_current_flagged_as_invalid(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        load.current_A = 0.0  # begin_pulse() UTÁN — mért áram 0 (pl. load hiba)
        _run_all_chunks(service)
        result = service.finalize()

        r1 = result.result_at(1.0)
        assert r1.reached is True
        assert r1.resistance_ohm is None
        assert result.has_invalid_resistance is True

    def test_non_positive_delta_voltage_flagged_as_invalid(self):
        """[regresszió, review-kör 4 (2026-09-09), Metrológia M4-03 (P2,
        DFAM-001 + DFAM-018)] kisütő impulzusnál a terhelés alatti
        feszültség fizikailag NEM lehet magasabb/egyenlő a baseline-nál --
        korábban csak a delta_i előjelére volt kapu, delta_v<=0 (pl. DMM
        range-váltás/Kelvin-érzékelő hiba az impulzus alatt) negatív/nulla
        resistance_ohm-ot adott quality-jelzés nélkül."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5  # baseline
        # A terhelés alatti "mért" feszültség a baseline FÖLÉ esik --
        # fizikailag lehetetlen kisütő impulzusnál, csak mérési hibából jöhet.
        dmm_v.timed_voltage_fn = lambda t: 13.6
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()

        r1 = result.result_at(1.0)
        assert r1.reached is True
        assert r1.resistance_ohm is None
        assert result.has_invalid_resistance is True


class TestRealClockDrift:
    """Review-lelet: a chunkok közötti valós holtidőt (pl. TestRunner
    tick-padding alvása) a service NE hallgassa el — a time_error_s/reached
    mezők tükrözzék őszintén, ha a valós idő eltér a deklarálttól."""

    def test_injected_gap_between_chunks_delays_pulse_duration_actual(self):
        """5 chunk (1 minta/chunk, 0.2s intervallum, névlegesen 1.0s össz.),
        a 3. chunk UTÁN +2.0s valós holtidő (pl. runner tick-padding) —
        pulse_duration_actual_s a VALÓS (drifttel terhelt) időt tükrözze,
        ne a névleges 1.0s-ot."""
        clock = FakeMonotonicClock(step_s=0.2, drift_after_call_index=4, drift_s=2.0)
        service, load, dmm_v = make_service(
            chunk_sample_count=1, sample_interval_s=0.2, time_source=clock
        )
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)  # 5 chunk, 1 minta/chunk

        for _ in range(5):
            service.step_chunk()
        result = service.finalize()

        # Drift nélkül pontosan 1.0s lenne (5 × 0.2s) — a beszúrt +2.0s
        # holtidő miatt a VALÓS utolsó minta ideje ennél jóval nagyobb.
        assert result.pulse_duration_actual_s > 1.0 + 1.5

    def test_removed_ah_uses_real_gap_not_nominal_when_drifted(self):
        """A holtidő alatt is folyamatosan folyik az áram (CC mód) — az
        Ah-integrálásnak a VALÓS (drifttel terhelt) időrést kell használnia,
        különben csendben alulbecsülné az eltávolított töltést."""
        clock_no_drift = FakeMonotonicClock(step_s=0.2)
        service_a, load_a, dmm_a = make_service(
            chunk_sample_count=1, sample_interval_s=0.2, time_source=clock_no_drift
        )
        dmm_a.voltage_V = 13.5
        dmm_a.timed_voltage_fn = lambda t: 13.2
        load_a.current_A = 1.4
        service_a.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        for _ in range(5):
            service_a.step_chunk()
        result_a = service_a.finalize()

        clock_drift = FakeMonotonicClock(step_s=0.2, drift_after_call_index=4, drift_s=2.0)
        service_b, load_b, dmm_b = make_service(
            chunk_sample_count=1, sample_interval_s=0.2, time_source=clock_drift
        )
        dmm_b.voltage_V = 13.5
        dmm_b.timed_voltage_fn = lambda t: 13.2
        load_b.current_A = 1.4
        service_b.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        for _ in range(5):
            service_b.step_chunk()
        result_b = service_b.finalize()

        # A drifttel terhelt futás Ah-ja NAGYOBB, mert a valós idő (nem a
        # névleges) alapján integrálunk — ha csendben a névlegest használnánk,
        # a két érték megegyezne (ez lenne a hiba, amit a review feltárt).
        assert result_b.removed_Ah > result_a.removed_Ah


# ══════════════════════════════════════════════════════════════════════════
# RB Pulse Digitalizálás spec kiegészítés (Folyamatok/tervek/RB_Pulse_
# Digitalizalas_Fejlesztesi_Spec_v1_2026-08.md, 2026-08-22) — mind OPT-IN,
# alapértelmezett paraméterekkel a fenti, MEGLÉVŐ tesztek bit-pontosan
# változatlanul futnak (l. modul PulseMeasurementService docstring).
# ══════════════════════════════════════════════════════════════════════════

class TestTriggerModeValidation:
    def test_rejects_unknown_trigger_mode(self):
        load, dmm_v = MockLoad(), MockDMM()
        with pytest.raises(ValueError, match="trigger_mode"):
            PulseMeasurementService(
                load, dmm_v, terminate_voltage_V=10.8, trigger_mode="NOT_A_REAL_MODE",
                safety=_pulse_safety(),
                max_voltage_V=14.55,
                max_load_power_W=SafetyManager.max_load_power_W,
            )

    def test_default_trigger_mode_is_instrument_timed(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.trigger_mode == "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"


class TestMeasuredBaselineWindow:
    def test_disabled_by_default_uses_single_read(self):
        """baseline_window_s=0.0 (alapértelmezés) — pontosan a régi,
        egyetlen-mintás read_voltage() viselkedés, nincs configure_timed_
        acquisition hívás a pulse ELŐTT."""
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        dmm_v.reset_calls()
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        # Csak EGY read_voltage() hívás történt a baseline-hoz, semmilyen
        # configure_timed_acquisition NEM futott le begin_pulse() alatt.
        pre_pulse_calls = list(dmm_v.calls)
        assert pre_pulse_calls == ["read_voltage()"]

    def test_enabled_calls_instrument_timed_acquisition_for_baseline(self):
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            baseline_window_s=1.0, baseline_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        started = service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        assert started is True
        assert dmm_v.called("configure_timed_acquisition(sample_count=5, interval_s=0.2)")

    def test_baseline_acquisition_failure_still_resets_sticky_timed_mode(self):
        """[regresszió, review-kör 5, Hardver HW5-01 / Architektúra A-3
        (P1, DFAM-001+DFAM-005)] _capture_baseline_window()-nek korábban
        EGYÁLTALÁN nem volt try/except-je a configure/arm/fetch hármas
        körül -- bármelyikük kivétele teljesen megkerülte a DMM sticky
        SAMP:COUN/SOURce állapotának visszaállítását."""
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            baseline_window_s=1.0, baseline_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        dmm_v.simulate_timed_acquisition_failure = True

        with pytest.raises(Exception):
            service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)

        dmm_v.simulate_timed_acquisition_failure = False
        # Ha a reset lefutott, egy sima read_voltage() ismét működik -- a
        # mai (fix előtti) kódon ez InstrumentInvalidReading-et dobna.
        dmm_v.read_voltage()

    def test_measured_baseline_current_populated_when_enabled(self):
        """A MockLoad.measure_current() a valós hardver viselkedését
        modellezi: ha az input KI van kapcsolva, 0A-t jelent (nem a
        beállított current_A-t) — ez helyes és realisztikus. A mért
        I_baseline VALÓDI értékét ezért egy olyan (rendellenes) állapottal
        igazoljuk, ahol a load ELVILEG ki kellene legyen, de ténylegesen
        nem az (pl. egy korábbi hívás elmaradt safe-off-ja) — pontosan azt
        az esetet, amit a spec §4 "mért I_baseline" kötelezettsége hivatott
        elkapni (egy FELTÉTELEZETT 0.0 A csendben elrejtené ezt)."""
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            baseline_window_s=1.0,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        load._input_is_on = True   # rendellenes: a load NEM valóban KI
        load.current_A = 0.02
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.measured_baseline_current_A == pytest.approx(0.02)

    def test_resistance_calc_uses_measured_baseline_current_not_assumed_default(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Metrológia/
        Adatintegritás P2 lelet] baseline_window_s>0 mellett a mért
        I_baseline korábban csak diagnosztikai mezőként (measured_baseline_
        current_A) jutott ki — a tényleges R(t) számítás (delta_i =
        current_A - self._baseline_current_A) MINDIG a begin_pulse()-nak
        átadott feltételezett (itt 0.0 default) bázisáramot használta, nem
        a ténylegesen mértet."""
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            baseline_window_s=1.0,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        load._input_is_on = True   # rendellenes: a load NEM valóban KI
        load.current_A = 0.05      # mért I_baseline
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        load.current_A = 1.42      # a tényleges pulzus alatt mért áram
        _run_all_chunks(service)
        result = service.finalize()

        assert result.measured_baseline_current_A == pytest.approx(0.05)
        point = result.result_at(1.0)
        assert point.reached is True
        assert point.delta_current_A == pytest.approx(1.42 - 0.05)

    def test_baseline_measurement_failure_keeps_assumed_value_and_flags_degraded(self):
        """Ha a mért I_baseline lekérdezése hibázik (pl. load comm lost),
        a feltételezett (hívó által átadott) baseline_current_A marad
        érvényben a számításban, DE ez most explicit degraded jelzővel
        látható — nem csendes visszaesés."""
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            baseline_window_s=1.0,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5

        def _raise():
            raise RuntimeError("simulated load comm lost")
        load.measure_current = _raise  # type: ignore[method-assign]

        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0, baseline_current_A=0.03)
        load.measure_current = MockLoad.measure_current.__get__(load)  # type: ignore[method-assign]
        load._input_is_on = True
        load.current_A = 1.42
        _run_all_chunks(service)
        result = service.finalize()

        assert result.measured_baseline_current_A is None
        assert result.baseline_current_degraded is True
        point = result.result_at(1.0)
        assert point.reached is True
        assert point.delta_current_A == pytest.approx(1.42 - 0.03)

    def test_measured_baseline_current_none_when_disabled(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.measured_baseline_current_A is None

    def test_baseline_window_resets_dmm_sticky_state_before_pulse_chunks(self):
        """A baseline-ablak configure_timed_acquisition-je is 'sticky' állapotot
        hagy a DMM-en — vissza kell állítani, mielőtt a pulse chunk-ok saját
        (esetleg eltérő méretű) konfigurációja lefut."""
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            baseline_window_s=1.0, baseline_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        # A configure_timed_acquisition hívások sorrendjében a baseline (5
        # minta, 1.0/0.2) ÉS a pulse chunk (5 minta, 0.2 interval) konfig is
        # szerepel — mindkettő UTÁN reset_to_single_sample() fut.
        reset_count_after_begin = sum(1 for c in dmm_v.calls if c == "reset_to_single_sample()")
        assert reset_count_after_begin >= 1


class TestRecoveryCapture:
    def test_disabled_by_default_no_recovery_samples(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.recovery_samples == []

    def test_enabled_captures_samples_after_pulse_end(self):
        load, dmm_v = MockLoad(), MockDMM()
        # step_s=1.0: a VALÓS idő (nem a `time_source()` hívások száma) alapú
        # visszafelé-anchorolás miatt a lépésnek nagyságrendileg a mért
        # műveletek (1 chunk ~1.0s, 1 recovery-ablak ~1.0s) valós idejét kell
        # tükröznie — l. TestHardwarePulseMode._make_hw_service hasonló
        # megjegyzése.
        clock = FakeMonotonicClock(step_s=1.0)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            recovery_duration_s=1.0, recovery_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        _run_all_chunks(service)
        result = service.finalize()

        assert len(result.recovery_samples) == 5
        pulse_end = result.pulse_duration_actual_s
        for s in result.recovery_samples:
            assert s.time_from_pulse_start_s > pulse_end

    def test_recovery_capture_after_cutoff_does_not_raise(self):
        """A recovery-ablak hibája (pl. DMM elérhetetlen a cutoff utáni
        pillanatban) NEM szakíthatja meg a már lezárt, érvényes pulse-
        eredményt — csendes best-effort."""
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=11.4,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            recovery_duration_s=1.0,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.0
        dmm_v.timed_voltage_fn = lambda t: 11.0  # azonnali cutoff
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        service.step_chunk()
        dmm_v.simulate_timed_acquisition_failure = True
        result = service.finalize()
        assert result.cutoff_during_pulse is True  # a fő eredmény érvényes marad

    def test_recovery_timestamps_reflect_real_gap_not_nominal_when_drifted(self):
        """Review-lelet: a Load OFF és a recovery-ablak fetch-e közti VALÓS
        holtidőt (SCPI kommunikáció, Python overhead) a recovery-minták
        időbélyegének tükröznie kell — ugyanaz a minta, mint a fő chunk-
        mintavételnél és a HARDWARE_PULSE waveform/trace fetch-eknél (a
        mért befejezési pillanathoz anchor-olva, visszafelé számolva, NEM
        egy nominális offsettel előre a pulse végétől)."""
        load_a, dmm_a = MockLoad(), MockDMM()
        clock_no_drift = FakeMonotonicClock(step_s=0.2)
        service_a = PulseMeasurementService(
            load_a, dmm_a, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock_no_drift,
            recovery_duration_s=1.0, recovery_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_a.voltage_V = 13.5
        dmm_a.timed_voltage_fn = lambda t: 13.2
        service_a.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        _run_all_chunks(service_a)
        result_a = service_a.finalize()

        load_b, dmm_b = MockLoad(), MockDMM()
        # 1. hívás: t0. 2. hívás: chunk_end (pulse vége). 3. hívás: Load
        # OFF-nyugta horgony (R1 B8a, F-21) — a drift ez UTÁN, a recovery-
        # fetch (4. hívás) ELŐTT injektálódik, tehát terheletlen holtidő.
        # (Az F-21 előtti index=2 az új OFF-horgony-hívás miatt a driftet
        # a terhelési időbe tolta, és egy OFF-horgonyból névlegesen számolt
        # recovery-időzítés is átment volna a teszten.)
        clock_drift = FakeMonotonicClock(step_s=0.2, drift_after_call_index=3, drift_s=3.0)
        service_b = PulseMeasurementService(
            load_b, dmm_b, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock_drift,
            recovery_duration_s=1.0, recovery_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_b.voltage_V = 13.5
        dmm_b.timed_voltage_fn = lambda t: 13.2
        service_b.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        _run_all_chunks(service_b)
        result_b = service_b.finalize()

        assert len(result_a.recovery_samples) == 5
        assert len(result_b.recovery_samples) == 5
        # A drift a Load OFF-nyugta UTÁN esik: a terhelési ablak (időtartam,
        # energia) a két futásban azonos -- ez rögzíti, hogy a drift
        # valóban a recovery-szakaszt terheli, nem a pulzust.
        assert result_b.pulse_duration_actual_s == pytest.approx(result_a.pulse_duration_actual_s)
        assert result_b.removed_Ah == pytest.approx(result_a.removed_Ah, rel=1e-12)
        # A drifttel terhelt futás UTOLSÓ recovery-mintájának ideje jóval
        # NAGYOBB, mert a valós (nem a névleges) fetch-befejezési pillanathoz
        # van anchor-olva — ha csendben a névlegest használnánk (a javítás
        # előtti hiba), a két érték megegyezne.
        assert (
            result_b.recovery_samples[-1].time_from_pulse_start_s
            > result_a.recovery_samples[-1].time_from_pulse_start_s + 2.0
        )


class TestReviewRound5Hw504RecoveryFetchTimeoutExtension:
    """[regresszió, review-kör 5, Hardver/Műszervezérlés HW5-04 (P3)] a
    recovery-ablak EGYETLEN, chunkolatlan fetch_timed_acquisition()
    (FETC?) hívást ad ki a TELJES ablakra -- a DMM fix, connect()-kori
    VISA I/O timeoutja (alapból 10s) egy 10s-nél hosszabb recovery_
    duration_s esetén GARANTÁLTAN lejárt volna, mielőtt a műszer
    befejezné a mintavételt, a hívó pedig ezt csendben, a best-effort
    except-ágban nyelte el -- egy hosszú recovery-ablak SOHA nem adott
    volna vissza tényleges mintát. A fix a hívás idejére a várható
    időtartamhoz igazítja az I/O timeoutot, majd visszaállítja."""

    def test_long_recovery_window_extends_and_restores_dmm_io_timeout(self):
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=1.0)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            recovery_duration_s=30.0, recovery_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        original_timeout_ms = dmm_v.timeout_ms
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        _run_all_chunks(service)
        service.finalize()

        set_timeout_calls = [c for c in dmm_v.calls if c.startswith("set_io_timeout_ms(")]
        assert len(set_timeout_calls) == 2  # 1x kiterjesztés + 1x visszaállítás
        # A 30s-os recovery-ablakhoz a timeoutnak az eredeti (10s) alapértéknél
        # NAGYOBBRA (30s + 5s margó = 35000 ms) kellett nőnie a fetch_timed_
        # acquisition() hívás idejére.
        assert set_timeout_calls[0] == "set_io_timeout_ms(35000)"
        # A hívás UTÁN a driver saját timeout_ms mezője vissza kell, hogy
        # álljon az EREDETI (normál üzemi) értékre.
        assert dmm_v.timeout_ms == original_timeout_ms

    def test_timeout_extension_precedes_fetch_and_restore_follows_it(self):
        """A sorrend biztonsági szempontból lényeges: a kiterjesztésnek a
        configure/arm/fetch sorozat ELŐTT, a visszaállításnak pedig UTÁNA
        kell megtörténnie (ne a rövid, normál timeouttal menjen ki a
        hosszú FETC?, és ne maradjon a hosszú timeout a hívás után sem)."""
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=1.0)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            recovery_duration_s=30.0, recovery_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        _run_all_chunks(service)
        service.finalize()

        calls = dmm_v.calls
        extend_idx = calls.index("set_io_timeout_ms(35000)")
        # A recovery-fetch az EZ UTÁNI első fetch_timed_acquisition() hívás.
        recovery_fetch_idx = next(
            i for i, c in enumerate(calls)
            if c == "fetch_timed_acquisition()" and i > extend_idx
        )
        restore_idx = calls.index(
            f"set_io_timeout_ms({dmm_v.timeout_ms})", recovery_fetch_idx
        )
        assert extend_idx < recovery_fetch_idx < restore_idx

    def test_short_recovery_window_still_uses_original_timeout_as_floor(self):
        """Egy RÖVID (a normál timeoutnál kisebb) recovery-ablaknál NEM
        szabad CSÖKKENTENI a timeoutot -- a max(eredeti, szükséges+margó)
        képlet miatt legalább az eredeti értéken kell maradnia."""
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            recovery_duration_s=1.0, recovery_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        original_timeout_ms = dmm_v.timeout_ms
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        _run_all_chunks(service)
        service.finalize()

        set_timeout_calls = [c for c in dmm_v.calls if c.startswith("set_io_timeout_ms(")]
        assert set_timeout_calls[0] == f"set_io_timeout_ms({original_timeout_ms})"


@pytest.mark.usefixtures("_legacy_hardware_acquisition")
class TestReviewRound5HwR5FSiblingFetchTimeoutExtension:
    """[regresszió, review-kör 5, Hardver/Műszervezérlés HW-R5-F (P2)] a
    HW5-04 fix (68a4ee7) csak _capture_recovery_if_configured()-öt
    javította -- a sibling-keresés kimaradt: ugyanez a hiányzó timeout-
    kiterjesztés fennállt _step_hardware_pulse() fő waveform-fetchénél
    (HARDWARE_PULSE mód) és _capture_baseline_window()-nél is."""

    def test_long_hardware_pulse_extends_and_restores_dmm_io_timeout(self):
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=1.0)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            sample_interval_s=0.2, time_source=clock,
            trigger_mode="HARDWARE_PULSE_SOFTWARE_TRIGGERED",
            trace_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        original_timeout_ms = dmm_v.timeout_ms
        # OCV/SOC alapertelmezett impulzushossz (ocv_soc_controller.py
        # impulse_duration_s=30.0) -- a 10s-es alapertelmezett DMM I/O
        # timeout enelkul garantaltan lejarna a fo fetch_timed_
        # acquisition()-nal.
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        service.step_chunk()

        set_timeout_calls = [c for c in dmm_v.calls if c.startswith("set_io_timeout_ms(")]
        assert len(set_timeout_calls) == 2  # 1x kiterjesztés + 1x visszaállítás
        assert set_timeout_calls[0] == "set_io_timeout_ms(35000)"
        assert dmm_v.timeout_ms == original_timeout_ms

    def test_hardware_pulse_timeout_restored_even_on_acquisition_failure(self):
        """A finally-agnak akkor is vissza kell allitania a timeoutot, ha a
        fo waveform-fetch (vagy egy korabbi lepes) hibazik -- a service
        maga is `raise`-t hasznal, ezert a hivonak (finalize()) NEM szabad
        elavult, kiterjesztett timeouttal talalkoznia."""
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=1.0)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            sample_interval_s=0.2, time_source=clock,
            trigger_mode="HARDWARE_PULSE_SOFTWARE_TRIGGERED",
            trace_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        original_timeout_ms = dmm_v.timeout_ms
        dmm_v.simulate_timed_acquisition_failure = True
        service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)

        with pytest.raises(Exception):
            service.step_chunk()

        assert dmm_v.timeout_ms == original_timeout_ms

    def test_long_baseline_window_extends_and_restores_dmm_io_timeout(self):
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            baseline_window_s=30.0, baseline_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        original_timeout_ms = dmm_v.timeout_ms
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)

        set_timeout_calls = [c for c in dmm_v.calls if c.startswith("set_io_timeout_ms(")]
        assert len(set_timeout_calls) == 2
        assert set_timeout_calls[0] == "set_io_timeout_ms(35000)"
        assert dmm_v.timeout_ms == original_timeout_ms

    def test_short_baseline_window_still_uses_original_timeout_as_floor(self):
        load, dmm_v = MockLoad(), MockDMM()
        clock = FakeMonotonicClock(step_s=0.2)
        service = PulseMeasurementService(
            load, dmm_v, terminate_voltage_V=10.8,
            chunk_sample_count=5, sample_interval_s=0.2, time_source=clock,
            baseline_window_s=1.0, baseline_sample_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        dmm_v.voltage_V = 13.5
        original_timeout_ms = dmm_v.timeout_ms
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)

        set_timeout_calls = [c for c in dmm_v.calls if c.startswith("set_io_timeout_ms(")]
        assert set_timeout_calls[0] == f"set_io_timeout_ms({original_timeout_ms})"


@pytest.mark.usefixtures("_legacy_hardware_acquisition")
class TestHardwarePulseMode:
    def _make_hw_service(
        self, load: MockLoad, dmm_v: MockDMM, clock=None,
        terminate_voltage_V: float = 10.8, sample_interval_s: float = 0.2,
        trace_interval_s: float = 0.2,
    ) -> PulseMeasurementService:
        # step_s=1.0: a HARDWARE_PULSE módban `time_source()`-nak csak KÉT
        # hívása van (t0 közvetlenül tüzelés előtt, majd a fetch UTÁN) — a
        # kettő közti "lépés" a szimulált teljes pulse-időt (ezekben a
        # tesztekben mind duration_s=1.0) reprezentálja, NEM egyetlen
        # mintaközi (0.2s) lépést, mint az INSTRUMENT_TIMED chunk-módnál.
        return PulseMeasurementService(
            load, dmm_v, terminate_voltage_V,
            sample_interval_s=sample_interval_s,
            time_source=clock or FakeMonotonicClock(step_s=1.0),
            trigger_mode="HARDWARE_PULSE_SOFTWARE_TRIGGERED",
            trace_interval_s=trace_interval_s,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )

    def test_configures_transient_pulse_and_trace(self):
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        service.step_chunk()

        assert load.called(
            "configure_transient_pulse(pulse_A=1.4, baseline_A=0.0, pulse_width_s=1.0)"
        )
        assert load.called("configure_trace(feed='CURR'")
        assert load.called("fire_transient_pulse()")

    def test_does_not_turn_on_continuous_cc_mode(self):
        """HARDWARE_PULSE módban NEM a folytonos-CC útvonal fut — set_mode_cc()/
        set_current() NEM hívódik, a load a saját PULSE-generátorát használja."""
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        service.step_chunk()

        assert not load.called("set_mode_cc()")
        assert not load.called("set_current(")

    def test_input_on_happens_after_transient_config_not_before(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Hardver/
        Műszervezérlés P1 lelet] input_on() korábban a configure_transient_
        pulse() (BLEV-beállítás + TRAN 1) ELŐTT futott — a Keithley 2380
        kézikönyve szerint TRAN 1 előtt a load a REGULÁRIS (nem-tranziens),
        korábban beállított SOUR:CURR szinten szabályoz, tehát a load egy
        KORÁBBI teszt-fázis setpontjával kapcsolt volna be, nem a szándékolt
        baseline_A-val. Az input_on()-nak a configure_transient_pulse()/
        configure_trace()/check_error() UTÁN, fire_transient_pulse() előtt
        kell futnia."""
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        service.step_chunk()

        input_on_idx = load.calls.index("input_on()")
        transient_idx = next(
            i for i, c in enumerate(load.calls) if c.startswith("configure_transient_pulse(")
        )
        trace_idx = next(
            i for i, c in enumerate(load.calls) if c.startswith("configure_trace(")
        )
        fire_idx = load.calls.index("fire_transient_pulse()")
        assert transient_idx < input_on_idx
        assert trace_idx < input_on_idx
        assert input_on_idx < fire_idx

    def test_config_rejected_by_load_raises_and_turns_load_off(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Ha a Load
        csendben elutasítja a configure_transient_pulse()/configure_trace()
        beállítást (pl. -221 Settings Conflict), a pulzus NEM sülhet el
        csendben hamis, lapos waveform-mal — a check_error()-nek a
        fire_transient_pulse() ELŐTT el kell kapnia, és a Load-nak biztosan
        OFF-ba kell kerülnie."""
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        load.pending_errors = ['-221,"Settings conflict; cannot meet requested timing"']

        with pytest.raises(RuntimeError, match="HARDWARE_PULSE_CONFIG_REJECTED"):
            service.step_chunk()

        assert not load.called("fire_transient_pulse()")
        assert load.input_commanded_on is False

    def test_dmm_config_rejected_raises_and_turns_load_off(self):
        """Governance-v2 review-kör 1 (CF-04/MET-R1-01): a testvér Load-
        ellenőrzés mellett a DMM SAMPle:TIMer konfigurációját is
        ellenőrizni kell a HARDWARE_PULSE úton is — l. a fenti
        test_config_rejected_by_load_raises_and_turns_load_off analóg
        indoklása."""
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        dmm_v.pending_errors = ['-221,"Settings conflict; cannot meet requested timing"']

        with pytest.raises(RuntimeError, match="DMM_TIMED_ACQUISITION_CONFIG_REJECTED"):
            service.step_chunk()

        assert not load.called("fire_transient_pulse()")
        assert load.input_commanded_on is False

    def test_dmm_silent_samp_tim_mismatch_raises_and_turns_load_off(self):
        """[regresszió, 2026-09-12, musszer-only HIL-mérés, INSTR-DMM-SAMPTIM-001]
        A `test_dmm_config_rejected_raises_and_turns_load_off` sibling-esete
        (CLAUDE.md §8 sibling/analogue keresés): a HARDWARE_PULSE út is
        `configure_timed_acquisition()`-t hív a DMM-en, ugyanúgy ki van téve
        a csendes SAMP:TIM-elutasításnak — a fenti teszttel egyező HIL-lelet
        alapján. Ez a teszt a REGI kódon bukik."""
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        dmm_v.actual_sample_timer_override_s = 1.0

        with pytest.raises(RuntimeError, match="DMM_TIMED_ACQUISITION_CONFIG_REJECTED"):
            service.step_chunk()

        assert not load.called("fire_transient_pulse()")
        assert load.input_commanded_on is False

    def test_load_input_off_after_fetch(self):
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        service.step_chunk()

        assert load.input_commanded_on is False

    def test_load_input_off_failure_after_successful_fetch_is_not_silently_swallowed(self):
        """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), Hardver/
        Műszervezérlés P0 lelet] Korábban a SIKERES ágon a Load OFF hibája a
        finally blokk try/except Exception: pass-ébe volt csomagolva —
        csendben elnyelődött, a hívó (step_chunk() visszatérési értéke)
        semmit nem jelzett a bizonytalan Load-állapotról. A hibának
        FELSZÍNRE kell kerülnie, hogy a hívó (pl. ocv_soc_controller.py::
        _run_impulse_acquire()) FAULT-ba tudja vinni."""
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)

        with pytest.raises(RuntimeError, match="HARDWARE_PULSE_LOAD_OFF_FAILED"):
            service.step_chunk()

    def test_single_chunk_completes_whole_pulse(self):
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)

        more = service.step_chunk()

        assert more is False  # egyetlen hívás lezárja a teljes pulzust

    def test_current_trace_used_for_resistance_when_available(self):
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v, trace_interval_s=0.2)
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        load.trace_response = ",".join(["1.4"] * 5)  # 5 minta @0.2s = 1.0s trace
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        service.step_chunk()
        result = service.finalize()

        r1 = result.result_at(1.0)
        assert r1 is not None and r1.reached
        assert r1.current_A == pytest.approx(1.4)
        assert result.current_trace_incomplete is False

    def test_empty_trace_marks_current_degraded_not_crash(self):
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        load.trace_response = ""  # üres trace válasz
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        service.step_chunk()
        result = service.finalize()

        assert result.pulse_current_degraded is True
        assert result.current_trace_incomplete is True

    def test_trace_fetch_error_turns_load_off_and_reraises(self):
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        load.simulate_trace_error = True
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)

        with pytest.raises(Exception):
            service.step_chunk()
        assert load.input_commanded_on is False

    def test_cutoff_detected_post_hoc_from_waveform(self):
        """A hardveres módban a cutoff csak UTÓLAGOS (a lekérdezett
        waveformból) — a mintákat a cutoff-pontig vágja, cutoff_during_pulse
        True lesz, de a pulse TELJES ideje alatt fizikailag folyt az áram
        (l. modul docstring biztonsági korlát)."""
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v, terminate_voltage_V=10.8)
        dmm_v.voltage_V = 13.0
        # 5 minta: a 4. (index 3) az első cutoff alatti - a cutoff mintája
        # MAGA megmarad (ugyanaz a konvenció, mint az INSTRUMENT_TIMED
        # módnál, l. test_mid_chunk_crossing_discards_post_cutoff_samples),
        # csak az UTÁNA lévő (index 4) esik ki.
        values = [13.0, 13.0, 13.0, 5.0, 5.0]
        it = iter(values)
        dmm_v.timed_voltage_fn = lambda t, _it=it: next(_it, 5.0)
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        service.step_chunk()
        result = service.finalize()

        assert result.cutoff_during_pulse is True
        assert len(result.samples) == 4
        assert result.samples[-1].voltage_V == pytest.approx(5.0)

    def test_synchronization_quality_reflects_instrument_timed_software_trigger(self):
        """A HARDWARE_PULSE mód is szoftveresen triggerelt (FORC:TRIG egy
        Python-hívásra) — a szinkron-minőség NEM HARDWARE_SYNCHRONIZED,
        amíg nincs közös fizikai trigger-vezetékezés HIL-validálva (l. modul
        docstring)."""
        load, dmm_v = MockLoad(), MockDMM()
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        service.step_chunk()
        result = service.finalize()
        assert result.synchronization_quality == "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"

    def test_t0_not_recorded_during_begin_pulse(self):
        """[review-lelet] ha t0-t begin_pulse()-ban rögzítenénk, egy
        begin_pulse() és az ELSŐ step_chunk() közötti (pl. TestRunner tick-)
        rés hamisan beépülne a mintaidőkbe. Ez a teszt közvetlenül igazolja,
        hogy begin_pulse() HARDWARE_PULSE módban EGYETLEN time_source()
        hívást sem tesz — a t0 csak a step_chunk()-beli tényleges tüzelés
        előtt keletkezik."""
        load, dmm_v = MockLoad(), MockDMM()
        call_count = 0

        def counting_clock() -> float:
            nonlocal call_count
            call_count += 1
            return call_count * 1.0

        service = self._make_hw_service(load, dmm_v, clock=counting_clock)
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)

        assert call_count == 0, (
            "begin_pulse() HARDWARE_PULSE módban NEM hívhatja a time_source()-t "
            "- t0 csak step_chunk()-ban, közvetlenül fire_transient_pulse() előtt"
        )

        service.step_chunk()
        result = service.finalize()

        r1 = result.result_at(1.0)
        assert r1 is not None and r1.reached
        assert r1.time_error_s == pytest.approx(0.0, abs=0.01)

    def test_r3b1_voltage_empty_token_turns_load_off_and_raises(self):
        """[regresszió, R3-B1, DFAM-001] HARDWARE_PULSE feszültség-blokk
        üres mezővel: a teljes chunk elutasítva, Load OFF."""
        load, dmm_v = MockLoad(), MockDMM()
        load.trace_response = "1.4,1.4,1.4,1.4,1.4"
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        dmm_v.fetch_timed_acquisition_raw_override = "13.0,13.0,,13.0,13.0"
        with pytest.raises(InstrumentInvalidReading):
            service.step_chunk()
        assert load.input_commanded_on is False

    def test_r3b1_current_trace_empty_token_turns_load_off_and_raises(self):
        """[regresszió, R3-B1, DFAM-001] HARDWARE_PULSE áram-trace
        (TRAC:DATA?) üres mezővel: a teljes chunk elutasítva, Load OFF."""
        load, dmm_v = MockLoad(), MockDMM()
        load.trace_response = "1.4,1.4,,1.4,1.4"
        service = self._make_hw_service(load, dmm_v)
        dmm_v.voltage_V = 13.5
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        with pytest.raises(InstrumentInvalidReading):
            service.step_chunk()
        assert load.input_commanded_on is False


# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-12, FAST_TRANSIENT NPLC-javítás — regressziós tesztek
#
# A javított hibaosztály: a `sample_interval_s` a műszernek KÉRT mintavételi
# köz, de a ténylegesen elérhető minimumot az integrációs idő (NPLC/apertúra)
# határozza meg. A session normál üzemi NPLC=10-je a 34465A-n 50 Hz-en 0,2 s
# apertúrát jelent (34460-70-Manual.txt 14028-14052. sor), amivel a
# FAST_TRANSIENT 5 ms-os köze fizikailag teljesíthetetlen — a műszer -221
# "cannot meet requested timing"-ot ad ÉS elállítja a sample timert (uo.
# 9067-9072. sor). A javítás ideiglenesen NPLC=0,02-re vált, a MŰSZERTŐL
# (SAMP:TIM? MIN) kérdezi vissza a megvalósíthatóságot, és MINDEN terminális
# úton visszaáll.
# ─────────────────────────────────────────────────────────────────────────────
class _DmmWithoutCapabilityQuery:
    """Csökkentett képességű driver/test double, ami NEM támogatja a
    `query_sample_timer_capability()`-t (SAMP:TIM? MIN)."""

    def __init__(self, inner: MockDMM) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name == "query_sample_timer_capability":
            raise AttributeError(name)
        return getattr(self._inner, name)


def _make_nplc_service(
    *,
    sample_interval_s: float = 0.005,
    chunk_sample_count: int = 2,
    acquisition_nplc=0.02,
    restore_nplc=10.0,
    terminate_voltage_V: float = 10.8,
    dmm_v: MockDMM | None = None,
    dmm_for_service=None,
) -> tuple[PulseMeasurementService, MockLoad, MockDMM]:
    """FAST_TRANSIENT-szerű szolgáltatás: ms-tartományú mintavételi köz,
    ideiglenes, alacsony integrációs idővel.

    A MockDMM `nplc` mezője és a `query_sample_timer_capability()`
    (SAMP:TIM? MIN) válasza EGYÜTT mozog, ahogy a valós 34465A-n (l.
    mock_dmm.py kommentje) — így a képesség-ellenőrzés tesztje NEM
    tautologikus (rules/60-testing.md)."""
    load = MockLoad()
    dmm_v = dmm_v or MockDMM()
    clock = FakeMonotonicClock(step_s=chunk_sample_count * sample_interval_s)
    service = PulseMeasurementService(
        load, dmm_for_service if dmm_for_service is not None else dmm_v,
        terminate_voltage_V,
        chunk_sample_count=chunk_sample_count,
        sample_interval_s=sample_interval_s,
        target_times_s=(0.02,),
        time_tolerance_s=0.01,
        time_source=clock,
        acquisition_nplc=acquisition_nplc,
        restore_nplc=restore_nplc,
        safety=_pulse_safety(),
        max_voltage_V=14.55,
        max_load_power_W=SafetyManager.max_load_power_W,
    )
    return service, load, dmm_v


class TestAcquisitionNplcStandardPathUnchanged:
    """A standard (1/10/30 s) útvonal alapértelmezése `acquisition_nplc=None`
    — egyetlen új SCPI-hívás sem keletkezhet (CLAUDE.md §10, minimális diff)."""

    def test_default_service_never_touches_nplc(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        _run_all_chunks(service)
        service.finalize()
        assert not dmm_v.called("set_nplc(")
        assert not dmm_v.called("get_nplc()")
        assert not dmm_v.called("query_sample_timer_capability()")

    def test_default_service_result_has_no_timing_qualification(self):
        service, load, dmm_v = make_service()
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.timing_infeasible is False
        assert result.timing_infeasible_reason == ""
        assert result.nplc_restore_failed is False


class TestAcquisitionNplcApplied:
    def test_nplc_set_before_any_measurement(self):
        """A NPLC-váltás a baseline-olvasás ELŐTT fut — a v_baseline és az
        impulzus alatti minták AZONOS apertúrával készülnek (különben a
        ΔU/ΔI hányados szisztematikus hibát kapna)."""
        service, load, dmm_v = _make_nplc_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        assert dmm_v.calls.index("set_nplc(0.02)") < dmm_v.calls.index("read_voltage()")

    def test_nplc_applied_when_load_switched_on(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        assert dmm_v.nplc == pytest.approx(0.02)
        assert load.input_commanded_on is True

    def test_capability_queried_after_nplc_is_applied(self):
        """34460-70-Manual.txt 9053-9055. sor: "You must completely configure
        the measurement before setting the sample timer to MIN" — a SAMP:TIM?
        MIN válasza konfigurációfüggő, ezért CSAK az új NPLC után van
        értelme."""
        service, load, dmm_v = _make_nplc_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        set_idx = dmm_v.calls.index("set_nplc(0.02)")
        cap_idx = dmm_v.calls.index("query_sample_timer_capability()")
        assert set_idx < cap_idx

    def test_restore_value_comes_from_instrument_readback_not_config(self):
        """A DCV NPLC-nek több, egymástól független írója van (main_window.py
        `configure_dcv`, test_runner.py lépés-/alállapot-függő `set_nplc`) —
        a visszaállítás a VISSZAOLVASOTT értéket használja, nem a konfigurált
        fallbacket."""
        service, load, dmm_v = _make_nplc_service(restore_nplc=10.0)
        dmm_v.nplc = 1.0          # R10-B3: a runner gyors alapértéke
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        _run_all_chunks(service)
        service.finalize()
        assert dmm_v.nplc == pytest.approx(1.0)

    def test_config_fallback_used_when_driver_cannot_read_back(self):
        service, load, dmm_v = _make_nplc_service(restore_nplc=10.0)
        dmm_v.simulate_nplc_get_failure = True
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        _run_all_chunks(service)
        result = service.finalize()
        assert result.timing_infeasible is False
        assert dmm_v.nplc == pytest.approx(10.0)

    def test_no_restore_value_available_refuses_to_change_nplc(self):
        """Soha nem hagyunk a műszeren olyan állapotot, amit nem tudunk
        visszaállítani — se readback, se fallback → a mérés megtagadva, az
        NPLC érintetlen."""
        service, load, dmm_v = _make_nplc_service(restore_nplc=None)
        dmm_v.simulate_nplc_get_failure = True
        dmm_v.voltage_V = 13.5
        started = service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        assert started is False
        assert load.input_commanded_on is False
        assert not dmm_v.called("set_nplc(")
        result = service.finalize()
        assert result.timing_infeasible is True
        assert "RESTORE_VALUE_UNKNOWN" in result.timing_infeasible_reason


class TestAcquisitionNplcRestoredOnEveryExitPath:
    """rules/10-safety.md 9. pont: cleanup/restore MINDEN terminális úton
    vizsgálandó (DONE, FAULT, STOP, ABORT, timeout, exception, teardown)."""

    def test_restored_after_successful_pulse(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        _run_all_chunks(service)
        assert dmm_v.nplc == pytest.approx(10.0)
        result = service.finalize()
        assert result.nplc_restore_failed is False

    def test_not_restored_between_chunks(self):
        """A gyors integrációs időnek a chunkok KÖZÖTT is érvényben kell
        maradnia — különben a 2. chunk ismét a lassú apertúrával próbálná a
        ms-os mintavételi közt (regresszió a "chunkonkénti visszaállítás"
        naiv megoldására)."""
        service, load, dmm_v = _make_nplc_service(chunk_sample_count=2)
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)  # 4 minta = 2 chunk
        assert service.step_chunk() is True
        assert dmm_v.nplc == pytest.approx(0.02)

    def test_restored_on_cutoff_during_pulse(self):
        service, load, dmm_v = _make_nplc_service()
        service._terminate_voltage_V = 11.4
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 11.0   # azonnali cutoff
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        service.step_chunk()
        assert dmm_v.nplc == pytest.approx(10.0)
        assert service.finalize().cutoff_during_pulse is True

    def test_restored_on_skipped_low_voltage(self):
        service, load, dmm_v = _make_nplc_service()
        service._terminate_voltage_V = 11.4
        dmm_v.voltage_V = 11.0
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=0.02) is False
        assert dmm_v.nplc == pytest.approx(10.0)

    def test_restored_on_acquisition_error(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        dmm_v.simulate_timed_acquisition_failure = True
        with pytest.raises(Exception):
            service.step_chunk()
        assert dmm_v.nplc == pytest.approx(10.0)
        assert load.input_commanded_on is False

    def test_restored_on_baseline_read_error(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.dmm_valid = False
        with pytest.raises(Exception):
            service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        assert dmm_v.nplc == pytest.approx(10.0)
        assert load.input_commanded_on is False

    def test_restored_on_load_switch_on_error(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.voltage_V = 13.5

        def _boom() -> None:
            raise RuntimeError("MockLoad: simulated set_mode_cc failure")

        load.set_mode_cc = _boom
        with pytest.raises(RuntimeError):
            service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        assert dmm_v.nplc == pytest.approx(10.0)

    def test_public_restore_entry_point_is_idempotent(self):
        """A hívó (ocv_soc_controller.py::emergency_stop) a step_chunk()/
        finalize() cikluson KÍVÜLRŐL is lezárhatja a mérést."""
        service, load, dmm_v = _make_nplc_service()
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        assert service.restore_acquisition_nplc() is True
        assert dmm_v.nplc == pytest.approx(10.0)
        dmm_v.reset_calls()
        assert service.restore_acquisition_nplc() is False
        assert not dmm_v.called("set_nplc(")

    def test_restore_failure_is_reported_not_swallowed(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.voltage_V = 13.5
        dmm_v.timed_voltage_fn = lambda t: 13.2
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        dmm_v.simulate_nplc_set_failure = True
        _run_all_chunks(service)
        assert service.finalize().nplc_restore_failed is True


class TestAcquisitionNplcCapabilityGate:
    def test_infeasible_when_sample_timer_min_exceeds_requested_interval(self):
        """A JAVÍTOTT hibaosztály maga: NPLC=10 mellett (50 Hz → 0,2 s
        apertúra) az 5 ms-os mintavételi köz teljesíthetetlen. A régi kód
        ilyenkor elindította volna az impulzust; most a mérés meg sem
        kezdődik, és ezt explicit minősítés jelzi."""
        service, load, dmm_v = _make_nplc_service(acquisition_nplc=10.0)
        dmm_v.voltage_V = 13.5
        started = service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        assert started is False
        assert load.input_commanded_on is False
        result = service.finalize()
        assert result.started is False
        assert result.timing_infeasible is True
        assert "SAMPLE_TIMER_MIN_EXCEEDS_REQUESTED_INTERVAL" in result.timing_infeasible_reason
        assert dmm_v.nplc == pytest.approx(10.0)

    def test_infeasible_does_not_fabricate_measurement_data(self):
        service, load, dmm_v = _make_nplc_service(acquisition_nplc=10.0)
        dmm_v.voltage_V = 13.5
        service.begin_pulse(pulse_current_A=1.4, duration_s=0.02)
        result = service.finalize()
        assert result.samples == []
        assert result.results == {}
        assert result.removed_Ah == 0.0
        assert result.result_at(0.02) is None

    def test_infeasible_when_capability_query_fails(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.simulate_sample_timer_query_failure = True
        dmm_v.voltage_V = 13.5
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=0.02) is False
        result = service.finalize()
        assert result.timing_infeasible is True
        assert "SAMPLE_TIMER_CAPABILITY_QUERY_FAILED" in result.timing_infeasible_reason
        assert dmm_v.nplc == pytest.approx(10.0)

    def test_infeasible_when_capability_response_is_not_numeric(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.sample_timer_min_raw_override = "MIN"
        dmm_v.voltage_V = 13.5
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=0.02) is False
        result = service.finalize()
        assert result.timing_infeasible is True
        assert "SAMPLE_TIMER_CAPABILITY_INVALID_RESPONSE" in result.timing_infeasible_reason

    def test_infeasible_when_capability_response_is_nan(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.sample_timer_min_raw_override = "NaN"
        dmm_v.voltage_V = 13.5
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=0.02) is False
        assert service.finalize().timing_infeasible is True

    def test_infeasible_when_driver_cannot_query_capability(self):
        """Ellenőrizetlen képesség != teljesített képesség (fail-safe)."""
        inner = MockDMM()
        service, load, _ = _make_nplc_service(
            dmm_v=inner, dmm_for_service=_DmmWithoutCapabilityQuery(inner)
        )
        inner.voltage_V = 13.5
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=0.02) is False
        result = service.finalize()
        assert result.timing_infeasible is True
        assert "SAMPLE_TIMER_CAPABILITY_UNSUPPORTED" in result.timing_infeasible_reason
        assert inner.nplc == pytest.approx(10.0)

    def test_infeasible_when_nplc_set_itself_fails(self):
        service, load, dmm_v = _make_nplc_service()
        dmm_v.simulate_nplc_set_failure = True
        dmm_v.voltage_V = 13.5
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=0.02) is False
        assert load.input_commanded_on is False
        result = service.finalize()
        assert result.timing_infeasible is True
        assert "ACQUISITION_NPLC_SET_FAILED" in result.timing_infeasible_reason


class TestR2B4MeasuredEnergySoFar:
    """[regresszió, R2-B4 (R2-metrology-01), DFAM-033] measured_energy_so_far():
    a finalize() téglalap-integrálja az eddig RÖGZÍTETT áramleolvasásokig,
    az OFF-farok NÉLKÜL; tiszta számítás (nincs műszer-I/O, az óra sem
    lép); begin_pulse() után nulla. Az OcvSocController ezt könyveli minden
    sikeres chunk után, és a megszakított impulzus prefixét is ebből veszi.
    Óra: InstrumentLatencyClock -- fetch = 5 × 0,2 s, reset 0,3 s,
    áramolvasás 0,05 s, Load OFF 0,07 s (l. TestB8aPhysicalLoadWindow).
    Unit/mock."""

    I_SET = 1.4
    I_MEAS = 1.3
    V = 12.9
    RESET_S = 0.3
    MEASURE_S = 0.05
    OFF_S = 0.07

    def _make(self):
        clock = InstrumentLatencyClock()
        load = MockLoad()
        dmm_v = MockDMM()
        service = PulseMeasurementService(
            load, dmm_v, 10.8,
            chunk_sample_count=5,
            sample_interval_s=0.2,
            target_times_s=(1.0, 2.0),
            time_tolerance_s=0.2,
            time_source=clock,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        bind_pulse_instrument_latency(
            clock, dmm_v, load,
            reset_s=self.RESET_S, measure_current_s=self.MEASURE_S, input_off_s=self.OFF_S,
        )
        dmm_v.voltage_V = 13.0
        dmm_v.timed_voltage_fn = lambda t: self.V
        return service, load, dmm_v, clock

    def _begin(self, service, load):
        assert service.begin_pulse(pulse_current_A=self.I_SET, duration_s=2.0)
        load.current_A = self.I_MEAS  # mért != setpoint (begin_pulse() UTÁN)

    def test_equals_finalize_removed_without_off_tail(self):
        # chunk 1: fetch 1,0 (leolvasás), reset 1,3, áram 1,35;
        # chunk 2: fetch 2,35 (leolvasás), reset 2,65, áram 2,70; OFF 2,77.
        service, load, _dmm_v, _clock = self._make()
        self._begin(service, load)
        assert service.step_chunk() is True
        assert service.step_chunk() is False
        measured = service.measured_energy_so_far()
        result = service.finalize()

        t_last = 1.0 + self.RESET_S + self.MEASURE_S + 1.0
        t_off = t_last + self.RESET_S + self.MEASURE_S + self.OFF_S
        assert measured.reading_count == 2
        assert measured.last_reading_t_s == pytest.approx(t_last)
        assert measured.current_degraded is False
        assert measured.removed_Ah == pytest.approx(self.I_MEAS * t_last / 3600.0, rel=1e-12)
        assert measured.removed_Wh == pytest.approx(
            self.I_MEAS * self.V * t_last / 3600.0, rel=1e-12,
        )
        # a finalize() = ugyanez + az OFF-farok (BECSLÉS, HIL_PENDING)
        assert result.removed_Ah - measured.removed_Ah == pytest.approx(
            self.I_MEAS * (t_off - t_last) / 3600.0, rel=1e-9,
        )
        assert result.removed_Wh - measured.removed_Wh == pytest.approx(
            self.I_MEAS * self.V * (t_off - t_last) / 3600.0, rel=1e-9,
        )
        # a finalize() nem változtatja a rögzített prefixet
        assert service.measured_energy_so_far() == measured

    def test_mid_pulse_is_pure_computation_without_instrument_io(self):
        service, load, dmm_v, clock = self._make()
        self._begin(service, load)
        assert service.step_chunk() is True
        n_load, n_dmm, now = len(load.calls), len(dmm_v.calls), clock.now

        first = service.measured_energy_so_far()
        second = service.measured_energy_so_far()

        assert (len(load.calls), len(dmm_v.calls), clock.now) == (n_load, n_dmm, now)
        assert first == second
        assert load.input_commanded_on is True
        assert first.reading_count == 1
        assert first.last_reading_t_s == pytest.approx(1.0)
        assert first.removed_Ah == pytest.approx(self.I_MEAS * 1.0 / 3600.0, rel=1e-12)
        assert first.removed_Wh == pytest.approx(self.I_MEAS * self.V * 1.0 / 3600.0, rel=1e-12)

    def test_zero_before_first_and_after_next_begin_pulse(self):
        from Prog.src.pulse_measurement_service import NO_MEASURED_PULSE_ENERGY

        service, load, _dmm_v, _clock = self._make()
        assert service.measured_energy_so_far() == NO_MEASURED_PULSE_ENERGY
        self._begin(service, load)
        assert service.measured_energy_so_far() == NO_MEASURED_PULSE_ENERGY
        _run_all_chunks(service)
        service.finalize()
        assert service.measured_energy_so_far().reading_count == 2

        self._begin(service, load)

        assert service.measured_energy_so_far() == NO_MEASURED_PULSE_ENERGY

    def test_degraded_reading_is_flagged_with_setpoint_fallback(self):
        service, load, _dmm_v, _clock = self._make()
        self._begin(service, load)
        load.simulate_current_readback_failure = True
        assert service.step_chunk() is True

        measured = service.measured_energy_so_far()

        assert measured.current_degraded is True
        assert measured.removed_Ah == pytest.approx(self.I_SET * 1.0 / 3600.0, rel=1e-12)


@pytest.mark.usefixtures("_legacy_hardware_acquisition")
class TestR8B2PulseVoltageGate:
    """R8-B2: friss baseline es minden beolvasott pulzusminta kapuzasa."""

    @staticmethod
    def _make(fast=False, baseline_window_s=0.0, hardware=False):
        load, dmm_v = MockLoad(), MockDMM(voltage_V=13.5)
        clock = InstrumentLatencyClock()
        bind_pulse_instrument_latency(clock, dmm_v, load)
        interval = 0.005 if fast else 0.6
        service = PulseMeasurementService(
            load, dmm_v, 10.8,
            chunk_sample_count=3, sample_interval_s=interval,
            safety=_pulse_safety(),
            time_source=clock, max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
            baseline_window_s=baseline_window_s,
            baseline_sample_interval_s=interval,
            acquisition_nplc=0.02 if fast else None,
            trigger_mode=(
                "HARDWARE_PULSE_SOFTWARE_TRIGGERED" if hardware else "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"
            ),
        )
        return service, load, dmm_v

    def test_max_voltage_is_required(self):
        from inspect import Parameter, signature

        parameter = signature(PulseMeasurementService).parameters["max_voltage_V"]
        assert parameter.kind is Parameter.KEYWORD_ONLY
        assert parameter.default is Parameter.empty

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("window", [False, True])
    def test_baseline_overvoltage_never_energizes(self, fast, window):
        service, load, dmm_v = self._make(fast, baseline_window_s=0.03 if window else 0.0)
        if window:
            dmm_v.fetch_timed_acquisition_raw_override = "13.5,14.56,13.5"
        else:
            dmm_v.voltage_V = 14.56

        with pytest.raises(RuntimeError, match="BATTERY_OVERVOLTAGE") as error:
            service.begin_pulse(1.4, 1.8)

        assert type(error.value).__name__ == "PulseOvervoltageError"
        assert service._done is True
        assert not service.step_chunk()
        assert load.input_commanded_on is False
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert dmm_v.nplc == 10.0
        assert dmm_v.calls.count("read_voltage()") == (0 if window else 1)

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("bad_index", [0, 1, 2])
    def test_chunk_outlier_turns_load_off(self, fast, bad_index):
        service, load, dmm_v = self._make(fast)
        assert service.begin_pulse(1.4, 1.8)
        values = ["13.2"] * 3
        values[bad_index] = "14.56"
        dmm_v.fetch_timed_acquisition_raw_override = ",".join(values)
        load.reset_calls()

        with pytest.raises(RuntimeError, match="BATTERY_OVERVOLTAGE") as error:
            service.step_chunk()

        assert type(error.value).__name__ == "PulseOvervoltageError"
        assert service._done is True
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert dmm_v.nplc == 10.0
        calls = list(load.calls)
        assert not service.step_chunk()
        assert load.calls == calls
        assert dmm_v.calls.count("read_voltage()") == 1

    def test_hardware_waveform_outlier_is_rejected_after_off(self):
        service, load, dmm_v = self._make(hardware=True)
        assert service.begin_pulse(1.4, 1.8)
        dmm_v.fetch_timed_acquisition_raw_override = "13.2,14.56,13.2"

        with pytest.raises(RuntimeError, match="BATTERY_OVERVOLTAGE"):
            service.step_chunk()

        assert service._done is True
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls

    @pytest.mark.parametrize("fast", [False, True])
    def test_exact_maximum_is_accepted(self, fast):
        service, load, dmm_v = self._make(fast)
        dmm_v.voltage_V = 14.55
        assert service.begin_pulse(1.4, 0.015 if fast else 1.8)
        dmm_v.fetch_timed_acquisition_raw_override = "14.55,14.55,14.55"

        assert service.step_chunk() is False

        assert not service.finalize().cutoff_during_pulse
        assert load.input_commanded_on is False
        assert dmm_v.nplc == 10.0

    @pytest.mark.parametrize("fast", [False, True])
    def test_normal_low_cutoff_is_preserved(self, fast):
        service, load, dmm_v = self._make(fast)
        service._terminate_voltage_V = 11.4
        assert service.begin_pulse(1.4, 1.8)
        dmm_v.fetch_timed_acquisition_raw_override = "13.2,11.0,13.2"

        assert service.step_chunk() is False

        result = service.finalize()
        assert result.cutoff_during_pulse is True
        assert [s.voltage_V for s in result.samples] == [13.2, 11.0]
        assert load.input_commanded_on is False
        assert dmm_v.nplc == 10.0


@pytest.mark.usefixtures("_legacy_hardware_acquisition")
class TestR9B2PulsePowerGate:
    """R9-B2: friss nyers adatok, kozos teljesitmenyhatar."""

    @staticmethod
    def _make(fast=False, window=False, hardware=False):
        service, load, dmm = TestR8B2PulseVoltageGate._make(
            fast, baseline_window_s=0.03 if window else 0.0, hardware=hardware,
        )
        dmm.voltage_V = 12.49
        dmm.timed_voltage_fn = lambda t: 12.49
        return service, load, dmm

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize(
        ("baseline", "chunk", "current", "fault"),
        [
            (12.51, 12.51, 20.0, "baseline"),
            (12.49, 12.51, 20.0, "chunk"),
            (12.50, 12.50, 20.0, None),
            (12.49, 12.49, 20.0, None),
            (12.49, 12.49, 20.1, "chunk"),
        ],
    )
    def test_fresh_power_matrix(self, fast, baseline, chunk, current, fault):
        service, load, dmm = self._make(fast)
        dmm.voltage_V = baseline
        dmm.fetch_timed_acquisition_raw_override = f"12.49,{chunk},12.49"
        if fault == "baseline":
            with pytest.raises(RuntimeError, match="LOAD_POWER_LIMIT_EXCEEDED"):
                service.begin_pulse(20.0, 1.8)
            assert not load.called("input_on()")
        else:
            assert service.begin_pulse(20.0, 1.8)
            load.current_A = current
            if fault:
                with pytest.raises(RuntimeError, match="LOAD_POWER_LIMIT_EXCEEDED"):
                    service.step_chunk()
                assert load.input_commanded_on is False
            else:
                _run_all_chunks(service)
                assert service.finalize().started is True
                assert load.input_commanded_on is False
        assert dmm.nplc == 10.0

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("hardware", [False, True])
    def test_baseline_median_cannot_hide_power_spike(self, fast, hardware):
        service, load, dmm = self._make(fast, window=True, hardware=hardware)
        dmm.fetch_timed_acquisition_raw_override = "12.49,12.51,12.49"
        with pytest.raises(RuntimeError, match="LOAD_POWER_LIMIT_EXCEEDED"):
            service.begin_pulse(20.0, 1.8)
        assert not load.called("input_on()")
        assert not load.called("fire_transient_pulse()")
        assert service._done is True
        assert dmm.nplc == 10.0

    @pytest.mark.parametrize(
        ("voltage", "current", "fault"),
        [(12.51, 20.0, True), (12.49, 20.1, True), (12.50, 20.0, False)],
    )
    def test_hardware_waveform_power_check_is_post_acquisition(self, voltage, current, fault):
        service, load, dmm = self._make(hardware=True)
        assert service.begin_pulse(20.0, 1.8)
        dmm.fetch_timed_acquisition_raw_override = f"12.49,{voltage},12.49"
        load.trace_response = f"20,{current},20"
        if fault:
            with pytest.raises(RuntimeError, match="LOAD_POWER_LIMIT_EXCEEDED"):
                service.step_chunk()
        else:
            assert service.step_chunk() is False
        assert load.called("fire_transient_pulse()")
        assert load.input_commanded_on is False
        assert load.called("input_off()")
        assert service.step_chunk() is False

    def test_explicit_lower_limit_is_used_without_new_default(self):
        service = PulseMeasurementService(
            MockLoad(), MockDMM(voltage_V=12.49), 10.8,
            safety=_pulse_safety(),
            max_voltage_V=14.55, max_load_power_W=240.0,
        )
        with pytest.raises(RuntimeError, match="limit=240 W"):
            service.begin_pulse(20.0, 1.8)

    def test_hardware_cutoff_keeps_degraded_current_timestamp(self):
        service, load, dmm = self._make(hardware=True)
        assert service.begin_pulse(20.0, 1.8)
        dmm.fetch_timed_acquisition_raw_override = "12.49,10.0,12.49"
        load.trace_response = ""
        assert service.step_chunk() is False
        assert service._cutoff is True
        assert service._pulse_current_degraded is True
        assert service._current_readings[-1][0] == service._samples[-1].time_from_pulse_start_s


@pytest.mark.usefixtures("_legacy_hardware_acquisition")
class TestR16B3SampleTimerQueryFailure:
    # R16-B3: mindket acquisition-ag a meglevo cleanup-uton zarul.
    @staticmethod
    def _make(hardware):
        load, dmm = MockLoad(), MockDMM(voltage_V=13.5, nplc=1.0)
        service = PulseMeasurementService(
            load, dmm, 10.8,
            chunk_sample_count=5, sample_interval_s=0.2,
            time_source=FakeMonotonicClock(step_s=1.0),
            acquisition_nplc=0.02, restore_nplc=10.0,
            trigger_mode=(
                "HARDWARE_PULSE_SOFTWARE_TRIGGERED"
                if hardware else "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"
            ),
            trace_interval_s=0.2,
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=1.0)
        assert dmm.nplc == 0.02
        load.reset_calls()
        dmm.reset_calls()
        return service, load, dmm

    @pytest.mark.parametrize("hardware", [False, True])
    @pytest.mark.parametrize("failure", ["timeout", "invalid"])
    def test_getter_failure_rejects_before_init_and_cleans_up(
        self, hardware, failure, monkeypatch,
    ):
        from unittest.mock import Mock, call

        from Prog.src.exceptions import InstrumentTimeoutError

        service, load, dmm = self._make(hardware)
        error_type = InstrumentTimeoutError if failure == "timeout" else InstrumentInvalidReading
        query_error = error_type("sample timer readback unavailable")
        trace = Mock()
        for name, obj, attr in (
            ("check", dmm, "check_error"),
            ("off", load, "input_off"),
            ("reset", dmm, "reset_to_single_sample"),
            ("restore", dmm, "set_nplc"),
        ):
            wrapped = Mock(wraps=getattr(obj, attr))
            monkeypatch.setattr(obj, attr, wrapped)
            trace.attach_mock(wrapped, name)
        getter = Mock(side_effect=query_error)
        monkeypatch.setattr(dmm, "get_sample_timer_s", getter)
        trace.attach_mock(getter, "timer")
        assert dmm.pending_errors == []

        with pytest.raises(RuntimeError, match=(
            "^DMM_TIMED_ACQUISITION_CONFIG_REJECTED: SAMP:TIM readback failed$"
        )) as caught:
            service.step_chunk()

        assert caught.value.__cause__ is query_error
        owner_kwargs = {} if hardware else {"owner": trace.mock_calls[0].kwargs["owner"]}
        if not hardware:
            assert owner_kwargs["owner"] is not None
        expected_calls = [
            call.check(**owner_kwargs), call.timer(**owner_kwargs), call.off(),
            call.reset(**owner_kwargs), call.restore(1.0),
        ]
        assert trace.mock_calls == expected_calls
        assert not dmm.called("arm_timed_acquisition()")
        assert not dmm.called("fetch_timed_acquisition()")
        assert not load.called("input_on(")
        assert not load.called("fire_transient_pulse(")
        assert load.input_commanded_on is False
        assert dmm.nplc == 1.0
        assert dmm.read_voltage() == 13.5
        assert service.step_chunk() is False
        assert trace.mock_calls == expected_calls

    @pytest.mark.parametrize("hardware", [False, True])
    def test_matching_readback_acquires_and_restores(self, hardware, monkeypatch):
        from unittest.mock import Mock

        service, load, dmm = self._make(hardware)
        getter = Mock(return_value=0.2)
        monkeypatch.setattr(dmm, "get_sample_timer_s", getter)

        assert service.step_chunk() is False

        if hardware:
            getter.assert_called_once_with()
        else:
            owner = getter.call_args.kwargs["owner"]
            assert owner is not None
            getter.assert_called_once_with(owner=owner)
        assert dmm.calls.count("arm_timed_acquisition()") == 1
        assert dmm.calls.count("fetch_timed_acquisition()") == 1
        assert dmm.called("reset_to_single_sample()")
        assert load.input_commanded_on is False
        assert dmm.nplc == 1.0
        assert dmm.read_voltage() == 13.5
        result = service.finalize()
        assert result.timing_infeasible is False
        assert result.nplc_restore_failed is False


@pytest.mark.usefixtures("_legacy_hardware_acquisition")
class TestR16B9HardwarePulseRangeGate:
    """Valódi Load-driver + stateful VISA-double + MockDMM; nincs fizikai I/O."""

    @staticmethod
    def _make():
        from unittest.mock import MagicMock

        from Prog.drivers.device_load import Keithley2380Load
        from Prog.tests.mock_drivers.mock_load import StatefulLoadRangeResource

        res = StatefulLoadRangeResource()
        rm = MagicMock(open_resource=lambda *_: res)
        previous = Keithley2380Load()
        previous.connect("MOCK::LOAD", rm=rm)
        previous.set_current(0.1)
        previous.disconnect()
        load = Keithley2380Load()
        load.connect("MOCK::LOAD", rm=rm)
        assert res.ceiling == 0.1
        dmm = MockDMM()
        dmm.voltage_V = 13.5
        service = PulseMeasurementService(
            load, dmm, 10.8,
            sample_interval_s=0.2,
            time_source=FakeMonotonicClock(step_s=1.0),
            trigger_mode="HARDWARE_PULSE_SOFTWARE_TRIGGERED",
            safety=_pulse_safety(),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        assert service.begin_pulse(0.2, 1.0, baseline_current_A=0.1)
        res.log.clear()
        return service, load, dmm, res

    def test_inherited_small_ceiling_is_replaced_and_readbacks_precede_trace(self):
        service, load, dmm, res = self._make()
        assert service.step_chunk() is False
        assert res.ceiling == 0.2
        assert (res.levels["CURR:TRAN:ALEV"], res.levels["CURR:TRAN:BLEV"]) == (0.2, 0.1)
        messages = [m for k, m in res.log if k in ("Q", "W")]
        trace = messages.index("TRAC:FEED:CONT NEXT")
        assert messages.index("SYST:ERR?") < messages.index("CURR:TRAN:ALEV?")
        assert messages.index("CURR:TRAN:ALEV?") < messages.index("CURR:TRAN:BLEV?") < trace
        assert trace < messages.index("INP ON") < messages.index("FORC:TRIG")
        assert messages[trace + 1:messages.index("INP ON")] == ["SYST:ERR?"]
        assert res.clears_during_capture == 0
        assert res.input_on is False
        assert load.input_commanded_on is False
        assert dmm.called("arm_timed_acquisition()")

    @pytest.mark.parametrize("failure", [
        "scpi_rejection", "atomic_timeout", "queue_timeout", "off_readback_timeout",
        "alev_timeout", "blev_timeout", "alev_mismatch", "blev_mismatch",
        "alev_nan", "blev_inf", "alev_overload", "blev_malformed",
        "clipping_after_config", "trace_rejection", "inherited_on",
    ])
    def test_config_failure_never_energizes_and_runs_off_cleanup(self, failure, monkeypatch):
        from Prog.src.exceptions import InstrumentCommandError, InstrumentTimeoutError

        service, load, dmm, res = self._make()
        expected_error = RuntimeError
        match = "HARDWARE_PULSE_CONFIG_REJECTED"
        if failure == "scpi_rejection":
            res.reject.add("CURR:TRAN:ALEV 0.2")
        elif failure in ("atomic_timeout", "queue_timeout", "off_readback_timeout"):
            unit = {
                "atomic_timeout": "CURR:RANG 0.2",
                "queue_timeout": "SYST:ERR?",
                "off_readback_timeout": "INP?",
            }[failure]
            res.timeout_on.add(unit)
            expected_error, match = InstrumentTimeoutError, "Load query failed"
        elif failure in ("alev_timeout", "blev_timeout"):
            res.timeout_on.add("CURR:TRAN:ALEV?" if failure == "alev_timeout" else "CURR:TRAN:BLEV?")
        elif failure == "clipping_after_config":
            configure = load.configure_transient_pulse

            def clip_after_config(**kwargs):
                configure(**kwargs)
                load._query("CURR:RANG 0.05;*OPC?")
                assert res.errors == []

            monkeypatch.setattr(load, "configure_transient_pulse", clip_after_config)
        elif failure == "trace_rejection":
            res.reject.add("TRAC:POIN 1000")
        elif failure == "inherited_on":
            res.input_on = True
            expected_error, match = InstrumentCommandError, "INPUT OFF"
        else:
            query, reply = {
                "alev_mismatch": ("CURR:TRAN:ALEV?", "0.1"),
                "blev_mismatch": ("CURR:TRAN:BLEV?", "0.01"),
                "alev_nan": ("CURR:TRAN:ALEV?", "nan"),
                "blev_inf": ("CURR:TRAN:BLEV?", "inf"),
                "alev_overload": ("CURR:TRAN:ALEV?", "1e38"),
                "blev_malformed": ("CURR:TRAN:BLEV?", "bad"),
            }[failure]
            res.replies[query] = reply

        with pytest.raises(expected_error, match=match):
            service.step_chunk()

        messages = [m for k, m in res.log if k in ("Q", "W")]
        assert "INP ON" not in messages
        assert "FORC:TRIG" not in messages
        assert "INP OFF;*OPC?" in messages
        assert res.input_on is False
        assert load.input_commanded_on is False
        assert not dmm.called("arm_timed_acquisition()")
        assert not dmm.called("fetch_timed_acquisition()")
        assert dmm.called("reset_to_single_sample()")
        if failure != "trace_rejection":
            assert "TRAC:FEED:CONT NEXT" not in messages

    def test_readback_compares_grid_levels_with_float_noisy_input(self):
        load, dmm = MockLoad(), MockDMM()
        service = TestHardwarePulseMode()._make_hw_service(load, dmm)
        # 0,3 A rácson van, a host floatja mégsem pontosan 0.3.
        assert 0.1 + 0.2 != 0.3
        assert service.begin_pulse(0.1 + 0.2, 1.0, baseline_current_A=0.02)
        service.step_chunk()
        assert load.read_transient_current_levels() == (0.3, 0.02)
        assert load.called("fire_transient_pulse()")


# R16-B13: a stop-kapu nem előzheti meg a friss safety-döntést.
@pytest.mark.usefixtures("_legacy_hardware_acquisition")
class TestR16B13PulseHandoff:
    @pytest.mark.parametrize("mode", [
        "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED", "HARDWARE_PULSE_SOFTWARE_TRIGGERED",
    ])
    @pytest.mark.parametrize("fault", ["voltage", "power", "low"])
    def test_pending_stop_keeps_safety_gate_priority(self, mode, fault):
        from unittest.mock import Mock

        from Prog.src.pulse_measurement_service import (
            PulseOvervoltageError,
            PulsePowerLimitError,
        )

        service, load, dmm = make_service()
        service._trigger_mode = mode
        hook = Mock(return_value=True)
        service.handoff_stop_requested = hook
        dmm.voltage_V = 15.0 if fault == "voltage" else 10.8 if fault == "low" else 13.0
        current = 100.0 if fault == "power" else 1.4
        if fault == "low":
            assert not service.begin_pulse(current, 1.0)
            assert service.finalize().skipped_low_voltage
        else:
            error = PulseOvervoltageError if fault == "voltage" else PulsePowerLimitError
            with pytest.raises(error):
                service.begin_pulse(current, 1.0)
        hook.assert_not_called()
        assert not any(c.startswith((
            "set_mode_cc(", "set_current(", "input_on(", "configure_transient_pulse(",
        )) for c in load.calls)

    @pytest.mark.parametrize("mode", [
        "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED", "HARDWARE_PULSE_SOFTWARE_TRIGGERED",
    ])
    def test_false_hook_preserves_result_and_instrument_transcript(self, mode):
        from dataclasses import asdict

        def acquire(hook):
            service, load, dmm = make_service()
            service._trigger_mode = mode
            service.handoff_stop_requested = hook
            dmm.voltage_V = 13.5
            dmm.timed_voltage_fn = lambda _t: 13.2
            assert service.begin_pulse(1.4, 1.0)
            _run_all_chunks(service)
            result = asdict(service.finalize())
            return result, load.calls, dmm.calls

        assert acquire(None) == acquire(lambda: False)


# R16-B13F1: a visszatért I/O, a chunkköz és a tranziens várakozás testvérei.
@pytest.mark.usefixtures("_legacy_hardware_acquisition")
class TestR16B13F1Pulse:
    @pytest.mark.parametrize("hardware,arrival", [
        (False, "gap"), (False, "configure_timed_acquisition"),
        (False, "check_error"), (False, "get_sample_timer_s"),
        (False, "arm_timed_acquisition"), (False, "reset_to_single_sample"),
        (False, "measure_current"), (True, "input_on"),
        (True, "fire_transient_pulse"), (True, "fetch_trace"),
        (True, "set_io_timeout_ms"), (True, "reset_to_single_sample"),
    ])
    def test_energized_io_handoff(self, monkeypatch, hardware, arrival):
        service, load, dmm = make_service()
        if hardware:
            service._trigger_mode = "HARDWARE_PULSE_SOFTWARE_TRIGGERED"
            load.trace_response = "1.4,1.4"
        assert service.begin_pulse(1.4, 3.0)
        pending, transcript = [arrival == "gap"], []
        service.handoff_stop_requested = lambda: pending[0]
        device = load if arrival in {
            "input_on", "fire_transient_pulse", "fetch_trace", "measure_current",
        } else dmm
        if arrival != "gap":
            original = getattr(device, arrival)

            def signal_after_return(*args, **kwargs):
                value = original(*args, **kwargs)
                if not hardware or load.input_commanded_on:
                    pending[0] = True
                    transcript.clear()
                return value

            monkeypatch.setattr(device, arrival, signal_after_return)
        for target, methods in (
            (load, ("input_off", "input_on", "measure_current", "fire_transient_pulse", "fetch_trace")),
            (dmm, ("configure_timed_acquisition", "arm_timed_acquisition",
                   "fetch_timed_acquisition", "reset_to_single_sample", "set_io_timeout_ms")),
        ):
            for method in methods:
                original_method = getattr(target, method)

                def recorded(*args, original_method=original_method, method=method, **kwargs):
                    if pending[0]:
                        transcript.append(method)
                    return original_method(*args, **kwargs)

                monkeypatch.setattr(target, method, recorded)
        assert not service.step_chunk()
        assert transcript[0] == "input_off", transcript
        assert not load.input_commanded_on
        assert not any(c in transcript for c in (
            "input_on", "fire_transient_pulse", "arm_timed_acquisition",
            "configure_timed_acquisition", "fetch_timed_acquisition",
        ))
        before = list(transcript)
        assert not service.step_chunk()
        assert transcript == before

    @pytest.mark.parametrize("hardware", [False, True])
    @pytest.mark.parametrize("raw,reason", [
        ("NaN", "érvénytelen minta"), ("", "nincs érvényes DMM"),
        ("15.0", "BATTERY_OVERVOLTAGE"),
    ])
    def test_fetch_fault_precedes_pending_stop(self, monkeypatch, hardware, raw, reason):
        service, load, dmm = make_service()
        if hardware:
            service._trigger_mode = "HARDWARE_PULSE_SOFTWARE_TRIGGERED"
            load.trace_response = "1.4,1.4"
        assert service.begin_pulse(1.4, 1.0)
        pending = [False]
        service.handoff_stop_requested = lambda: pending[0]

        def fetch(**kwargs):
            pending[0] = True
            return raw

        monkeypatch.setattr(dmm, "fetch_timed_acquisition", fetch)
        with pytest.raises(Exception, match=reason):
            service.step_chunk()
        assert not load.input_commanded_on

    def test_real_driver_retry_budgets_run_after_off(self, monkeypatch):
        from Prog.drivers.device_dmm import Keysight34465ADMM
        from Prog.drivers.device_load import Keithley2380Load

        service, load, dmm = make_service()
        clock = InstrumentLatencyClock()
        service._time_source = clock
        assert service.begin_pulse(1.4, 3.0)
        pending, transcript = [False], []
        service.handoff_stop_requested = lambda: pending[0]

        class TimeoutResource:
            def __init__(self, label, seconds):
                self.label, self.seconds = label, seconds

            def clear(self):
                pass

            def query(self, command):
                transcript.append((self.label, clock.now))
                clock.advance(self.seconds)
                raise TimeoutError("Mock VISA retry, valódi transport nélkül")

        real_dmm, real_load = Keysight34465ADMM(), Keithley2380Load()
        real_dmm._resource = TimeoutResource("DMM", real_dmm.timeout_ms / 1000)
        real_load._resource = TimeoutResource("current", real_load.timeout_ms / 1000)
        def reset_via_real_driver(*, owner=None):
            assert dmm._measurement_owner is owner
            return real_dmm.reset_to_single_sample()

        monkeypatch.setattr(dmm, "reset_to_single_sample", reset_via_real_driver)
        monkeypatch.setattr(load, "measure_current", real_load.measure_current)
        original_fetch, original_off = dmm.fetch_timed_acquisition, load.input_off

        def fetch(**kwargs):
            raw = original_fetch(**kwargs)
            pending[0] = True
            return raw

        def off():
            transcript.append(("OFF", clock.now))
            return original_off()

        monkeypatch.setattr(dmm, "fetch_timed_acquisition", fetch)
        monkeypatch.setattr(load, "input_off", off)
        assert not service.step_chunk()
        assert transcript[0] == ("OFF", 100.0), transcript
        assert [label for label, _ in transcript] == ["OFF", "DMM", "DMM", "DMM"]
        assert clock.now == 130.0
        assert not service.measured_energy_so_far().reading_count

    @pytest.mark.parametrize("hardware", [False, True])
    def test_empty_trace_after_stop_does_not_create_setpoint_energy(self, monkeypatch, hardware):
        service, _load, dmm = make_service()
        if hardware:
            service._trigger_mode = "HARDWARE_PULSE_SOFTWARE_TRIGGERED"
        assert service.begin_pulse(1.4, 1.0)
        pending = [False]
        service.handoff_stop_requested = lambda: pending[0]
        original = dmm.fetch_timed_acquisition

        def fetch(**kwargs):
            raw = original(**kwargs)
            pending[0] = True
            return raw

        monkeypatch.setattr(dmm, "fetch_timed_acquisition", fetch)
        assert not service.step_chunk()
        assert service._samples
        assert not service.measured_energy_so_far().reading_count
        assert not service.measured_energy_so_far().removed_Ah
        if hardware:
            assert service._current_trace_incomplete

    def test_measured_power_fault_precedes_stop_during_current_read(self, monkeypatch):
        from Prog.src.pulse_measurement_service import PulsePowerLimitError

        service, load, _dmm = make_service()
        assert service.begin_pulse(1.4, 1.0)
        pending = [False]
        service.handoff_stop_requested = lambda: pending[0]

        def current():
            pending[0] = True
            return 100.0

        monkeypatch.setattr(load, "measure_current", current)
        with pytest.raises(PulsePowerLimitError, match="LOAD_POWER_LIMIT_EXCEEDED"):
            service.step_chunk()
        assert not load.input_commanded_on


# R16-B13F3: a HW finally OFF-hibája mellett is kötelező a DMM-reset.
@pytest.mark.usefixtures("_legacy_hardware_acquisition")
class TestR16B13F3Pulse:
    @pytest.mark.parametrize("reset_fails", [False, True])
    def test_timeout_stop_off_error_still_attempts_reset(self, monkeypatch, reset_fails):
        service, load, dmm = make_service()
        service._trigger_mode = "HARDWARE_PULSE_SOFTWARE_TRIGGERED"
        load.trace_response = "1.4,1.4"
        assert service.begin_pulse(1.4, 1.0)
        pending, transcript, timeout_calls = [False], [], []
        service.handoff_stop_requested = lambda: pending[0]
        original_timeout = dmm.set_io_timeout_ms
        original_reset = dmm.reset_to_single_sample
        off_error = RuntimeError("Mockolt Load OFF hiba")

        def timeout(value):
            original_timeout(value)
            timeout_calls.append(value)
            if len(timeout_calls) == 2:
                # Csak a sikeres fetch utáni timeout-visszaállítás jelez.
                pending[0] = True
                transcript.append("signal")

        def off():
            transcript.append("off")
            raise off_error

        def reset():
            transcript.append("reset")
            if reset_fails:
                raise RuntimeError("Mockolt DMM-reset hiba")
            original_reset()

        monkeypatch.setattr(dmm, "set_io_timeout_ms", timeout)
        monkeypatch.setattr(load, "input_off", off)
        monkeypatch.setattr(dmm, "reset_to_single_sample", reset)
        with pytest.raises(RuntimeError) as caught:
            service.step_chunk()
        assert caught.value is off_error
        assert len(timeout_calls) == 2
        assert transcript == ["signal", "off", "reset"]
        assert service._done
        assert dmm._timed_mode_active is reset_fails


class TestR16B16bPulseFloor:
    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("values", [
        (2.0, 1.90, 1.79),  # az első cutoff UTÁN rejtőző alacsony minta
        (2.0, 1.79, 2.0),  # a középső nyers minta sem veszhet el
        (1.79, 2.0, 2.0),  # az első átlépő pontra sincs Q-35 kivétel
    ])
    @pytest.mark.parametrize("off_fails", [False, True])
    def test_every_raw_sample_before_cutoff_truncation(self, cells, values, off_fails):
        from Prog.src.pulse_measurement_service import PulseDischargeError
        safety = _pulse_safety(cells)
        load, dmm = MockLoad(), MockDMM(voltage_V=2.1 * cells)
        service = PulseMeasurementService(
            load, dmm, 1.90 * cells, safety=safety,
            max_voltage_V=2.425 * cells, max_load_power_W=250.0,
            chunk_sample_count=3, time_source=FakeMonotonicClock(step_s=0.6),
        )
        assert service.begin_pulse(1.34, 1.8)
        dmm.fetch_timed_acquisition_raw_override = ",".join(str(v * cells) for v in values)
        if off_fails:
            def bad_off():
                load.calls.append("input_off()")
                raise RuntimeError("OFF failed")
            load.input_off = bad_off
        with pytest.raises(RuntimeError, match="BATTERY_UNDERVOLTAGE") as caught:
            service.step_chunk()
        if off_fails:
            assert isinstance(caught.value.__cause__, PulseDischargeError)
        else:
            assert isinstance(caught.value, PulseDischargeError)
            assert not load.input_commanded_on
        assert load.called("input_off()")
        before = list(load.calls)
        assert service.step_chunk() is False
        with pytest.raises(PulseDischargeError):
            service.begin_pulse(1.34, 1.8)
        assert load.calls == before

    @pytest.mark.parametrize("cutoff", [float("nan"), float("inf"), -float("inf"), 10.799])
    def test_invalid_cutoff_rejected_without_io(self, cutoff):
        load, dmm = MockLoad(), MockDMM()
        with pytest.raises(ValueError, match="GOV-029"):
            PulseMeasurementService(
                load, dmm, cutoff, safety=_pulse_safety(),
                max_voltage_V=14.55, max_load_power_W=250.0,
            )
        assert load.calls == []
        assert dmm.calls == []

    def test_setup_below_floor_faults_and_cannot_restart(self):
        from Prog.src.pulse_measurement_service import PulseDischargeError
        service, load, dmm = make_service()
        dmm.voltage_V = 10.799
        with pytest.raises(PulseDischargeError, match="BATTERY_UNDERVOLTAGE"):
            service.begin_pulse(1.34, 1.8)
        dmm.voltage_V = 13.0
        with pytest.raises(PulseDischargeError):
            service.begin_pulse(1.34, 1.8)
        assert not load.input_commanded_on

    @pytest.mark.parametrize("entry", ["begin", "hardware_step", "timed_private_step"])
    def test_hardware_mode_refused_without_io(self, entry):
        from Prog.src.pulse_measurement_service import PulseDischargeError
        service, load, dmm = make_service()
        if entry != "timed_private_step":
            service._trigger_mode = "HARDWARE_PULSE_SOFTWARE_TRIGGERED"
        with pytest.raises(PulseDischargeError, match="HARDWARE_PULSE_HIL_PENDING"):
            if entry == "begin":
                service.begin_pulse(1.34, 1.8)
            else:
                service._step_hardware_pulse()
        assert load.calls == []
        assert dmm.calls == []


@pytest.fixture
def _legacy_hardware_acquisition(monkeypatch):
    # Csak a régi, tisztán mockolt hardver-acquisition unitok belső útját
    # nyitjuk meg. Az új B16b tiltástesztek NEM használják ezt a fixture-t.
    monkeypatch.setattr(
        "Prog.src.pulse_measurement_service.require_battery_pulse_mode",
        lambda mode: None,
    )


class TestR16B17PulseContracts:
    def test_floor_fault_wins_over_stop_arriving_at_fetch(self, monkeypatch):
        from Prog.src.pulse_measurement_service import PulseDischargeError

        service, load, dmm = make_service()
        pending = [False]
        service.handoff_stop_requested = lambda: pending[0]
        assert service.begin_pulse(1.34, 3.0)
        dmm.fetch_timed_acquisition_raw_override = "12.0,10.799,12.0"
        fetch = dmm.fetch_timed_acquisition

        def fetch_then_stop(**kwargs):
            raw = fetch(**kwargs)
            pending[0] = True
            return raw

        monkeypatch.setattr(dmm, "fetch_timed_acquisition", fetch_then_stop)
        with pytest.raises(PulseDischargeError, match="BATTERY_UNDERVOLTAGE"):
            service.step_chunk()
        assert pending[0]
        assert load.called("input_off()") and not load.input_commanded_on
        before = (list(load.calls), list(dmm.calls))
        assert service.step_chunk() is False
        pending[0] = False
        with pytest.raises(PulseDischargeError, match="BATTERY_UNDERVOLTAGE"):
            service.begin_pulse(1.34, 3.0)
        assert (load.calls, dmm.calls) == before

    def test_safety_argument_is_required_without_io(self):
        load, dmm = MockLoad(), MockDMM()
        with pytest.raises(TypeError, match="safety"):
            PulseMeasurementService(
                load, dmm, 10.8, max_voltage_V=14.55, max_load_power_W=250.0,
            )
        assert load.calls == [] and dmm.calls == []



class TestR16B21aChunkOwner:
    """A régi pacing mellett minden élő timed chunk kizárólagos DMM-tranzakció."""

    @pytest.mark.parametrize("interval", [0.2, 0.005])
    @pytest.mark.parametrize("boundary", [
        "configure_timed_acquisition", "check_error", "get_sample_timer_s",
        "arm_timed_acquisition", "fetch_timed_acquisition", "reset_to_single_sample",
    ])
    def test_real_driver_owner_covers_every_io_boundary(self, monkeypatch, interval, boundary):
        from unittest.mock import MagicMock

        from Prog.drivers.device_dmm import DMMMeasurementBusy, Keysight34465ADMM

        service, load, _mock = make_service(chunk_sample_count=3, sample_interval_s=interval)
        dmm = Keysight34465ADMM()
        resource = MagicMock()
        dmm._resource = resource

        def query(command):
            if command == "READ?":
                return "13.5"
            if command == "SYST:ERR?":
                return "0"
            if command == "SAMP:TIM?":
                return str(interval)
            if command == "FETC?":
                return "13.4,13.3,13.2"
            return "1"

        resource.query.side_effect = query
        service._dmm_v = dmm
        original = getattr(dmm, boundary)
        observed = []

        def inspect(*args, **kwargs):
            if kwargs.get("owner") is not dmm._measurement_owner:
                return original(*args, **kwargs)
            assert dmm.measurement_owned
            assert kwargs["owner"] is dmm._measurement_owner
            observed.append(kwargs["owner"])
            before = list(resource.mock_calls)
            for operation in (
                dmm.read_voltage, dmm.check_error, dmm.disconnect,
                lambda: dmm.set_nplc(2),
                lambda: dmm.acquire_measurement(object()),
                lambda: dmm.configure_timed_acquisition(1, interval, owner=object()),
            ):
                with pytest.raises(DMMMeasurementBusy):
                    operation()
                assert resource.mock_calls == before
            return original(*args, **kwargs)

        monkeypatch.setattr(dmm, boundary, inspect)
        assert service.begin_pulse(1.4, 6 * interval)
        assert service.step_chunk() is True
        assert not dmm.measurement_owned
        assert dmm.read_voltage() == 13.5
        assert service.step_chunk() is False
        assert len(observed) == 2
        assert not dmm.measurement_owned
        assert not load.input_commanded_on
        assert service.measured_energy_so_far().reading_count == 2

    @pytest.mark.parametrize("interval", [0.2, 0.005])
    @pytest.mark.parametrize("outcome", [
        "normal", "stop_config", "stop_fetch", "fetch_error", "reset_error",
        "fetch_and_reset_error", "floor_fault",
    ])
    def test_release_on_terminal_paths_preserves_off_and_original_error(
        self, monkeypatch, interval, outcome,
    ):
        from Prog.src.exceptions import InstrumentTimeoutError
        from Prog.src.pulse_measurement_service import PulseDischargeError

        service, load, dmm = make_service(chunk_sample_count=3, sample_interval_s=interval)
        assert service.begin_pulse(1.4, 3 * interval)
        pending = [False]
        service.handoff_stop_requested = lambda: pending[0]
        events = []
        for name in ("acquire_measurement", "release_measurement", "reset_to_single_sample"):
            original = getattr(dmm, name)

            def record(*args, name=name, original=original, **kwargs):
                events.append(name)
                if name == "reset_to_single_sample" and outcome in ("reset_error", "fetch_and_reset_error"):
                    raise RuntimeError("DMM reset hiba")
                return original(*args, **kwargs)

            monkeypatch.setattr(dmm, name, record)
        original_off = load.input_off

        def off():
            events.append("off")
            return original_off()

        monkeypatch.setattr(load, "input_off", off)
        cause = InstrumentTimeoutError("eredeti fetch hiba")
        fetch = dmm.fetch_timed_acquisition

        def fetch_result(**kwargs):
            if outcome in ("fetch_error", "fetch_and_reset_error"):
                raise cause
            if outcome == "stop_fetch":
                pending[0] = True
            if outcome == "floor_fault":
                return "13.4,10.799,13.2"
            return fetch(**kwargs)

        monkeypatch.setattr(dmm, "fetch_timed_acquisition", fetch_result)
        if outcome == "stop_config":
            configure = dmm.configure_timed_acquisition

            def stop_config(*args, **kwargs):
                result = configure(*args, **kwargs)
                pending[0] = True
                return result

            monkeypatch.setattr(dmm, "configure_timed_acquisition", stop_config)
        if outcome in ("fetch_error", "fetch_and_reset_error"):
            with pytest.raises(InstrumentTimeoutError) as caught:
                service.step_chunk()
            assert caught.value is cause
        elif outcome == "floor_fault":
            with pytest.raises(PulseDischargeError, match="BATTERY_UNDERVOLTAGE"):
                service.step_chunk()
        else:
            assert service.step_chunk() is False
        assert events.count("acquire_measurement") == 1
        assert events.count("release_measurement") == 1
        assert events.index("reset_to_single_sample") < events.index("release_measurement")
        if outcome in ("stop_config", "stop_fetch", "fetch_error", "fetch_and_reset_error"):
            assert events.index("off") < events.index("reset_to_single_sample")
        assert not dmm.measurement_owned
        assert not load.input_commanded_on
        if outcome in ("reset_error", "fetch_and_reset_error"):
            assert dmm._timed_mode_active
        else:
            assert dmm.read_voltage() == 12.5
        if outcome in ("stop_config", "stop_fetch", "fetch_error", "fetch_and_reset_error", "floor_fault"):
            assert service.measured_energy_so_far().reading_count == 0

    def test_failed_acquire_does_not_reset_or_release_foreign_owner(self, monkeypatch):
        from unittest.mock import Mock

        from Prog.drivers.device_dmm import DMMMeasurementBusy

        service, load, dmm = make_service()
        assert service.begin_pulse(1.4, 3.0)
        owner = object()
        dmm.acquire_measurement(owner)
        reset = Mock(wraps=dmm.reset_to_single_sample)
        release = Mock(wraps=dmm.release_measurement)
        monkeypatch.setattr(dmm, "reset_to_single_sample", reset)
        monkeypatch.setattr(dmm, "release_measurement", release)
        before = list(dmm.calls)
        try:
            with pytest.raises(DMMMeasurementBusy):
                service.step_chunk()
            reset.assert_not_called()
            release.assert_not_called()
            assert dmm._measurement_owner is owner
            assert dmm.calls == before
            assert not load.input_commanded_on
        finally:
            dmm.release_measurement(owner)

    def test_stop_during_acquire_prevents_new_configuration(self, monkeypatch):
        from unittest.mock import Mock

        service, load, dmm = make_service()
        assert service.begin_pulse(1.4, 3.0)
        pending = [False]
        service.handoff_stop_requested = lambda: pending[0]
        acquire = dmm.acquire_measurement

        def acquire_then_stop(owner):
            acquire(owner)
            pending[0] = True

        monkeypatch.setattr(dmm, "acquire_measurement", acquire_then_stop)
        configure = Mock(wraps=dmm.configure_timed_acquisition)
        monkeypatch.setattr(dmm, "configure_timed_acquisition", configure)
        assert service.step_chunk() is False
        configure.assert_not_called()
        assert not dmm.measurement_owned
        assert not load.input_commanded_on
        assert not service.measured_energy_so_far().reading_count


from Prog.tests.mock_drivers.fake_clock import _B21bRig


class TestR16B21bOwnedAcquisition:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("arrival", [
        "acquire_measurement", "configure_timed_acquisition", "check_error",
        "get_sample_timer_s", "arm_timed_acquisition", "fetch_timed_acquisition",
        "measure_current",
    ])
    def test_stop_at_each_boundary_off_before_drain_no_more_io(self, monkeypatch, fast, arrival):
        from threading import Event, get_ident

        rig = _B21bRig.make(monkeypatch, fast)
        stop = Event()
        rig.ctrl.handoff_stop_requested = stop.is_set
        events = _B21bRig.trace(monkeypatch, rig)
        device = rig.load if arrival == "measure_current" else rig.dmm
        original = getattr(device, arrival)

        def interrupt(*args, **kwargs):
            result = original(*args, **kwargs)
            events.append(("stop", arrival, get_ident()))
            stop.set()
            return result

        monkeypatch.setattr(device, arrival, interrupt)
        _B21bRig.run(rig)
        names = [name for _, name, _ in events]
        index = next(i for i, (label, _, _) in enumerate(events) if label == "stop")
        assert names[index + 1] == "input_off"
        assert names.index("input_off") < names.index("all_outputs_off")
        assert names.index("all_outputs_off") < names.index("reset_to_single_sample")
        assert names.index("reset_to_single_sample") < names.index("release_measurement")
        assert names.index("release_measurement") < names.index("set_nplc")
        allowed = {"input_off", "all_outputs_off", "reset_to_single_sample", "release_measurement", "set_nplc"}
        assert all(name in allowed for name in names[index + 1:])
        expected_prefix = (0.1 if fast else 0.6) * 1.4 / 3600.0 if arrival == "measure_current" else 0.0
        assert rig.service.measured_energy_so_far().removed_Ah == pytest.approx(expected_prefix)
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert rig.dmm._measurement_owner is None
        before = list(events)
        assert not rig.service.step_chunk()
        assert events == before

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("position", [0, 1, 2])
    @pytest.mark.parametrize("bad", ["10.79", "14.56", "nan", "inf", "9.9e37"])
    def test_full_raw_gate_rejects_whole_fault_chunk(self, monkeypatch, fast, position, bad):
        from Prog.src.ocv_soc_controller import OcvSocState

        rig = _B21bRig.make(monkeypatch, fast)
        raw = ["13.2"] * 3
        raw[position] = bad
        rig.dmm.fetch_timed_acquisition_raw_override = ",".join(raw)
        _B21bRig.run(rig)
        assert rig.ctrl.state is OcvSocState.FAULT
        if bad == "10.79":
            assert rig.ctrl.fault_reason.startswith("BATTERY_UNDERVOLTAGE:")
        if bad == "14.56":
            assert rig.ctrl.fault_reason.startswith("BATTERY_OVERVOLTAGE:")
        assert rig.ctrl.accumulated_discharge_Ah == 0.0
        assert rig.ctrl.accumulated_discharge_Wh == 0.0
        assert rig.service.measured_energy_so_far().reading_count == 0
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert rig.dmm.nplc == 10.0 and rig.dmm._measurement_owner is None

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("bad", ["10.79", "14.56"])
    def test_late_raw_fault_wins_over_reset_failure(self, monkeypatch, fast, bad):
        from threading import Event

        from Prog.src.ocv_soc_controller import OcvSocState

        rig = _B21bRig.make(monkeypatch, fast)
        entered, release = Event(), Event()
        events = _B21bRig.trace(monkeypatch, rig)
        psu_off = rig.psu.all_outputs_off

        def fetch(**kwargs):
            entered.set()
            assert release.wait(1.0)
            return f"13.2,{bad},13.2"

        def off():
            psu_off()
            release.set()

        def reset(**kwargs):
            raise RuntimeError("reset proof failure")

        def expire(force, before_io):
            assert entered.wait(1.0)
            rig.clock.now = rig.service._t0 + rig.service._duration_s
            # A callback csak állapotot jelez, eredményt nem ad.

        monkeypatch.setattr(rig.dmm, "fetch_timed_acquisition", fetch)
        monkeypatch.setattr(rig.dmm, "reset_to_single_sample", reset)
        monkeypatch.setattr(rig.psu, "all_outputs_off", off)
        _B21bRig.run(rig, expire)
        assert rig.ctrl.state is OcvSocState.FAULT
        expected = "BATTERY_UNDERVOLTAGE:" if bad == "10.79" else "BATTERY_OVERVOLTAGE:"
        assert rig.ctrl.fault_reason.startswith(expected)
        assert "reset proof failure" in rig.ctrl.fault_reason
        assert rig.ctrl.accumulated_discharge_Ah == 0.0
        assert rig.dmm._measurement_owner is None
        names = [name for _, name, _ in events]
        assert names.index("input_off") < names.index("all_outputs_off") < names.index("release_measurement")

    @pytest.mark.parametrize("fast", [False, True])
    def test_unreleased_worker_blocks_reentry_and_late_booking(self, monkeypatch, fast):
        from threading import Event

        from Prog.src.ocv_soc_controller import OcvSocState

        rig = _B21bRig.make(monkeypatch, fast)
        entered, release = Event(), Event()
        original_join = []

        def fetch(**kwargs):
            entered.set()
            assert release.wait(2.0)
            return "13.2,13.2,13.2"

        def expire(force, before_io):
            assert entered.wait(1.0)
            worker = rig.service._worker
            if not original_join:
                original_join.append(worker._thread.join)
                monkeypatch.setattr(worker._thread, "join", lambda timeout: original_join[0](0.0))
            rig.clock.now = rig.service._t0 + rig.service._duration_s
            # A callback csak állapotot jelez, eredményt nem ad.

        monkeypatch.setattr(rig.dmm, "fetch_timed_acquisition", fetch)
        try:
            _B21bRig.run(rig, expire)
            assert rig.ctrl.state is OcvSocState.FAULT
            assert "PULSE_DMM_RELEASE_PENDING" in rig.ctrl.fault_reason
            assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
            assert rig.dmm._measurement_owner is not None
            prefix = rig.ctrl.accumulated_discharge_Ah
            with pytest.raises(RuntimeError, match="PULSE_DMM_RELEASE_PENDING"):
                rig.service.begin_pulse(1.4, 30.0)
            release.set()
            original_join[0](1.0)
            assert not rig.service._worker._thread.is_alive()
            assert rig.dmm._measurement_owner is None
            assert rig.ctrl.accumulated_discharge_Ah == prefix == 0.0
        finally:
            release.set()
            if original_join:
                original_join[0](1.0)

    @pytest.mark.parametrize("fast", [False, True])
    def test_missing_first_current_is_explicit_degraded_not_setpoint(self, monkeypatch, fast):
        from threading import Event

        from Prog.src.ocv_soc_controller import OcvSocState

        rig = _B21bRig.make(monkeypatch, fast)
        entered, release = Event(), Event()
        psu_off = rig.psu.all_outputs_off
        rig.load.current_A = 0.73

        def fetch(**kwargs):
            entered.set()
            assert release.wait(1.0)
            return "13.2,13.2,13.2"

        def off():
            psu_off()
            release.set()

        def expire(force, before_io):
            assert entered.wait(1.0)
            rig.clock.now = rig.service._t0 + rig.service._duration_s
            # A callback csak állapotot jelez, eredményt nem ad.

        monkeypatch.setattr(rig.dmm, "fetch_timed_acquisition", fetch)
        monkeypatch.setattr(rig.psu, "all_outputs_off", off)
        _B21bRig.run(rig, expire)
        assert rig.ctrl.state is OcvSocState.LOG_SOC_POINT
        result = rig.service.finalize()
        assert result.current_trace_incomplete
        assert result.pulse_current_set_A == 1.4
        assert result.pulse_current_measured_mean_A is None
        assert result.removed_Ah == result.removed_Wh == 0.0
        assert all(point.current_A is None and point.resistance_ohm is None for point in result.results.values())
        if not fast:
            assert rig.ctrl._determine_point_quality() != "OK"

    @pytest.mark.parametrize("fast", [False, True])
    def test_fault_cancel_precedes_blocked_off_and_new_worker_io(self, monkeypatch, fast):
        from threading import Event

        from Prog.src.ocv_soc_controller import OcvSocState

        rig = _B21bRig.make(monkeypatch, fast)
        entered, continue_config = Event(), Event()
        events = _B21bRig.trace(monkeypatch, rig)
        configure, load_off = rig.dmm.configure_timed_acquisition, rig.load.input_off

        def config(*args, **kwargs):
            value = configure(*args, **kwargs)
            entered.set()
            assert continue_config.wait(1.0)
            return value

        def off():
            assert rig.service._worker is None or rig.service._worker.cancel.is_set()
            continue_config.set()
            return load_off()

        def fault(force, before_io):
            assert entered.wait(1.0)
            return "SCPI_CRITICAL_ERROR[Load]: injected trip"

        monkeypatch.setattr(rig.dmm, "configure_timed_acquisition", config)
        monkeypatch.setattr(rig.load, "input_off", off)
        try:
            _B21bRig.run(rig, fault)
            assert rig.ctrl.state is OcvSocState.FAULT
            assert rig.ctrl.fault_reason.startswith("SCPI_CRITICAL_ERROR[Load]: injected trip")
            names = [name for _, name, _ in events]
            assert "get_sample_timer_s" not in names
            assert "arm_timed_acquisition" not in names
            assert "fetch_timed_acquisition" not in names
            assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        finally:
            continue_config.set()


class TestR16B21bTerminalBoundaries:
    @pytest.mark.parametrize("fast", [False, True])
    def test_current_communication_error_stops_with_prefix(self, monkeypatch, fast):
        rig = _B21bRig.make(monkeypatch, fast)
        current = rig.load.measure_current
        calls = []

        def read():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("B21b current communication lost")
            return current()

        monkeypatch.setattr(rig.load, "measure_current", read)
        _B21bRig.run(rig)
        assert rig.ctrl.state.name == "FAULT"
        assert "B21b current communication lost" in rig.ctrl.fault_reason
        prefix = 1.4 * (0.1 if fast else 0.6) / 3600.0
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(prefix)
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert rig.dmm._measurement_owner is None and rig.dmm.nplc == 10.0

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("arrival", [
        "configure_timed_acquisition", "arm_timed_acquisition", "fetch_timed_acquisition",
    ])
    def test_recovery_stop_does_not_start_next_measurement(self, monkeypatch, fast, arrival):
        rig = _B21bRig.make(monkeypatch, fast, start=False)
        rig.service._recovery_duration_s = 1.0
        _B21bRig.runner(monkeypatch, rig)
        events = _B21bRig.trace(monkeypatch, rig)
        method = getattr(rig.dmm, arrival)
        stopped = []

        def during_recovery(*args, **kwargs):
            value = method(*args, **kwargs)
            if kwargs.get("owner") is None and rig.service._started:
                stopped.append((len(events), rig.ctrl.accumulated_discharge_Ah))
                rig.runner.request_stop()
            return value

        monkeypatch.setattr(rig.dmm, arrival, during_recovery)
        session = rig.runner.run(rig.plan)
        assert session.status == "STOPPED" and len(stopped) == 1
        later = [name for label, name, _ in events[stopped[0][0]:] if label == "dmm"]
        assert "arm_timed_acquisition" not in later
        assert "fetch_timed_acquisition" not in later
        assert "configure_timed_acquisition" not in later
        assert not rig.service.finalize().recovery_samples
        assert session.total_discharge_ah == pytest.approx(stopped[0][1])
        assert rig.dmm._measurement_owner is None and rig.dmm.nplc == 10.0


class TestR16B21bFailedOwnerRelease:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("bad", [None, "10.79", "14.56"])
    def test_dead_worker_without_release_ack_blocks_reentry_and_checks_raw(
        self, monkeypatch, fast, bad,
    ):
        from threading import Event

        rig = _B21bRig.make(monkeypatch, fast)
        fetch_entered, return_fetch = Event(), Event()
        psu_off = rig.psu.all_outputs_off
        fetch, release = rig.dmm.fetch_timed_acquisition, rig.dmm.release_measurement

        def late_fetch(**kwargs):
            fetch_entered.set()
            assert return_fetch.wait(3)
            raw = fetch(**kwargs)
            return raw if bad is None else f"13.2,{bad},13.2"

        def off():
            value = psu_off()
            return_fetch.set()
            return value

        def expire(force, before_io):
            assert fetch_entered.wait(1)
            rig.clock.now = rig.service._t0 + rig.service._duration_s

        def lost_release(owner):
            raise RuntimeError("B21b release proof lost")

        monkeypatch.setattr(rig.psu, "all_outputs_off", off)
        monkeypatch.setattr(rig.dmm, "fetch_timed_acquisition", late_fetch)
        monkeypatch.setattr(rig.dmm, "release_measurement", lost_release)
        try:
            _B21bRig.run(rig, expire)
            assert rig.ctrl.state.name == "FAULT"
            worker = rig.service._worker
            assert worker is not None and not worker._thread.is_alive()
            assert not worker.owner_release_confirmed
            assert rig.dmm._measurement_owner is not None
            assert "RELEASE_PENDING" in rig.ctrl.fault_reason
            assert "release proof lost" in rig.ctrl.fault_reason
            if bad is not None:
                expected = "BATTERY_UNDERVOLTAGE" if bad == "10.79" else "BATTERY_OVERVOLTAGE"
                assert rig.ctrl.fault_reason.startswith(expected)
            assert rig.ctrl.accumulated_discharge_Ah == 0.0
            before = (list(rig.dmm.calls), list(rig.load.calls))
            with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
                rig.service.prepare_owned_acquisition()
            with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
                rig.service.begin_pulse(1.4, 30.0)
            assert (rig.dmm.calls, rig.load.calls) == before
            assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        finally:
            return_fetch.set()
            if rig.service._worker is not None:
                rig.service._worker._thread.join(2)
            owner = rig.dmm._measurement_owner
            if owner is not None:
                release(owner)


class TestR16B21bBlockedResetRawGate:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("bad", ["10.79", "14.56"])
    def test_returned_unsafe_raw_is_checked_while_reset_and_release_pending(
        self, monkeypatch, fast, bad,
    ):
        from threading import Event

        from Prog.src.pulse_measurement_service import _PulseDmmWorker

        rig = _B21bRig.make(monkeypatch, fast)
        entered, release = Event(), Event()
        fetch_entered, return_fetch = Event(), Event()
        psu_off = rig.psu.all_outputs_off
        fetch, reset = rig.dmm.fetch_timed_acquisition, rig.dmm.reset_to_single_sample
        close = _PulseDmmWorker.close_after_safe_off
        joins = []

        def late_fetch(**kwargs):
            fetch_entered.set()
            assert return_fetch.wait(3)
            fetch(**kwargs)
            return f"13.2,{bad},13.2"

        def off():
            value = psu_off()
            return_fetch.set()
            return value

        def expire(force, before_io):
            assert fetch_entered.wait(1)
            rig.clock.now = rig.service._t0 + rig.service._duration_s

        def blocked_reset(**kwargs):
            entered.set()
            assert release.wait(3)
            return reset(**kwargs)

        def close_without_join(worker):
            if not joins:
                joins.append((worker._thread, worker._thread.join))

                def timed_out_join(timeout=None):
                    assert entered.wait(1)

                monkeypatch.setattr(worker._thread, "join", timed_out_join)
            return close(worker)

        monkeypatch.setattr(rig.psu, "all_outputs_off", off)
        monkeypatch.setattr(rig.dmm, "fetch_timed_acquisition", late_fetch)
        monkeypatch.setattr(rig.dmm, "reset_to_single_sample", blocked_reset)
        monkeypatch.setattr(_PulseDmmWorker, "close_after_safe_off", close_without_join)
        try:
            _B21bRig.run(rig, expire)
            expected = "BATTERY_UNDERVOLTAGE" if bad == "10.79" else "BATTERY_OVERVOLTAGE"
            assert rig.ctrl.state.name == "FAULT"
            assert rig.ctrl.fault_reason.startswith(expected)
            assert "RELEASE_PENDING" in rig.ctrl.fault_reason
            assert rig.service._worker is not None and joins[0][0].is_alive()
            assert rig.dmm._measurement_owner is not None
            assert rig.ctrl.accumulated_discharge_Ah == 0.0
            assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        finally:
            return_fetch.set()
            release.set()
            if joins:
                joins[0][1](2)
        assert rig.dmm._measurement_owner is None
        before = (list(rig.dmm.calls), list(rig.load.calls), rig.ctrl.accumulated_discharge_Ah)
        rig.service.finish_owned_acquisition()
        assert rig.service._worker is None
        assert (rig.dmm.calls, rig.load.calls, rig.ctrl.accumulated_discharge_Ah) == before


class TestR16B21bReleaseDrainRace:
    def test_empty_then_release_keeps_late_raw_handle_for_explicit_drain(self, monkeypatch):
        from queue import Empty, Queue
        from threading import Event
        from types import SimpleNamespace

        from Prog.src.pulse_measurement_service import PulseDischargeError

        rig = _B21bRig.make(monkeypatch)
        rig.service._owned_off()
        rig.psu.all_outputs_off()
        results = Queue()
        state = {"alive": True, "injected": False}
        worker = SimpleNamespace(
            results=results, owner_release_confirmed=False, cancel=Event(),
            _thread=SimpleNamespace(is_alive=lambda: state["alive"]),
        )

        def close():
            if state["alive"]:
                raise RuntimeError("PULSE_DMM_RELEASE_PENDING")

        original = results.get_nowait

        def racing_get():
            try:
                return original()
            except Empty:
                if not state["injected"]:
                    state["injected"] = True
                    results.put(("13.2,10.79,13.2", 0.6, None))
                    worker.owner_release_confirmed = True
                    state["alive"] = False
                raise

        worker.close_after_safe_off = close
        monkeypatch.setattr(results, "get_nowait", racing_get)
        rig.service._worker = worker
        before = list(rig.dmm.calls)
        with pytest.raises(RuntimeError, match="RELEASE_PENDING"):
            rig.service.finish_owned_acquisition()
        assert rig.service._worker is worker
        assert results.qsize() == 1
        with pytest.raises(PulseDischargeError, match="BATTERY_UNDERVOLTAGE"):
            rig.service.finish_owned_acquisition()
        assert rig.service._worker is None and results.empty()
        assert rig.dmm.calls == before
        assert rig.ctrl.accumulated_discharge_Ah == 0.0


class TestR16B24Pms:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("route", ["before_get", "after_get", "stop_after_get"])
    @pytest.mark.parametrize("always_fail", [False, True])
    def test_off_exception_keeps_returned_raw_gate_and_original(self, monkeypatch, fast, route, always_fail):
        from queue import Queue
        from threading import Event
        from types import SimpleNamespace

        from Prog.src.ocv_soc_controller import OcvSocState
        from Prog.tests.test_ocv_soc_controller import _b24_bound

        rig = _B21bRig.make(monkeypatch, fast)
        _b24_bound(monkeypatch, rig)
        service = rig.service
        service._block_deadline = service._t0 + service._duration_s
        results = Queue()
        results.put(("13.2,10.0,13.2", service._duration_s, None))
        worker = SimpleNamespace(
            results=results, cancel=Event(), close_after_safe_off=lambda: None,
            owner_release_confirmed=True, _thread=SimpleNamespace(is_alive=lambda: False),
        )
        service._worker = worker
        stop, off_errors = [], []
        service.handoff_stop_requested = lambda: bool(stop)
        if route == "before_get":
            rig.clock.now = service._block_deadline
        else:
            get = results.get

            def returned(**kwargs):
                value = get(**kwargs)
                if route == "after_get":
                    rig.clock.now = service._block_deadline
                else:
                    stop.append(1)
                return value

            monkeypatch.setattr(results, "get", returned)
        off = rig.load.input_off

        def fail_off():
            if not off_errors or always_fail:
                error = OSError("B24 first OFF" if not off_errors else "B24 later OFF")
                off_errors.append(error)
                raise error
            return off()

        monkeypatch.setattr(rig.load, "input_off", fail_off)
        with pytest.raises(OSError) as caught:
            service.step_chunk()
        assert off_errors and caught.value is off_errors[0]
        assert service._discharge_fault is not None
        assert "BATTERY_UNDERVOLTAGE" in str(service._discharge_fault)
        assert results.empty() and service.measured_energy_so_far().reading_count == 0
        events = []
        rig.ctrl.on_event = events.append
        rig.ctrl.emergency_stop("B24_FIRST_OFF")
        assert rig.ctrl.state is OcvSocState.FAULT
        assert "BATTERY_UNDERVOLTAGE" in rig.ctrl.fault_reason
        assert any(e["event_code"] == "BATTERY_UNDERVOLTAGE" and e["severity"] == "CRITICAL" for e in events)
        assert not rig.psu.output_commanded_on
        if not always_fail:
            assert not rig.load.input_commanded_on
        assert service._worker is None and rig.dmm.nplc == 10.0

    def test_post_off_raw_never_supplies_r30(self, monkeypatch):
        from threading import Event

        from Prog.tests.test_ocv_soc_controller import _b24_bound

        rig = _B21bRig.make(monkeypatch)
        _b24_bound(monkeypatch, rig)
        fetch = rig.dmm.fetch_timed_acquisition
        note_off = rig.service.note_load_off_return
        off_returned, triggered, unloaded_returned = Event(), [], []

        def returned_off():
            value = note_off()
            off_returned.set()
            return value

        def final_fetch(**kwargs):
            if not triggered and rig.service._block_deadline - rig.clock() <= 0.65:
                triggered.append(1)
                rig.clock.now = rig.service._block_deadline
                assert off_returned.wait(1.0), "B24 deadline OFF did not return"
                rig.dmm.timed_voltage_fn = lambda t: 13.45
            raw = fetch(**kwargs)
            if triggered:
                unloaded_returned.append(raw)
            return raw

        monkeypatch.setattr(rig.service, "note_load_off_return", returned_off)
        monkeypatch.setattr(rig.dmm, "fetch_timed_acquisition", final_fetch)
        _B21bRig.run(rig)
        assert triggered == [1] and off_returned.is_set()
        assert unloaded_returned and all("13.45" in raw for raw in unloaded_returned)
        result = rig.service.finalize()
        assert result.samples and all(s.voltage_V == 13.2 for s in result.samples)
        assert all(s.time_from_pulse_start_s <= rig.service._load_off_real_s for s in result.samples)
        assert result.results[30.0].voltage_V in (None, 13.2)
        assert rig.dmm._measurement_owner is None

    def test_worker_waits_for_both_offs_before_reset_release(self, monkeypatch):
        from threading import Event
        from types import SimpleNamespace

        from Prog.src.pulse_measurement_service import _PulseDmmWorker

        rig = _B21bRig.make(monkeypatch)
        settled, closing = Event(), Event()
        order, captures = [], []
        reset, release = rig.dmm.reset_to_single_sample, rig.dmm.release_measurement

        def capture(self, owner, count):
            captures.append(count)
            raise OSError("B24 capture failed before runner OFF")

        def wait():
            settled.set()
            assert closing.wait(1.0), "B24 worker close was not signalled"

        def reset_dmm(**kwargs):
            order.append("reset")
            settled.set()
            return reset(**kwargs)

        def release_dmm(owner):
            order.append("release")
            return release(owner)

        monkeypatch.setattr(_PulseDmmWorker, "_capture", capture)
        monkeypatch.setattr(rig.dmm, "reset_to_single_sample", reset_dmm)
        monkeypatch.setattr(rig.dmm, "release_measurement", release_dmm)
        worker = _PulseDmmWorker(rig.service)
        worker._closing = SimpleNamespace(wait=wait, set=closing.set)
        try:
            worker.request(3)
            assert settled.wait(1.0), "B24 worker never reached terminal wait/reset"
            assert captures == [3]
            assert order == []
            assert rig.load.input_commanded_on
            rig.load.input_off()
            order.append("load_off")
            rig.psu.all_outputs_off()
            order.append("psu_off")
        finally:
            worker.close_after_safe_off()
        assert order == ["load_off", "psu_off", "reset", "release"]
        assert worker.owner_release_confirmed and not worker._thread.is_alive()


@pytest.mark.usefixtures("_legacy_hardware_acquisition")
@pytest.mark.parametrize("query,reply", [
    ("CURR:TRAN:ALEV?", "0.2001"), ("CURR:TRAN:ALEV?", "0.1999"),
    ("CURR:TRAN:BLEV?", "0.1001"), ("CURR:TRAN:BLEV?", "0.0999"),
])
def test_r16b24_one_current_grid_step_is_rejected(monkeypatch, query, reply):
    service, load, dmm, resource = TestR16B9HardwarePulseRangeGate._make()
    resource.replies[query] = reply
    with pytest.raises(RuntimeError, match="HARDWARE_PULSE_CONFIG_REJECTED"):
        service.step_chunk()
    messages = [message for kind, message in resource.log if kind in ("Q", "W")]
    assert query in messages
    assert "INP ON" not in messages and "FORC:TRIG" not in messages
    assert not load.input_commanded_on and not resource.input_on
    assert not dmm.called("arm_timed_acquisition()")
    assert dmm.called("reset_to_single_sample()")


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("drain_type,restore_type", [
    (RuntimeError, KeyboardInterrupt), (KeyboardInterrupt, SystemExit),
])
def test_r16b24_drain_error_survives_interrupted_restore(monkeypatch, fast, drain_type, restore_type):
    from queue import Queue
    from threading import Event
    from types import SimpleNamespace

    rig = _B21bRig.make(monkeypatch, fast)
    rig.service._owned_off()
    rig.psu.all_outputs_off()
    primary, attempted = drain_type("B24 primary drain"), []

    def close():
        raise primary

    def restore(value):
        attempted.append(value)
        raise restore_type("B24 restore interrupted")

    rig.service._worker = SimpleNamespace(
        results=Queue(), cancel=Event(), close_after_safe_off=close,
        owner_release_confirmed=True, _thread=SimpleNamespace(is_alive=lambda: False),
    )
    monkeypatch.setattr(rig.dmm, "set_nplc", restore)
    with pytest.raises(drain_type) as caught:
        rig.service.restore_acquisition_nplc()
    assert caught.value is primary
    assert attempted == [10.0]
    assert rig.service.nplc_restore_failed and rig.service._worker is None
    assert any("NPLC_RESTORE_INTERRUPTED:B24 restore interrupted" in note for note in primary.__notes__)
    assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on


_B24FIX1_RAW_CASES = [
    ("floor", "13.2,10.0,13.2", "PulseDischargeError"),
    ("nan", "13.2,NaN,13.2", "InstrumentInvalidReading"),
    ("parse", "13.2,bad,13.2", "InstrumentInvalidReading"),
    ("empty", "", "ValueError"),
    ("overvoltage", "13.2,99.0,13.2", "PulseOvervoltageError"),
    ("power", "13.2,13.2,13.2", "PulsePowerLimitError"),
    ("cutoff", None, None),
    ("valid", "13.2,13.2,13.2", None),
]


class TestR16B24Fix1OffGate:
    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("route", [
        "deadline_before_get", "stop_before_get", "deadline_after_get", "stop_after_get",
    ])
    @pytest.mark.parametrize("kind,raw,gate_error", _B24FIX1_RAW_CASES)
    def test_first_off_error_allows_raw_gate_without_load_io(
        self, monkeypatch, fast, route, kind, raw, gate_error,
    ):
        from queue import Queue
        from threading import Event
        from types import SimpleNamespace

        from Prog.tests.test_ocv_soc_controller import _b24_bound

        rig = _B21bRig.make(monkeypatch, fast)
        _b24_bound(monkeypatch, rig)
        service = rig.service
        if kind == "power":
            service._max_load_power_W = 1.0
        if kind == "cutoff":
            raw = f"13.2,{service._terminate_voltage_V},13.2"
        service._block_deadline = service._t0 + service._duration_s
        results = Queue()
        results.put((raw, 0.6, None))
        order, triggered, stop = [], [], []
        service._worker = SimpleNamespace(
            results=results, cancel=Event(),
            close_after_safe_off=lambda: order.append("close"),
            owner_release_confirmed=True, _thread=SimpleNamespace(is_alive=lambda: False),
        )
        service.handoff_stop_requested = lambda: bool(stop)

        def trigger():
            assert not triggered, "B24 fix1 stop/deadline trigger repeated"
            triggered.append(1)
            if route.startswith("deadline"):
                rig.clock.now = service._block_deadline
            else:
                stop.append(1)

        if route.endswith("before_get"):
            trigger()
        else:
            get = results.get

            def returned(*args, **kwargs):
                value = get(*args, **kwargs)
                trigger()
                return value

            monkeypatch.setattr(results, "get", returned)
        primary = OSError("B24 fix1 first OFF")
        off_errors, off_times = [], []

        def failed_off():
            order.append("load_off")
            off_times.append(rig.clock())
            rig.clock.advance(2.0)
            error = primary if not off_errors else OSError("B24 fix1 later OFF")
            off_errors.append(error)
            raise error

        def forbidden_current():
            raise AssertionError("B24 fix1 Load read after failed OFF")

        psu_off = rig.psu.all_outputs_off
        psu_times = []

        def turn_psu_off():
            order.append("psu_off")
            psu_times.append(rig.clock())
            return psu_off()

        monkeypatch.setattr(rig.load, "input_off", failed_off)
        monkeypatch.setattr(rig.load, "measure_current", forbidden_current)
        monkeypatch.setattr(rig.psu, "all_outputs_off", turn_psu_off)
        with pytest.raises(OSError) as caught:
            service.step_chunk()
        assert triggered == [1] and results.empty()
        assert caught.value is primary
        assert order == ["load_off"], "Raw gate retried Load I/O before controller cleanup"
        if gate_error is not None:
            assert type(primary.__cause__).__name__ == gate_error
        if kind == "floor":
            assert service._discharge_fault is not None
            assert "10.000000 V < 10.800000 V" in str(service._discharge_fault)
        if kind == "cutoff":
            assert service._cutoff
        assert service.measured_energy_so_far().reading_count == 0
        rig.ctrl.emergency_stop("B24_FIX1_FIRST_OFF")
        assert order == ["load_off", "load_off", "psu_off", "close"]
        assert psu_times == [pytest.approx(off_times[0] + 4.0)]
        assert service._worker is None and rig.dmm.nplc == 10.0
        assert not rig.psu.output_commanded_on

    @pytest.mark.parametrize("fast", [False, True])
    @pytest.mark.parametrize("load_fails", [False, True])
    @pytest.mark.parametrize("kind,raw,gate_error", _B24FIX1_RAW_CASES)
    def test_drain_after_controller_offs_never_retries_load(
        self, monkeypatch, fast, load_fails, kind, raw, gate_error,
    ):
        from queue import Queue
        from threading import Event
        from types import SimpleNamespace

        from Prog.tests.test_ocv_soc_controller import _b24_bound

        rig = _B21bRig.make(monkeypatch, fast)
        _b24_bound(monkeypatch, rig)
        service = rig.service
        if kind == "power":
            service._max_load_power_W = 1.0
        if kind == "cutoff":
            raw = f"13.2,{service._terminate_voltage_V},13.2"
        results = Queue()
        results.put((raw, 0.6, None))
        order, events = [], []
        rig.ctrl.on_event = events.append
        service._worker = SimpleNamespace(
            results=results, cancel=Event(),
            close_after_safe_off=lambda: order.append("close"),
            owner_release_confirmed=True, _thread=SimpleNamespace(is_alive=lambda: False),
        )
        load_off, psu_off = rig.load.input_off, rig.psu.all_outputs_off

        def turn_load_off():
            order.append("load_off")
            if load_fails:
                raise OSError("B24 fix1 controller OFF")
            return load_off()

        def turn_psu_off():
            order.append("psu_off")
            return psu_off()

        def forbidden_current():
            raise AssertionError("B24 fix1 Load read during terminal drain")

        monkeypatch.setattr(rig.load, "input_off", turn_load_off)
        monkeypatch.setattr(rig.load, "measure_current", forbidden_current)
        monkeypatch.setattr(rig.psu, "all_outputs_off", turn_psu_off)
        rig.clock.advance(0.6)
        rig.ctrl.emergency_stop("B24_FIX1_DRAIN")
        assert order == ["load_off", "psu_off", "close"]
        assert results.empty() and service._worker is None
        assert rig.dmm.nplc == 10.0 and not service.nplc_restore_failed
        assert not rig.psu.output_commanded_on
        assert rig.load.input_commanded_on is load_fails
        if kind == "floor":
            assert service._discharge_fault is not None
            floor = [e for e in events if e["event_code"] == "BATTERY_UNDERVOLTAGE"]
            assert len(floor) == 1 and floor[0]["severity"] == "CRITICAL"
        if kind == "cutoff":
            assert service._cutoff
