from collections.abc import Callable


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


class ActivationStopHook:
    """Élő stop-jelzés és a be nem fejezett aktiválási nyugta ténye."""

    def __init__(self, stop_requested: Callable[[], bool]) -> None:
        self._stop_requested = stop_requested
        self.activation_interrupted = False

    def __call__(self) -> bool:
        return self._stop_requested()


def check_activation_stop(stop_requested: Callable[[], bool] | None) -> None:
    """Élő jelzésre megszakítjuk a nyugtát; az okfeloldás és az OFF a hívóé."""
    if stop_requested is not None and stop_requested():
        if isinstance(stop_requested, ActivationStopHook):
            stop_requested.activation_interrupted = True
        raise TestAbortedByUser("Bekapcsolási nyugta megszakítva")
