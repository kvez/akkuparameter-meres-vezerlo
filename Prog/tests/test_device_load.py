"""
Keithley2380Load unit tesztek — pyvisa resource mock-kal.
"""
import pytest
from unittest.mock import MagicMock
from Prog.drivers.device_load import Keithley2380Load
from Prog.src.exceptions import InstrumentTimeoutError, InstrumentInvalidReading


def make_load() -> tuple[Keithley2380Load, MagicMock]:
    load = Keithley2380Load()
    res = MagicMock()
    res.query.return_value = '+0,"No error"'
    load._resource = res
    load._connected = True
    load._input_cmd = "INPUT"
    return load, res


def writes(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.write.call_args_list]


def queries(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.query.call_args_list]


class TestInputOnOff:
    def test_input_on_sends_input_on(self):
        load, res = make_load()
        load.input_on()
        assert any("ON" in c for c in writes(res))

    def test_input_off_sends_input_off(self):
        load, res = make_load()
        load.input_off()
        assert any("OFF" in c for c in writes(res))

    def test_input_on_sets_commanded_true(self):
        """[N9] Commanded state nyilvántartás."""
        load, res = make_load()
        assert load.input_commanded_on is False
        load.input_on()
        assert load.input_commanded_on is True

    def test_input_off_sets_commanded_false(self):
        load, res = make_load()
        load.input_on()
        load.input_off()
        assert load.input_commanded_on is False

    def test_safe_off_idempotent(self):
        """[v1.2] safe_off nem dob kivételt ha már off."""
        load, res = make_load()
        load.safe_off()
        load.safe_off()


class TestCurrentMode:
    def test_set_mode_cc_sends_func_curr(self):
        load, res = make_load()
        load.set_mode_cc()
        assert any("CURR" in c for c in writes(res))

    def test_set_current_sends_sour_curr(self):
        load, res = make_load()
        load.set_current(1.40)
        assert any("CURR" in c and "1.4" in c for c in writes(res))

    def test_set_current_zero(self):
        load, res = make_load()
        load.set_current(0.0)
        assert any("CURR" in c for c in writes(res))


class TestMeasurement:
    def test_measure_voltage_returns_float(self):
        load, res = make_load()
        res.query.return_value = "+1.24000000E+001"
        result = load.measure_voltage()
        assert isinstance(result, float)
        assert abs(result - 12.4) < 0.01

    def test_measure_current_returns_float(self):
        load, res = make_load()
        res.query.return_value = "+1.40000000E+000"
        result = load.measure_current()
        assert isinstance(result, float)

    def test_measure_power_returns_float(self):
        load, res = make_load()
        res.query.return_value = "+1.73600000E+001"
        result = load.measure_power()
        assert isinstance(result, float)

    def test_measure_voltage_queries_meas_volt(self):
        load, res = make_load()
        res.query.return_value = "+1.20000000E+001"
        load.measure_voltage()
        assert any("MEAS:VOLT" in q for q in queries(res))

    def test_measure_current_queries_meas_curr(self):
        load, res = make_load()
        res.query.return_value = "+1.40000000E+000"
        load.measure_current()
        assert any("MEAS:CURR" in q for q in queries(res))

    def test_measure_power_queries_meas_pow(self):
        load, res = make_load()
        res.query.return_value = "+1.73600000E+001"
        load.measure_power()
        assert any("MEAS:POW" in q for q in queries(res))


class TestBasicScpi:
    def test_idn_queries_idn(self):
        load, res = make_load()
        res.query.return_value = "KEITHLEY,2380-120-60,SN,FW"
        result = load.idn()
        assert "*IDN?" in queries(res)
        assert "KEITHLEY" in result

    def test_reset_sends_rst(self):
        load, res = make_load()
        load.reset()
        assert any("*RST" in c for c in writes(res))

    def test_clear_status_sends_cls(self):
        load, res = make_load()
        load.clear_status()
        assert any("*CLS" in c for c in writes(res))

    def test_check_error_no_error_returns_empty(self):
        load, res = make_load()
        res.query.return_value = '+0,"No error"'
        errors = load.check_error()
        assert errors == []

    def test_is_connected_true(self):
        load, res = make_load()
        assert load.is_connected() is True

    def test_is_connected_false_when_not_connected(self):
        load = Keithley2380Load()
        assert load.is_connected() is False


class TestRetryLogic:
    def test_raises_after_max_retries(self):
        load, res = make_load()
        res.write.side_effect = Exception("persistent error")
        with pytest.raises(InstrumentTimeoutError):
            load.input_on()
