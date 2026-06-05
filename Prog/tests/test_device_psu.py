"""
Keithley2220PSU unit tesztek — pyvisa resource mock-kal.
Fizikai hardware nélkül futtatható.
"""
import pytest
from unittest.mock import MagicMock
from Prog.drivers.device_psu import Keithley2220PSU
from Prog.src.safety import PsuMode
from Prog.src.exceptions import InstrumentTimeoutError


def make_psu(mode: PsuMode = PsuMode.INDEPENDENT) -> tuple[Keithley2220PSU, MagicMock]:
    """Előre összekötött PSU mock resource-szal."""
    psu = Keithley2220PSU(combination_mode=mode)
    res = MagicMock()
    res.query.return_value = '+0,"No error"'
    psu._resource = res
    psu._connected = True
    psu._output_cmd = "OUTP"
    return psu, res


def writes(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.write.call_args_list]


def queries(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.query.call_args_list]


class TestChannelSelect:
    def test_set_output_voltage_selects_ch1_first(self):
        psu, res = make_psu()
        psu.set_output_voltage(14.0)
        cmds = writes(res)
        ch1_idx = next(i for i, c in enumerate(cmds) if "CH1" in c)
        volt_idx = next(i for i, c in enumerate(cmds) if "VOLT" in c)
        assert ch1_idx < volt_idx

    def test_set_output_current_selects_ch1_first(self):
        psu, res = make_psu()
        psu.set_output_current(1.5)
        cmds = writes(res)
        ch1_idx = next(i for i, c in enumerate(cmds) if "CH1" in c)
        curr_idx = next(i for i, c in enumerate(cmds) if "CURR" in c)
        assert ch1_idx < curr_idx


class TestOutputVoltageAndCurrent:
    def test_set_output_voltage_sends_sour_volt(self):
        psu, res = make_psu()
        psu.set_output_voltage(14.4)
        assert any("SOUR:VOLT" in c and "14.4" in c for c in writes(res))

    def test_set_output_voltage_series_mode(self):
        psu, res = make_psu(PsuMode.SERIES)
        psu.set_output_voltage(28.8)
        assert any("SOUR:VOLT" in c and "28.8" in c for c in writes(res))

    def test_set_output_current_sends_sour_curr(self):
        psu, res = make_psu()
        psu.set_output_current(1.75)
        assert any("SOUR:CURR" in c and "1.75" in c for c in writes(res))


class TestOutputOnOff:
    def test_output_on_sends_outp_on(self):
        psu, res = make_psu()
        psu.output_on()
        assert any("OUTP" in c and "ON" in c for c in writes(res))

    def test_output_off_sends_outp_off(self):
        psu, res = make_psu()
        psu.output_off()
        assert any("OUTP" in c and "OFF" in c for c in writes(res))

    def test_all_outputs_off_sends_outp_off(self):
        psu, res = make_psu()
        psu.all_outputs_off()
        assert any("OUTP" in c for c in writes(res))

    def test_output_on_sets_commanded_true(self):
        """[N9] Commanded state nyilvántartás."""
        psu, res = make_psu()
        assert psu.output_commanded_on is False
        psu.output_on()
        assert psu.output_commanded_on is True

    def test_output_off_sets_commanded_false(self):
        psu, res = make_psu()
        psu.output_on()
        psu.output_off()
        assert psu.output_commanded_on is False

    def test_all_outputs_off_sets_commanded_false(self):
        psu, res = make_psu()
        psu.output_on()
        psu.all_outputs_off()
        assert psu.output_commanded_on is False

    def test_safe_off_idempotent_no_exception(self):
        """[v1.2] safe_off nem dob kivételt ha már off — idempotens."""
        psu, res = make_psu()
        psu.safe_off()
        psu.safe_off()  # második hívás sem dob


class TestMeasurement:
    def test_measure_output_voltage_returns_float(self):
        psu, res = make_psu()
        res.query.return_value = "+1.44000000E+001"
        result = psu.measure_output_voltage()
        assert isinstance(result, float)
        assert abs(result - 14.4) < 0.01

    def test_measure_output_current_returns_float(self):
        psu, res = make_psu()
        res.query.return_value = "+1.75000000E+000"
        result = psu.measure_output_current()
        assert isinstance(result, float)
        assert abs(result - 1.75) < 0.001

    def test_measure_voltage_queries_meas_volt(self):
        psu, res = make_psu()
        res.query.return_value = "+1.40000000E+001"
        psu.measure_output_voltage()
        assert any("MEAS:VOLT" in q for q in queries(res))

    def test_measure_current_queries_meas_curr(self):
        psu, res = make_psu()
        res.query.return_value = "+1.40000000E+000"
        psu.measure_output_current()
        assert any("MEAS:CURR" in q for q in queries(res))


class TestCombinationMode:
    def test_set_mode_independent_sends_comb_off(self):
        psu, res = make_psu()
        psu.set_mode_independent()
        assert any("COMB" in c and "OFF" in c for c in writes(res))

    def test_set_mode_series_sends_comb_ser(self):
        psu, res = make_psu()
        psu.set_mode_series()
        assert any("COMB" in c and "SER" in c for c in writes(res))

    def test_set_mode_parallel_sends_comb_par(self):
        psu, res = make_psu()
        psu.set_mode_parallel()
        assert any("COMB" in c and "PAR" in c for c in writes(res))

    def test_query_combination_mode_queries_comb(self):
        psu, res = make_psu()
        res.query.return_value = "NONE"
        psu.query_combination_mode()
        assert any("COMB" in q for q in queries(res))

    def test_set_mode_updates_combination_mode(self):
        psu, res = make_psu()
        psu.set_mode_series()
        assert psu.combination_mode == PsuMode.SERIES

    def test_set_mode_parallel_updates_combination_mode(self):
        psu, res = make_psu()
        psu.set_mode_parallel()
        assert psu.combination_mode == PsuMode.PARALLEL


class TestBasicScpi:
    def test_idn_queries_idn(self):
        psu, res = make_psu()
        res.query.return_value = "KEITHLEY,2220-30-1,SN,FW"
        result = psu.idn()
        assert "*IDN?" in queries(res)
        assert "KEITHLEY" in result

    def test_reset_sends_rst(self):
        psu, res = make_psu()
        psu.reset()
        assert any("*RST" in c for c in writes(res))

    def test_clear_status_sends_cls(self):
        psu, res = make_psu()
        psu.clear_status()
        assert any("*CLS" in c for c in writes(res))

    def test_check_error_no_error_returns_empty(self):
        psu, res = make_psu()
        res.query.return_value = '+0,"No error"'
        errors = psu.check_error()
        assert errors == []

    def test_check_error_with_error_returns_list(self):
        psu, res = make_psu()
        res.query.return_value = '-102,"Syntax error"'
        errors = psu.check_error()
        assert len(errors) > 0

    def test_is_connected_true_when_connected(self):
        psu, res = make_psu()
        assert psu.is_connected() is True

    def test_is_connected_false_when_not_connected(self):
        psu = Keithley2220PSU()
        assert psu.is_connected() is False


class TestRetryLogic:
    def test_write_retries_on_exception(self):
        psu, res = make_psu()
        call_count = 0

        def flaky_write(cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("comm error")

        res.write.side_effect = flaky_write
        psu.set_output_voltage(14.0)  # INST:SEL CH1 fails once, then succeeds

    def test_write_raises_after_max_retries(self):
        psu, res = make_psu()
        res.write.side_effect = Exception("persistent comm error")
        with pytest.raises(InstrumentTimeoutError):
            psu.set_output_voltage(14.0)
