import pytest
from Prog.src.exceptions import (
    InstrumentError,
    InstrumentTimeoutError,
    InstrumentCommandError,
    InstrumentConnectionLost,
    InstrumentInvalidReading,
    SafetyFault,
    CriticalSafetyFault,
    WarningSafetyFault,
    ConfigurationError,
    ProfileValidationError,
    TestAbortedByUser,
)


class TestInstrumentErrorHierarchy:
    def test_timeout_is_instrument_error(self):
        with pytest.raises(InstrumentError):
            raise InstrumentTimeoutError("DMM timeout")

    def test_command_error_is_instrument_error(self):
        with pytest.raises(InstrumentError):
            raise InstrumentCommandError("bad SCPI command")

    def test_connection_lost_is_instrument_error(self):
        with pytest.raises(InstrumentError):
            raise InstrumentConnectionLost("USB disconnected")

    def test_invalid_reading_is_instrument_error(self):
        with pytest.raises(InstrumentError):
            raise InstrumentInvalidReading("NaN voltage")

    def test_instrument_error_is_exception(self):
        with pytest.raises(Exception):
            raise InstrumentError("base")


class TestSafetyFaultHierarchy:
    def test_critical_fault_is_safety_fault(self):
        with pytest.raises(SafetyFault):
            raise CriticalSafetyFault("BATTERY_OVERVOLTAGE")

    def test_warning_fault_is_safety_fault(self):
        with pytest.raises(SafetyFault):
            raise WarningSafetyFault("HEADROOM_APPROACHING")

    def test_safety_fault_is_exception(self):
        with pytest.raises(Exception):
            raise SafetyFault("base")


class TestConfigurationErrorHierarchy:
    def test_profile_validation_is_configuration_error(self):
        with pytest.raises(ConfigurationError):
            raise ProfileValidationError("bad profile field")

    def test_configuration_error_is_exception(self):
        with pytest.raises(Exception):
            raise ConfigurationError("base")


class TestTestAbortedByUser:
    def test_is_exception(self):
        with pytest.raises(Exception):
            raise TestAbortedByUser("user pressed stop")


class TestExceptionMessages:
    def test_exception_message_preserved(self):
        msg = "DMM1 feszültség timeout 10000ms után"
        exc = InstrumentTimeoutError(msg)
        assert msg in str(exc)

    def test_profile_validation_message_preserved(self):
        msg = "model mező kötelező"
        exc = ProfileValidationError(msg)
        assert msg in str(exc)
