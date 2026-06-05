"""
Keysight34465ADMM unit tesztek — pyvisa resource mock-kal.
"""
import pytest
from unittest.mock import MagicMock
from Prog.drivers.device_dmm import Keysight34465ADMM
from Prog.src.exceptions import InstrumentTimeoutError, InstrumentInvalidReading


def make_dmm() -> tuple[Keysight34465ADMM, MagicMock]:
    dmm = Keysight34465ADMM()
    res = MagicMock()
    res.query.return_value = '+0,"No error"'
    dmm._resource = res
    dmm._connected = True
    return dmm, res


def writes(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.write.call_args_list]


def queries(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.query.call_args_list]


class TestDcvConfiguration:
    def test_configure_dcv_sends_conf_volt_dc(self):
        dmm, res = make_dmm()
        dmm.configure_dcv(range_V=100, nplc=10)
        assert any("CONF:VOLT:DC" in c or "VOLT:DC" in c for c in writes(res))

    def test_configure_dcv_sets_nplc(self):
        dmm, res = make_dmm()
        dmm.configure_dcv(range_V=100, nplc=10)
        assert any("NPLC" in c and "10" in c for c in writes(res))

    def test_configure_dcv_disables_autorange(self):
        dmm, res = make_dmm()
        dmm.configure_dcv(range_V=100, nplc=10)
        cmds = writes(res)
        assert any("AUTO" in c or "RANG" in c for c in cmds)

    def test_set_nplc_sends_nplc_command(self):
        dmm, res = make_dmm()
        dmm.set_nplc(100)
        assert any("NPLC" in c and "100" in c for c in writes(res))


class TestVoltageReading:
    def test_read_voltage_returns_float(self):
        dmm, res = make_dmm()
        res.query.return_value = "+1.24500000E+001"
        result = dmm.read_voltage()
        assert isinstance(result, float)
        assert abs(result - 12.45) < 0.001

    def test_read_voltage_sends_read_query(self):
        dmm, res = make_dmm()
        res.query.return_value = "+1.20000000E+001"
        dmm.read_voltage()
        assert any("READ" in q for q in queries(res))

    def test_read_voltage_tracks_last_valid(self):
        """Első mérés eltárolódik a jump-detection-hoz."""
        dmm, res = make_dmm()
        res.query.return_value = "+1.20000000E+001"
        dmm.read_voltage()
        assert dmm._last_valid_voltage is not None


class TestVoltageValidation:
    """[N8] NaN / Inf / overload validáció driver szinten."""

    def test_nan_raises_invalid_reading(self):
        dmm, res = make_dmm()
        res.query.return_value = "NAN"
        with pytest.raises(InstrumentInvalidReading):
            dmm.read_voltage()

    def test_inf_raises_invalid_reading(self):
        dmm, res = make_dmm()
        res.query.return_value = "INF"
        with pytest.raises(InstrumentInvalidReading):
            dmm.read_voltage()

    def test_overload_9e37_raises_invalid_reading(self):
        """SCPI overload kód: 9.9E37 — float-ként rendkívül nagy."""
        dmm, res = make_dmm()
        res.query.return_value = "+9.90000000E+037"
        with pytest.raises(InstrumentInvalidReading):
            dmm.read_voltage()

    def test_large_voltage_jump_raises_invalid_reading(self):
        """Pillanatnyi 3V-os ugrás → kontakthiba gyanú."""
        dmm, res = make_dmm()
        res.query.return_value = "+1.20000000E+001"
        dmm.read_voltage()  # first: 12.0V

        res.query.return_value = "+9.00000000E+000"  # jump: -3V egyszerre
        with pytest.raises(InstrumentInvalidReading):
            dmm.read_voltage()

    def test_normal_gradual_change_does_not_raise(self):
        """Lassú, normál feszültségváltozás nem vált ki hibát."""
        dmm, res = make_dmm()
        res.query.return_value = "+1.20000000E+001"
        dmm.read_voltage()  # first: 12.0V

        res.query.return_value = "+1.21000000E+001"  # +0.1V → OK
        result = dmm.read_voltage()
        assert abs(result - 12.1) < 0.001


class TestTempConfiguration:
    def test_configure_temp_sends_conf_temp_frtd(self):
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=10)
        assert any("CONF:TEMP" in c and "FRTD" in c for c in writes(res))

    def test_configure_temp_sets_r0_100_ohm(self):
        """PT100: R0=100 Ω @ 0°C — SENS:TEMP:TRAN:FRTD:RES 100"""
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=10)
        assert any("FRTD:RES" in c and "100" in c for c in writes(res))

    def test_configure_temp_sets_nplc(self):
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=10)
        assert any("NPLC" in c and "10" in c for c in writes(res))

    def test_configure_temp_sets_celsius(self):
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=10)
        assert any("UNIT:TEMP" in c and "C" in c for c in writes(res))

    def test_configure_temp_uses_iec751_type_85(self):
        """IEC 751 típuskód: 85 (alpha=0.00385 Ω/Ω/°C) — NEM R0 érték!"""
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=10)
        assert any("FRTD,85" in c for c in writes(res))


class TestTemperatureReading:
    def test_read_temperature_returns_float(self):
        dmm, res = make_dmm()
        res.query.return_value = "+2.20000000E+001"
        result = dmm.read_temperature()
        assert isinstance(result, float)
        assert abs(result - 22.0) < 0.001

    def test_read_temperature_sends_read_query(self):
        dmm, res = make_dmm()
        res.query.return_value = "+2.20000000E+001"
        dmm.read_temperature()
        assert any("READ" in q for q in queries(res))


class TestBasicScpi:
    def test_idn_queries_idn(self):
        dmm, res = make_dmm()
        res.query.return_value = "KEYSIGHT,34465A,SN,FW"
        result = dmm.idn()
        assert "*IDN?" in queries(res)
        assert "KEYSIGHT" in result or "34465" in result

    def test_reset_sends_rst(self):
        dmm, res = make_dmm()
        dmm.reset()
        assert any("*RST" in c for c in writes(res))

    def test_clear_status_sends_cls(self):
        dmm, res = make_dmm()
        dmm.clear_status()
        assert any("*CLS" in c for c in writes(res))

    def test_check_error_no_error_returns_empty(self):
        dmm, res = make_dmm()
        res.query.return_value = '+0,"No error"'
        errors = dmm.check_error()
        assert errors == []

    def test_is_connected_true(self):
        dmm, res = make_dmm()
        assert dmm.is_connected() is True

    def test_safe_off_idempotent(self):
        dmm, res = make_dmm()
        dmm.safe_off()
        dmm.safe_off()


class TestRetryLogic:
    def test_raises_after_max_retries(self):
        dmm, res = make_dmm()
        res.query.side_effect = Exception("LAN timeout")
        with pytest.raises(InstrumentTimeoutError):
            dmm.read_voltage()
