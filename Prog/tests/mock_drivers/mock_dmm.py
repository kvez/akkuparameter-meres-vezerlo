"""
MockDMM — determinisztikus szimulált DMM teszteléshez.
Kezeli a DMM1 (feszültség) és DMM2 (hőmérséklet) szerepeket egyaránt.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from Prog.src.exceptions import InstrumentTimeoutError, InstrumentInvalidReading


@dataclass
class MockDMM:
    # Programozható állapot
    voltage_V: float = 12.5
    temperature_C: float = 22.0
    dmm_valid: bool = True
    simulate_timeout: bool = False
    simulate_nan: bool = False
    simulate_overload: bool = False

    # Feszültség szimulációhoz: lineáris emelkedés töltés közben
    voltage_slope_V_per_sample: float = 0.0

    calls: list[str] = field(default_factory=list)
    _sample_count: int = field(default=0, init=False, repr=False)

    def connect(self, resource: str) -> None:
        self.calls.append(f"connect({resource!r})")

    def disconnect(self) -> None:
        self.calls.append("disconnect()")

    def idn(self) -> str:
        self.calls.append("idn()")
        return "KEYSIGHT TECHNOLOGIES,34465A,MOCK,A.02.00"

    def reset(self) -> None:
        self.calls.append("reset()")

    def clear_status(self) -> None:
        self.calls.append("clear_status()")

    def check_error(self) -> list[str]:
        self.calls.append("check_error()")
        return []

    def is_connected(self) -> bool:
        return True

    def safe_off(self) -> None:
        self.calls.append("safe_off()")

    def configure_dcv(self, range_V: float, nplc: float) -> None:
        self.calls.append(f"configure_dcv(range_V={range_V}, nplc={nplc})")

    def set_nplc(self, nplc: float) -> None:
        self.calls.append(f"set_nplc({nplc})")

    def autorange_off(self) -> None:
        self.calls.append("autorange_off()")

    def configure_temp_4wire_pt100(self, nplc: float = 10.0) -> None:
        self.calls.append(f"configure_temp_4wire_pt100(nplc={nplc})")

    def read_voltage(self) -> float:
        """[N8] NaN / overload / timeout szimulálható."""
        self.calls.append("read_voltage()")
        if not self.dmm_valid:
            raise InstrumentInvalidReading("MockDMM: dmm_valid=False")
        if self.simulate_timeout:
            raise InstrumentTimeoutError("MockDMM: simulated timeout")
        if self.simulate_nan:
            raise InstrumentInvalidReading("MockDMM: NaN reading")
        if self.simulate_overload:
            raise InstrumentInvalidReading("MockDMM: overload (9.9E37)")

        v = self.voltage_V + self._sample_count * self.voltage_slope_V_per_sample
        self._sample_count += 1
        return v

    def read_temperature(self) -> float:
        self.calls.append("read_temperature()")
        if self.simulate_timeout:
            raise InstrumentTimeoutError("MockDMM: simulated temp timeout")
        return self.temperature_C

    def set_dmm_invalid(self) -> None:
        """Szimulál egy DMM_FEEDBACK_LOST eseményt."""
        self.dmm_valid = False

    def set_dmm_valid(self) -> None:
        self.dmm_valid = True

    def called(self, method: str) -> bool:
        return any(method in c for c in self.calls)

    def reset_calls(self) -> None:
        self.calls.clear()
        self._sample_count = 0
