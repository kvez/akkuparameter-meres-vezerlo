"""
MockPSU — determinisztikus szimulált tápegység teszteléshez.
Minden hívás logolódik; az állapot programozható.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from collections.abc import Callable
from Prog.src.exceptions import InstrumentTimeoutError


@dataclass
class MockPSU:
    # Programozható állapot
    voltage_V: float = 0.0
    current_A: float = 0.0
    _output_is_on: bool = False          # NEM output_on — névütközés elkerülése
    output_commanded_on: bool = False   # tükrözi a valós driver attribútumát
    combination_mode: str = "OFF"
    # R16-B14: kért mód és sessionön át megmaradó műszerállapot külön.
    actual_combination_mode: str = field(default="", init=False)
    channel_enabled: dict[str, bool] = field(default_factory=lambda: {"CH1": False, "CH2": False})
    raise_on_connect: bool = False
    raise_on_output_on: bool = False
    raise_on_safe_off: bool = False
    simulate_combination_mode_mismatch: bool = False
    simulate_timeout: bool = False
    simulate_voltage_readback_timeout: bool = False
    simulate_current_readback_timeout: bool = False
    # Review-kör 5, Hardver/Műszervezérlés HW5-02 (P3): a valós
    # device_psu.py::read_output_state() tükre -- a `_output_is_on` mezőt
    # (a fenti kommentje szerint SZÁNDÉKOSAN külön a `output_commanded_on`-
    # tól) olvassa vissza, hogy egy teszt szimulálhassa a commanded/actual
    # eltérést (pl. `_output_is_on` közvetlen felülírásával, `output_on()`/
    # `all_outputs_off()` hívása nélkül).
    simulate_output_state_readback_timeout: bool = False
    # Review 2026-09-15 (C-11/DFAM-018+DFAM-012): a valós SYST:ERR? hívás
    # kommunikációs kiesését szimulálja (nem egy VISSZAADOTT SCPI-hibát,
    # hanem magát a lekérdezést meghiúsító I/O-kivételt).
    simulate_check_error_failure: bool = False

    # Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2): a valós
    # device_psu.py::read_regulation_state() tükre -- alapból mindhárom
    # bit False, egy teszt közvetlenül állíthatja be a szimulált
    # regulációs módot.
    regulation_cv: bool = False
    regulation_cc: bool = False
    regulation_on: bool = False
    simulate_regulation_state_readback_timeout: bool = False

    # Review-kör 5, Hardver/Műszervezérlés HW5-05 (P3) -- l. mock_dmm.py
    # azonos hivatkozású mezőjének kommentje ugyanerről az elvről.
    serial: str = ""

    # Call log
    calls: list[str] = field(default_factory=list)
    pending_errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.actual_combination_mode = self.combination_mode
        if self.actual_combination_mode == "SER":
            self.channel_enabled = {"CH1": True, "CH2": True}
        if not self.serial:
            self.serial = f"SN{id(self):012X}"

    def connect(self, resource: str) -> None:
        self.calls.append(f"connect({resource!r})")
        if self.raise_on_connect:
            raise InstrumentTimeoutError("MockPSU: simulated connect timeout")

    def disconnect(self) -> None:
        self.calls.append("disconnect()")

    def idn(self) -> str:
        self.calls.append("idn()")
        # *IDN? formátum: <gyártó>,<modell>,<sorozatszám>,<firmware> -- l.
        # mock_dmm.py::idn() azonos hivatkozású kommentje.
        return f"KEITHLEY INSTRUMENTS,MODEL 2220-30-1,{self.serial},1.0"

    def reset(self) -> None:
        self.calls.append("reset()")

    def clear_status(self) -> None:
        self.calls.append("clear_status()")

    def check_error(self) -> list[str]:
        self.calls.append("check_error()")
        if self.simulate_check_error_failure:
            raise InstrumentTimeoutError("MockPSU: simulated check_error failure")
        if self.pending_errors:
            return [self.pending_errors.pop(0)]
        return []

    def is_connected(self) -> bool:
        return True

    def safe_off(self) -> bool:
        self.calls.append("safe_off()")
        if self.raise_on_safe_off:
            self.output_commanded_on = False
            return False
        try:
            self.all_outputs_off()
            return True
        except Exception:
            return False

    # Magasabb szintű mód-agnosztikus API [N7]

    def set_output_voltage(self, v: float) -> None:
        self.calls.append(f"set_output_voltage({v})")
        if self.simulate_timeout:
            raise InstrumentTimeoutError("MockPSU: simulated timeout")
        self.voltage_V = v

    def set_output_current(self, i: float) -> None:
        self.calls.append(f"set_output_current({i})")
        self.current_A = i

    def output_on(self, *, stop_requested: Callable[[], bool] | None = None) -> None:
        self.calls.append("output_on()")
        # R2-08 MAX verifier F3 (2026-09-15, 13. writer-kör): a valódi
        # driver (device_psu.py::output_on()) a commanded-flaget egy
        # try/finally-ben KONZERVATÍVAN True-ra állítja hiba esetén IS
        # (l. ott a docstring-kommentje) -- a mock korábban CSAK sikeres
        # ágon állította True-ra, tehát egy `raise_on_output_on=True`
        # szimulált hiba mellett a controller-szintű tesztek "output_
        # commanded_on is False" állítása HAMISAN mindig teljesült volna
        # (a mock sosem jutott el a True-ig), nem a tényleges emergency_
        # stop()->all_outputs_off() lánc törölte volna vissza. A fizikai
        # kimenet-állapotot (`_output_is_on`) NEM állítjuk True-ra hiba
        # esetén -- az a driver saját, sikeres esetre korlátozott
        # feltételezése marad, csak a COMMANDED (attempted) flag
        # konzervatív.
        self.output_commanded_on = True
        if self.raise_on_output_on:
            raise InstrumentTimeoutError("MockPSU: simulated output_on timeout")
        if self.query_combination_mode() != self.combination_mode:
            from Prog.src.exceptions import InstrumentInvalidReading
            raise InstrumentInvalidReading("MockPSU: kombinációs mód eltérés")
        self.channel_enabled["CH1"] = True
        self.channel_enabled["CH2"] = self.combination_mode != "OFF"
        self._output_is_on = True

    def output_off(self) -> None:
        self.calls.append("output_off()")
        self._output_is_on = False
        self.output_commanded_on = False

    def all_outputs_off(self) -> None:
        self.calls.append("all_outputs_off()")
        self._output_is_on = False
        self.output_commanded_on = False

    def measure_output_voltage(self) -> float:
        self.calls.append("measure_output_voltage()")
        if self.simulate_timeout or self.simulate_voltage_readback_timeout:
            raise InstrumentTimeoutError("MockPSU: simulated voltage readback timeout")
        return self.voltage_V

    def measure_output_current(self) -> float:
        self.calls.append("measure_output_current()")
        if self.simulate_timeout or self.simulate_current_readback_timeout:
            raise InstrumentTimeoutError("MockPSU: simulated read timeout")
        return self.current_A

    def read_output_state(self) -> bool:
        self.calls.append("read_output_state()")
        if self.simulate_output_state_readback_timeout:
            raise InstrumentTimeoutError("MockPSU: simulated output state readback timeout")
        return self._output_is_on

    def read_regulation_state(self) -> dict[str, bool]:
        self.calls.append("read_regulation_state()")
        if self.simulate_regulation_state_readback_timeout:
            raise InstrumentTimeoutError("MockPSU: simulated regulation state readback timeout")
        # R14-B2: a driver SERIES-korlátja a mock saját módkódjával.
        if self.combination_mode == "SER":
            return {"CVCC_VALID": False, "ON": self.regulation_on}
        return {"CV": self.regulation_cv, "CC": self.regulation_cc, "ON": self.regulation_on}

    def query_combination_mode(self) -> str:
        """A valós driverrel (device_psu.py) egyező rövid kódot ad vissza
        ("OFF"/"SER"/"PAR") — korábban a teljes "INDEPENDENT"/"SERIES"/
        "PARALLEL" szót adta, ami eltért a valós SCPI-readback-visszaigazolás
        formátumától (teljes kódbázis review 1. kör, 2026-08-26, P1 lelet)."""
        self.calls.append("query_combination_mode()")
        if self.simulate_combination_mode_mismatch:
            return "OFF" if self.combination_mode != "OFF" else "SER"
        return self.actual_combination_mode

    def set_mode_independent(self) -> None:
        self.calls.append("set_mode_independent()")
        self.combination_mode = self.actual_combination_mode = "OFF"

    def set_mode_series(self) -> None:
        self.calls.append("set_mode_series()")
        self.combination_mode = self.actual_combination_mode = "SER"
        self.channel_enabled = {"CH1": True, "CH2": True}

    def set_mode_parallel(self) -> None:
        self.calls.append("set_mode_parallel()")
        self.combination_mode = self.actual_combination_mode = "PAR"

    def called(self, method: str) -> bool:
        """Segédfüggvény: adott metódus meghívódott-e?"""
        return any(method in c for c in self.calls)

    def reset_calls(self) -> None:
        self.calls.clear()
