"""
Keithley2220PSU SCPI driver.
Mód-agnosztikus output API [N7] — controller réteg CH1/CH2-t nem lát.
[N9] output_commanded_on nyilvántartás.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

from Prog.src.safety import PsuMode
from Prog.src.exceptions import (
    InstrumentTimeoutError,
)


@dataclass
class Keithley2220PSU:
    combination_mode: PsuMode = PsuMode.INDEPENDENT
    timeout_ms: int = 5000
    max_retries: int = 3

    _resource: Any = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _output_cmd: str = field(default="OUTP", init=False, repr=False)
    _last_cmd: str = field(default="", init=False, repr=False)
    output_commanded_on: bool = field(default=False, init=False)

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
        self._detect_output_command()

    def _detect_output_command(self) -> None:
        """
        [ELLENŐRIZENDŐ] OUTP OFF vs OUTP:ENAB OFF firmware különbség.
        Próbálja OUTP OFF-fal; ha kivétel, OUTP:ENAB OFF-ra vált.
        """
        try:
            self._write("OUTP OFF")
            self._output_cmd = "OUTP"
        except Exception:
            self._write("OUTP:ENAB OFF")
            self._output_cmd = "OUTP:ENAB"

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
        return [f"{resp.strip()} [last: {self._last_cmd}]"]

    def safe_off(self) -> None:
        try:
            self.all_outputs_off()
        except Exception:
            pass
        self.output_commanded_on = False

    # ------------------------------------------------------------------ #
    # Mód-agnosztikus output API [N7]                                     #
    # Controller réteg CSAK ezeket hívja — CH1/CH2 nem látható felfelé.  #
    # ------------------------------------------------------------------ #

    def set_output_voltage(self, v: float) -> None:
        self._select_ch1()
        self._write(f"SOUR:VOLT {v:.6g}")

    def set_output_current(self, i: float) -> None:
        self._select_ch1()
        self._write(f"SOUR:CURR {i:.6g}")

    def output_on(self) -> None:
        self._select_ch1()
        # OUTP:ENAB ON: csatorna-szintű engedélyezés — szükséges, ha CH1 korábban
        # front panel "Channel Enable" menüből volt tiltva (OUTP ON önmagában nem elég).
        self._write("OUTP:ENAB ON")
        self._write(f"{self._output_cmd} ON")
        self.output_commanded_on = True

    def output_off(self) -> None:
        self._write(f"{self._output_cmd} OFF")
        self.output_commanded_on = False

    def all_outputs_off(self) -> None:
        self._write(f"{self._output_cmd} OFF")
        self.output_commanded_on = False

    def measure_output_voltage(self) -> float:
        resp = self._query("MEAS:VOLT? CH1")
        return float(resp)

    def measure_output_current(self) -> float:
        resp = self._query("MEAS:CURR? CH1")
        return float(resp)

    # ------------------------------------------------------------------ #
    # Kombinációs mód                                                      #
    # ------------------------------------------------------------------ #

    def set_mode_independent(self) -> None:
        # Fw 1.15-1.05: INST:COMB:OFF → error 170. Helyes: SOUR:OUTP:SER/PAR OFF/ON.
        self._write("SOUR:OUTP:SER OFF")
        self._write("SOUR:OUTP:PAR OFF")
        self.combination_mode = PsuMode.INDEPENDENT

    def set_mode_series(self) -> None:
        self._write("SOUR:OUTP:PAR OFF")
        self._write("SOUR:OUTP:SER ON")
        self.combination_mode = PsuMode.SERIES

    def set_mode_parallel(self) -> None:
        self._write("SOUR:OUTP:SER OFF")
        self._write("SOUR:OUTP:PAR ON")
        self.combination_mode = PsuMode.PARALLEL

    def query_combination_mode(self) -> str:
        ser = self._query("SOUR:OUTP:SER?").strip()
        par = self._query("SOUR:OUTP:PAR?").strip()
        if ser == "1":
            return "SER"
        if par == "1":
            return "PAR"
        return "OFF"

    # ------------------------------------------------------------------ #
    # Belső segédek                                                        #
    # ------------------------------------------------------------------ #

    def _select_ch1(self) -> None:
        self._write("INST:SEL CH1")

    def _write(self, cmd: str) -> None:
        self._last_cmd = cmd
        last_err: Optional[Exception] = None
        for _ in range(self.max_retries):
            try:
                self._resource.write(cmd)
                return
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"PSU write failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err

    def _query(self, cmd: str) -> str:
        last_err: Optional[Exception] = None
        for _ in range(self.max_retries):
            try:
                return self._resource.query(cmd)
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"PSU query failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err
