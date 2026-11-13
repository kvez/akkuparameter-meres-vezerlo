"""
Tesztek: IecDcResistanceController állapotgép (IEC 60896-21 §6.3 kétpontos
DC belső ellenállás mérés).

Minta: test_ocv_soc_controller.py (Mock PSU/Load/DMM, direkt belső-állapot
manipuláció a lassú fázisok (PRECHARGE) gyors átugrásához).
"""
from __future__ import annotations

import pytest

from Prog.src.battery_profile import BatteryProfile
from Prog.src.charge_controller import ChargeState
from Prog.src.exceptions import ConfigurationError
from Prog.src.iec_dc_resistance_controller import (
    IecDcResistanceConfig,
    IecDcResistanceController,
    IecDcResistanceState,
)
from Prog.src.safety import PsuMode, SafetyManager, TempCompMode
from Prog.tests.mock_drivers.fake_clock import TickClock, bind_tick_clock
from Prog.tests.mock_drivers.fast_loop_charge import (
    DT_S,
    EXTRA_S,
    PERIOD_S,
    SUBSTEPS_PER_TICK,
    ScriptedChargeReadings,
    assert_parent_delta_equals_independent_expectation,
    cv_currents,
    expected_delta,
    fast_loop_charge_config,
    force_child_state,
    install_fast_loop_clock,
    run_tick,
    taper_currents,
    varying_voltages,
)
from Prog.tests.mock_drivers.mock_dmm import MockDMM
from Prog.tests.mock_drivers.mock_load import MockLoad
from Prog.tests.mock_drivers.mock_psu import MockPSU


def _make_profile(i10_A: float | None = 0.62, nominal_capacity_Ah: float = 7.2) -> BatteryProfile:
    return BatteryProfile(
        battery_name="TEST", manufacturer="FIAMM", model="FG20721",
        nominal_voltage_V=12.0, cell_count=6,
        nominal_capacity_Ah=nominal_capacity_Ah,
        i10_discharge_current_A=i10_A,
    )


def make_iec_controller(
    dmm_voltage_V: float = 12.8,
    i10_A: float | None = 0.62,
    config: IecDcResistanceConfig | None = None,
    clock: TickClock | None = None,
    charge_config=None,
) -> tuple[IecDcResistanceController, MockPSU, MockLoad, MockDMM, MockDMM]:
    """METRO-R20-1 (Kör 20 Batch E1): a controller mostantól
    `_load_on_elapsed_s`-t egy injektált monoton `time_source`-ból
    származtatja (l. iec_dc_resistance_controller.py). A `TickClock` +
    `bind_tick_clock()` alapértelmezésben ε=0-t szimulál -- azaz a
    `advance(dt_s)` hívások pontosan úgy léptetik az órát, ahogy a régi,
    dt_s-akkumuláló kód tette -- így ez a gyár és a MEGLÉVŐ tesztek
    VÁLTOZATLANOK maradnak. ε>0-t (a setup-tick input_on() ELŐTTI valós
    SCPI-időfogyasztást) a `burn_before_input_on()` explicit injektálja."""
    psu = MockPSU()
    load = MockLoad(voltage_V=dmm_voltage_V)
    dmm_v = MockDMM(voltage_V=dmm_voltage_V)
    dmm_t = MockDMM(temperature_C=22.0)
    profile = _make_profile(i10_A)
    safety = SafetyManager(
        profile=profile, psu_mode=PsuMode.INDEPENDENT,
        temp_comp_mode=TempCompMode.MONITOR_ONLY,
    )
    # Gyors tesztkonfiguráció -- a valós IEC időzítések (20s/25s/5s/300s/
    # 7200s) másodperc-pontosan skálázva, hogy a tesztek ne fussanak
    # ténylegesen percekig/órákig (advance(dt_s) szimulált időt léptet,
    # nem valós wall-clock-ot).
    cfg = config or IecDcResistanceConfig(
        sample_delay_1_s=2.0, cutoff_1_s=2.5,
        sample_delay_2_s=0.5, rest_duration_s=3.0,
        relax_before_measurement_s=1.0,
    )
    tick_clock = clock if clock is not None else TickClock()
    ctrl = IecDcResistanceController(
        psu, load, dmm_v, dmm_t, profile, safety, cfg, time_source=tick_clock,
        charge_config=charge_config,
    )
    bind_tick_clock(ctrl)
    return ctrl, psu, load, dmm_v, dmm_t


def _force_charge_done(ctrl) -> None:
    from Prog.src.charge_controller import ChargeState
    if ctrl._charge_ctrl is None:
        ctrl.advance(0.0)
    ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE


class TestConstructorRequiresI10:
    def test_raises_without_i10(self):
        psu, load = MockPSU(), MockLoad()
        dmm_v, dmm_t = MockDMM(), MockDMM()
        profile = _make_profile(i10_A=None)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        with pytest.raises(ConfigurationError):
            IecDcResistanceController(
                psu, load, dmm_v, dmm_t, profile, safety, IecDcResistanceConfig(),
            )

    def test_ia_ib_computed_from_i10(self):
        ctrl, *_ = make_iec_controller(i10_A=0.62)
        assert ctrl._ia_A == pytest.approx(4 * 0.62)
        assert ctrl._ib_A == pytest.approx(20 * 0.62)


class TestConfigValidation:
    def test_cutoff_must_exceed_sample_delay(self):
        with pytest.raises(ValueError):
            IecDcResistanceConfig(sample_delay_1_s=25.0, cutoff_1_s=20.0)

    def test_rest_duration_must_be_positive(self):
        with pytest.raises(ValueError):
            IecDcResistanceConfig(rest_duration_s=0.0)


class TestStateSequence:
    def test_starts_in_init_then_precharge(self):
        ctrl, *_ = make_iec_controller()
        assert ctrl.state == IecDcResistanceState.INIT
        ctrl.advance(0.0)
        assert ctrl.state == IecDcResistanceState.PRECHARGE

    def test_precharge_to_precharge_relax_on_charge_done(self):
        ctrl, *_ = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == IecDcResistanceState.PRECHARGE_RELAX

    def test_full_sequence_to_done(self):
        ctrl, psu, load, dmm_v, _dmm_t = make_iec_controller()
        ctrl.advance(0.0)                      # INIT -> PRECHARGE
        _force_charge_done(ctrl)
        ctrl.advance(1.0)                      # -> PRECHARGE_RELAX
        assert ctrl.state == IecDcResistanceState.PRECHARGE_RELAX

        ctrl.advance(1.0)                      # relax_before_measurement_s=1.0 -> LOAD_1
        assert ctrl.state == IecDcResistanceState.LOAD_1

        dmm_v.voltage_V = 12.0                 # Ua szimulált értéke
        # MAX verifier F1 fix: az input_on() a LOAD_1-be lépés UTÁNI ELSŐ
        # advance()-hívásban fut le (setup tick) -- a mintavétel csak az
        # EZUTÁNI (a load-on pillanatától mért) tickektől számít, l.
        # iec_dc_resistance_controller.py::_run_load_1() docstringje.
        ctrl.advance(0.0)                      # LOAD_1 setup (input_on())
        ctrl.advance(2.0)                      # sample_delay_1_s=2.0 (load-on óta) -> mintavétel + OPEN_CIRCUIT_REST
        assert ctrl.state == IecDcResistanceState.OPEN_CIRCUIT_REST
        assert ctrl._ua_V == pytest.approx(12.0)
        assert ctrl._ia_measured_A == pytest.approx(4 * 0.62)
        assert load.input_commanded_on is False

        ctrl.advance(3.0)                      # rest_duration_s=3.0 -> LOAD_2
        assert ctrl.state == IecDcResistanceState.LOAD_2

        dmm_v.voltage_V = 11.5                 # Ub szimulált értéke (Ub < Ua, Ib > Ia)
        ctrl.advance(0.0)                      # LOAD_2 setup (input_on())
        ctrl.advance(0.5)                      # sample_delay_2_s=0.5 (load-on óta) -> mintavétel + COMPUTE_RESULT
        assert ctrl.state == IecDcResistanceState.COMPUTE_RESULT
        ctrl.advance(0.0)                      # COMPUTE_RESULT végrehajtása -> DONE
        assert ctrl.state == IecDcResistanceState.DONE
        assert ctrl._ub_V == pytest.approx(11.5)
        assert ctrl._ib_measured_A == pytest.approx(20 * 0.62)
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False

    def test_load_1_current_far_below_setpoint_gives_warning_not_fault(self):
        """[regresszió, Hardver/Műszervezérlés HW4-02 (review-kör 4, P1,
        DFAM-001)] l. discharge_controller.py testvér tesztjének
        docstringje ugyanerről a hiányzó setpoint-vs-mért kapuról. Itt a
        forrás szerinti failure scenario szó szerinti alakzata: a 2380
        elutasítja a 20×I10-es setpointot, az áram az előző (itt: 0 A)
        szinten ragad, de a mért Ua/Ia fizikailag hihető marad."""
        ctrl, psu, load, dmm_v, _dmm_t = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)  # -> LOAD_1
        assert ctrl.state == IecDcResistanceState.LOAD_1

        dmm_v.voltage_V = 12.0
        ctrl.advance(0.0)  # LOAD_1 setup (input_on())
        load.current_A = ctrl._ia_A * 0.1  # a setpoint töredéke -- "ragadt"
        ctrl.advance(0.5)  # jóval sample_delay_1_s (2.0) előtt
        assert ctrl.state != IecDcResistanceState.FAULT
        assert ctrl.last_warning_code == "LOAD_CURRENT_SETPOINT_NOT_REACHED"

    def test_load_2_current_far_below_setpoint_gives_warning_not_fault(self):
        ctrl, psu, load, dmm_v, _dmm_t = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)  # -> LOAD_1
        dmm_v.voltage_V = 12.0
        ctrl.advance(0.0)  # LOAD_1 setup (input_on())
        ctrl.advance(2.0)  # -> OPEN_CIRCUIT_REST
        ctrl.advance(3.0)  # -> LOAD_2
        assert ctrl.state == IecDcResistanceState.LOAD_2

        dmm_v.voltage_V = 11.5
        ctrl.advance(0.0)  # LOAD_2 setup (input_on())
        load.current_A = ctrl._ib_A * 0.1  # a setpoint töredéke -- "ragadt"
        ctrl.advance(0.2)  # jóval sample_delay_2_s (0.5) előtt
        assert ctrl.state != IecDcResistanceState.FAULT
        assert ctrl.last_warning_code == "LOAD_CURRENT_SETPOINT_NOT_REACHED"

    def test_setpoint_warning_tick_counter_shared_across_load_1_and_load_2_survives_higher_priority_warning(self):
        """[regresszió, MAX verifier (review-kör 4, HW4-02, 329c1a3) N1
        lelete] l. discharge_controller.py testvér tesztjének docstringje
        ugyanerről a hiányzó, a _last_warning_code slot-tól független
        tick-számlálóról. Itt emellett azt is igazolja, hogy a LOAD_1/
        LOAD_2 KÖZÖS számlálót használ (l. __init__ docstringje)."""
        ctrl, psu, load, dmm_v, dmm_t = make_iec_controller()
        dmm_t.temperature_C = 40.0  # profil névleges tartományán (15-30C) kívül -- MINDEN tickben aktív
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)  # -> LOAD_1
        assert ctrl.state == IecDcResistanceState.LOAD_1

        dmm_v.voltage_V = 12.0
        ctrl.advance(0.0)  # LOAD_1 setup (input_on())
        load.current_A = ctrl._ia_A * 0.1  # a setpoint töredéke -- "ragadt"
        assert ctrl.load_current_setpoint_warning_ticks == 0
        ctrl.advance(0.5)
        assert ctrl.last_warning_code == "TEMPERATURE_OUT_OF_RANGE"
        assert ctrl.load_current_setpoint_warning_ticks == 1
        ctrl.advance(2.0)  # -> OPEN_CIRCUIT_REST (mintavétel Ua-nál)
        ctrl.advance(3.0)  # -> LOAD_2
        assert ctrl.state == IecDcResistanceState.LOAD_2

        dmm_v.voltage_V = 11.5
        ctrl.advance(0.0)  # LOAD_2 setup (input_on())
        load.current_A = ctrl._ib_A * 0.1  # a setpoint töredéke -- "ragadt"
        ctrl.advance(0.2)
        # 1 (LOAD_1, t=0.5) + 1 (LOAD_1, t=2.5 -> mintavétel/OPEN_CIRCUIT_REST
        # átmenet UGYANAZON a ticken) + 1 (LOAD_2, t=0.2) = 3 -- ugyanaz a
        # (LOAD_1/LOAD_2 közös) számláló nő tovább, sosem nullázódik.
        assert ctrl.load_current_setpoint_warning_ticks == 3


class TestRiIscComputation:
    """[Kör 21 F17, SRC21-04] I10=0,62A a forrásdokumentum (Folyamatok/
    tervek/AGM_VRLA_belsoellenallas_IEC60896_ACIR_HRM10.txt 8. szakasz,
    "FG20721 PÉLDA") saját munkapélda-értéke (Ia=2,48A, Ib=12,40A) --
    ezt a primer FIAMM adatlap azóta I10=0,67A-ra korrigálta (l.
    CLAIM_LEDGER.yaml BAT-FG20721-I10-001, iec_dc_resistance_controller.py
    modul docstring 24-28. sor). A lenti tesztek emiatt kizárólag a
    képlet (Ri=(Ua-Ub)/(Ib-Ia), Isc) formula-ellenőrzésére használják a
    forrás elavult 0,62A számát, NEM a FG20721 akku jelenlegi, korrigált
    I10 értékét állítják."""

    def _drive_to_done(self, ctrl, dmm_v, ua_V: float, ub_V: float):
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)                      # -> PRECHARGE_RELAX
        ctrl.advance(1.0)                      # -> LOAD_1
        dmm_v.voltage_V = ua_V
        ctrl.advance(0.0)                      # LOAD_1 setup (input_on())
        ctrl.advance(2.0)                      # -> OPEN_CIRCUIT_REST (mintavétel Ua-nál)
        ctrl.advance(3.0)                      # -> LOAD_2
        dmm_v.voltage_V = ub_V
        ctrl.advance(0.0)                      # LOAD_2 setup (input_on())
        ctrl.advance(0.5)                      # -> COMPUTE_RESULT
        ctrl.advance(0.0)                      # COMPUTE_RESULT végrehajtása -> DONE

    def test_ri_isc_formula(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        ia, ib = 4 * 0.62, 20 * 0.62
        ua, ub = 12.60, 12.10
        self._drive_to_done(ctrl, dmm_v, ua, ub)

        expected_ri = (ua - ub) / (ib - ia)
        expected_isc = (ua * ib - ub * ia) / (ua - ub)

        result = ctrl.result
        assert result is not None
        assert result["Ri_ohm"] == pytest.approx(expected_ri)
        assert result["Isc_A"] == pytest.approx(expected_isc)
        assert result["quality"] == "OK"
        assert result["method_origin"] == "IEC_60896-21_6.3"
        assert result["i10_source_A"] == pytest.approx(0.62)

    def test_on_result_callback_fired_once(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        calls = []
        ctrl.on_result = lambda data: calls.append(data)
        self._drive_to_done(ctrl, dmm_v, 12.6, 12.1)
        assert len(calls) == 1
        assert calls[0]["Ri_ohm"] is not None

    def test_invalid_when_ua_not_greater_than_ub(self):
        """Fizikailag implauzibilis: Ib>Ia esetén Ub kell < Ua legyen. Ha
        nem (pl. mérési hiba), Ri/Isc None marad, quality jelzi."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        self._drive_to_done(ctrl, dmm_v, ua_V=12.0, ub_V=12.5)  # Ub > Ua
        result = ctrl.result
        assert result["Ri_ohm"] is None
        assert result["Isc_A"] is None
        assert result["quality"] == "INVALID_RESISTANCE"

    def test_invalid_when_ua_equals_ub(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        self._drive_to_done(ctrl, dmm_v, ua_V=12.0, ub_V=12.0)
        result = ctrl.result
        assert result["Ri_ohm"] is None
        assert result["quality"] == "INVALID_RESISTANCE"

    def test_temp_dmm_valid_true_after_successful_read(self):
        """[regresszió, MAX verifier (review-kör 3, a9cb161469c468fc8) P2-1]
        a temp_dmm_valid property (Metrológia M3-01, P1) egyik
        controlleren sem kapott önálló, controller-szintű, a LIVE
        property-t (nem a mérési pillanatra befagyasztott result[
        "temperature_valid"]-ot) közvetlenül fedő regressziós tesztet --
        ez a hiány engedte át a chemid_controller.py-beli P1-1 blokkoló
        hibát 1792 zöld teszt mellett."""
        ctrl, _psu, _load, _dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        ctrl.advance(0.0)
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_after_failed_read(self):
        ctrl, _psu, _load, _dmm_v, dmm_t = make_iec_controller(i10_A=0.62)
        dmm_t.simulate_temp_failure = True
        ctrl.advance(0.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_valid_true_delegated_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a3f0e6dcf93588360) P2-1]
        a self._dmm_valid (feszültség) delegálás a beágyazott
        ChargeControllerből mindig megtörtént PRECHARGE alatt, de a
        self._temp_dmm_valid nem -- egy ott ténylegesen hibázó
        hőmérséklet-DMM a szülő szintjén hamisan True-t jelentett volna,
        mert a saját _read_dmm() ebben az állapotban ki van kapcsolva."""
        ctrl, *_ = make_iec_controller(i10_A=0.62)
        ctrl.advance(0.0)  # INIT -> PRECHARGE, saját _read_dmm()
        assert ctrl.state == IecDcResistanceState.PRECHARGE

        ctrl.advance(1.0)  # PRECHARGE: beágyazott ChargeController.advance()
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_delegated_from_embedded_charge_controller(self):
        ctrl, _psu, _load, _dmm_v, dmm_t = make_iec_controller(i10_A=0.62)
        ctrl.advance(0.0)
        assert ctrl.state == IecDcResistanceState.PRECHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_fault_ticks_live_add_and_carry_over_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a306e7e764e3a717b) P2-1,
        DFAM-001 további előfordulása] l. test_chemid_controller.py azonos
        tesztjének docstringje ugyanerről a hiányzó live-add + carry-over
        mintáról."""
        ctrl, _psu, _load, _dmm_v, dmm_t = make_iec_controller(i10_A=0.62)
        ctrl.advance(0.0)
        assert ctrl.state == IecDcResistanceState.PRECHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_fault_ticks == 2

        dmm_t.simulate_temp_failure = False
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # PRECHARGE -> PRECHARGE_RELAX
        assert ctrl._charge_ctrl is None
        assert ctrl.temp_dmm_fault_ticks == 2

    def test_temperature_valid_true_when_temp_dmm_never_failed(self):
        """[regresszió, MAX verifier (review-kör 2, 45ea824 P2-1), DFAM-021]
        a temperature_valid mező a mérési pillanat self._temp_dmm_valid
        értékét fagyasztja be -- sikeres hőmérséklet-DMM-olvasások mellett
        True marad."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        self._drive_to_done(ctrl, dmm_v, 12.6, 12.1)
        assert ctrl.result["temperature_valid"] is True

    def test_temperature_valid_false_when_temp_dmm_fails(self):
        """[regresszió, MAX verifier (review-kör 2, f1c49b7 P3-1)] a
        korábbi teszt csak a mindig-sikeres ágat fedte -- a mező
        LÉTEZÉSÉNEK egész értelme (hogy egy sikertelen hőmérséklet-DMM-
        olvasás mellett a stale/default temperature_C=20.0 NE quality=OK-
        ként exportálódjon) korábban teszt nélkül maradt, holott a
        production kódúton (temperature_valid=True hardkódolására
        mutálva) a teljes non-GUI suite zöld maradt volna."""
        ctrl, _psu, _load, dmm_v, dmm_t = make_iec_controller(i10_A=0.62)
        dmm_t.simulate_temp_failure = True
        self._drive_to_done(ctrl, dmm_v, 12.6, 12.1)
        result = ctrl.result
        assert result["temperature_valid"] is False
        assert result["temperature_C"] == pytest.approx(20.0)  # konstruktor-default, stale

    def test_temperature_2_independent_from_temperature_1_validity(self):
        """[regresszió, Metrológia M4-06 (review-kör-4, DFAM-021+DFAM-001)]
        a temperature_C/temperature_valid KIZÁRÓLAG a LOAD_1 (Ua) mintavétel
        pillanatára vonatkozik -- korábban a LOAD_2 (Ub) mintavétel saját
        hőmérséklet-jelzés nélkül maradt, holott a két mintavétel között
        rest_duration_s + egy teljes terhelési ciklus telik el, tehát a
        hőmérséklet-DMM állapota köztük megváltozhat. Itt a LOAD_1 sikeres,
        a LOAD_2 sikertelen -- a két mezőpárnak EGYMÁSTÓL FÜGGETLENÜL kell
        ezt tükröznie."""
        ctrl, _psu, _load, dmm_v, dmm_t = make_iec_controller(i10_A=0.62)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)                      # -> PRECHARGE_RELAX
        ctrl.advance(1.0)                      # -> LOAD_1
        dmm_v.voltage_V = 12.6
        ctrl.advance(0.0)                      # LOAD_1 setup (input_on())
        ctrl.advance(2.0)                      # -> OPEN_CIRCUIT_REST (mintavétel Ua-nál, DMM OK)
        ctrl.advance(3.0)                      # -> LOAD_2
        dmm_t.simulate_temp_failure = True     # csak a LOAD_2 mintavétel pillanatára
        dmm_v.voltage_V = 12.1
        ctrl.advance(0.0)                      # LOAD_2 setup (input_on())
        ctrl.advance(0.5)                      # -> COMPUTE_RESULT (mintavétel Ub-nál, DMM HIBA)
        ctrl.advance(0.0)                      # COMPUTE_RESULT végrehajtása -> DONE

        result = ctrl.result
        assert result["temperature_valid"] is True
        assert result["temperature_2_valid"] is False
        assert result["temperature_2_C"] == pytest.approx(22.0)  # sticky, LOAD_1-es olvasásból ragadt


class TestSchemaCompletenessIecDcResistanceColumns:
    """[regresszió, MAX verifier (review-kör 2, 45ea824 P2-1), DFAM-021
    "computed_but_never_persisted" -- a testvér ocv_soc_controller.py
    test_on_soc_point_scalar_keys_all_persisted_to_ocv_soc_columns
    mintáját 1:1 átemelve] a controller `result` dict-jének MINDEN
    kulcsának szerepelnie kell a `logger.py::IEC_DC_RESISTANCE_COLUMNS`
    listában, különben a `Logger.log_iec_dc_resistance_result()` (ami
    szigorúan `{col: data.get(col, "") for col in IEC_DC_RESISTANCE_
    COLUMNS}` szűr) csendben eldobja -- a MAX verifier pontosan ezt
    találta a temperature_valid mezőre: kiszámolódott a controllerben,
    de sosem került az iec_dc_resistance_result.csv-be."""

    def test_result_keys_all_persisted_to_iec_dc_resistance_columns(self):
        from Prog.src.logger import IEC_DC_RESISTANCE_COLUMNS

        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        TestRiIscComputation()._drive_to_done(ctrl, dmm_v, 12.6, 12.1)
        result = ctrl.result
        assert result is not None
        missing = set(result.keys()) - set(IEC_DC_RESISTANCE_COLUMNS)
        assert not missing, (
            f"result dict kulcsai hiányoznak az IEC_DC_RESISTANCE_COLUMNS-ból, "
            f"csendben elveszve az iec_dc_resistance_result.csv-ből: {sorted(missing)}"
        )


class TestLoad1CutoffDeadline:
    def test_load_off_immediately_after_sample_well_within_cutoff(self):
        """A normál útvonalon a Load OFF a sample_delay_1_s-nél, jóval a
        cutoff_1_s előtt történik -- a cutoff csak egy redundáns backstop,
        aminek NEM szabadna kiváltania normál működés mellett."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)  # -> LOAD_1
        dmm_v.voltage_V = 12.0
        ctrl.advance(0.0)  # LOAD_1 setup (input_on())
        ctrl.advance(2.0)  # sample_delay_1_s=2.0 (load-on óta) pontosan itt éri el
        assert ctrl.state == IecDcResistanceState.OPEN_CIRCUIT_REST
        assert ctrl.fault_reason == ""


class TestEmergencyStopOffFailureReported:
    def test_load_and_psu_off_failures_both_reported(self):
        ctrl, psu, load, _dmm_v, _dmm_t = make_iec_controller()
        load.raise_on_safe_off = False

        def _raise_load(*a, **kw):
            raise RuntimeError("load off failed")

        def _raise_psu(*a, **kw):
            raise RuntimeError("psu off failed")

        load.input_off = _raise_load  # type: ignore[method-assign]
        psu.all_outputs_off = _raise_psu  # type: ignore[method-assign]

        ctrl.emergency_stop("TEST_REASON")

        assert ctrl.state == IecDcResistanceState.FAULT
        assert "TEST_REASON" in ctrl.fault_reason
        assert "LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "PSU_OFF_FAILED" in ctrl.fault_reason


class TestPhaseElapsedS:
    """[CF-19] ez a controller a phase_elapsed_s-t magát használja belső
    fázis-időzítőként -- a property helyesen tükrözi ezt."""

    def test_resets_on_transition(self):
        ctrl, *_ = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # -> PRECHARGE_RELAX, phase_elapsed_s resets to 0
        assert ctrl.state == IecDcResistanceState.PRECHARGE_RELAX
        assert ctrl.phase_elapsed_s == pytest.approx(0.0)

    def test_grows_within_state(self):
        ctrl, *_ = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # -> PRECHARGE_RELAX (relax_before_measurement_s=1.0)
        # Egy tickkel a resetnél tovább figyeljük -- még nem éri el az 1.0-t.
        cfg = ctrl._config
        assert cfg.relax_before_measurement_s == pytest.approx(1.0)


class TestTerminalStatesFreeze:
    def test_advance_after_done_is_noop(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        dmm_v.voltage_V = 12.0
        ctrl.advance(0.0)  # LOAD_1 setup (input_on())
        ctrl.advance(2.0)
        ctrl.advance(3.0)
        dmm_v.voltage_V = 11.5
        ctrl.advance(0.0)  # LOAD_2 setup (input_on())
        ctrl.advance(0.5)
        ctrl.advance(0.0)
        assert ctrl.state == IecDcResistanceState.DONE
        phase_before = ctrl.phase_elapsed_s
        state = ctrl.advance(100.0)
        assert state == IecDcResistanceState.DONE
        assert ctrl.phase_elapsed_s == phase_before


class TestF2InvalidLoadCurrentPairGuard:
    """MAX verifier lelet (governance-v2 kör 2, F2): a COMPUTE_RESULT
    korábban csak Ua<=Ub-t őrizte -- Ib<=Ia esetén VAGY ZeroDivisionError
    szökött ki (Ib==Ia, kiszökött volna az advance()-ból, UNHANDLED_
    EXCEPTION-né vált volna a runnerben), VAGY egy fizikailag lehetetlen
    NEGATÍV Ri/Isc került "OK" quality-vel a result dict-be (Ib<Ia)."""

    def _drive_with_forced_ib(self, ib_forced_A: float | None):
        ctrl, _psu, load, dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)                      # -> LOAD_1
        dmm_v.voltage_V = 12.6
        ctrl.advance(0.0)                      # LOAD_1 setup (input_on())
        ctrl.advance(2.0)                      # -> OPEN_CIRCUIT_REST, Ia mérve (=4*0.62)
        ctrl.advance(3.0)                      # -> LOAD_2
        dmm_v.voltage_V = 12.1
        ctrl.advance(0.0)                      # LOAD_2 setup (set_current(ib_A) -> current_A=20*0.62)
        if ib_forced_A is not None:
            load.current_A = ib_forced_A
        ctrl.advance(0.5)                      # -> COMPUTE_RESULT, Ib mérve = (forced vagy eredeti)
        ctrl.advance(0.0)                      # COMPUTE_RESULT végrehajtása -> DONE
        return ctrl

    def test_ib_equals_ia_no_crash_invalid_quality(self):
        """A pre-fix kódon ez a hívássorozat ZeroDivisionError-t dobott
        volna (kiszökve az utolsó advance(0.0) hívásból)."""
        ctrl = self._drive_with_forced_ib(ib_forced_A=4 * 0.62)
        assert ctrl.state == IecDcResistanceState.DONE
        result = ctrl.result
        assert result is not None
        assert result["Ri_ohm"] is None
        assert result["Isc_A"] is None
        assert result["quality"] == "INVALID_LOAD_CURRENT_PAIR"

    def test_ib_less_than_ia_no_negative_resistance(self):
        """A pre-fix kódon ez a hívássorozat egy fizikailag lehetetlen
        NEGATÍV Ri/Isc-t adott "OK" quality-vel."""
        ctrl = self._drive_with_forced_ib(ib_forced_A=4 * 0.62 - 0.1)
        result = ctrl.result
        assert result["Ri_ohm"] is None
        assert result["Isc_A"] is None
        assert result["quality"] == "INVALID_LOAD_CURRENT_PAIR"

    def test_normal_ib_greater_than_ia_still_computes(self):
        """Regressziós ellenőrzés: a normál (Ib>Ia) eset NEM sérült a
        guard bővítésével."""
        ctrl = self._drive_with_forced_ib(ib_forced_A=None)
        result = ctrl.result
        assert result["Ri_ohm"] is not None
        assert result["Isc_A"] is not None
        assert result["quality"] == "OK"


class _B22Rig:
    @staticmethod
    def make(monkeypatch, phase, latency=()):
        from types import SimpleNamespace

        import Prog.src.iec_dc_resistance_controller as module
        import Prog.src.test_runner as runner_module

        clock = TickClock(start=100.0)
        ctrl, psu, load, voltage, temperature = make_iec_controller(
            config=IecDcResistanceConfig(), clock=clock,
        )
        # A blokk sajat oraja lep; az advance nem leptet kulso ticket.
        monkeypatch.setattr(ctrl, "advance", type(ctrl).advance.__get__(ctrl))
        waits = []
        def bounded_sleep(delay):
            waits.append(delay)
            assert len(waits) <= 5000, "IEC blokk nem zárult 5000 várakozás alatt"
            clock.burn(delay)
        monkeypatch.setattr(module, "time", SimpleNamespace(sleep=bounded_sleep))
        monkeypatch.setattr(runner_module, "time", SimpleNamespace(
            monotonic=clock, perf_counter=clock, sleep=clock.burn,
        ))
        ctrl._state = getattr(IecDcResistanceState, phase)
        trace, hooks, stopped = [], {}, [False]
        ctrl.handoff_stop_requested = lambda: stopped[0]
        latencies = dict(latency)
        count = [0]
        limit_stop = [lambda: None]
        for device, names in (
            (voltage, ("acquire_measurement", "read_voltage", "release_measurement")),
            (temperature, ("read_temperature",)),
            (load, ("set_mode_cc", "set_current", "input_on", "read_input_state", "measure_current", "input_off")),
            (psu, ("all_outputs_off",)),
        ):
            for name in names:
                original = getattr(device, name)
                def operation(*args, _name=name, _original=original, **kwargs):
                    count[0] += 1
                    if count[0] > 20000:
                        stopped[0] = True
                        limit_stop[0]()
                        raise AssertionError("IEC blokk nem zarult 20000 I/O alatt")
                    trace.append((_name, clock(), load.input_commanded_on))
                    value = _original(*args, **kwargs)
                    clock.burn(latencies.get(_name, 0.0))
                    if _name in hooks:
                        hooks[_name](value)
                    return value
                monkeypatch.setattr(device, name, operation)
        return SimpleNamespace(
            ctrl=ctrl, psu=psu, load=load, voltage=voltage, temperature=temperature,
            clock=clock, trace=trace, hooks=hooks, stopped=stopped, limit_stop=limit_stop,
        )

    @staticmethod
    def run(rig, tick=0.0, poll=None):
        ctrl = rig.ctrl
        if hasattr(ctrl, "prepare_pulse_block"):
            ctrl.prepare_pulse_block()
            ctrl.advance(tick)
            return ctrl.run_pulse_block(poll or (lambda force, before_io: None))
        # A HEAD ellenproba ugyanazt a meresi fazist hajtja, korlatosan.
        phase = ctrl.state
        ctrl.advance(0.0)
        for _ in range(2000):
            if ctrl.state != phase:
                break
            rig.clock.burn(tick or 0.02)
            ctrl.advance(tick or 0.02)
        else:
            rig.stopped[0] = True
            raise AssertionError("A HEAD fazis nem zarult le 2000 tick alatt")

    @staticmethod
    def off_order(rig, start=0):
        names = [name for name, _, _ in rig.trace[start:]]
        assert "input_off" in names
        if start:
            assert names[:2] == ["input_off", "all_outputs_off"]
        tail = names[names.index("input_off"):]
        assert tail[:2] == ["input_off", "all_outputs_off"]
        assert tail[-1] == "release_measurement"
        assert not any(name in tail[2:] for name in (
            "read_voltage", "read_temperature", "measure_current", "input_on",
        ))
        assert rig.voltage._measurement_owner is None


class TestR16B22:
    LATENCIES = (
        (),
        (("read_temperature", 0.45),),
        (("set_mode_cc", 1.1), ("set_current", 0.7)),
        (("read_voltage", 0.21), ("measure_current", 0.11), ("input_off", 0.3)),
        (("input_on", 0.2),),
    )

    @pytest.mark.parametrize("phase,target", [("LOAD_1", 20.0), ("LOAD_2", 5.0)])
    @pytest.mark.parametrize("tick", [0.0, 2.0, 5.0, 30.0])
    @pytest.mark.parametrize("latency", LATENCIES, ids=["ideal", "temperature", "config", "read_off", "on"])
    def test_own_clock_sample_and_off(self, monkeypatch, phase, target, tick, latency):
        rig = _B22Rig.make(monkeypatch, phase, latency)
        _B22Rig.run(rig, tick)
        ctrl = rig.ctrl
        assert ctrl.state.name == ("OPEN_CIRCUIT_REST" if phase == "LOAD_1" else "COMPUTE_RESULT")
        assert not ctrl.fault_reason
        sample = ctrl._t_sample_1_s if phase == "LOAD_1" else ctrl._t_sample_2_s
        if not latency:
            assert sample == pytest.approx(target, abs=1e-7)
            assert not ctrl._timing_degraded
        else:
            assert sample is not None
            assert target <= sample + 1e-8 <= target + 0.45
        window = ctrl._iec_windows[-1]
        largest_io = max([0.0, *dict(latency).values()])
        assert target - 1e-7 <= window["off_command_s"] - window["on_command_s"] <= target + largest_io + 1e-7
        assert window["on_return_s"] >= window["on_command_s"]
        assert window["off_return_s"] >= window["off_command_s"]
        assert sum(name == "input_on" for name, _, _ in rig.trace) == 1
        _B22Rig.off_order(rig)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("stop_kind", ["stop", "emergency", "fault"])
    def test_stop_at_post_load_handoff_prevents_next_temperature_io(self, monkeypatch, phase, stop_kind):
        rig = _B22Rig.make(monkeypatch, phase)
        target = 20.0 if phase == "LOAD_1" else 5.0
        triggered = []
        def last_voltage(_value):
            if rig.load.input_commanded_on and rig.clock() >= 100.0 + target - 1e-8:
                triggered.append(len(rig.trace))
                if stop_kind == "stop":
                    rig.stopped[0] = True
                else:
                    rig.ctrl.emergency_stop("POST_LOAD_" + stop_kind.upper())
        rig.hooks["read_voltage"] = last_voltage
        _B22Rig.run(rig, 30.0)
        assert len(triggered) == 1
        _B22Rig.off_order(rig, triggered[0])
        before = list(rig.trace)
        rig.ctrl.advance(30.0)
        rig.ctrl.prepare_pulse_block()
        rig.ctrl.run_pulse_block(lambda force, before_io: pytest.fail("Uj poll stop utan"))
        assert rig.trace == before
        windows = rig.ctrl._iec_windows
        assert len(windows) == 1 and windows[0]["off_command_s"] is not None

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_expired_on_ack_is_not_recovered_by_a_new_late_poll(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase, (("input_on", 1.1),))
        polls = []
        def late_poll(force, before_io):
            polls.append(force)
            rig.load.read_input_state()
        _B22Rig.run(rig, 30.0, late_poll)
        assert "ON_ACK_EXPIRED" in rig.ctrl.fault_reason
        assert not polls
        assert not any(name == "read_input_state" for name, _, _ in rig.trace)
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        _B22Rig.off_order(rig)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("boundary", [
        "acquire_measurement", "read_voltage", "read_temperature", "set_mode_cc",
        "set_current", "input_on", "read_input_state", "measure_current", "release_measurement",
    ])
    def test_stop_at_each_io_boundary(self, monkeypatch, phase, boundary):
        rig = _B22Rig.make(monkeypatch, phase)
        triggered = []
        def stop(_value):
            if not triggered:
                triggered.append(len(rig.trace))
                rig.stopped[0] = True
        rig.hooks[boundary] = stop
        _B22Rig.run(rig, 30.0)
        assert len(triggered) == 1
        # A release a ket OFF utan tortenik; minden mas hatar utan OFF az elso I/O.
        if boundary != "release_measurement":
            _B22Rig.off_order(rig, triggered[0])
        before = list(rig.trace)
        rig.ctrl.advance(30.0)
        assert rig.trace == before

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("off_fails", ["load", "psu", "base"])
    def test_final_raw_floor_survives_off_exception(self, monkeypatch, phase, off_fails):
        rig = _B22Rig.make(monkeypatch, phase)
        events, triggered = [], []
        rig.ctrl.on_event = events.append
        original_voltage = rig.voltage.read_voltage
        target = 20.0 if phase == "LOAD_1" else 5.0
        def voltage(**kwargs):
            if rig.load.input_commanded_on and rig.clock() >= 100.0 + target - 1e-8:
                rig.voltage.voltage_V = 10.0
                triggered.append(True)
            return original_voltage(**kwargs)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        def fail(_value):
            if rig.ctrl._iec_window.get("on_command_s") is not None:
                if off_fails == "base":
                    raise KeyboardInterrupt("OFF interrupt")
                raise RuntimeError("OFF error")
        rig.hooks["all_outputs_off" if off_fails == "psu" else "input_off"] = fail
        if off_fails == "base":
            with pytest.raises(KeyboardInterrupt, match="OFF interrupt"):
                _B22Rig.run(rig)
        else:
            _B22Rig.run(rig)
        assert triggered
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert "BATTERY_UNDERVOLTAGE" in rig.ctrl.fault_reason
        assert any(event["event_code"] == "BATTERY_UNDERVOLTAGE" for event in events)
        _B22Rig.off_order(rig)
        assert rig.ctrl._ua_V is None and rig.ctrl._ub_V is None

    def test_backstop_uses_on_anchor_without_next_advance(self, monkeypatch):
        rig = _B22Rig.make(monkeypatch, "LOAD_1", (("set_current", 3.0),))
        # Csak a normal celpontot tesszuk elerhetetlenne; a 25 s kapu valtozatlan.
        rig.ctrl._config.sample_delay_1_s = 40.0
        rig.ctrl._phase_elapsed_s = 10000.0
        _B22Rig.run(rig, 30.0)
        window = rig.ctrl._iec_windows[0]
        assert window["off_command_s"] - window["on_command_s"] == pytest.approx(25.0, abs=1e-7)
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert rig.ctrl.fault_reason == "IEC_LOAD_1_CUTOFF_DEADLINE_EXCEEDED"
        assert rig.ctrl._ua_V is None and rig.ctrl._t_sample_1_s is None
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        _B22Rig.off_order(rig)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_measured_energy_and_tail_are_booked_once(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase, (("input_off", 0.3),))
        samples = []
        measured = [0.0]
        def current(_value):
            measured[0] = 0.8 + int((rig.clock() - 100.0) / 2.0) * 0.1
            rig.load.current_A = measured[0]
        rig.hooks["measure_current"] = current
        original = rig.load.measure_current
        def read_current():
            original()
            return measured[0]
        monkeypatch.setattr(rig.load, "measure_current", read_current)
        rig.hooks["read_voltage"] = lambda value: (
            samples.append((rig.clock(), value, measured[0])) if rig.load.input_commanded_on else None
        )
        _B22Rig.run(rig)
        window = rig.ctrl._iec_windows[0]
        previous = window["on_command_s"]
        expected_ah, expected_wh = 0.0, 0.0
        for stamp, voltage, current_A in samples:
            expected_ah += current_A * (stamp - previous) / 3600.0
            expected_wh += voltage * current_A * (stamp - previous) / 3600.0
            previous = stamp
        tail = window["off_return_s"] - previous
        expected_ah += measured[0] * tail / 3600.0
        expected_wh += samples[-1][1] * measured[0] * tail / 3600.0
        assert expected_ah > 0
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected_ah)
        assert rig.ctrl.accumulated_discharge_Wh == pytest.approx(expected_wh)
        rig.ctrl.run_pulse_block(lambda force, before_io: pytest.fail("Ismetelt poll"))
        rig.ctrl.advance(0.0)
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected_ah)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("tick", [0.0, 2.0, 5.0, 30.0])
    @pytest.mark.parametrize("latency", LATENCIES, ids=["ideal", "temperature", "config", "read_off", "on"])
    def test_runner_handoff_stop_and_prefix_once(self, monkeypatch, phase, tick, latency):
        from Prog.src.test_runner import StepKind, TestPlan, TestType
        from Prog.tests.test_test_runner import TestR16B12

        rig = _B22Rig.make(monkeypatch, phase, latency)
        runner, _logger, _events, _off, _order, step = TestR16B12.runner(
            rig.ctrl, StepKind.IEC_DC_RESISTANCE, monkeypatch, tick,
        )
        rig.limit_stop[0] = runner.request_stop
        samples, triggered = [], []
        def sample(_data):
            samples.append(True)
            if len(samples) >= 8:
                runner.request_stop()
            if rig.ctrl.state.name in ("OPEN_CIRCUIT_REST", "COMPUTE_RESULT"):
                triggered.append(True)
                runner.request_stop()
        runner.on_sample = sample
        result = runner.run(TestPlan(TestType.IEC_DC_INTERNAL_RESISTANCE, (step,)))
        assert triggered == [True], "A blokk utani triggernek el kell sulnie"
        assert result.status == "STOPPED" and result.reason == "USER_STOP_REQUESTED"
        assert result.total_discharge_ah == pytest.approx(rig.ctrl.accumulated_discharge_Ah)
        assert result.total_discharge_ah > 0

class TestF3IntegratorFedDuringLoadAndRest:
    """MAX verifier lelet (governance-v2 kör 2, F3): a LOAD_1/LOAD_2
    add_sample() hívásai korábban hardkódolt dt_s=0.0-t kaptak, az
    OPEN_CIRCUIT_REST pedig egyáltalán nem hívta az integrátort --
    accumulated_discharge_Ah/extreme_dt_s_count emiatt vak volt PONT az
    aktív energiaátadás és az 5 perces pihenő alatt."""

    def test_discharge_ah_accumulates_during_load_1_and_load_2(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)                      # -> LOAD_1
        dmm_v.voltage_V = 12.6
        ctrl.advance(0.0)                      # LOAD_1 setup
        assert ctrl.accumulated_discharge_Ah == pytest.approx(0.0)
        ctrl.advance(2.0)                      # -> OPEN_CIRCUIT_REST, 2.0s @ Ia
        after_load_1 = ctrl.accumulated_discharge_Ah
        assert after_load_1 > 0.0
        ctrl.advance(3.0)                      # -> LOAD_2 (rest alatt 0A, de dt_s továbbra is táplált)
        dmm_v.voltage_V = 12.1
        ctrl.advance(0.0)                      # LOAD_2 setup
        ctrl.advance(0.5)                      # -> COMPUTE_RESULT, 0.5s @ Ib
        assert ctrl.accumulated_discharge_Ah > after_load_1

    def test_extreme_dt_s_detected_during_open_circuit_rest(self):
        """A pre-fix kódon az OPEN_CIRCUIT_REST alatt EGYÁLTALÁN nem
        hívta az integrátort -- egy extrém dt_s tüske itt észrevétlen
        maradt volna (a session-szintű extreme_dt_s_count minőségi kapu
        vak volt az 5 perces pihenő alatt)."""
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller(i10_A=0.62)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)                      # -> LOAD_1
        dmm_v.voltage_V = 12.6
        ctrl.advance(0.0)                      # LOAD_1 setup
        ctrl.advance(2.0)                      # -> OPEN_CIRCUIT_REST
        assert ctrl.state == IecDcResistanceState.OPEN_CIRCUIT_REST
        assert ctrl.extreme_dt_s_count == 0
        ctrl.advance(400.0)                    # extrém dt_s tüske a pihenő KÖZBEN (küszöb: 300.0s)
        assert ctrl.extreme_dt_s_count == 1


class TestF5RelaxDmmFaultEventsAndCounter:
    """MAX verifier lelet (governance-v2 kör 2, F5): on_event korábban
    SOHA nem lett meghívva a controlleren belülről -- egy tartós DMM-
    kiesés a 2 órás PRECHARGE_RELAX vagy az 5 perces OPEN_CIRCUIT_REST
    alatt élőben láthatatlan maradt, és nem volt session-szintű összegző
    számláló sem (l. ocv_soc_controller.py::relax_dmm_fault_total
    azonos mintája)."""

    def test_precharge_relax_dmm_fault_fires_event_and_increments_counter(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller()
        events = []
        ctrl.on_event = lambda ev: events.append(ev)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(0.5)                      # -> PRECHARGE_RELAX
        assert ctrl.state == IecDcResistanceState.PRECHARGE_RELAX
        assert ctrl.relax_dmm_fault_total == 0
        dmm_v.dmm_valid = False
        ctrl.advance(0.1)                      # DMM hiba a relax alatt
        assert ctrl.relax_dmm_fault_total == 1
        assert any(ev["event_code"] == "IEC_RELAX_DMM_FAULT" for ev in events)

    def test_open_circuit_rest_dmm_fault_fires_event_and_increments_counter(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller()
        events = []
        ctrl.on_event = lambda ev: events.append(ev)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)                      # -> LOAD_1
        dmm_v.voltage_V = 12.6
        ctrl.advance(0.0)                      # LOAD_1 setup
        ctrl.advance(2.0)                      # -> OPEN_CIRCUIT_REST
        assert ctrl.state == IecDcResistanceState.OPEN_CIRCUIT_REST
        assert ctrl.relax_dmm_fault_total == 0
        dmm_v.dmm_valid = False
        ctrl.advance(0.5)
        assert ctrl.relax_dmm_fault_total == 1
        assert any(ev["event_code"] == "IEC_RELAX_DMM_FAULT" for ev in events)


class TestD5RelaxDmmFaultEventTransitionGuard:
    """Kör 19 Batch D5 (F19-S2, DFAM-001 4. controller-előfordulás):
    korábban a PRECHARGE_RELAX/OPEN_CIRCUIT_REST alatt MINDEN ticken
    tüzelt az IEC_RELAX_DMM_FAULT esemény, amíg a DMM-kiesés tartott --
    alapértelmezett relax_before_measurement_s=7200s mellett akár ~3600
    azonos esemény ürítette volna ki az EventLogWidget 500-soros gyűrűjét
    egyetlen tartós dropout alatt. Ez a teszt a régi (nem-kapuzott) kódon
    bukna: 5 egymást követő hibás tick 5 eseményt adna 1 helyett."""

    def test_precharge_relax_sustained_dmm_fault_fires_exactly_one_event(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller()
        events = []
        ctrl.on_event = lambda ev: events.append(ev)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(0.1)                      # -> PRECHARGE_RELAX
        assert ctrl.state == IecDcResistanceState.PRECHARGE_RELAX
        dmm_v.dmm_valid = False
        for _ in range(5):                     # 5 egymást követő hibás tick
            ctrl.advance(0.05)
        assert ctrl.relax_dmm_fault_total == 5            # számláló MINDEN ticken nő
        fault_events = [ev for ev in events if ev["event_code"] == "IEC_RELAX_DMM_FAULT"]
        assert len(fault_events) == 1                     # de csak az ÁTMENETKOR tüzel

    def test_precharge_relax_dmm_fault_recovery_then_refault_fires_second_event(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller()
        events = []
        ctrl.on_event = lambda ev: events.append(ev)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(0.1)                      # -> PRECHARGE_RELAX
        dmm_v.dmm_valid = False
        ctrl.advance(0.05)
        ctrl.advance(0.05)
        fault_events = [ev for ev in events if ev["event_code"] == "IEC_RELAX_DMM_FAULT"]
        assert len(fault_events) == 1
        dmm_v.dmm_valid = True
        ctrl.advance(0.05)                     # helyreállás -- nincs új esemény
        dmm_v.dmm_valid = False
        ctrl.advance(0.05)                     # újrahibázás -- új átmenet
        fault_events = [ev for ev in events if ev["event_code"] == "IEC_RELAX_DMM_FAULT"]
        assert len(fault_events) == 2
        assert ctrl.relax_dmm_fault_total == 3

    def test_open_circuit_rest_sustained_dmm_fault_fires_exactly_one_event(self):
        ctrl, _psu, _load, dmm_v, _dmm_t = make_iec_controller()
        events = []
        ctrl.on_event = lambda ev: events.append(ev)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)                      # -> LOAD_1
        dmm_v.voltage_V = 12.6
        ctrl.advance(0.0)                      # LOAD_1 setup
        ctrl.advance(2.0)                      # -> OPEN_CIRCUIT_REST
        assert ctrl.state == IecDcResistanceState.OPEN_CIRCUIT_REST
        dmm_v.dmm_valid = False
        for _ in range(5):
            ctrl.advance(0.1)
        assert ctrl.relax_dmm_fault_total == 5
        fault_events = [ev for ev in events if ev["event_code"] == "IEC_RELAX_DMM_FAULT"]
        assert len(fault_events) == 1


class TestF6LoadFaultPathsLeaveSafeState:
    """MAX verifier lelet (governance-v2 kör 2, F6): a DMM_FEEDBACK_LOST/
    LOAD_COMM_LOST útvonalaknak eddig nem volt pozitív tesztje (csak az
    emergency_stop() önmagában, kivétel-dobó OFF-hívásokkal). L.
    .claude/rules/10-safety.md: 'Safety findinghoz kötelező tesztelni:
    fault code, Load OFF, PSU OFF, nincs további aktív parancs, log/event.'"""

    def test_dmm_feedback_lost_during_load_1_leaves_safe_state(self):
        ctrl, psu, load, dmm_v, _dmm_t = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)                      # -> LOAD_1
        ctrl.advance(0.0)                      # LOAD_1 setup (input_on())
        dmm_v.dmm_valid = False
        ctrl.advance(1.0)
        assert ctrl.state == IecDcResistanceState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False

    def test_load_comm_lost_during_load_2_leaves_safe_state(self):
        ctrl, psu, load, dmm_v, _dmm_t = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)                      # -> LOAD_1
        dmm_v.voltage_V = 12.6
        ctrl.advance(0.0)
        ctrl.advance(2.0)                      # -> OPEN_CIRCUIT_REST
        ctrl.advance(3.0)                      # -> LOAD_2
        ctrl.advance(0.0)                      # LOAD_2 setup (input_on())
        load.simulate_current_readback_failure = True
        ctrl.advance(0.5)
        assert ctrl.state == IecDcResistanceState.FAULT
        assert "LOAD_COMM_LOST" in ctrl.fault_reason
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False


class TestV1PreActuationSafetyGates:
    """MAX verifier 2. kör lelete (V1, blokkoló): a LOAD_1/LOAD_2 setup-
    tick szétválasztása (F1 fix, előző kör) elfelejtette átvenni a
    testvér DischargeController setup-blokkjának pre-actuation safety-
    kapuit -- egy már ismerten érvénytelen DMM vagy egy implauzibilisan
    nagy SETPOINT-áram EGY TELJES TICKIG (produkciós configon 2,0 s-ig,
    a load névleges teljesítményének akár többszörösén) a terhelésre
    kapcsolva maradt volna, mielőtt a régi (csak input_on() UTÁNI, MÉRT
    áramra épülő) ellenőrzés elkapta volna. Most a DMM-érvényesség,
    feszültség, SETPOINT-áram alapú teljesítmény és egy előrejelző
    concurrent-ellenőrzés MIND az input_on() ELŐTT fut."""

    def test_load_1_setup_tick_dmm_invalid_faults_before_input_on(self):
        ctrl, psu, load, dmm_v, _dmm_t = make_iec_controller()
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)                      # -> LOAD_1 (setup MÉG nem hívódott)
        dmm_v.dmm_valid = False
        ctrl.advance(1.0)                      # LOAD_1 setup tick, DMM MÁR érvénytelen
        assert ctrl.state == IecDcResistanceState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert "input_on()" not in load.calls
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False

    def test_load_2_setup_tick_excessive_setpoint_power_faults_before_input_on(self):
        """MAX verifier saját reprodukciója: i10=2,0A egy 7,2 Ah profilon
        (=0,278×C -- a battery_profile.py 0,5×C plauzibilitási korlátján
        BELÜL van, tehát nem a profil-validáció kapja el) -> Ib=20×I10=
        40A -> ~504W a load 250W-os hardverkorlátján -- a SETPOINT-alapú
        ellenőrzésnek az input_on() ELŐTT kell elkapnia, mért áram
        nélkül is. Bőséges cutoff_1_s-t használ, hogy a LOAD_1 fázis a
        setup-tick extra idejével együtt is simán, cutoff nélkül
        lezáruljon -- ennek a tesztnek NEM a cutoff-ot kell vizsgálnia."""
        cfg = IecDcResistanceConfig(
            sample_delay_1_s=2.0, cutoff_1_s=10.0,
            sample_delay_2_s=0.5, rest_duration_s=1.0,
            relax_before_measurement_s=1.0,
        )
        ctrl, psu, load, dmm_v, _dmm_t = make_iec_controller(i10_A=2.0, config=cfg)
        ctrl.advance(0.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(1.0)                      # -> LOAD_1
        dmm_v.voltage_V = 12.6
        ctrl.advance(1.0)                      # LOAD_1 setup (Ia=8.0A@12.6V=100.8W, OK)
        assert ctrl.state == IecDcResistanceState.LOAD_1
        ctrl.advance(2.0)                      # -> OPEN_CIRCUIT_REST (mintavétel Ua-nál)
        ctrl.advance(1.0)                      # -> LOAD_2
        # A LOAD_1 setup-ja MÁR hívott egy sikeres input_on()-t -- csak azt
        # ellenőrizzük, hogy a LOAD_2 setup NEM ad hozzá egy MÁSODIKAT.
        input_on_calls_before = load.calls.count("input_on()")
        ctrl.advance(1.0)                      # LOAD_2 setup tick -- Ib=40A@12.6V=504W > 250W
        assert ctrl.state == IecDcResistanceState.FAULT
        assert "LOAD_POWER_LIMIT_EXCEEDED" in ctrl.fault_reason
        assert load.calls.count("input_on()") == input_on_calls_before
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False


class TestReviewRound5S505UnhandledStateRaises:
    """[regresszió, review-kör 5, Session/Workflow S5-05 (P3, DFAM-004
    sibling) -- l. test_charge_controller.py azonos hivatkozású tesztje
    ugyanerről a hibaosztályról] advance() korábban hallgatólagosan
    (else nélkül) no-op-olt egy le nem kezelt IecDcResistanceState-re."""

    def test_advance_raises_on_unhandled_state(self):
        ctrl, *_ = make_iec_controller()
        ctrl._state = "BOGUS_FUTURE_STATE"
        with pytest.raises(ValueError, match="BOGUS_FUTURE_STATE"):
            ctrl.advance(1.0)


class TestChargeEnergyDeltaFromEmbeddedFastLoop:
    """[regresszió, R1 B8a, F-20/DFAM-035 -- a korábbi C-06
    TestChargeEnergyIntegratorIncludesFastLoopExtraTime utódja] A session-
    szintű Ah/Wh a beágyazott, VALÓDI ChargeController fast-loop
    résztickjeinek (hívásonként változó áram ÉS feszültség) elintegrált
    deltáját veszi át (l. Prog/tests/mock_drivers/fast_loop_charge.py). A
    régi C-06 teszt a _last_fast_loop_extra_s-t belső integrálás nélkül
    injektálta, és az elvárást ugyanabból a hibás modellből (utolsó áram ×
    (dt + extra)) számolta (tautologikus fixture, DFAM-006) -- a javítás
    előtti kódon ezek a tesztek buknak. Megőrzött assertek: a publikus
    last_fast_loop_extra_s proxy (MAX verifier V-5) és a dt-diagnosztika
    (max_observed_dt_s == dt + extra), nulla delta mellett is."""

    def _reach_child(self, monkeypatch, **charge_cfg_kwargs):
        install_fast_loop_clock(monkeypatch)
        cfg = fast_loop_charge_config(**charge_cfg_kwargs)
        ctrl, psu, _load, dmm_v, _dmm_t = make_iec_controller(charge_config=cfg)
        ctrl.advance(0.0)  # INIT -> PRECHARGE (beágyazott ChargeController létrejön)
        assert ctrl.state == IecDcResistanceState.PRECHARGE
        assert ctrl._charge_ctrl is not None
        return ctrl, psu, dmm_v

    def test_multi_tick_variable_current_and_voltage(self, monkeypatch):
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V)
        )
        for _ in range(3):
            rec = run_tick(ctrl, child, readings)
            assert child.state == ChargeState.CHARGE_CV_DMM_CONTROL
            assert_parent_delta_equals_independent_expectation(
                rec, SUBSTEPS_PER_TICK, DT_S + EXTRA_S
            )
            assert ctrl.last_fast_loop_extra_s == child.last_fast_loop_extra_s == EXTRA_S
        assert ctrl.max_observed_dt_s == DT_S + EXTRA_S
        assert ctrl.extreme_dt_s_count == 0

    def test_partial_success_then_fault_tick_keeps_integrated_substeps(self, monkeypatch):
        """A 4. résztick PSU-áramolvasása hibázik (PSU_COMM_LOST): az első
        3 résztick már elintegrált töltése a session-szintű Ah/Wh-ba is
        bekerül (a régi kód a FAULT-tick 0,0 áramával mindet elveszítette)."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V), fail_current_at=3
        )
        rec = run_tick(ctrl, child, readings)
        assert child.state == ChargeState.FAULT
        assert child.fault_reason == "PSU_COMM_LOST"
        assert ctrl.state == IecDcResistanceState.FAULT
        assert_parent_delta_equals_independent_expectation(
            rec, 3, DT_S + 3 * PERIOD_S, check_last_sample_model=False
        )
        assert ctrl.max_observed_dt_s == DT_S + 3 * PERIOD_S

    def test_charge_done_tick_delta_transferred_before_child_dropped(self, monkeypatch):
        """TAPER_HOLD-ban a 4. résztick éri el a taper_hold_s-t (2,6 s):
        CHARGE_DONE, a szülő eldobja a gyermeket -- a tick teljes deltája
        előtte átkerül."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch, taper_hold_s=2.6)
        child = ctrl._charge_ctrl
        child._taper_timer_s = 0.0
        target_V = force_child_state(child, psu, ChargeState.TAPER_HOLD)
        readings = ScriptedChargeReadings(
            psu, dmm_v, taper_currents(), varying_voltages(target_V)
        )
        rec = run_tick(ctrl, child, readings)
        assert child.state == ChargeState.CHARGE_DONE
        assert ctrl._charge_ctrl is None
        assert ctrl.state == IecDcResistanceState.PRECHARGE_RELAX
        assert_parent_delta_equals_independent_expectation(rec, 4, DT_S + 3 * PERIOD_S)
        assert ctrl.max_observed_dt_s == DT_S + 3 * PERIOD_S

    def test_zero_delta_tick_still_records_dt_diagnostics(self, monkeypatch):
        """Nem integráló gyermek-tick (INIT -> PRECHECK): nulla delta, a
        dt-diagnosztika (max_observed_dt_s) ekkor is vezetődik."""
        ctrl, _psu, _dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        assert child.state == ChargeState.INIT
        ctrl.advance(7.0)
        assert child.accumulated_charge_Ah == 0.0
        assert ctrl.accumulated_charge_Ah == 0.0
        assert ctrl.accumulated_charge_Wh == 0.0
        assert ctrl.max_observed_dt_s == 7.0

    # -------------------------------------------------------------- #
    # R2-B5 (R2-session-02, DFAM-033): kivételes kilépés a gyermek
    # fast-loopjából (Ctrl+C a résztickek közötti sleep()-ben).
    @staticmethod
    def _install_interrupting_fast_loop_clock(monkeypatch, k_sleep):
        """A FastLoopClock-kal azonos kézi óra (sleep() léptet,
        perf_counter() olvas), de a k_sleep-edik fast-loop sleep()
        KeyboardInterrupt-ot dob. Csak a charge_controller modul `time`
        nevét cseréli (l. install_fast_loop_clock())."""
        from types import SimpleNamespace

        import Prog.src.charge_controller as cc_module

        clock = {"now": 0.0, "sleeps": 0}

        def perf_counter():
            return clock["now"]

        def sleep(seconds):
            clock["sleeps"] += 1
            if clock["sleeps"] == k_sleep:
                raise KeyboardInterrupt("R2-B5 scripted Ctrl+C in fast-loop sleep")
            clock["now"] += seconds

        monkeypatch.setattr(
            cc_module, "time",
            SimpleNamespace(perf_counter=perf_counter, sleep=sleep),
        )

    @pytest.mark.parametrize("k_sleep", [1, 4])
    def test_keyboard_interrupt_in_fast_loop_sleep_transfers_child_delta_once(
        self, monkeypatch, k_sleep
    ):
        """[regresszió, R2-B5 (R2-session-02), DFAM-033] Ctrl+C a gyermek
        k-adik fast-loop sleep()-jében: a gyermek által addig MÁR
        elintegrált k résztick Ah/Wh-deltája és a dt-diagnosztika
        (dt_s + (k-1)·period_s) pontosan egyszer átkerül a session-szintű
        integrátorba, az eredeti kivétel változatlanul továbbmegy. A javítás
        előtt az átadás csak az advance() normál visszatérése után állt --
        a szülő a már elintegrált töltést elveszítette."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V)
        )
        self._install_interrupting_fast_loop_clock(monkeypatch, k_sleep)
        p_ah, p_wh = ctrl.accumulated_charge_Ah, ctrl.accumulated_charge_Wh
        c_ah, c_wh = child.accumulated_charge_Ah, child.accumulated_charge_Wh
        with pytest.raises(KeyboardInterrupt):
            ctrl.advance(DT_S)
        assert len(readings.pairs) == k_sleep
        exp_ah, exp_wh = expected_delta(readings.pairs, DT_S, PERIOD_S)
        assert exp_ah > 0.0 and exp_wh > 0.0
        assert child.accumulated_charge_Ah - c_ah == pytest.approx(exp_ah, rel=1e-12, abs=1e-15)
        assert child.accumulated_charge_Wh - c_wh == pytest.approx(exp_wh, rel=1e-12, abs=1e-15)
        assert ctrl.accumulated_charge_Ah - p_ah == pytest.approx(exp_ah, rel=1e-12, abs=1e-15)
        assert ctrl.accumulated_charge_Wh - p_wh == pytest.approx(exp_wh, rel=1e-12, abs=1e-15)
        assert ctrl.max_observed_dt_s == DT_S + (k_sleep - 1) * PERIOD_S


def test_c08_psu_readback_proxies_delegate_to_charge_ctrl():
    """[MAX verifier P3-4, 2026-09-15, review-kör 6, C-08a/C-08b] l.
    test_ocv_soc_controller.py azonos nevű tesztje a teljes indoklásért."""
    ctrl, *_ = make_iec_controller()
    assert ctrl._charge_ctrl is None
    assert ctrl.i_psu_measured_A is None
    assert ctrl.psu_current_readback_valid is None
    assert ctrl.u_psu_measured_V is None
    assert ctrl.psu_voltage_readback_valid is None

    class _FakeChargeCtrl:
        i_psu_measured_A = 1.23
        psu_current_readback_valid = True
        u_psu_measured_V = 14.56
        psu_voltage_readback_valid = False

    ctrl._charge_ctrl = _FakeChargeCtrl()
    assert ctrl.i_psu_measured_A == 1.23
    assert ctrl.psu_current_readback_valid is True
    assert ctrl.u_psu_measured_V == 14.56
    assert ctrl.psu_voltage_readback_valid is False


class TestLoadCurrentReadbackResetOrder:
    """MAX verifier V-1 (2026-09-15, R2-06, 12. writer-kör): l.
    test_golden_gg_controller.py azonos nevű teszt-osztálya a teljes
    indoklásért -- a C-08a F-1 hibaosztály-minta replikálása iec_dc_
    resistance_controller.py-ra (LOAD_1 fázis)."""

    def test_resets_to_none_on_dmm_feedback_lost_gate(self):
        ctrl, _psu, load, dmm_v, _dmm_t = make_iec_controller()
        ctrl.advance(0.0)                      # INIT -> PRECHARGE
        _force_charge_done(ctrl)
        ctrl.advance(1.0)                      # -> PRECHARGE_RELAX
        ctrl.advance(1.0)                      # relax_before_measurement_s=1.0 -> LOAD_1
        assert ctrl.state == IecDcResistanceState.LOAD_1
        ctrl.advance(0.0)                      # LOAD_1 setup (input_on())

        load.current_A = 2.48  # 4 * i10_A(0.62)
        ctrl.advance(0.5)  # load-on ota meg nem eri el a sample_delay_1_s-t -- csak olvas
        assert ctrl.state == IecDcResistanceState.LOAD_1
        assert ctrl.load_current_readback_valid is True
        assert ctrl.i_load_measured_A == pytest.approx(2.48)

        dmm_v.simulate_timeout = True  # top-of-tick DMM_FEEDBACK_LOST kapu
        ctrl.advance(0.5)
        assert ctrl.state == IecDcResistanceState.FAULT
        assert ctrl.load_current_readback_valid is None
        assert ctrl.i_load_measured_A is None


class TestEnergyIntegratorStaleGuard:
    """Teljes kódbázis review 2. kör (2026-09-15, R2-18, 12. writer-kör):
    l. golden_gg_controller.py azonos nevű teszt-osztálya a teljes
    indoklásért -- `i_psu_measured_A` NEM tick-scoped, egy top-of-tick
    safety-kapu FAULT-ticken az ELŐZŐ, sikeres tick áram-értékén marad,
    miközben `psu_current_readback_valid` helyesen None-ra reset."""

    def _reach_charge_cc_with_baseline_current(self, dmm_voltage_V=12.8, psu_current_A=1.0):
        from Prog.src.charge_controller import ChargeState

        ctrl, psu, _load, dmm_v, _dmm_t = make_iec_controller(dmm_voltage_V=dmm_voltage_V)
        ctrl.advance(0.0)  # INIT -> PRECHARGE
        assert ctrl.state == IecDcResistanceState.PRECHARGE
        ctrl._charge_ctrl._charge_current_set_A = 1.5
        psu.set_output_current(1.5)
        ctrl._charge_ctrl._state = ChargeState.CHARGE_CC
        psu.current_A = psu_current_A
        ctrl.advance(2.0)  # valódi, sikeres tick -- _i_charge/valid mindkettő friss
        assert ctrl._charge_ctrl.psu_current_readback_valid is True
        assert ctrl._charge_ctrl.i_psu_measured_A == pytest.approx(psu_current_A)
        return ctrl, dmm_v

    def test_fault_gate_tick_does_not_integrate_stale_psu_current(self):
        ctrl, dmm_v = self._reach_charge_cc_with_baseline_current()
        ah_before = ctrl.accumulated_charge_Ah

        dmm_v.simulate_timeout = True  # top-of-tick DMM_FEEDBACK_LOST kapu
        ctrl.advance(3600.0)  # nagy dt_s -- ha elévült áramot integrálna, azonnal látszana

        assert ctrl._charge_ctrl.psu_current_readback_valid is None
        assert ctrl.accumulated_charge_Ah == pytest.approx(ah_before)

    def test_dt_statistics_still_recorded_on_such_tick(self):
        """Mutációs őr: a HELYES fix `0.0`-t integrál (nem `skip`-el), hogy
        a dt-statisztikák TOVÁBBRA IS frissüljenek ezen a ticken."""
        ctrl, dmm_v = self._reach_charge_cc_with_baseline_current()

        dmm_v.simulate_timeout = True
        ctrl.advance(3600.0)

        assert ctrl.max_observed_dt_s >= 3600.0


class TestR9B1CompositeChargeLimit:
    """R9-B1: a kompozit factory is a kozos ChargeControlleren halad at."""

    def test_precharge_rejects_profile_excess(self):
        from Prog.src.charge_controller import ChargeConfig, ChargeController

        ctrl, psu, load, _, _ = make_iec_controller(
            charge_config=ChargeConfig(charge_current_A_override=3.0),
        )
        ctrl._safety.psu_mode = PsuMode.PARALLEL
        ctrl.advance(0.0)
        assert isinstance(ctrl._charge_ctrl, ChargeController)
        assert ctrl._profile.effective_max_charge_A == pytest.approx(1.8)
        for _ in range(3):
            ctrl.advance(1.0)
        assert ctrl.state is IecDcResistanceState.FAULT
        assert ctrl.fault_reason.startswith("CHARGE_CURRENT_LIMIT_EXCEEDED")
        assert "3.0 A" in ctrl.fault_reason
        assert "1.8 A" in ctrl.fault_reason
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        for _ in range(3):
            ctrl.advance(1.0)
        assert not any(c.startswith("set_output_") or c == "output_on()" for c in psu.calls)
        assert "input_on()" not in load.calls


class TestR16B16bIecFloor:
    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("phase", [1, 2])
    @pytest.mark.parametrize("started", [False, True])
    @pytest.mark.parametrize("failed_off", ["none", "load", "psu"])
    def test_floor_fault_off_event_and_latch(self, cells, phase, started, failed_off):
        profile = BatteryProfile(
            battery_name="TEST", manufacturer="FIAMM", model="FG20721",
            nominal_voltage_V=2.0 * cells, cell_count=cells,
            nominal_capacity_Ah=7.2, i10_discharge_current_A=0.67,
        )
        psu, load = MockPSU(), MockLoad()
        dmm, temp = MockDMM(voltage_V=1.80 * cells), MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        ctrl = IecDcResistanceController(psu, load, dmm, temp, profile, safety, IecDcResistanceConfig())
        ctrl._state = getattr(IecDcResistanceState, f"LOAD_{phase}")
        setattr(ctrl, f"_load_{phase}_started", started)
        load.input_commanded_on = started
        calls, events = [], []
        def load_off():
            calls.append("load_off")
            if failed_off == "load":
                raise RuntimeError("load OFF error")
            load.input_commanded_on = False
        def psu_off():
            calls.append("psu_off")
            if failed_off == "psu":
                raise RuntimeError("psu OFF error")
            psu.output_commanded_on = False
        load.input_off, psu.all_outputs_off = load_off, psu_off
        ctrl.on_event = events.append
        # Az első átlépő minta is FAULT; nincs normál kisütésvégi kivétel.
        dmm.voltage_V = 1.80 * cells - 0.001
        ctrl.advance(0.01)
        assert ctrl.state == IecDcResistanceState.FAULT
        assert ctrl.fault_reason.startswith("BATTERY_UNDERVOLTAGE")
        assert calls[:2] == ["load_off", "psu_off"]
        assert any(e["event_code"] == "BATTERY_UNDERVOLTAGE" for e in events)
        before = list(load.calls)
        dmm.voltage_V = 2.1 * cells
        ctrl.advance(0.01)
        assert load.calls == before
        assert not any(c.startswith("input_on(") for c in load.calls)

    @pytest.mark.parametrize("phase", [1, 2])
    def test_exact_floor_is_allowed(self, phase):
        ctrl, _psu, load, dmm, _temp = make_iec_controller()
        ctrl._state = getattr(IecDcResistanceState, f"LOAD_{phase}")
        dmm.voltage_V = 10.8
        ctrl.advance(0.0)
        assert ctrl.state != IecDcResistanceState.FAULT
        assert load.input_commanded_on


class TestR16B22Lifecycle:
    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    def test_release_failure_latches_and_does_not_allow_new_io(self, monkeypatch, phase, error_type):
        rig = _B22Rig.make(monkeypatch, phase)
        trigger = []
        def release(_value):
            trigger.append(True)
            raise error_type("release error")
        rig.hooks["release_measurement"] = release
        with pytest.raises(error_type, match="release error"):
            _B22Rig.run(rig)
        assert trigger == [True]
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert "IEC_DMM_RELEASE_FAILED" in rig.ctrl.fault_reason
        before = list(rig.trace)
        rig.ctrl.prepare_pulse_block()
        rig.ctrl.advance(30.0)
        rig.ctrl.run_pulse_block(lambda force, before_io: pytest.fail("release utani poll"))
        assert rig.trace == before

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_slow_ack_poll_cannot_close_an_expired_window(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase, (("read_input_state", 1.1),))
        _B22Rig.run(rig)
        assert "ON_ACK_EXPIRED" in rig.ctrl.fault_reason
        _B22Rig.off_order(rig)

    def test_both_phases_result_events_and_prefixes_are_idempotent(self, monkeypatch):
        rig = _B22Rig.make(monkeypatch, "LOAD_1")
        events, results = [], []
        rig.ctrl.on_event = events.append
        rig.ctrl.on_result = results.append
        _B22Rig.run(rig)
        first_ah = rig.ctrl.accumulated_discharge_Ah
        rig.ctrl.advance(300.0)
        assert rig.ctrl.state == IecDcResistanceState.LOAD_2
        rig.voltage.voltage_V = 11.8
        _B22Rig.run(rig)
        rig.ctrl.advance(0.0)
        assert rig.ctrl.state == IecDcResistanceState.DONE
        expected = (4 * 0.62 * 20 + 20 * 0.62 * 5) / 3600
        assert 0 < first_ah < rig.ctrl.accumulated_discharge_Ah
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected)
        assert len(results) == 1 and len(results[0]["load_windows"]) == 2
        assert not results[0]["timing_degraded"]
        assert results[0]["quality"] == "OK"
        assert len([event for event in events if event["event_code"] == "IEC_LOAD_WINDOW"]) == 2
        before = list(rig.trace)
        rig.ctrl.advance(30.0)
        assert rig.trace == before and len(results) == 1
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected)


class TestR16B22RunnerSafety:
    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("signal", ["stop", "emergency", "fault"])
    def test_stop_at_post_load_handoff_prevents_next_temperature_io(self, monkeypatch, phase, signal):
        from unittest.mock import Mock

        from Prog.src.test_runner import StepKind, TestPlan, TestType
        from Prog.tests.test_test_runner import TestR16B12

        rig = _B22Rig.make(monkeypatch, phase)
        runner, _logger, _events, _off, _order, step = TestR16B12.runner(
            rig.ctrl, StepKind.IEC_DC_RESISTANCE, monkeypatch, 30.0,
        )
        rig.limit_stop[0] = runner.request_stop
        build = Mock(side_effect=AssertionError("Uj mintagyujtes a blokk utani stopnal"))
        configure = Mock(side_effect=AssertionError("Uj DMM-konfiguracio stop utan"))
        monkeypatch.setattr(runner, "_build_sample", build)
        monkeypatch.setattr(runner, "_reconfigure_dmm_nplc_for_relax_substate", configure)
        target = 20.0 if phase == "LOAD_1" else 5.0
        triggered = []
        def last_voltage(_value):
            if rig.load.input_commanded_on and rig.clock() >= 100.0 + target - 1e-8:
                triggered.append(len(rig.trace))
                if signal == "stop":
                    runner.request_stop()
                elif signal == "emergency":
                    runner.request_emergency_stop("USER_EMERGENCY_STOP")
                else:
                    rig.ctrl.emergency_stop("POST_LOAD_FAULT")
        rig.hooks["read_voltage"] = last_voltage
        result = runner.run(TestPlan(TestType.IEC_DC_INTERNAL_RESISTANCE, (step,)))
        assert len(triggered) == 1, "A vegso I/O-trigger nem sult el"
        expected = {
            "stop": ("STOPPED", "USER_STOP_REQUESTED"),
            "emergency": ("FAULT", "USER_EMERGENCY_STOP"),
            "fault": ("FAULT", "POST_LOAD_FAULT"),
        }[signal]
        assert (result.status, result.reason) == expected
        build.assert_not_called()
        configure.assert_not_called()
        tail = [name for name, _, _ in rig.trace[triggered[0]:]]
        assert tail[:2] == ["input_off", "all_outputs_off"]
        assert not any(name in tail for name in ("read_voltage", "read_temperature", "measure_current", "input_on"))
        assert rig.ctrl.result["load_windows"][0]["off_return_s"] is not None

    @pytest.mark.parametrize("safety_fault", [False, True])
    def test_timing_only_continues_but_safety_fault_stops_session(self, monkeypatch, safety_fault):
        from Prog.src.test_runner import StepKind, TestPlan, TestStep, TestType
        from Prog.tests.test_test_runner import TestR16B12, _StubRelaxCtrl

        rig = _B22Rig.make(monkeypatch, "LOAD_1")
        runner, _logger, _events, _off, _order, step = TestR16B12.runner(
            rig.ctrl, StepKind.IEC_DC_RESISTANCE, monkeypatch, 30.0,
        )
        rig.limit_stop[0] = runner.request_stop
        rig.ctrl._config.rest_duration_s = 1e-9
        entered_next, samples, fault_trigger = [], [], []
        def factory(requested):
            if requested.kind == StepKind.IEC_DC_RESISTANCE:
                return rig.ctrl
            entered_next.append(True)
            return _StubRelaxCtrl(steps_to_done=1)
        runner._controller_for_step = factory
        def sample(_value):
            samples.append(True)
            # A külső idő a javított, pontos blokk-végpont után is halad.
            rig.clock.burn(0.02)
            if len(samples) >= 16:
                runner.request_stop()
        runner.on_sample = sample
        def voltage(_value):
            if safety_fault and rig.load.input_commanded_on and not fault_trigger:
                fault_trigger.append(True)
                rig.voltage.voltage_V = 10.0
        rig.hooks["read_voltage"] = voltage
        timing_trigger = []
        def unexpected_current_delay(_value):
            on = rig.ctrl._iec_window["on_command_s"]
            if not safety_fault and on is not None and rig.clock() - on >= 19.6 and not timing_trigger:
                timing_trigger.append(True)
                rig.clock.burn(0.6)
        rig.hooks["measure_current"] = unexpected_current_delay
        result = runner.run(TestPlan(
            TestType.CHARACTERIZATION,
            (step, TestStep(StepKind.RELAX, "kovetkezo lepes")),
        ))
        assert len(samples) < 16, "A terminator nem ert el idoben"
        if safety_fault:
            assert fault_trigger == [True]
            assert result.status == "FAULT"
            assert "BATTERY_UNDERVOLTAGE" in result.reason
            assert not entered_next
            assert sum(name == "input_on" for name, _, _ in rig.trace) == 1
        else:
            assert timing_trigger == [True], "A cutoff alatti időzítési trigger nem sült el"
            assert entered_next == [True], "A timing-only lepes utan folytatni kell"
            assert result.status == "DONE"
            assert rig.ctrl.result["timing_degraded"]
            assert rig.ctrl.result["quality"] == "INVALID_MEASUREMENT_MISSING"


class TestR16B22ReturnedTemperature:
    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("during_load", [False, True])
    def test_returned_overtemperature_precedes_pending_stop(self, monkeypatch, phase, during_load):
        rig = _B22Rig.make(monkeypatch, phase)
        original = rig.temperature.read_temperature
        triggered = []
        def temperature():
            value = original()
            if rig.load.input_commanded_on == during_load and not triggered:
                triggered.append(True)
                rig.stopped[0] = True
                return 85.0
            return value
        monkeypatch.setattr(rig.temperature, "read_temperature", temperature)
        _B22Rig.run(rig)
        assert triggered == [True]
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert "BATTERY_OVERTEMPERATURE" in rig.ctrl.fault_reason
        _B22Rig.off_order(rig)


class TestR16B22WindowCallback:
    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_window_callback_stop_prevents_next_advance_io(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase)
        triggered = []
        def event(value):
            if value["event_code"] == "IEC_LOAD_WINDOW":
                triggered.append(True)
                rig.stopped[0] = True
        rig.ctrl.on_event = event
        _B22Rig.run(rig)
        assert triggered == [True]
        before = list(rig.trace)
        rig.ctrl.advance(30.0)
        assert rig.trace == before
        assert rig.ctrl.result["load_windows"][0]["stop_requested"]


class TestR16B22ReadbackFaults:
    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("source,reason", [
        ("read_voltage", "DMM_FEEDBACK_LOST"),
        ("measure_current", "LOAD_COMM_LOST"),
    ])
    def test_readback_fault_precedes_simultaneous_runner_stop(self, monkeypatch, phase, source, reason):
        from Prog.src.test_runner import StepKind, TestPlan, TestType
        from Prog.tests.test_test_runner import TestR16B12

        rig = _B22Rig.make(monkeypatch, phase)
        runner, _logger, _events, _off, _order, step = TestR16B12.runner(
            rig.ctrl, StepKind.IEC_DC_RESISTANCE, monkeypatch, 30.0,
        )
        rig.limit_stop[0] = runner.request_stop
        triggered = []
        device = rig.voltage if source == "read_voltage" else rig.load
        original = getattr(device, source)
        def unavailable(*args, **kwargs):
            if rig.load.input_commanded_on:
                triggered.append(len(rig.trace))
                runner.request_stop()
                raise RuntimeError("critical readback error")
            return original(*args, **kwargs)
        monkeypatch.setattr(device, source, unavailable)
        result = runner.run(TestPlan(TestType.IEC_DC_INTERNAL_RESISTANCE, (step,)))
        assert len(triggered) == 1
        assert (result.status, result.reason) == ("FAULT", reason)
        names = [name for name, _, _ in rig.trace[triggered[0]:]]
        assert names[:2] == ["input_off", "all_outputs_off"]
        assert not any(name in names for name in ("read_voltage", "read_temperature", "measure_current", "input_on"))

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_psu_flag_still_on_rejects_load_on(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase)
        triggered = []
        def sticky_psu(_value):
            triggered.append(True)
            rig.psu.output_commanded_on = True
        rig.hooks["all_outputs_off"] = sticky_psu
        _B22Rig.run(rig)
        assert triggered
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert "PSU" in rig.ctrl.fault_reason
        assert not any(name == "input_on" for name, _, _ in rig.trace)
        _B22Rig.off_order(rig)


class TestR16B22TerminalSafetyFacts:
    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_current_returning_at_deadline_keeps_power_fault(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase, (("measure_current", 0.11),))
        original = rig.load.measure_current
        target = 20.0 if phase == "LOAD_1" else 5.0
        triggered = []
        def current():
            value = original()
            on = rig.ctrl._iec_window["on_command_s"]
            if on is not None and rig.clock() - on >= target - 0.3:
                # A tanult I/O-idő előretekintése már nem enged ismert
                # késést a célon át; itt egy új, váratlan elakadást injektálunk.
                rig.clock.burn(max(0.0, on + target - rig.clock()))
                triggered.append(len(rig.trace))
                return 100.0
            return value
        monkeypatch.setattr(rig.load, "measure_current", current)
        _B22Rig.run(rig)
        assert len(triggered) == 1
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert "LOAD_POWER_LIMIT_EXCEEDED" in rig.ctrl.fault_reason
        _B22Rig.off_order(rig, triggered[0])

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_concurrent_state_snapshot_precedes_stop_off(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase)
        triggered = []
        def current(_value):
            triggered.append(len(rig.trace))
            rig.psu.output_commanded_on = True
            rig.stopped[0] = True
        rig.hooks["measure_current"] = current
        _B22Rig.run(rig)
        assert len(triggered) == 1
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert "PSU" in rig.ctrl.fault_reason
        _B22Rig.off_order(rig, triggered[0])


class TestR16B22Fix1:
    @staticmethod
    def runner(rig, monkeypatch):
        from Prog.src.test_runner import StepKind
        from Prog.tests.test_test_runner import TestR16B12

        runner, _logger, _events, _off, _order, step = TestR16B12.runner(
            rig.ctrl, StepKind.IEC_DC_RESISTANCE, monkeypatch, 30.0,
        )
        rig.limit_stop[0] = runner.request_stop
        return runner, step

    @staticmethod
    def plan(step):
        from Prog.src.test_runner import TestPlan, TestType

        return TestPlan(TestType.IEC_DC_INTERNAL_RESISTANCE, (step,))

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("mode", list(TempCompMode))
    @pytest.mark.parametrize("legacy", [False, True])
    @pytest.mark.parametrize("dt", [0.0, 2.0])
    def test_initial_temperature_failure_prevents_enabled_on(self, monkeypatch, phase, mode, legacy, dt):
        rig = _B22Rig.make(monkeypatch, phase)
        rig.ctrl._safety.temp_comp_mode = mode
        rig.temperature.simulate_timeout = True
        if legacy:
            rig.ctrl.advance(dt)
        else:
            _B22Rig.run(rig, dt)
        assert rig.ctrl.temp_dmm_fault_ticks > 0, "A hibas T-olvasas nem tortent meg"
        names = [name for name, _, _ in rig.trace]
        if mode == TempCompMode.ENABLED:
            assert rig.ctrl.state == IecDcResistanceState.FAULT
            assert rig.ctrl.fault_reason == "TEMPERATURE_MONITOR_LOST_CRITICAL"
            assert not any(name in names for name in ("set_mode_cc", "set_current", "input_on"))
            off = names.index("input_off")
            assert names[off:off + 2] == ["input_off", "all_outputs_off"]
            if not legacy:
                _B22Rig.off_order(rig)
            before = list(rig.trace)
            rig.ctrl.advance(2.0)
            assert rig.trace == before
        else:
            assert not rig.ctrl.fault_reason
            assert names.count("input_on") == 1
            if legacy:
                rig.ctrl.emergency_stop("TEST_END")
            else:
                _B22Rig.off_order(rig)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_common_poll_ack_is_adopted_before_next_slow_io(self, monkeypatch, phase):
        from types import SimpleNamespace

        import Prog.src.instrument_manager as manager_module
        import Prog.tests.mock_drivers.mock_load as mock_load_module
        from Prog.src.test_runner import TestRunner

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step = self.runner(rig, monkeypatch)
        monkeypatch.setattr(manager_module, "time", SimpleNamespace(monotonic=rig.clock))
        monkeypatch.setattr(mock_load_module, "time", SimpleNamespace(monotonic=rig.clock))
        monkeypatch.setattr(runner, "_poll_device_errors", TestRunner._poll_device_errors.__get__(runner))
        monkeypatch.setattr(runner, "_should_poll_device_errors", lambda: False)
        rig.hooks["input_on"] = lambda _value: setattr(rig.load, "_input_is_on", False)
        original = rig.load.read_input_state
        reads, status, stopped = [], [], []
        def read_state():
            if rig.load.input_commanded_on:
                reads.append(rig.clock())
                if len(reads) == 2:
                    rig.clock.burn(0.2)
                    rig.load._input_is_on = True
            return original()
        monkeypatch.setattr(rig.load, "read_input_state", read_state)
        original_status = rig.load.read_questionable_status
        def slow_status():
            if rig.load.input_commanded_on and not status:
                status.append(rig.clock())
                rig.clock.burn(0.9)
            return original_status()
        monkeypatch.setattr(rig.load, "read_questionable_status", slow_status)
        def sample(_data):
            stopped.append(True)
            runner.request_stop()
        runner.on_sample = sample
        result = runner.run(self.plan(step))
        assert status and len(reads) >= 2, "A kozos manager-poll nem nyugtazott"
        assert stopped == [True], "A blokk utani runner-trigger nem sult el"
        assert (result.status, result.reason) == ("STOPPED", "USER_STOP_REQUESTED")
        window = rig.ctrl._iec_windows[0]
        assert window["ack_return_s"] - window["on_command_s"] == pytest.approx(0.2)
        assert window["off_command_s"] - window["on_command_s"] >= 5.0 - 1e-8
        assert "ON_ACK_EXPIRED" not in rig.ctrl.fault_reason

    @pytest.mark.parametrize("phase,target", [("LOAD_1", 20.0), ("LOAD_2", 5.0)])
    @pytest.mark.parametrize("failure", ["load", "psu", "release", "event"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
    def test_cleanup_keeps_returned_prefix_tail_and_session_once(
        self, monkeypatch, phase, target, failure, error_type,
    ):
        rig = _B22Rig.make(monkeypatch, phase, (("input_off", 0.3),))
        runner, step = self.runner(rig, monkeypatch)
        original_voltage = rig.voltage.read_voltage
        returned, triggered = [], []
        def voltage(**kwargs):
            if rig.load.input_commanded_on and not returned:
                returned.append(True)
                rig.clock.burn(target)
            return original_voltage(**kwargs)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        def fail(_value):
            if not triggered and rig.ctrl._iec_window.get("on_command_s") is not None:
                triggered.append(True)
                raise error_type("fix1 cleanup")
        if failure == "event":
            def event(value):
                if value["event_code"] == "IEC_LOAD_WINDOW":
                    fail(value)
            rig.ctrl.on_event = event
        else:
            rig.hooks[{"load": "input_off", "psu": "all_outputs_off", "release": "release_measurement"}[failure]] = fail
        if error_type is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt, match="fix1 cleanup"):
                runner.run(self.plan(step))
        else:
            result = runner.run(self.plan(step))
            assert result.status == "FAULT"
        assert returned == [True] and triggered == [True], "A cleanup-trigger nem sult el"
        measured = 2.48 if phase == "LOAD_1" else 12.4
        expected_ah = measured * (target + 0.3) / 3600.0
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected_ah)
        assert rig.ctrl.accumulated_discharge_Wh == pytest.approx(12.8 * expected_ah)
        assert runner._total_discharge_ah == pytest.approx(expected_ah)
        assert runner._total_discharge_wh == pytest.approx(12.8 * expected_ah)
        window = rig.ctrl._iec_windows[0]
        assert window["tail_estimated_s"] == pytest.approx(0.3)
        assert rig.ctrl.last_pulse_observed_extra_s == pytest.approx(window["off_return_s"] - 100.0)
        assert "FAILED" in rig.ctrl.fault_reason
        names = [name for name, _, _ in rig.trace]
        off = names.index("input_off")
        release = names.index("release_measurement")
        assert names[off:off + 2] == ["input_off", "all_outputs_off"]
        assert not any(name in names[off + 2:] for name in (
            "read_voltage", "read_temperature", "measure_current", "input_on",
        ))
        assert release > off + 1 and rig.voltage._measurement_owner is None
        before = list(rig.trace)
        rig.ctrl.prepare_pulse_block()
        rig.ctrl.advance(30.0)
        rig.ctrl.run_pulse_block(lambda force, before_io: pytest.fail("Ismetelt cleanup-poll"))
        assert rig.trace == before
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected_ah)
        assert runner._total_discharge_ah == pytest.approx(expected_ah)

    @pytest.mark.parametrize("phase,target", [("LOAD_1", 20.0), ("LOAD_2", 5.0)])
    @pytest.mark.parametrize("invalid", [10.0, float("nan"), float("inf"), 16.0])
    @pytest.mark.parametrize("prefix", [False, True])
    def test_invalid_returned_raw_never_becomes_prefix_or_tail(self, monkeypatch, phase, target, invalid, prefix):
        rig = _B22Rig.make(monkeypatch, phase, (("input_off", 0.3),))
        original = rig.voltage.read_voltage
        triggered = []
        def voltage(**kwargs):
            if rig.load.input_commanded_on:
                triggered.append(True)
                rig.clock.burn(target / 2.0 if prefix else target)
                value = original(**kwargs)
                if prefix and len(triggered) == 1:
                    return value
                return invalid
            return original(**kwargs)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        _B22Rig.run(rig)
        assert len(triggered) == (2 if prefix else 1), "A nyersadat-trigger nem sult el"
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        measured = 2.48 if phase == "LOAD_1" else 12.4
        expected = measured * target / 2.0 / 3600.0 if prefix else 0.0
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected)
        assert rig.ctrl.accumulated_discharge_Wh == pytest.approx(12.8 * expected)
        _B22Rig.off_order(rig)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("initial", [False, True])
    def test_transient_temperature_count_reaches_session_and_report(self, monkeypatch, phase, initial):
        import ast
        from pathlib import Path

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step = self.runner(rig, monkeypatch)
        rig.temperature.temperature_C = 45.0 if initial else 22.0
        original = rig.temperature.read_temperature
        readings = []
        def temperature():
            offset = rig.clock() - 100.0
            rig.temperature.temperature_C = 45.0 if (offset == 0.0 if initial else 0.1 <= offset < 0.5) else 22.0
            value = original()
            readings.append(value)
            return value
        monkeypatch.setattr(rig.temperature, "read_temperature", temperature)
        # Mindkét blokk után a normál visszatérést és a valódi session-aggregációt futtatjuk.
        rig.ctrl._config.rest_duration_s = 1e-9
        samples = []
        def sample(_data):
            samples.append(True)
            rig.clock.burn(0.02)
            if len(samples) >= 12:
                runner.request_stop()
        runner.on_sample = sample
        result = runner.run(self.plan(step))
        assert len(samples) < 12 and result.status == "DONE", "A runner nem fejezodott be"
        assert any(value == 45.0 for value in readings) and readings[-1] == 22.0
        expected = readings.count(45.0)
        assert expected > 0 and rig.ctrl.battery_temperature_C == 22.0
        assert result.temperature_out_of_range_count == expected
        assert rig.ctrl.temperature_out_of_range_count == expected
        assert result.communication_faults_count == result.extreme_dt_s_count == 0
        assert result.relax_dmm_fault_count == result.temp_dmm_fault_ticks == 0
        source = (Path(__file__).resolve().parents[1] / "gui" / "main_window.py").read_text(encoding="utf-8")
        expressions = [
            value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and key.value == "capacity_result_quality"
        ]
        assert len(expressions) == 1
        expression = compile(ast.Expression(expressions[0]), "<report-quality>", "eval")
        context = {"result": result, "is_resumed": False, "relax_dmm_fault_total": 0, "temp_dmm_fault_ticks_total": 0}
        assert eval(expression, context) == "DEGRADED"
        result.temperature_out_of_range_count = 0
        assert eval(expression, context) == "OK"

    @pytest.mark.parametrize("phase,target", [("LOAD_1", 20.0), ("LOAD_2", 5.0)])
    @pytest.mark.parametrize("off_delay", [0.0, 0.3, 3.0])
    def test_off_endpoint_is_not_counted_again_as_rest(self, monkeypatch, phase, target, off_delay):
        rig = _B22Rig.make(monkeypatch, phase, (
            ("input_off", off_delay), ("release_measurement", 0.4),
        ))
        runner, step = self.runner(rig, monkeypatch)
        original_advance = rig.ctrl.advance
        rest, samples, triggered = [], [], []
        def advance(dt):
            if rig.ctrl.state in (IecDcResistanceState.OPEN_CIRCUIT_REST, IecDcResistanceState.COMPUTE_RESULT):
                rest.append((dt, rig.clock()))
                runner.request_stop()
            return original_advance(dt)
        monkeypatch.setattr(rig.ctrl, "advance", advance)
        def sample(_data):
            samples.append(True)
            if len(samples) >= 8:
                runner.request_stop()
            if len(samples) == 1:
                triggered.append(True)
                rig.clock.burn(0.7)
        runner.on_sample = sample
        result = runner.run(self.plan(step))
        assert triggered == [True] and len(rest) == 1, "A REST-hatartrigger nem sult el"
        window = rig.ctrl._iec_windows[0]
        assert rest[0][0] == pytest.approx(rest[0][1] - window["off_return_s"])
        assert rest[0][0] == pytest.approx(1.1)
        measured = 2.48 if phase == "LOAD_1" else 12.4
        expected_ah = measured * (target + off_delay) / 3600.0
        assert result.total_discharge_ah == pytest.approx(expected_ah)
        assert result.total_discharge_wh == pytest.approx(12.8 * expected_ah)
        assert result.total_discharge_ah == pytest.approx(rig.ctrl.accumulated_discharge_Ah)
        assert rig.ctrl.last_pulse_observed_extra_s == 0.0

    @pytest.mark.parametrize("phase,target", [("LOAD_1", 20.0), ("LOAD_2", 5.0)])
    @pytest.mark.parametrize("delay,degraded", [(0.4, False), (0.6, True)])
    def test_finite_late_sample_tolerance_and_continuation(self, monkeypatch, phase, target, delay, degraded):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step = self.runner(rig, monkeypatch)
        original = rig.voltage.read_voltage
        triggered, samples = [], []
        def voltage(**kwargs):
            if rig.load.input_commanded_on and rig.clock() >= 100.0 + target - 1e-8 and not triggered:
                triggered.append(True)
                rig.clock.burn(delay)
            return original(**kwargs)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        rig.ctrl._config.rest_duration_s = 1e-9
        def sample(_data):
            samples.append(True)
            rig.clock.burn(0.02)
            if len(samples) >= 12:
                runner.request_stop()
        runner.on_sample = sample
        result = runner.run(self.plan(step))
        assert triggered == [True] and len(samples) < 12, "A zaro V-mintatrigger nem sult el"
        assert result.status == "DONE" and not rig.ctrl.fault_reason
        recorded = rig.ctrl._t_sample_1_s if phase == "LOAD_1" else rig.ctrl._t_sample_2_s
        assert recorded == pytest.approx(target + delay)
        assert rig.ctrl.result["timing_degraded"] is degraded
        assert rig.ctrl._iec_windows[0]["command_error_s"] == pytest.approx(delay)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_legacy_range_counts_each_fresh_valid_measurement(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase)
        rig.temperature.temperature_C = 45.0
        rig.ctrl.advance(0.0)
        rig.ctrl.advance(0.0)
        assert rig.ctrl.temperature_out_of_range_count == 2
        rig.temperature.simulate_timeout = True
        rig.ctrl.advance(0.0)
        assert rig.ctrl.temp_dmm_fault_ticks == 1, "A hibas T-trigger nem sult el"
        assert rig.ctrl.temperature_out_of_range_count == 2
        rig.temperature.simulate_timeout = False
        rig.temperature.temperature_C = 22.0
        rig.ctrl.advance(0.0)
        assert rig.ctrl.temperature_out_of_range_count == 2
        rig.ctrl.emergency_stop("TEST_END")

    @staticmethod
    def timestamp_notes(source, phase):
        import ast

        functions = {
            node.name: node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)
        }
        legacy = "_run_load_1" if phase == "LOAD_1" else "_run_load_2"
        assert legacy in functions
        constructor = "\n".join(source.splitlines()[functions["__init__"].lineno - 1:functions["__init__"].end_lineno])
        block = "\n".join(source.splitlines()[functions["run_pulse_block"].lineno - 1:functions["run_pulse_block"].end_lineno])
        assert "ON-parancs ELŐTT" in constructor and "legacy advance()-úton" in constructor
        assert "VISSZATÉRÉSI bélyege" in constructor
        assert "I/U-pár aszinkron" in constructor
        assert "az áramminta korát nem minősíti" in constructor
        assert "horgony az ON-parancs ELŐTT" in block
        assert "az előző iteráció mért áramával" in block
        assert "csak a V-mintapontot minősíti" in block

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_timestamp_comments_document_both_paths(self, phase):
        from pathlib import Path

        import Prog.src.iec_dc_resistance_controller as module

        self.timestamp_notes(Path(module.__file__).read_text(encoding="utf-8"), phase)


class TestR16B22Fix2:
    @staticmethod
    def off_order(rig):
        from types import SimpleNamespace

        # A runner közös cleanupja az IEC-release után újra OFF-olhat és
        # diagnosztikát kérhet; a blokk saját OFF -> release sorrendje külön kapu.
        release = next(i for i, (name, _, _) in enumerate(rig.trace) if name == "release_measurement")
        _B22Rig.off_order(SimpleNamespace(trace=rig.trace[:release + 1], voltage=rig.voltage))
        assert not any(name in (
            "read_voltage", "read_temperature", "measure_current", "input_on",
        ) for name, _, _ in rig.trace[release + 1:])

    @pytest.mark.parametrize("phase,target", [("LOAD_1", 20.0), ("LOAD_2", 5.0)])
    @pytest.mark.parametrize("tick", [0.0, 2.0, 5.0, 30.0])
    @pytest.mark.parametrize("temperature_s", [0.0, 0.3, 0.4, 0.7])
    @pytest.mark.parametrize("current_s", [0.03, 0.05, 0.3, 0.32])
    @pytest.mark.parametrize("voltage_s", [0.2, 0.4])
    def test_io_lookahead_keeps_final_voltage(
        self, monkeypatch, phase, target, tick, temperature_s, current_s, voltage_s,
    ):
        rig = _B22Rig.make(monkeypatch, phase, (
            ("read_temperature", temperature_s), ("measure_current", current_s),
            ("read_voltage", voltage_s), ("read_input_state", 0.05),
            ("input_on", 0.1), ("input_off", 0.1),
        ))
        polls = []
        def poll(force, before_io):
            assert before_io()
            polls.append(rig.clock())
            rig.clock.burn(0.05)
        _B22Rig.run(rig, tick, poll)
        ctrl = rig.ctrl
        sample = ctrl._t_sample_1_s if phase == "LOAD_1" else ctrl._t_sample_2_s
        window = ctrl._iec_windows[0]
        on, off = window["on_command_s"], window["off_command_s"]
        assert polls and sum(name == "read_voltage" and active for name, _, active in rig.trace) > 1
        assert sample is not None
        assert target - 1e-8 <= sample <= target + voltage_s + 0.02
        assert target - 1e-8 <= off - on <= target + voltage_s + 0.02
        assert ctrl.state.name == ("OPEN_CIRCUIT_REST" if phase == "LOAD_1" else "COMPUTE_RESULT")
        assert not ctrl.fault_reason
        self.off_order(rig)

    @pytest.mark.parametrize("source", ["read_voltage", "measure_current", "read_temperature"])
    @pytest.mark.parametrize("stall_s", [6.0, 10.0])
    def test_cutoff_deadline_actually_fires_and_leaves_safe_state(self, monkeypatch, source, stall_s):
        rig = _B22Rig.make(monkeypatch, "LOAD_1")
        runner, step = TestR16B22Fix1.runner(rig, monkeypatch)
        triggered, samples = [], []
        def stalled(_value):
            on = rig.ctrl._iec_window["on_command_s"]
            if on is not None and rig.clock() - on >= 19.6 and not triggered:
                triggered.append(len(rig.trace))
                rig.clock.burn(stall_s)
        rig.hooks[source] = stalled
        def bounded_sample(_data):
            samples.append(True)
            if len(samples) >= 8:
                runner.request_stop()
        runner.on_sample = bounded_sample
        result = runner.run(TestR16B22Fix1.plan(step))
        assert len(triggered) == 1, "A cutoffot átlépő I/O-trigger nem sült el"
        assert result.status == "FAULT"
        assert result.reason == rig.ctrl.fault_reason == "IEC_LOAD_1_CUTOFF_DEADLINE_EXCEEDED"
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert rig.ctrl._ua_V is None and rig.ctrl._t_sample_1_s is None
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        assert sum(name == "input_on" for name, _, _ in rig.trace) == 1
        self.off_order(rig)
        before = list(rig.trace)
        rig.ctrl.advance(30.0)
        rig.ctrl.prepare_pulse_block()
        rig.ctrl.run_pulse_block(lambda *_args: pytest.fail("Poll cutoff után"))
        assert rig.trace == before

    @pytest.mark.parametrize("source", ["read_voltage", "measure_current", "read_temperature"])
    @pytest.mark.parametrize("stall_s", [6.0, 10.0])
    def test_load_2_does_not_gain_a_load_1_backstop(self, monkeypatch, source, stall_s):
        rig = _B22Rig.make(monkeypatch, "LOAD_2")
        triggered = []
        def stalled(_value):
            on = rig.ctrl._iec_window["on_command_s"]
            if on is not None and rig.clock() - on >= 4.6 and not triggered:
                triggered.append(True)
                rig.clock.burn(stall_s)
        rig.hooks[source] = stalled
        _B22Rig.run(rig)
        assert triggered == [True]
        assert rig.ctrl.state == IecDcResistanceState.COMPUTE_RESULT
        assert not rig.ctrl.fault_reason
        assert rig.ctrl._timing_degraded
        self.off_order(rig)

    def test_cutoff_overrun_is_not_hidden_by_simultaneous_stop(self, monkeypatch):
        rig = _B22Rig.make(monkeypatch, "LOAD_1")
        triggered = []
        def stalled(_value):
            on = rig.ctrl._iec_window["on_command_s"]
            if on is not None and rig.clock() - on >= 19.6 and not triggered:
                triggered.append(True)
                rig.clock.burn(6.0)
                rig.stopped[0] = True
        rig.hooks["read_voltage"] = stalled
        _B22Rig.run(rig)
        assert triggered == [True]
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert rig.ctrl.fault_reason == "IEC_LOAD_1_CUTOFF_DEADLINE_EXCEEDED"
        assert rig.ctrl._ua_V is None
        self.off_order(rig)

    @pytest.mark.parametrize("phase,target", [("LOAD_1", 20.0), ("LOAD_2", 5.0)])
    @pytest.mark.parametrize("tick", [0.0, 2.0, 5.0, 30.0])
    @pytest.mark.parametrize("on_s", [0.7, 0.9])
    def test_target_anchor_precedes_slow_on(self, monkeypatch, phase, target, tick, on_s):
        rig = _B22Rig.make(monkeypatch, phase, (
            ("input_on", on_s), ("read_voltage", 0.02),
            ("measure_current", 0.01), ("read_temperature", 0.03),
        ))
        _B22Rig.run(rig, tick)
        window = rig.ctrl._iec_windows[0]
        on_calls = [stamp for name, stamp, _ in rig.trace if name == "input_on"]
        assert len(on_calls) == 1
        assert window["on_command_s"] == on_calls[0]
        assert window["on_return_s"] - on_calls[0] == pytest.approx(on_s)
        sample = rig.ctrl._t_sample_1_s if phase == "LOAD_1" else rig.ctrl._t_sample_2_s
        assert sample == pytest.approx(target + 0.02, abs=0.02)
        assert window["off_command_s"] - on_calls[0] == pytest.approx(target + 0.02, abs=0.02)
        assert not rig.ctrl.fault_reason
        self.off_order(rig)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_common_poll_critical_reason_is_recorded_once(self, monkeypatch, phase):
        from Prog.src.test_runner import TestRunner

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step = TestR16B22Fix1.runner(rig, monkeypatch)
        monkeypatch.setattr(runner, "_poll_device_errors", TestRunner._poll_device_errors.__get__(runner))
        triggered, samples = [], []
        def critical(_value):
            triggered.append(True)
            rig.psu.pending_errors.append('-222,"Data out of range"')
        rig.hooks["input_on"] = critical
        def bounded_sample(_data):
            samples.append(True)
            if len(samples) >= 8:
                runner.request_stop()
        runner.on_sample = bounded_sample
        result = runner.run(TestR16B22Fix1.plan(step))
        assert triggered == [True], "A közös poll hibatriggere nem sült el"
        assert result.status == "FAULT"
        reason = 'SCPI_CRITICAL_ERROR[PSU]: -222,"Data out of range"'
        assert rig.ctrl.fault_reason == result.reason == reason
        self.off_order(rig)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("source", ["all_outputs_off", "set_mode_cc", "set_current", "input_on", "read_input_state"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
    def test_setup_exception_preserves_typed_reason(self, monkeypatch, phase, source, error_type):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step = TestR16B22Fix1.runner(rig, monkeypatch)
        triggered, samples = [], []
        def fail(_value):
            if not triggered:
                triggered.append(len(rig.trace))
                raise error_type("fix2 setup")
        rig.hooks[source] = fail
        def bounded_sample(_data):
            samples.append(True)
            if len(samples) >= 8:
                runner.request_stop()
        runner.on_sample = bounded_sample
        if error_type is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt, match="fix2 setup"):
                runner.run(TestR16B22Fix1.plan(step))
        else:
            result = runner.run(TestR16B22Fix1.plan(step))
            assert result.status == "FAULT"
            assert result.reason == rig.ctrl.fault_reason == f"IEC_{phase}_SETUP_FAILED: fix2 setup"
        assert len(triggered) == 1, "A setup-hibatrigger nem sült el"
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        self.off_order(rig)
        before = list(rig.trace)
        rig.ctrl.advance(30.0)
        assert rig.trace == before

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_warning_ticks_count_once_per_runner_block(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step = TestR16B22Fix1.runner(rig, monkeypatch)
        rig.temperature.temperature_C = 45.0
        currents, samples = [], []
        original = rig.load.measure_current
        def low_current():
            currents.append(True)
            return original() * 0.5
        monkeypatch.setattr(rig.load, "measure_current", low_current)
        def stop_after_block(_data):
            samples.append(True)
            runner.request_stop()
        runner.on_sample = stop_after_block
        result = runner.run(TestR16B22Fix1.plan(step))
        assert samples == [True] and len(currents) > 1
        assert result.status == "STOPPED"
        assert rig.ctrl.load_current_setpoint_warning_ticks == 1
        assert result.warning_code_counts == {
            "TEMPERATURE_OUT_OF_RANGE": 1, "LOAD_CURRENT_SETPOINT_NOT_REACHED": 1,
        }

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("temperature_s,voltage_s", [(0.4, 0.4), (0.7, 0.4)])
    def test_pending_ack_precedes_slow_temperature_and_voltage(self, monkeypatch, phase, temperature_s, voltage_s):
        rig = _B22Rig.make(monkeypatch, phase, (
            ("read_temperature", temperature_s), ("read_voltage", voltage_s),
        ))
        rig.hooks["input_on"] = lambda _value: setattr(rig.load, "_input_is_on", False)
        reads = []
        original = rig.load.read_input_state
        def delayed_ack():
            reads.append(rig.clock())
            if len(reads) >= 2:
                rig.load._input_is_on = True
            return original()
        monkeypatch.setattr(rig.load, "read_input_state", delayed_ack)
        _B22Rig.run(rig)
        assert len(reads) == 2, "A második, pozitív nyugta nem érkezett meg"
        window = rig.ctrl._iec_windows[0]
        assert window["ack_return_s"] - window["on_command_s"] <= 0.02 + 1e-8
        assert not rig.ctrl.fault_reason
        assert rig.ctrl.state.name in ("OPEN_CIRCUIT_REST", "COMPUTE_RESULT")
        self.off_order(rig)


class TestR16B22Fix3:
    @staticmethod
    def runner(rig, monkeypatch):
        runner, step = TestR16B22Fix1.runner(rig, monkeypatch)
        samples = []
        def bounded_sample(_data):
            samples.append(True)
            if len(samples) >= 4:
                runner.request_stop()
        runner.on_sample = bounded_sample
        return runner, step, samples

    @staticmethod
    def terminal(rig, samples):
        assert len(samples) < 4, "A runner csak a mintaszámkorláttal állt meg"
        assert not rig.load.input_commanded_on and not rig.psu.output_commanded_on
        TestR16B22Fix2.off_order(rig)
        before = list(rig.trace)
        rig.ctrl.prepare_pulse_block()
        rig.ctrl.advance(30.0)
        rig.ctrl.run_pulse_block(lambda *_args: pytest.fail("Terminális állapot utáni poll"))
        assert rig.trace == before

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("next_voltage_s", [0.4, 6.0])
    def test_power_gate_precedes_voltage_and_setpoint_policy(self, monkeypatch, phase, next_voltage_s):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = self.runner(rig, monkeypatch)
        triggered, new_voltages, setpoint_checks = [], [], []
        original_voltage = rig.voltage.read_voltage
        def voltage(**kwargs):
            if rig.load.input_commanded_on:
                new_voltages.append(True)
                rig.clock.burn(next_voltage_s)
            return original_voltage(**kwargs)
        def current():
            triggered.append((len(rig.trace), rig.clock()))
            rig.clock.burn(0.05)
            return 25.0
        original_setpoint = rig.ctrl._safety.check_load_current_setpoint
        def setpoint(*args):
            setpoint_checks.append(True)
            return original_setpoint(*args)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        monkeypatch.setattr(rig.load, "measure_current", current)
        monkeypatch.setattr(rig.ctrl._safety, "check_load_current_setpoint", setpoint)
        result = runner.run(TestR16B22Fix1.plan(step))
        assert len(triggered) == 1, "A határidő előtti túláram nem tért vissza"
        window = rig.ctrl._iec_windows[0]
        assert triggered[0][1] - window["on_command_s"] < 0.1
        assert not new_voltages and not setpoint_checks
        assert window["off_command_s"] == pytest.approx(triggered[0][1] + 0.05)
        assert (result.status, result.reason) == ("FAULT", "LOAD_POWER_LIMIT_EXCEEDED")
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("failure", [
        "i_nan", "i_error", "v_error", "v_nan", "final_v_error", "final_v_nan",
        "i_keyboard", "i_exit", "v_keyboard", "v_exit", "final_v_keyboard", "final_v_exit",
    ])
    def test_failed_measurement_keeps_only_validated_prefix(self, monkeypatch, phase, failure):
        rig = _B22Rig.make(monkeypatch, phase, (("input_off", 0.3),))
        runner, step, samples = self.runner(rig, monkeypatch)
        original_current, original_voltage = rig.load.measure_current, rig.voltage.read_voltage
        currents, valid_stamps, triggered = [], [], []
        target = 20.0 if phase == "LOAD_1" else 5.0
        def corrupt():
            triggered.append(True)
            rig.clock.burn(0.2)
            if not failure.endswith("nan"):
                error_type = KeyboardInterrupt if failure.endswith("keyboard") else (
                    SystemExit if failure.endswith("exit") else RuntimeError
                )
                raise error_type("FIX3_BAD_MEASUREMENT")
            return float("nan")
        def current():
            value = original_current()
            currents.append(True)
            return corrupt() if failure.startswith("i_") and len(currents) == 2 else value
        def voltage(**kwargs):
            active = rig.load.input_commanded_on
            final = rig.clock() >= rig.ctrl._load_on_t0_s + target - 1e-8
            if active and (
                (failure.startswith("v_") and len(valid_stamps) == 1)
                or (failure.startswith("final_v_") and final)
            ):
                return corrupt()
            value = original_voltage(**kwargs)
            if active:
                rig.clock.burn(0.25 if not valid_stamps else 0.0)
                valid_stamps.append(rig.clock())
            return value
        monkeypatch.setattr(rig.load, "measure_current", current)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        result = None
        if failure.endswith(("keyboard", "exit")):
            error_type = KeyboardInterrupt if failure.endswith("keyboard") else SystemExit
            with pytest.raises(error_type, match="FIX3_BAD_MEASUREMENT"):
                runner.run(TestR16B22Fix1.plan(step))
        else:
            result = runner.run(TestR16B22Fix1.plan(step))
        assert triggered == [True] and valid_stamps, "A prefix/hibás minta trigger nem sült el"
        measured = 2.48 if phase == "LOAD_1" else 12.4
        window = rig.ctrl._iec_windows[0]
        expected = measured * (valid_stamps[-1] - window["on_command_s"]) / 3600.0
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected)
        assert rig.ctrl.accumulated_discharge_Wh == pytest.approx(12.8 * expected)
        assert runner._total_discharge_ah == pytest.approx(expected)
        assert runner._total_discharge_wh == pytest.approx(12.8 * expected)
        assert window.get("tail_estimated_s", 0.0) == 0.0
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        if result is not None:
            assert result.total_discharge_ah == pytest.approx(expected)
            assert result.total_discharge_wh == pytest.approx(12.8 * expected)
            assert (result.status, result.reason) == (
                "FAULT", "LOAD_COMM_LOST" if failure.startswith("i_") else "DMM_FEEDBACK_LOST",
            )
        else:
            assert rig.ctrl.fault_reason == f"IEC_{phase}_IO_FAILED:FIX3_BAD_MEASUREMENT"
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    def test_fault_handoff_caps_prefix_at_load_off_return(self, monkeypatch, phase):
        rig = _B22Rig.make(monkeypatch, phase, (
            ("read_voltage", 0.2), ("measure_current", 0.05),
            ("input_off", 0.3), ("all_outputs_off", 0.7),
        ))
        runner, step, samples = self.runner(rig, monkeypatch)
        triggered = []
        def fault(_value):
            if rig.load.input_commanded_on:
                rig.ctrl.emergency_stop("FIX3_FAULT_HANDOFF")
                triggered.append(rig.clock())
        rig.hooks["read_voltage"] = fault
        result = runner.run(TestR16B22Fix1.plan(step))
        assert len(triggered) == 1, "A V-I/O alatti fault-handoff nem sült el"
        window = rig.ctrl._iec_windows[0]
        assert triggered[0] - window["off_return_s"] == pytest.approx(0.7)
        measured = 2.48 if phase == "LOAD_1" else 12.4
        expected = measured * (window["off_return_s"] - window["on_command_s"]) / 3600.0
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected)
        assert rig.ctrl.accumulated_discharge_Wh == pytest.approx(12.8 * expected)
        assert result.total_discharge_ah == pytest.approx(expected)
        assert result.total_discharge_wh == pytest.approx(12.8 * expected)
        assert window.get("tail_estimated_s", 0.0) == 0.0
        assert rig.ctrl._t_voltage_sample_s == window["last_voltage_return_s"] == triggered[0]
        assert (result.status, result.reason) == ("FAULT", "FIX3_FAULT_HANDOFF")
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("source", ["poll", "measure_current", "read_voltage", "read_temperature"])
    def test_non_stop_abort_is_terminal_unhandled_exception(self, monkeypatch, phase, source):
        from Prog.src.exceptions import TestAbortedByUser as UserAbort

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = self.runner(rig, monkeypatch)
        triggered = []
        def fail(*_args, **_kwargs):
            triggered.append(True)
            raise UserAbort("NON_STOP_ABORT")
        if source == "poll":
            monkeypatch.setattr(runner, "_pulse_poll_fault", fail)
        else:
            device = {"measure_current": rig.load, "read_voltage": rig.voltage,
                      "read_temperature": rig.temperature}[source]
            original = getattr(device, source)
            def read(*args, **kwargs):
                if rig.load.input_commanded_on:
                    return fail()
                return original(*args, **kwargs)
            monkeypatch.setattr(device, source, read)
        result = runner.run(TestR16B22Fix1.plan(step))
        assert triggered == [True], "A stopjel nélküli megszakítás nem sült el"
        assert (result.status, result.reason) == ("FAULT", "UNHANDLED_EXCEPTION: NON_STOP_ABORT")
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert not rig.ctrl._iec_block_halted
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("source", ["activation", "poll"])
    def test_real_abort_keeps_stop_path(self, monkeypatch, phase, source):
        from Prog.src.exceptions import check_activation_stop

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = self.runner(rig, monkeypatch)
        triggered = []
        def stop(*_args, **_kwargs):
            triggered.append(True)
            runner.request_stop()
            check_activation_stop(rig.ctrl.handoff_stop_requested)
        if source == "activation":
            rig.hooks["input_on"] = stop
        else:
            monkeypatch.setattr(runner, "_pulse_poll_fault", stop)
        result = runner.run(TestR16B22Fix1.plan(step))
        assert triggered == [True], "A valódi aktiválási stop nem sült el"
        assert (result.status, result.reason) == ("STOPPED", "USER_STOP_REQUESTED")
        assert rig.ctrl._iec_block_halted
        assert rig.ctrl.handoff_stop_requested.activation_interrupted
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("ending", [
        "stop", "emergency", "fault", "read_error", "abort", "keyboard", "system_exit",
        "release_error", "event_error",
    ])
    def test_terminal_warnings_reach_session_once(self, monkeypatch, phase, ending):
        from Prog.src.exceptions import TestAbortedByUser as UserAbort

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = self.runner(rig, monkeypatch)
        rig.temperature.temperature_C = 45.0
        original_current, original_voltage = rig.load.measure_current, rig.voltage.read_voltage
        currents, voltages, triggered = [], [], []
        def current():
            currents.append(True)
            return original_current() * 0.5
        def finish():
            triggered.append(True)
            if ending == "stop":
                runner.request_stop()
            elif ending == "emergency":
                runner.request_emergency_stop("FIX3_EMERGENCY")
            elif ending == "fault":
                rig.ctrl.emergency_stop("FIX3_FAULT")
            else:
                error_type = {"abort": UserAbort, "keyboard": KeyboardInterrupt,
                              "system_exit": SystemExit}.get(ending, RuntimeError)
                raise error_type("FIX3_TERMINAL")
        def voltage(**kwargs):
            value = original_voltage(**kwargs)
            if rig.load.input_commanded_on:
                voltages.append(True)
                if len(voltages) == 2 and ending not in ("release_error", "event_error"):
                    finish()
            return value
        monkeypatch.setattr(rig.load, "measure_current", current)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        if ending == "release_error":
            rig.hooks["release_measurement"] = lambda _value: finish()
        elif ending == "event_error":
            def event(value):
                if value["event_code"] == "IEC_LOAD_WINDOW":
                    finish()
            rig.ctrl.on_event = event
        result = None
        if ending in ("keyboard", "system_exit"):
            error_type = KeyboardInterrupt if ending == "keyboard" else SystemExit
            with pytest.raises(error_type, match="FIX3_TERMINAL"):
                runner.run(TestR16B22Fix1.plan(step))
        else:
            result = runner.run(TestR16B22Fix1.plan(step))
            assert result.status == ("STOPPED" if ending == "stop" else "FAULT")
        assert triggered == [True] and len(currents) >= 2, "A warning utáni terminális trigger nem sült el"
        expected = {"TEMPERATURE_OUT_OF_RANGE": 1, "LOAD_CURRENT_SETPOINT_NOT_REACHED": 1}
        assert runner._warning_code_counts == expected
        if result is not None:
            assert result.warning_code_counts == expected
        assert rig.ctrl.load_current_setpoint_warning_ticks == 1
        assert rig.ctrl.take_pulse_warnings() == {}
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("source", ["poll", "range", "setpoint"])
    def test_measurement_policy_exception_retains_io_origin(self, monkeypatch, phase, source):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = self.runner(rig, monkeypatch)
        triggered = []
        def fail(*_args, **_kwargs):
            triggered.append(True)
            raise RuntimeError("temperature policy crashed DURING LOAD")
        if source == "poll":
            monkeypatch.setattr(runner, "_pulse_poll_fault", fail)
        else:
            method = "check_test_temperature_range" if source == "range" else "check_load_current_setpoint"
            original = getattr(rig.ctrl._safety, method)
            def policy(*args, **kwargs):
                return fail() if rig.load.input_commanded_on else original(*args, **kwargs)
            monkeypatch.setattr(rig.ctrl._safety, method, policy)
        result = runner.run(TestR16B22Fix1.plan(step))
        assert triggered == [True], "A mérés közbeni policy-kivétel nem sült el"
        assert result.status == "FAULT"
        assert result.reason == rig.ctrl.fault_reason == (
            f"IEC_{phase}_IO_FAILED: temperature policy crashed DURING LOAD"
        )
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("voltage_V,current_A,reason", [
        (10.0, 2.48, "BATTERY_UNDERVOLTAGE"),
        (16.0, 2.48, "BATTERY_OVERVOLTAGE"),
        (14.0, 19.0, "LOAD_POWER_LIMIT_EXCEEDED"),
    ])
    def test_voltage_fault_prevents_next_wait(self, monkeypatch, phase, voltage_V, current_A, reason):
        import Prog.src.iec_dc_resistance_controller as module

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = self.runner(rig, monkeypatch)
        original_voltage, original_sleep = rig.voltage.read_voltage, module.time.sleep
        triggered, fault_waits = [], []
        def voltage(**kwargs):
            value = original_voltage(**kwargs)
            if rig.load.input_commanded_on:
                triggered.append(True)
                return voltage_V
            return value
        def sleep(delay):
            if rig.ctrl.state == IecDcResistanceState.FAULT:
                fault_waits.append(delay)
            original_sleep(delay)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        monkeypatch.setattr(rig.load, "measure_current", lambda: current_A)
        monkeypatch.setattr(module.time, "sleep", sleep)
        result = runner.run(TestR16B22Fix1.plan(step))
        assert triggered == [True], "A visszatért V-adat kapuja nem sült el"
        assert not fault_waits
        assert (result.status, result.reason) == ("FAULT", reason)
        assert rig.ctrl.accumulated_discharge_Ah == rig.ctrl.accumulated_discharge_Wh == 0.0
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("failure", ["nan", "error", "keyboard", "system_exit"])
    def test_invalid_temperature_also_disables_tail(self, monkeypatch, phase, failure):
        rig = _B22Rig.make(monkeypatch, phase, (("input_off", 0.3),))
        runner, step, samples = self.runner(rig, monkeypatch)
        original_temperature, original_voltage = rig.temperature.read_temperature, rig.voltage.read_voltage
        triggered, valid_stamps = [], []
        def voltage(**kwargs):
            value = original_voltage(**kwargs)
            if rig.load.input_commanded_on:
                valid_stamps.append(rig.clock())
            return value
        def temperature():
            if rig.load.input_commanded_on:
                triggered.append(True)
                if failure in ("nan", "error"):
                    runner.request_stop()
                if failure == "nan":
                    return float("nan")
                error_type = {"error": RuntimeError, "keyboard": KeyboardInterrupt,
                              "system_exit": SystemExit}[failure]
                raise error_type("FIX3_BAD_TEMPERATURE")
            return original_temperature()
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        monkeypatch.setattr(rig.temperature, "read_temperature", temperature)
        if failure in ("keyboard", "system_exit"):
            error_type = KeyboardInterrupt if failure == "keyboard" else SystemExit
            with pytest.raises(error_type, match="FIX3_BAD_TEMPERATURE"):
                runner.run(TestR16B22Fix1.plan(step))
        else:
            result = runner.run(TestR16B22Fix1.plan(step))
            assert result.status == "STOPPED"
        assert triggered == [True] and valid_stamps, "A prefix utáni hibás T-trigger nem sült el"
        measured = 2.48 if phase == "LOAD_1" else 12.4
        window = rig.ctrl._iec_windows[0]
        expected = measured * (valid_stamps[-1] - window["on_command_s"]) / 3600.0
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected)
        assert rig.ctrl.accumulated_discharge_Wh == pytest.approx(12.8 * expected)
        assert runner._total_discharge_ah == pytest.approx(expected)
        assert runner._total_discharge_wh == pytest.approx(12.8 * expected)
        assert not rig.ctrl._temp_dmm_valid
        assert rig.ctrl.temp_dmm_fault_ticks == 1
        assert window.get("tail_estimated_s", 0.0) == 0.0
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("during_load", [False, True])
    @pytest.mark.parametrize("off_source", ["input_off", "all_outputs_off"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    def test_returned_temperature_fault_survives_stop_off_failure(
        self, monkeypatch, phase, during_load, off_source, error_type,
    ):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = self.runner(rig, monkeypatch)
        triggered, off_failures = [], []
        def temperature(_value):
            if rig.load.input_commanded_on == during_load and not triggered:
                triggered.append(True)
                runner.request_stop()
        rig.hooks["read_temperature"] = temperature
        rig.temperature.temperature_C = 22.0 if during_load else 65.0
        if during_load:
            rig.hooks["input_on"] = lambda _value: setattr(rig.temperature, "temperature_C", 65.0)
        def failed_off(_value):
            if triggered and not off_failures:
                off_failures.append(True)
                raise error_type("FIX3_OFF_FAILURE")
        rig.hooks[off_source] = failed_off
        if error_type is RuntimeError:
            result = runner.run(TestR16B22Fix1.plan(step))
            assert result.status == "FAULT"
            assert "BATTERY_OVERTEMPERATURE" in result.reason
        else:
            with pytest.raises(error_type, match="FIX3_OFF_FAILURE"):
                runner.run(TestR16B22Fix1.plan(step))
        assert triggered == off_failures == [True], "A T/stop/OFF-hiba trigger nem sült el"
        assert "BATTERY_OVERTEMPERATURE" in rig.ctrl.fault_reason
        assert "OFF_FAILED:FIX3_OFF_FAILURE" in rig.ctrl.fault_reason
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("mode", list(TempCompMode))
    @pytest.mark.parametrize("value", [float("nan"), float("inf")], ids=["nan", "inf"])
    def test_nonfinite_initial_temperature_uses_existing_mode_policy(self, monkeypatch, phase, mode, value):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = self.runner(rig, monkeypatch)
        rig.ctrl._safety.temp_comp_mode = mode
        original = rig.temperature.read_temperature
        triggered = []
        def temperature():
            if not triggered:
                triggered.append(True)
                return value
            return original()
        def stop_after_block(_data):
            samples.append(True)
            runner.request_stop()
        monkeypatch.setattr(rig.temperature, "read_temperature", temperature)
        runner.on_sample = stop_after_block
        result = runner.run(TestR16B22Fix1.plan(step))
        assert triggered == [True], "A kezdeti nem véges T-trigger nem sült el"
        assert rig.ctrl.temp_dmm_fault_ticks == 1
        on_count = sum(name == "input_on" for name, _, _ in rig.trace)
        if mode == TempCompMode.ENABLED:
            assert (result.status, result.reason) == ("FAULT", "TEMPERATURE_MONITOR_LOST_CRITICAL")
            assert on_count == 0 and not samples
        else:
            assert (result.status, result.reason) == ("STOPPED", "USER_STOP_REQUESTED")
            assert on_count == 1 and samples == [True]
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("failure", ["policy_error", "policy_keyboard", "policy_exit", "invalid_none"])
    def test_unvalidated_current_cannot_extend_previous_tail(self, monkeypatch, phase, failure):
        rig = _B22Rig.make(monkeypatch, phase, (("input_off", 0.3),))
        runner, step, samples = self.runner(rig, monkeypatch)
        original_current = rig.load.measure_current
        original_voltage = rig.voltage.read_voltage
        original_power = rig.ctrl._safety.check_load_power
        currents, valid_stamps, triggered = [], [], []
        def current():
            value = original_current()
            currents.append(True)
            if len(currents) == 2:
                rig.clock.burn(0.2)
                if failure == "invalid_none":
                    triggered.append(True)
                    return None
            return value
        def voltage(**kwargs):
            value = original_voltage(**kwargs)
            if rig.load.input_commanded_on:
                rig.clock.burn(0.25)
                if len(currents) == 1:
                    valid_stamps.append(rig.clock())
            return value
        def power(*args, **kwargs):
            if len(currents) == 2 and failure != "invalid_none":
                triggered.append(True)
                error_type = KeyboardInterrupt if failure.endswith("keyboard") else (
                    SystemExit if failure.endswith("exit") else RuntimeError
                )
                raise error_type("FIX3_CURRENT_GATE")
            return original_power(*args, **kwargs)
        monkeypatch.setattr(rig.load, "measure_current", current)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        monkeypatch.setattr(rig.ctrl._safety, "check_load_power", power)
        if failure.endswith(("keyboard", "exit")):
            error_type = KeyboardInterrupt if failure.endswith("keyboard") else SystemExit
            with pytest.raises(error_type, match="FIX3_CURRENT_GATE"):
                runner.run(TestR16B22Fix1.plan(step))
        else:
            result = runner.run(TestR16B22Fix1.plan(step))
            assert result.status == "FAULT"
            assert result.reason.startswith(f"IEC_{phase}_IO_FAILED:")
        assert triggered == [True] and valid_stamps, "A nem validálható áram trigger nem sült el"
        if failure == "invalid_none":
            assert rig.ctrl.load_current_readback_valid is False
        window = rig.ctrl._iec_windows[0]
        measured = 2.48 if phase == "LOAD_1" else 12.4
        expected = measured * (valid_stamps[-1] - window["on_command_s"]) / 3600.0
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected)
        assert rig.ctrl.accumulated_discharge_Wh == pytest.approx(12.8 * expected)
        assert runner._total_discharge_ah == pytest.approx(expected)
        assert runner._total_discharge_wh == pytest.approx(12.8 * expected)
        assert window.get("tail_estimated_s", 0.0) == 0.0
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        self.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("fault", ["lost", "overtemperature"])
    def test_temperature_fault_precedes_later_policy_check(self, monkeypatch, phase, fault):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = self.runner(rig, monkeypatch)
        rig.ctrl._safety.temp_comp_mode = TempCompMode.ENABLED
        original_temperature = rig.temperature.read_temperature
        triggered, later_calls = [], []
        def temperature():
            if rig.load.input_commanded_on:
                triggered.append(True)
                if fault == "lost":
                    raise RuntimeError("FIX3_LOST_T")
                return 65.0
            return original_temperature()
        method = "check_battery_overtemperature" if fault == "lost" else "check_test_temperature_range"
        original_check = getattr(rig.ctrl._safety, method)
        def later_policy(*args, **kwargs):
            if triggered:
                later_calls.append(True)
                raise RuntimeError("FIX3_LATER_POLICY")
            return original_check(*args, **kwargs)
        monkeypatch.setattr(rig.temperature, "read_temperature", temperature)
        monkeypatch.setattr(rig.ctrl._safety, method, later_policy)
        result = runner.run(TestR16B22Fix1.plan(step))
        assert triggered == [True], "A korábbi T-fault trigger nem sült el"
        assert not later_calls
        expected = "TEMPERATURE_MONITOR_LOST_CRITICAL" if fault == "lost" else "BATTERY_OVERTEMPERATURE"
        assert (result.status, result.reason) == ("FAULT", expected)
        assert rig.ctrl.fault_reason == expected
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        self.terminal(rig, samples)


class TestR16B22Fix4:
    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("mode", [TempCompMode.MONITOR_ONLY, TempCompMode.OFF])
    @pytest.mark.parametrize("failure", ["error", "nan", "inf"])
    @pytest.mark.parametrize("initial", [False, True], ids=["late", "initial"])
    @pytest.mark.parametrize("recover", [False, True], ids=["invalid_until_off", "recovered"])
    def test_temperature_tail_requires_own_finite_recovery(
        self, monkeypatch, phase, mode, failure, initial, recover,
    ):
        rig = _B22Rig.make(monkeypatch, phase, (("input_off", 0.3),))
        runner, step, samples = TestR16B22Fix3.runner(rig, monkeypatch)
        rig.ctrl._safety.temp_comp_mode = mode
        target = 20.0 if phase == "LOAD_1" else 5.0
        original_temperature, original_voltage = rig.temperature.read_temperature, rig.voltage.read_voltage
        failed, recovered, valid_stamps, windows = [], [], [], []
        def temperature():
            active = rig.load.input_commanded_on
            elapsed = rig.clock() - rig.ctrl._load_on_t0_s if active else 0.0
            due = (initial and not active) or (active and elapsed >= target - 0.25)
            if (due and not failed) or (failed and not recover):
                failed.append(rig.clock())
                if failure == "error":
                    raise RuntimeError("FIX4_INVALID_T")
                return float(failure)
            value = original_temperature()
            if failed:
                recovered.append(rig.clock())
            return value
        def voltage(**kwargs):
            value = original_voltage(**kwargs)
            if rig.load.input_commanded_on:
                valid_stamps.append(rig.clock())
            return value
        def event(value):
            if value["event_code"] == "IEC_LOAD_WINDOW":
                windows.append(value)
                runner.request_stop()
        monkeypatch.setattr(rig.temperature, "read_temperature", temperature)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        rig.ctrl.on_event = event
        result = runner.run(TestR16B22Fix1.plan(step))
        assert failed and valid_stamps and len(windows) == 1, "A T-hiba/lezárás trigger nem sült el"
        assert bool(recovered) is recover
        assert rig.ctrl.temp_dmm_valid is recover
        assert valid_stamps[-1] > failed[0], "A hiba után nem futott érvényes I/V-validálás"
        window = rig.ctrl._iec_windows[0]
        tail = window["off_return_s"] - valid_stamps[-1] if recover else 0.0
        assert window.get("tail_estimated_s", 0.0) == pytest.approx(tail)
        assert tail == pytest.approx(0.3 if recover else 0.0)
        measured = 2.48 if phase == "LOAD_1" else 12.4
        expected = measured * (valid_stamps[-1] - window["on_command_s"] + tail) / 3600.0
        assert rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected)
        assert rig.ctrl.accumulated_discharge_Wh == pytest.approx(12.8 * expected)
        assert runner._total_discharge_ah == result.total_discharge_ah == pytest.approx(expected)
        assert runner._total_discharge_wh == result.total_discharge_wh == pytest.approx(12.8 * expected)
        assert (result.status, result.reason) == ("STOPPED", "USER_STOP_REQUESTED")
        TestR16B22Fix3.terminal(rig, samples)


    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("mode", [TempCompMode.MONITOR_ONLY, TempCompMode.OFF])
    @pytest.mark.parametrize("failure", ["error", "nan", "inf"])
    @pytest.mark.parametrize("recover", [False, True], ids=["invalid", "recovered_before_handoff"])
    def test_current_handoff_respects_temperature_gate(self, monkeypatch, phase, mode, failure, recover):
        rig = _B22Rig.make(monkeypatch, phase, (("input_off", 0.3),))
        runner, step, samples = TestR16B22Fix3.runner(rig, monkeypatch)
        rig.ctrl._safety.temp_comp_mode = mode
        original_temperature = rig.temperature.read_temperature
        original_current, original_voltage = rig.load.measure_current, rig.voltage.read_voltage
        failed, recovered, stopped, valid_stamps = [], [], [], []
        def temperature():
            if rig.load.input_commanded_on and (not failed or not recover):
                failed.append(True)
                if failure == "error":
                    raise RuntimeError("FIX4_HANDOFF_T")
                return float(failure)
            value = original_temperature()
            if failed:
                recovered.append(True)
            return value
        def current():
            value = original_current()
            if failed and (not recover or recovered):
                stopped.append(True)
                runner.request_stop()
            return value
        def voltage(**kwargs):
            value = original_voltage(**kwargs)
            if rig.load.input_commanded_on:
                rig.clock.burn(0.06)
                valid_stamps.append(rig.clock())
            return value
        monkeypatch.setattr(rig.temperature, "read_temperature", temperature)
        monkeypatch.setattr(rig.load, "measure_current", current)
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        result = runner.run(TestR16B22Fix1.plan(step))
        assert failed == stopped == [True] and valid_stamps, "A T-hiba utáni I-handoff trigger nem sült el"
        assert bool(recovered) is recover and rig.ctrl.temp_dmm_valid is recover
        window = rig.ctrl._iec_windows[0]
        tail = window["off_return_s"] - valid_stamps[-1] if recover else 0.0
        assert window.get("tail_estimated_s", 0.0) == pytest.approx(tail)
        assert tail >= 0.3 if recover else tail == 0.0
        measured = 2.48 if phase == "LOAD_1" else 12.4
        expected = measured * (valid_stamps[-1] - window["on_command_s"] + tail) / 3600.0
        assert result.total_discharge_ah == rig.ctrl.accumulated_discharge_Ah == pytest.approx(expected)
        assert result.total_discharge_wh == rig.ctrl.accumulated_discharge_Wh == pytest.approx(12.8 * expected)
        assert (result.status, result.reason) == ("STOPPED", "USER_STOP_REQUESTED")
        TestR16B22Fix3.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["OPEN_CIRCUIT_REST", "PRECHARGE_RELAX"])
    @pytest.mark.parametrize("ending", ["stop", "fault", "continue"])
    def test_no_block_tick_preserves_head_warning_once(self, monkeypatch, phase, ending):
        from Prog.src.test_runner import TestRunner as Runner

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step = TestR16B22Fix1.runner(rig, monkeypatch)
        monkeypatch.setattr(runner, "_build_sample", Runner._build_sample.__get__(runner))
        rig.ctrl._config.rest_duration_s = 0.01
        rig.ctrl._config.relax_before_measurement_s = 0.01
        rig.temperature.temperature_C = 45.0
        triggered, samples, blocks = [], [], []
        def temperature(_value):
            if not triggered:
                triggered.append(True)
                rig.temperature.temperature_C = 22.0
                if ending == "stop":
                    runner.request_stop()
        rig.hooks["read_temperature"] = temperature
        original_block = rig.ctrl.run_pulse_block
        def block(poll):
            value = original_block(poll)
            blocks.append(value)
            if ending == "fault" and len(blocks) == 1:
                rig.ctrl.emergency_stop("FIX4_REST_FAULT")
            return value
        monkeypatch.setattr(rig.ctrl, "run_pulse_block", block)
        def sample(value):
            samples.append(value)
            rig.clock.burn(0.02)
            if len(samples) >= 12:
                runner.request_stop()
        runner.on_sample = sample
        result = runner.run(TestR16B22Fix1.plan(step))
        assert triggered == [True] and blocks and blocks[0] is None, "A blokk nélküli T-trigger nem sült el"
        assert len(samples) < 12, "A runner csak a mintaszámkorláttal állt meg"
        assert result.warning_code_counts == {"TEMPERATURE_OUT_OF_RANGE": 1}
        assert runner._warning_code_counts == result.warning_code_counts
        assert samples and samples[0]["warning_flags"] == "TEMPERATURE_OUT_OF_RANGE"
        assert result.temperature_out_of_range_count == 1
        assert result.status == {"stop": "STOPPED", "fault": "FAULT", "continue": "DONE"}[ending]
        if ending == "fault":
            assert result.reason == "FIX4_REST_FAULT"
        if ending != "continue":
            assert not rig.ctrl._iec_windows

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("site", ["except", "before_event", "after_event"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("via_runner", [False, True], ids=["direct", "runner"])
    def test_abort_survives_throwing_stop_hook(
        self, monkeypatch, phase, site, error_type, via_runner,
    ):
        from Prog.src.exceptions import TestAbortedByUser as UserAbort

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = TestR16B22Fix3.runner(rig, monkeypatch)
        aborted, hook_failures, windows = [], [], []
        def stop_hook():
            released = not rig.ctrl._iec_block_active
            due = {"except": not released, "before_event": released and not windows,
                   "after_event": bool(windows)}[site]
            if due and not hook_failures:
                hook_failures.append(True)
                raise error_type("FIX4_STOP_HOOK_FAILED")
            return False
        def poll(*_args, **_kwargs):
            aborted.append(True)
            rig.ctrl.handoff_stop_requested = stop_hook
            raise UserAbort("FIX4_ORIGINAL_ABORT")
        def event(value):
            if value["event_code"] == "IEC_LOAD_WINDOW":
                windows.append(value)
        rig.ctrl.on_event = event
        if via_runner:
            monkeypatch.setattr(runner, "_pulse_poll_fault", poll)
            try:
                result = runner.run(TestR16B22Fix1.plan(step))
            except BaseException as exc:
                raise AssertionError("A másodlagos hookhiba kiszökött a runnerből") from exc
            assert (result.status, result.reason) == ("FAULT", "UNHANDLED_EXCEPTION: FIX4_ORIGINAL_ABORT")
        else:
            with pytest.raises((UserAbort, RuntimeError, KeyboardInterrupt, SystemExit)) as caught:
                _B22Rig.run(rig, poll=poll)
            assert isinstance(caught.value, UserAbort), repr(caught.value)
            assert "FIX4_ORIGINAL_ABORT" in str(caught.value)
        assert aborted == hook_failures == [True], "Az abort/stop-hook trigger nem sült el"
        assert len(windows) == 1, "A cleanup stop-hookja átugrotta az eseményt"
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert "FIX4_ORIGINAL_ABORT" in rig.ctrl.fault_reason
        window = rig.ctrl._iec_windows[0]
        assert window["fault_reason"] == rig.ctrl.fault_reason
        assert window["stop_requested"] == rig.ctrl._iec_block_halted
        TestR16B22Fix3.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("primary", ["abort", "poll_error", "fault"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("via_runner", [False, True], ids=["direct", "runner"])
    def test_cleanup_event_preserves_primary_and_finishes_audit(
        self, monkeypatch, phase, primary, error_type, via_runner,
    ):
        from Prog.src.exceptions import TestAbortedByUser as UserAbort

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = TestR16B22Fix3.runner(rig, monkeypatch)
        triggered, windows, terminal_reasons = [], [], []
        def poll(*_args, **_kwargs):
            triggered.append(True)
            if primary == "fault":
                return "FIX4_PRIMARY"
            if primary == "abort":
                raise UserAbort("FIX4_PRIMARY")
            raise RuntimeError("FIX4_PRIMARY")
        def event(value):
            if value["event_code"] == "IEC_LOAD_WINDOW":
                windows.append(value)
                raise error_type("FIX4_EVENT_SECONDARY")
        rig.ctrl.on_event = event
        original_terminal = runner._emergency_stop
        def terminal(reason):
            terminal_reasons.append(reason)
            return original_terminal(reason)
        monkeypatch.setattr(runner, "_emergency_stop", terminal)
        if via_runner:
            monkeypatch.setattr(runner, "_pulse_poll_fault", poll)
            if primary != "abort" and error_type is not RuntimeError:
                with pytest.raises(error_type, match="FIX4_EVENT_SECONDARY"):
                    runner.run(TestR16B22Fix1.plan(step))
            else:
                try:
                    result = runner.run(TestR16B22Fix1.plan(step))
                except BaseException as exc:
                    raise AssertionError("Az eseményhiba elfedte az eredeti abortot") from exc
                assert result.status == "FAULT" and "FIX4_PRIMARY" in result.reason
            assert terminal_reasons and all("FIX4_PRIMARY" in reason for reason in terminal_reasons)
        else:
            expected_type = UserAbort if primary == "abort" else error_type
            expected_message = "FIX4_PRIMARY" if primary == "abort" else "FIX4_EVENT_SECONDARY"
            with pytest.raises((UserAbort, RuntimeError, KeyboardInterrupt, SystemExit)) as caught:
                _B22Rig.run(rig, poll=poll)
            assert isinstance(caught.value, expected_type), repr(caught.value)
            assert expected_message in str(caught.value)
        assert triggered == [True] and len(windows) == 1, "Az elsődleges hiba/esemény trigger nem sült el"
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert "FIX4_PRIMARY" in rig.ctrl.fault_reason
        window = rig.ctrl._iec_windows[0]
        assert window["fault_reason"] == rig.ctrl.fault_reason
        assert window["stop_requested"] is True
        TestR16B22Fix3.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("secondary_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    def test_cleanup_raw_event_cannot_replace_off_baseexception(
        self, monkeypatch, phase, error_type, secondary_type,
    ):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = TestR16B22Fix3.runner(rig, monkeypatch)
        target = 20.0 if phase == "LOAD_1" else 5.0
        original_voltage = rig.voltage.read_voltage
        returned, off_failures, events = [], [], []
        def voltage(**kwargs):
            if rig.load.input_commanded_on and rig.clock() >= rig.ctrl._load_on_t0_s + target - 1e-8:
                returned.append(True)
                rig.voltage.voltage_V = 10.0
            return original_voltage(**kwargs)
        def off(_value):
            if returned and not off_failures:
                off_failures.append(True)
                raise error_type("FIX4_PRIMARY_OFF")
        def event(value):
            events.append(value["event_code"])
            if value["event_code"] == "BATTERY_UNDERVOLTAGE":
                raise secondary_type("FIX4_RAW_EVENT_SECONDARY")
        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        rig.hooks["input_off"] = off
        rig.ctrl.on_event = event
        with pytest.raises((RuntimeError, KeyboardInterrupt, SystemExit)) as caught:
            runner.run(TestR16B22Fix1.plan(step))
        assert isinstance(caught.value, error_type), repr(caught.value)
        assert "FIX4_PRIMARY_OFF" in str(caught.value)
        assert returned == off_failures == [True], "A nyers alsókapu/OFF-hiba trigger nem sült el"
        assert events == ["BATTERY_UNDERVOLTAGE", "IEC_LOAD_WINDOW"]
        assert "IEC_LOAD_OFF_FAILED:FIX4_PRIMARY_OFF" in rig.ctrl.fault_reason
        assert "BATTERY_UNDERVOLTAGE" in rig.ctrl.fault_reason
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        TestR16B22Fix3.terminal(rig, samples)

class TestR16B22Fix5:
    PRIMARY_KINDS = ("none", "exception", "abort_stop", "abort", "keyboard", "exit")
    CLEANUP_SITES = ("load", "psu", "raw", "release", "stop_before", "stop_after", "event")

    @staticmethod
    def case(monkeypatch, phase, primary_kind, site, error_type, outer, via_runner):
        from Prog.src.exceptions import TestAbortedByUser as UserAbort

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = TestR16B22Fix3.runner(rig, monkeypatch)
        primary = {
            "none": None, "exception": RuntimeError("FIX5_PRIMARY"),
            "abort_stop": UserAbort("FIX5_PRIMARY"), "abort": UserAbort("FIX5_PRIMARY"),
            "keyboard": KeyboardInterrupt("FIX5_PRIMARY"), "exit": SystemExit("FIX5_PRIMARY"),
        }[primary_kind]
        secondary = error_type("FIX5_SECONDARY")
        armed, primaries, failures, windows, stop_checks = [], [], [], [], []
        original_voltage = rig.voltage.read_voltage
        target = 20.0 if phase == "LOAD_1" else 5.0

        def voltage(**kwargs):
            active = rig.load.input_commanded_on
            # A primary nélküli nyers cleanup-kapu a kezdeti V utáni stop-út.
            if site == "raw" and (active or primary is None):
                rig.voltage.voltage_V = 10.0
            value = original_voltage(**kwargs)
            if active or (site == "raw" and primary is None):
                armed.append(True)
                rig.stopped[0] = primary_kind == "abort_stop" or primary is None and site == "raw"
                if primary_kind == "abort_stop":
                    runner.request_stop()
                if active and primary is None:
                    rig.clock.burn(target)
            return value

        def stop_hook():
            if armed and primary is not None and not primaries:
                primaries.append(True)
                raise primary
            if armed and not rig.ctrl._iec_block_active:
                stop_checks.append(bool(windows))
                wanted = "stop_after" if windows else "stop_before"
                if site == wanted and not failures:
                    failures.append(site)
                    raise secondary
            return rig.stopped[0]

        def fail(_value):
            if armed and not failures:
                failures.append(site)
                raise secondary

        def event(value):
            code = value["event_code"]
            if code == "IEC_LOAD_WINDOW":
                windows.append(value)
            if (site == "event" and code == "IEC_LOAD_WINDOW") or (
                site == "raw" and code == "BATTERY_UNDERVOLTAGE"
            ):
                fail(value)

        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        original_block = rig.ctrl.run_pulse_block
        def run_block(poll):
            rig.ctrl.handoff_stop_requested = stop_hook
            return original_block(poll)
        monkeypatch.setattr(rig.ctrl, "run_pulse_block", run_block)
        rig.ctrl.on_event = event
        if site in ("load", "psu", "release"):
            rig.hooks[{"load": "input_off", "psu": "all_outputs_off",
                       "release": "release_measurement"}[site]] = fail
        propagating_primary = primary_kind in ("abort", "keyboard", "exit")
        expected = primary if propagating_primary else (
            None if site in ("load", "psu") and error_type is RuntimeError else secondary
        )
        expected_reason = {
            "exception": f"IEC_{phase}_IO_FAILED: FIX5_PRIMARY",
            "abort": "UNHANDLED_EXCEPTION: FIX5_PRIMARY",
            "keyboard": f"IEC_{phase}_IO_FAILED:FIX5_PRIMARY",
            "exit": f"IEC_{phase}_IO_FAILED:FIX5_PRIMARY",
        }.get(primary_kind, {
            "load": "IEC_LOAD_OFF_FAILED:FIX5_SECONDARY",
            "psu": "IEC_PSU_OFF_FAILED:FIX5_SECONDARY",
            "raw": "BATTERY_UNDERVOLTAGE",
            "release": "IEC_DMM_RELEASE_FAILED:FIX5_SECONDARY",
            "stop_before": "IEC_STOP_HOOK_FAILED:FIX5_SECONDARY",
            "stop_after": "IEC_STOP_HOOK_FAILED:FIX5_SECONDARY",
            "event": "IEC_WINDOW_EVENT_FAILED:FIX5_SECONDARY",
        }[site])
        result, caught = None, None

        def invoke():
            if via_runner:
                return runner.run(TestR16B22Fix1.plan(step))
            return _B22Rig.run(rig)

        try:
            if outer:
                try:
                    raise ValueError("ALREADY_HANDLED")
                except ValueError:
                    result = invoke()
            else:
                result = invoke()
        except (RuntimeError, UserAbort, KeyboardInterrupt, SystemExit) as exc:
            caught = exc
        # A runner a normál Exception-t FAULT-tá alakítja, a BaseException kijut.
        assert caught is (None if via_runner and isinstance(expected, Exception) else expected)
        if via_runner and caught is None:
            assert result.status == "FAULT"
            assert result.reason.startswith(expected_reason)
            if primary_kind == "abort":
                assert result.reason == expected_reason
        assert armed and primaries == ([True] if primary is not None else [])
        assert failures == [site], "A cleanup-hiba trigger nem sült el"
        assert len(windows) == 1 and stop_checks[:2] == [False, True]
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert rig.ctrl.fault_reason.startswith(expected_reason)
        assert "ALREADY_HANDLED" not in rig.ctrl.fault_reason
        assert "FIX5_SECONDARY" in rig.ctrl.fault_reason
        window = rig.ctrl._iec_windows[0]
        assert window["fault_reason"] == rig.ctrl.fault_reason
        assert window["stop_requested"] == rig.ctrl._iec_block_halted
        assert window["physical_timing"] == "HIL_PENDING"
        assert window["command_error_s"] is None or isinstance(window["command_error_s"], float)
        TestR16B22Fix3.terminal(rig, samples)
        assert not rig.ctrl._iec_block_active
        assert (rig.ctrl._iec_owner is not None) == (site == "release")
        return rig, expected

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("primary_kind", PRIMARY_KINDS)
    @pytest.mark.parametrize("site", CLEANUP_SITES)
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("outer", [False, True], ids=["plain", "outer_except"])
    @pytest.mark.parametrize("via_runner", [False, True], ids=["direct", "runner"])
    def test_exception_priority_matrix(
        self, monkeypatch, phase, primary_kind, site, error_type, outer, via_runner,
    ):
        self.case(monkeypatch, phase, primary_kind, site, error_type, outer, via_runner)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("off_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    def test_voltage_interrupt_identity_survives_off_and_release(
        self, monkeypatch, phase, error_type, off_type,
    ):
        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = TestR16B22Fix3.runner(rig, monkeypatch)
        primary = error_type("FIX5_ORIGINAL_INTERRUPT")
        triggered, failures = [], []
        original_voltage = rig.voltage.read_voltage

        def voltage(**kwargs):
            if rig.load.input_commanded_on:
                triggered.append(True)
                raise primary
            return original_voltage(**kwargs)

        def fail(label, cls):
            def operation(_value):
                if triggered and label not in failures:
                    failures.append(label)
                    raise cls(label)
            return operation

        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        rig.hooks["input_off"] = fail("FIX5_LOAD", off_type)
        rig.hooks["all_outputs_off"] = fail("FIX5_PSU", off_type)
        rig.hooks["release_measurement"] = fail("FIX5_RELEASE", RuntimeError)
        caught = None
        try:
            runner.run(TestR16B22Fix1.plan(step))
        except (KeyboardInterrupt, SystemExit) as exc:
            caught = exc
        assert caught is primary
        assert triggered == [True] and failures == ["FIX5_LOAD", "FIX5_PSU", "FIX5_RELEASE"]
        assert rig.ctrl.fault_reason.startswith(f"IEC_{phase}_IO_FAILED:FIX5_ORIGINAL_INTERRUPT")
        assert all(label in rig.ctrl.fault_reason for label in failures)
        TestR16B22Fix3.terminal(rig, samples)

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("outer", [False, True], ids=["plain", "outer_except"])
    @pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
    def test_active_raw_callback_interrupt_is_not_a_callers_exception(
        self, monkeypatch, phase, outer, error_type,
    ):
        rig = _B22Rig.make(monkeypatch, phase)
        failure = error_type("FIX5_NEW_INTERRUPT")
        events, triggered = [], []
        original_voltage = rig.voltage.read_voltage

        def voltage(**kwargs):
            if rig.load.input_commanded_on:
                rig.voltage.voltage_V = 10.0
                triggered.append(True)
            return original_voltage(**kwargs)

        def event(value):
            events.append(value["event_code"])
            if value["event_code"] == "BATTERY_UNDERVOLTAGE":
                raise failure

        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        rig.ctrl.on_event = event
        caught = None
        try:
            if outer:
                try:
                    raise ValueError("ALREADY_HANDLED")
                except ValueError:
                    _B22Rig.run(rig)
            else:
                _B22Rig.run(rig)
        except (KeyboardInterrupt, SystemExit) as exc:
            caught = exc
        assert caught is failure
        assert triggered == [True] and events == ["BATTERY_UNDERVOLTAGE", "IEC_LOAD_WINDOW"]
        assert rig.ctrl.fault_reason.startswith("BATTERY_UNDERVOLTAGE")
        assert "ALREADY_HANDLED" not in rig.ctrl.fault_reason
        TestR16B22Fix3.terminal(rig, [])

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("primary_kind", PRIMARY_KINDS)
    def test_no_cleanup_error_control(self, monkeypatch, phase, primary_kind):
        from Prog.src.exceptions import TestAbortedByUser as UserAbort

        rig = _B22Rig.make(monkeypatch, phase)
        runner, step, samples = TestR16B22Fix3.runner(rig, monkeypatch)
        primary = {
            "none": None, "exception": RuntimeError("FIX5_CONTROL"),
            "abort_stop": UserAbort("FIX5_CONTROL"), "abort": UserAbort("FIX5_CONTROL"),
            "keyboard": KeyboardInterrupt("FIX5_CONTROL"), "exit": SystemExit("FIX5_CONTROL"),
        }[primary_kind]
        triggered = []

        def poll(*_args, **_kwargs):
            triggered.append(True)
            if primary is None:
                rig.stopped[0] = True
                runner.request_stop()
                return
            if primary_kind == "abort_stop":
                rig.stopped[0] = True
                runner.request_stop()
            raise primary

        monkeypatch.setattr(runner, "_pulse_poll_fault", poll)
        if primary_kind in ("keyboard", "exit"):
            caught = None
            try:
                runner.run(TestR16B22Fix1.plan(step))
            except (KeyboardInterrupt, SystemExit) as exc:
                caught = exc
            assert caught is primary
        else:
            result = runner.run(TestR16B22Fix1.plan(step))
            assert result.status == ("STOPPED" if primary_kind in ("none", "abort_stop") else "FAULT")
        assert triggered == [True], "A kontroll trigger nem sült el"
        TestR16B22Fix3.terminal(rig, samples)
        assert len(samples) < 4, "A kontroll csak a runner mintaszámkorlátjával állt meg"

    @pytest.mark.parametrize("phase", ["LOAD_1", "LOAD_2"])
    @pytest.mark.parametrize("site", ["cutoff", "tail", "audit"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("with_primary", [False, True])
    def test_all_cleanup_steps_finish_after_multiple_errors(
        self, monkeypatch, phase, site, error_type, with_primary,
    ):
        rig = _B22Rig.make(monkeypatch, phase, (("input_off", 0.3),))
        primary = KeyboardInterrupt("FIX5_PRIMARY") if with_primary else None
        extra = error_type("FIX5_EXTRA")
        release_error = KeyboardInterrupt("FIX5_RELEASE_SECONDARY")
        armed, primaries, failures, windows = [], [], [], []
        target = 20.0 if phase == "LOAD_1" else 5.0
        original_voltage = rig.voltage.read_voltage
        original_stop = rig.ctrl.emergency_stop
        original_add = rig.ctrl._energy_integrator.add_sample

        class AuditDict(dict):
            def __setitem__(self, key, value):
                if key == "physical_timing" and "audit" not in failures:
                    failures.append("audit")
                    raise extra
                return super().__setitem__(key, value)

        def voltage(**kwargs):
            value = original_voltage(**kwargs)
            if rig.load.input_commanded_on and not armed:
                armed.append(True)
                rig.clock.burn(target)
                if site == "audit":
                    rig.ctrl._iec_window = AuditDict(rig.ctrl._iec_window)
                    rig.ctrl._iec_windows[-1] = rig.ctrl._iec_window
            return value

        def stop_hook():
            if armed and primary is not None and not primaries:
                primaries.append(True)
                raise primary
            if armed and not rig.ctrl._iec_block_active:
                label = "stop_after" if windows else "stop_before"
                if label not in failures:
                    failures.append(label)
                    raise SystemExit(label)
            return False

        def input_off(_value):
            if armed and site == "cutoff":
                # Kizárólag a cleanup cutoff-ellenőrzésének hibáját injektáljuk.
                rig.ctrl._iec_window["off_command_s"] = rig.ctrl._load_on_t0_s + 26.0

        def emergency_stop(reason):
            original_stop(reason)
            if reason == "IEC_LOAD_1_CUTOFF_DEADLINE_EXCEEDED" and site == "cutoff":
                failures.append("cutoff")
                raise extra

        def add_sample(current, voltage_value, dt):
            if armed and site == "tail" and abs(dt - 0.3) < 1e-7:
                failures.append("tail")
                raise extra
            return original_add(current, voltage_value, dt)

        def release(_value):
            failures.append("release")
            raise release_error

        def event(value):
            if value["event_code"] == "IEC_LOAD_WINDOW":
                windows.append(value)
                failures.append("event")
                raise RuntimeError("FIX5_WINDOW_SECONDARY")

        monkeypatch.setattr(rig.voltage, "read_voltage", voltage)
        monkeypatch.setattr(rig.ctrl, "emergency_stop", emergency_stop)
        monkeypatch.setattr(rig.ctrl._energy_integrator, "add_sample", add_sample)
        rig.ctrl.handoff_stop_requested = stop_hook
        rig.ctrl.on_event = event
        rig.hooks["input_off"] = input_off
        rig.hooks["release_measurement"] = release
        if phase == "LOAD_2" and site == "cutoff":
            # LOAD_2 nem kap új backstopot: itt release az első cleanup-hiba.
            expected_extra = False
        else:
            expected_extra = True
        expected = primary if with_primary else (
            extra if site in ("cutoff", "tail") and expected_extra else release_error
        )
        with pytest.raises((RuntimeError, KeyboardInterrupt, SystemExit)) as caught:
            _B22Rig.run(rig)
        assert caught.value is expected
        assert armed == [True] and primaries == ([True] if with_primary else [])
        assert set(failures) == {"release", "stop_before", "event", "stop_after"} | (
            {site} if expected_extra else set()
        ), "Egy cleanup-trigger kimaradt"
        assert len(failures) == len(set(failures)) and len(windows) == 1
        assert rig.ctrl.state == IecDcResistanceState.FAULT
        assert "FIX5_RELEASE_SECONDARY" in rig.ctrl.fault_reason
        assert "FIX5_WINDOW_SECONDARY" in rig.ctrl.fault_reason
        assert "stop_before" in rig.ctrl.fault_reason and "stop_after" in rig.ctrl.fault_reason
        if expected_extra:
            assert "FIX5_EXTRA" in rig.ctrl.fault_reason
        if with_primary:
            assert rig.ctrl.fault_reason.startswith(f"IEC_{phase}_IO_FAILED:FIX5_PRIMARY")
        window = rig.ctrl._iec_windows[0]
        assert window["fault_reason"] == rig.ctrl.fault_reason and window["stop_requested"] is True
        TestR16B22Fix3.terminal(rig, [])


class TestR16B25IecEvidence:
    @pytest.mark.parametrize("poll_s", [6.0, 12.0])
    def test_unmeasured_energy_is_qualified(self, monkeypatch, poll_s):
        rig = _B22Rig.make(monkeypatch, "LOAD_2")
        events, polls = [], []
        rig.ctrl.on_event = events.append

        def poll(force, before_io):
            polls.append(force)
            assert len(polls) == 1
            rig.clock.burn(poll_s)

        _B22Rig.run(rig, 30.0, poll)
        assert polls == [True]
        assert not any(name == "measure_current" for name, _, _ in rig.trace)
        assert rig.ctrl.accumulated_discharge_Ah == 0.0
        assert rig.ctrl.accumulated_discharge_Wh == 0.0
        assert rig.ctrl.last_integration_source == "UNAVAILABLE"
        event, = [e for e in events if e["event_code"] == "IEC_LOAD_WINDOW"]
        assert "energy=UNAVAILABLE" in event["event_message"]
        assert "no measured current" in event["event_message"]
        assert "Ah/Wh excludes this ON window" in event["event_message"]
        window, = rig.ctrl._iec_windows
        assert window["off_command_s"] - window["on_command_s"] == pytest.approx(poll_s)
        _B22Rig.off_order(rig)

    @pytest.mark.parametrize("phase,target", [("LOAD_1", 20.0), ("LOAD_2", 5.0)])
    def test_target_due_uses_command_before_unexpected_io_overrun(self, monkeypatch, phase, target):
        rig = _B22Rig.make(monkeypatch, phase, (("input_on", 0.6),))
        polls, temperatures, voltages = [], [], []

        def poll(force, before_io):
            polls.append(force)
            assert len(polls) == 1
            rig.clock.burn(target - 0.7)

        def temperature(_value):
            if rig.load.input_commanded_on:
                temperatures.append(True)
                rig.clock.burn(0.5)

        def voltage(_value):
            if rig.load.input_commanded_on:
                voltages.append(True)
                rig.clock.burn(0.3)

        rig.hooks["read_temperature"] = temperature
        rig.hooks["read_voltage"] = voltage
        _B22Rig.run(rig, 30.0, poll)
        assert polls == [True] and temperatures == [True]
        window, = rig.ctrl._iec_windows
        assert window["on_return_s"] - window["on_command_s"] == pytest.approx(0.6)
        assert window["off_command_s"] - window["on_command_s"] <= target + 0.5 + 1e-7
        assert voltages == []
        _B22Rig.off_order(rig)
