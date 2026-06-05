"""
Keithley2380Load SCPI driver.
[N9] input_commanded_on nyilvántartás.
[ELLENŐRIZENDŐ] INPUT ON vs INP ON firmware — _detect_input_command() önellenőrzés.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

from Prog.src.exceptions import (
    InstrumentTimeoutError,
)


@dataclass
class Keithley2380Load:
    timeout_ms: int = 5000
    max_retries: int = 3

    _resource: Any = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _input_cmd: str = field(default="INP", init=False, repr=False)
    input_commanded_on: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ #
    # Kapcsolat                                                            #
    # ------------------------------------------------------------------ #

    def connect(self, resource_string: str, rm=None) -> None:
        if rm is None:
            import pyvisa
            rm = pyvisa.ResourceManager()
        self._resource = rm.open_resource(resource_string)
        self._resource.timeout = self.timeout_ms
        self._connected = True
        self._write("*CLS")
        self._detect_input_command()

    def _detect_input_command(self) -> None:
        """
        [IGAZOLVA] Keithley 2380 firmware: INP OFF (nem INPUT OFF).
        INP 0 és INP OFF egyaránt elfogadott a műszer által.
        """
        self._input_cmd = "INP"

    def disconnect(self) -> None:
        if self._resource is not None:
            try:
                self._resource.close()
            except Exception:
                pass
        self._resource = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._resource is not None

    # ------------------------------------------------------------------ #
    # Alapvető SCPI                                                        #
    # ------------------------------------------------------------------ #

    def idn(self) -> str:
        return self._query("*IDN?")

    def reset(self) -> None:
        self._write("*RST")

    def clear_status(self) -> None:
        self._write("*CLS")

    def check_error(self) -> list[str]:
        resp = self._query("SYST:ERR?")
        if resp.startswith("+0") or resp.startswith("0,"):
            return []
        return [resp.strip()]

    def safe_off(self) -> None:
        try:
            self.input_off()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Terhelés vezérlés                                                    #
    # ------------------------------------------------------------------ #

    def set_mode_cc(self) -> None:
        self._write("SOUR:FUNC CURR")

    def set_current(self, i: float) -> None:
        self._write(f"SOUR:CURR {i:.6g}")

    def input_on(self) -> None:
        self._write(f"{self._input_cmd} ON")
        self.input_commanded_on = True

    def input_off(self) -> None:
        self._write(f"{self._input_cmd} OFF")
        self.input_commanded_on = False

    # ------------------------------------------------------------------ #
    # Mérések                                                              #
    # ------------------------------------------------------------------ #

    def measure_voltage(self) -> float:
        return float(self._query("MEAS:VOLT?"))

    def measure_current(self) -> float:
        return float(self._query("MEAS:CURR?"))

    def measure_power(self) -> float:
        return float(self._query("MEAS:POW?"))

    # ------------------------------------------------------------------ #
    # Belső segédek                                                        #
    # ------------------------------------------------------------------ #

    def _write(self, cmd: str) -> None:
        last_err: Optional[Exception] = None
        for _ in range(self.max_retries):
            try:
                self._resource.write(cmd)
                return
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"Load write failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err

    def _query(self, cmd: str) -> str:
        last_err: Optional[Exception] = None
        for _ in range(self.max_retries):
            try:
                return self._resource.query(cmd)
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"Load query failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err
