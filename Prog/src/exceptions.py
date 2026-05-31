class InstrumentError(Exception):
    pass

class InstrumentTimeoutError(InstrumentError):
    pass

class InstrumentCommandError(InstrumentError):
    pass

class InstrumentConnectionLost(InstrumentError):
    pass

class InstrumentInvalidReading(InstrumentError):
    pass


class SafetyFault(Exception):
    pass

class CriticalSafetyFault(SafetyFault):
    pass

class WarningSafetyFault(SafetyFault):
    pass


class ConfigurationError(Exception):
    pass

class ProfileValidationError(ConfigurationError):
    pass


class TestAbortedByUser(Exception):
    pass
