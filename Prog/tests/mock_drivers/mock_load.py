"""
MockLoad — determinisztikus szimulált elektronikus terhelés teszteléshez.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from Prog.src.exceptions import InstrumentTimeoutError, InstrumentError


@dataclass
class MockLoad:
    # Programozható állapot
    voltage_V: float = 12.0
    current_A: float = 0.0
    power_W: float = 0.0
    _input_is_on: bool = False           # NEM input_on — névütközés elkerülése
    simulate_timeout: bool = False
    simulate_current_readback_failure: bool = False
    simulate_power_limit: bool = False
    power_limit_W: float = 60.0
    raise_on_connect: bool = False

    # Feszültség csökkentési szimulációhoz
    voltage_discharge_slope_V_per_Ah: float = 0.5

    calls: list[str] = field(default_factory=list)

    def connect(self, resource: str) -> None:
        self.calls.append(f"connect({resource!r})")
        if self.raise_on_connect:
            raise InstrumentError("MockLoad: simulated connect failure")

    def disconnect(self) -> None:
        self.calls.append("disconnect()")

    def idn(self) -> str:
        self.calls.append("idn()")
        return "KEITHLEY INSTRUMENTS,MODEL 2380-120-60,MOCK,1.0"

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
        try:
            self.input_off()
        except Exception:
            pass

    def set_mode_cc(self) -> None:
        self.calls.append("set_mode_cc()")

    def set_current(self, i: float) -> None:
        self.calls.append(f"set_current({i})")
        self.current_A = i
        if self._input_is_on:
            self.power_W = self.voltage_V * self.current_A

    def input_on(self) -> None:
        self.calls.append("input_on()")
        self._input_is_on = True
        self.power_W = self.voltage_V * self.current_A

    def input_off(self) -> None:
        self.calls.append("input_off()")
        self._input_is_on = False
        self.power_W = 0.0

    def measure_voltage(self) -> float:
        self.calls.append("measure_voltage()")
        if self.simulate_timeout:
            raise InstrumentTimeoutError("MockLoad: simulated timeout")
        return self.voltage_V

    def measure_current(self) -> float:
        self.calls.append("measure_current()")
        if self.simulate_current_readback_failure:
            raise InstrumentTimeoutError("MockLoad: simulated current readback failure")
        return self.current_A if self._input_is_on else 0.0

    def measure_power(self) -> float:
        self.calls.append("measure_power()")
        p = self.power_W
        if self.simulate_power_limit:
            p = self.power_limit_W + 1.0
        return p

    def simulate_discharge_step(self, removed_Ah: float) -> None:
        """Kisütési szimulációhoz: csökkenti a feszültséget az eltávolított Ah arányában."""
        self.voltage_V -= self.voltage_discharge_slope_V_per_Ah * removed_Ah

    def called(self, method: str) -> bool:
        return any(method in c for c in self.calls)

    def reset_calls(self) -> None:
        self.calls.clear()
