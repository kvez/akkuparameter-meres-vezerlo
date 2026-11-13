"""
MockLoad — determinisztikus szimulált elektronikus terhelés teszteléshez.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from collections.abc import Callable
import time
from Prog.drivers.device_load import _QUESTIONABLE_STATUS_BITS
from Prog.src.exceptions import InstrumentTimeoutError, InstrumentError


@dataclass
class MockLoad:
    # Programozható állapot
    voltage_V: float = 12.0
    current_A: float = 0.0
    power_W: float = 0.0
    _input_is_on: bool = False           # NEM input_on — névütközés elkerülése
    input_commanded_on: bool = False    # tükrözi a valós driver attribútumát
    input_on_commanded_at: float | None = field(default=None, init=False)
    simulate_timeout: bool = False
    simulate_current_readback_failure: bool = False
    # Review-kör 5, Hardver/Műszervezérlés HW5-03 (P3) -- l. mock_psu.py
    # read_output_state() tükrének azonos hivatkozású kommentje.
    simulate_input_state_readback_timeout: bool = False
    # Review-kör 5, Hardver/Műszervezérlés HW4-01 (P3): a valós
    # device_load.py::read_questionable_status() tükre -- alapból minden
    # bit False (nincs aktív védelmi/állapot-jelzés), a teszt a névvel
    # indexelt set-tel ({"OC", "PS"} stb.) jelölheti ki, mely bitek
    # legyenek aktívak.
    questionable_status_active_bits: frozenset[str] = field(default_factory=frozenset)
    simulate_questionable_status_readback_timeout: bool = False
    simulate_power_limit: bool = False
    power_limit_W: float = 60.0
    raise_on_connect: bool = False
    raise_on_safe_off: bool = False

    # Feszültség csökkentési szimulációhoz
    voltage_discharge_slope_V_per_Ah: float = 0.5

    # RB Pulse Digitalizálás spec (2026-08-22): hardveres tranziens PULSE +
    # trace buffer szimulációja. `trace_response` a fetch_trace() válaszát
    # adja (nyers SCPI CSV-string, a hívó parszolja) - alapértelmezésben
    # üres string (a hívónak explicit be kell állítania a teszt elvárt
    # trace-adatát, mint ahogy a MockDMM.timed_voltage_fn-nél is szokás).
    trace_response: str = ""
    simulate_trace_error: bool = False
    simulate_fire_error: bool = False

    # Review-kör 5, Hardver/Műszervezérlés HW5-05 (P3) -- l. mock_dmm.py
    # azonos hivatkozású mezőjének kommentje ugyanerről az elvről.
    serial: str = ""
    calls: list[str] = field(default_factory=list)
    pending_errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.serial:
            self.serial = f"SN{id(self):012X}"

    def connect(self, resource: str) -> None:
        self.calls.append(f"connect({resource!r})")
        if self.raise_on_connect:
            raise InstrumentError("MockLoad: simulated connect failure")

    def disconnect(self) -> None:
        self.calls.append("disconnect()")

    def idn(self) -> str:
        self.calls.append("idn()")
        # *IDN? formátum: <gyártó>,<modell>,<sorozatszám>,<firmware> -- l.
        # mock_dmm.py::idn() azonos hivatkozású kommentje.
        return f"KEITHLEY INSTRUMENTS,MODEL 2380-120-60,{self.serial},1.0"

    def reset(self) -> None:
        self.calls.append("reset()")

    def clear_status(self) -> None:
        self.calls.append("clear_status()")

    def check_error(self, clear_first: bool = True) -> list[str]:
        self.calls.append("check_error()")
        if self.pending_errors:
            return [self.pending_errors.pop(0)]
        return []

    def is_connected(self) -> bool:
        return True

    def safe_off(self) -> bool:
        self.calls.append("safe_off()")
        if self.raise_on_safe_off:
            # Kör 17 MAX-effort review, R17-21 (DFAM-005+DFAM-015): a fenti
            # komment korábban azt állította, hogy input_commanded_on
            # SZÁNDÉKOSAN változatlan marad -- ez HAMIS volt a valós
            # device_load.py::safe_off()-hoz képest, ami a try/except UTÁN
            # FELTÉTEL NÉLKÜL nullázza a flaget (input_off() maga is
            # try/finally-ben teszi, CF-13/HW-R1-04) -- ugyanaz az elv, mint
            # mock_psu.py::safe_off()-nál.
            self.input_commanded_on = False
            self.input_on_commanded_at = None
            return False
        try:
            self.input_off()
            return True
        except Exception:
            return False

    def set_mode_cc(self) -> None:
        self.calls.append("set_mode_cc()")

    def set_current(self, i: float) -> None:
        self.calls.append(f"set_current({i})")
        self.current_A = i
        if self._input_is_on:
            self.power_W = self.voltage_V * self.current_A

    def input_on(
        self, clear_first: bool = True, *, wait_for_ack: bool = True,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.calls.append("input_on()")
        self.input_on_commanded_at = time.monotonic()
        self._input_is_on = True
        self.input_commanded_on = True
        self.power_W = self.voltage_V * self.current_A
        if clear_first and wait_for_ack:
            # A mock normál ON-ja azonnal nyugtázott, mint a sikeres driverhívás.
            self.input_on_commanded_at = None

    def input_off(self) -> None:
        try:
            self.calls.append("input_off()")
            self._input_is_on = False
            self.power_W = 0.0
        finally:
            self.input_commanded_on = False
            self.input_on_commanded_at = None

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

    def read_input_state(self) -> bool:
        self.calls.append("read_input_state()")
        if self.simulate_input_state_readback_timeout:
            raise InstrumentTimeoutError("MockLoad: simulated input state readback timeout")
        if self._input_is_on and self.input_commanded_on:
            self.input_on_commanded_at = None
        return self._input_is_on

    def read_questionable_status(self) -> dict[str, bool]:
        self.calls.append("read_questionable_status()")
        if self.simulate_questionable_status_readback_timeout:
            raise InstrumentTimeoutError("MockLoad: simulated questionable status readback timeout")
        return {
            name: name in self.questionable_status_active_bits
            for name in _QUESTIONABLE_STATUS_BITS.values()
        }

    def configure_transient_pulse(
        self, pulse_A: float, baseline_A: float, pulse_width_s: float,
    ) -> None:
        self.calls.append(
            f"configure_transient_pulse(pulse_A={pulse_A}, baseline_A={baseline_A}, "
            f"pulse_width_s={pulse_width_s})"
        )

        self.transient_pulse_A = pulse_A
        self.transient_baseline_A = baseline_A

    def read_transient_current_levels(self) -> tuple[float, float]:
        self.calls.append("read_transient_current_levels()")
        # DFAM-006: független, 0,1 mA-es tesztrács; nem .6g-visszhang.
        # Csak rácsra eső bemenetet használunk; hardverkvantálás HIL_PENDING.
        return round(self.transient_pulse_A, 4), round(self.transient_baseline_A, 4)

    def fire_transient_pulse(self, clear_first: bool = True) -> None:
        self.calls.append("fire_transient_pulse()")
        if self.simulate_fire_error:
            raise InstrumentTimeoutError("MockLoad: simulated fire_transient_pulse failure")

    def configure_trace(
        self, feed: str = "CURR", interval_s: float = 0.001, points: int = 1000,
    ) -> None:
        self.calls.append(f"configure_trace(feed={feed!r}, interval_s={interval_s}, points={points})")

    def fetch_trace(self) -> str:
        self.calls.append("fetch_trace()")
        if self.simulate_trace_error:
            raise InstrumentTimeoutError("MockLoad: simulated fetch_trace failure")
        return self.trace_response

    def simulate_discharge_step(self, removed_Ah: float) -> None:
        """Kisütési szimulációhoz: csökkenti a feszültséget az eltávolított Ah arányában."""
        self.voltage_V -= self.voltage_discharge_slope_V_per_Ah * removed_Ah

    def called(self, method: str) -> bool:
        return any(method in c for c in self.calls)

    def reset_calls(self) -> None:
        self.calls.clear()


class StatefulLoadRangeResource:
    """R16-B9 SCPI-double; nem HIL. A plafon/szintek Clear és close után is élnek.

    A csökkentéskori vágás és a 120 modellje a K0 JSON-on alapul:
    SN 802436052777870003, fw 1.00-1.00, CC/FIX, INPUT OFF.
    A BLEV-túllépés analóg modell, külön fizikai bizonyíték nélkül.
    """

    def __init__(self, *, deferred=False):
        self.ceiling = 0.01
        self.levels = {"SOUR:CURR": 0.01, "CURR:TRAN:ALEV": 0.01, "CURR:TRAN:BLEV": 0.01}
        self.input_on = False
        self.deferred = deferred
        self.pending = None
        self.errors = []
        self.log = []
        self.timeout_on = set()
        self.reject = set()
        self.replies = {}
        self.capturing = False
        self.clears_during_capture = 0
        self.points = 1000
        self.timeout = 5000
        self.closed = False

    def clear(self):
        self.log.append(("DCL", ""))
        self.clears_during_capture += int(self.capturing)
        self.capturing = False
        self.pending = None

    def close(self):
        self.closed = True

    @staticmethod
    def _units(message):
        # A root-specifier hiánya hibás lenne a SCPI head-path miatt.
        parts = message.split(";")
        assert all(p.startswith(("*", ":")) for p in parts[1:])
        return [p.lstrip(":") for p in parts]

    def _check_timeout(self, message):
        if self.timeout_on.intersection(self._units(message)):
            raise TimeoutError("szimulált OPC/transport timeout")

    def _execute(self, unit):
        head, _, arg = unit.partition(" ")
        if unit in self.reject:
            self.errors.append('120,"Parameter overflowed"')
        elif head == "*CLS":
            self.errors.clear()
        elif head == "CURR:RANG":
            self.ceiling = float(arg)
            self.levels = {k: min(v, self.ceiling) for k, v in self.levels.items()}
        elif head in self.levels:
            value = float(arg)
            if value > self.ceiling:
                self.errors.append('120,"Parameter overflowed"')
            else:
                self.levels[head] = value
        elif head == "INP":
            self.input_on = arg in ("ON", "1")
        elif head == "TRAC:POIN":
            self.points = int(arg)
        elif head == "TRAC:FEED:CONT":
            self.capturing = arg == "NEXT"

    def _flush(self):
        if self.pending is not None:
            for unit in self._units(self.pending):
                self._execute(unit)
            self.pending = None

    def write(self, message):
        self.log.append(("W", message))
        self._check_timeout(message)
        self._flush()
        if self.deferred:
            self.pending = message
        else:
            for unit in self._units(message):
                self._execute(unit)

    def query(self, message):
        self.log.append(("Q", message))
        self._check_timeout(message)
        self._flush()
        answer = "1"
        for unit in self._units(message):
            if not unit.endswith("?"):
                self._execute(unit)
            elif unit in self.replies:
                answer = self.replies[unit]
            elif unit[:-1] in self.levels:
                answer = str(self.levels[unit[:-1]])
            elif unit == "CURR:RANG?":
                answer = str(self.ceiling)
            elif unit == "INP?":
                answer = str(int(self.input_on))
            elif unit in ("INP:SHOR?", "INP:TIM?"):
                answer = "0"
            elif unit == "SYST:ERR?":
                answer = self.errors.pop(0) if self.errors else '0,"No error"'
            elif unit == "TRAC:DATA?":
                assert self.capturing
                self.capturing = False
                answer = ",".join([str(self.levels["CURR:TRAN:ALEV"])] * self.points)
            else:
                assert unit == "*OPC?", unit
        return answer
