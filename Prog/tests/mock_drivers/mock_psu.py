"""
MockPSU — determinisztikus szimulált tápegység teszteléshez.
Minden hívás logolódik; az állapot programozható.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from Prog.src.exceptions import InstrumentTimeoutError


@dataclass
class MockPSU:
    # Programozható állapot
    voltage_V: float = 0.0
    current_A: float = 0.0
    _output_is_on: bool = False          # NEM output_on — névütközés elkerülése
    combination_mode: str = "INDEPENDENT"
    raise_on_connect: bool = False
    raise_on_output_on: bool = False
    simulate_timeout: bool = False

    # Call log
    calls: list[str] = field(default_factory=list)

    def connect(self, resource: str) -> None:
        self.calls.append(f"connect({resource!r})")
        if self.raise_on_connect:
            raise InstrumentTimeoutError("MockPSU: simulated connect timeout")

    def disconnect(self) -> None:
        self.calls.append("disconnect()")

    def idn(self) -> str:
        self.calls.append("idn()")
        return "KEITHLEY INSTRUMENTS,MODEL 2220-30-1,MOCK,1.0"

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
            self.all_outputs_off()
        except Exception:
            pass

    # Magasabb szintű mód-agnosztikus API [N7]

    def set_output_voltage(self, v: float) -> None:
        self.calls.append(f"set_output_voltage({v})")
        if self.simulate_timeout:
            raise InstrumentTimeoutError("MockPSU: simulated timeout")
        self.voltage_V = v

    def set_output_current(self, i: float) -> None:
        self.calls.append(f"set_output_current({i})")
        self.current_A = i

    def output_on(self) -> None:
        self.calls.append("output_on()")
        if self.raise_on_output_on:
            raise InstrumentTimeoutError("MockPSU: simulated output_on timeout")
        self._output_is_on = True

    def output_off(self) -> None:
        self.calls.append("output_off()")
        self._output_is_on = False

    def all_outputs_off(self) -> None:
        self.calls.append("all_outputs_off()")
        self._output_is_on = False

    def measure_output_voltage(self) -> float:
        self.calls.append("measure_output_voltage()")
        return self.voltage_V

    def measure_output_current(self) -> float:
        self.calls.append("measure_output_current()")
        if self.simulate_timeout:
            raise InstrumentTimeoutError("MockPSU: simulated read timeout")
        return self.current_A

    def query_combination_mode(self) -> str:
        self.calls.append("query_combination_mode()")
        return self.combination_mode

    def set_mode_independent(self) -> None:
        self.calls.append("set_mode_independent()")
        self.combination_mode = "INDEPENDENT"

    def set_mode_series(self) -> None:
        self.calls.append("set_mode_series()")
        self.combination_mode = "SERIES"

    def set_mode_parallel(self) -> None:
        self.calls.append("set_mode_parallel()")
        self.combination_mode = "PARALLEL"

    def called(self, method: str) -> bool:
        """Segédfüggvény: adott metódus meghívódott-e?"""
        return any(method in c for c in self.calls)

    def reset_calls(self) -> None:
        self.calls.clear()
