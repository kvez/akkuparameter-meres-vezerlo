"""
Keysight34465ADMM SCPI driver.
DCV mód: DMM1 (feszültségmérés)
TEMP mód: DMM2 (4-wire PT100, FRTD)
[N8] NaN / Inf / overload (9.9E37) validáció.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional

from Prog.src.exceptions import (
    InstrumentTimeoutError,
    InstrumentInvalidReading,
)

_OVERLOAD_THRESHOLD = 1e30   # 9.9E37 overload kódhoz


@dataclass
class Keysight34465ADMM:
    timeout_ms: int = 10000
    max_retries: int = 3
    max_voltage_jump_V: float = 2.0     # [N8] max megengedett ugrás mintánként

    _resource: object = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _last_valid_voltage: Optional[float] = field(default=None, init=False, repr=False)

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
        pass  # DMM-nek nincs output — safe_off no-op

    # ------------------------------------------------------------------ #
    # DCV konfiguráció (DMM1 — feszültségmérés)                           #
    # ------------------------------------------------------------------ #

    def configure_dcv(self, range_V: float, nplc: float) -> None:
        self._write(f"CONF:VOLT:DC {range_V:.6g}")
        self._write(f"VOLT:DC:NPLC {nplc:.6g}")
        self._write(f"VOLT:DC:RANG {range_V:.6g}")
        self._write("VOLT:DC:RANG:AUTO OFF")
        self._last_valid_voltage = None  # reset jump detector

    def set_nplc(self, nplc: float) -> None:
        self._write(f"VOLT:DC:NPLC {nplc:.6g}")

    def autorange_off(self) -> None:
        self._write("VOLT:DC:RANG:AUTO OFF")

    def read_voltage(self) -> float:
        """
        [N8] Validált DCV olvasás.
        Kivétel: InstrumentInvalidReading — NaN, Inf, overload, nagy ugrás.
        """
        raw = self._query("READ?")
        v = self._parse_float(raw, context="voltage")
        self._validate_voltage(v)
        self._last_valid_voltage = v
        return v

    # ------------------------------------------------------------------ #
    # TEMP konfiguráció (DMM2 — 4-wire PT100, FRTD)                       #
    # SCPI igazolva: 34460-70-Manual.txt sor 6988                         #
    # ------------------------------------------------------------------ #

    def configure_temp_4wire_pt100(self, nplc: float = 10.0) -> None:
        self._write("CONF:TEMP FRTD,85")          # 85 = IEC 751 alfa=0.00385 kód
        self._write("SENS:TEMP:TRAN:FRTD:RES 100")  # PT100: R0=100 Ω
        self._write(f"SENS:TEMP:NPLC {nplc:.6g}")
        self._write("UNIT:TEMP C")

    def read_temperature(self) -> float:
        raw = self._query("READ?")
        return self._parse_float(raw, context="temperature")

    # ------------------------------------------------------------------ #
    # Belső segédek                                                        #
    # ------------------------------------------------------------------ #

    def _validate_voltage(self, v: float) -> None:
        """[N8] NaN / Inf / overload / jump ellenőrzés."""
        if math.isnan(v) or math.isinf(v):
            raise InstrumentInvalidReading(f"DMM: NaN vagy Inf feszültség: {v}")
        if abs(v) > _OVERLOAD_THRESHOLD:
            raise InstrumentInvalidReading(f"DMM: overload érték: {v:.3e}")
        if self._last_valid_voltage is not None:
            jump = abs(v - self._last_valid_voltage)
            if jump > self.max_voltage_jump_V:
                raise InstrumentInvalidReading(
                    f"DMM: feszültségugrás {jump:.3f}V > limit {self.max_voltage_jump_V}V "
                    f"(volt: {self._last_valid_voltage:.3f}V, most: {v:.3f}V)"
                )

    @staticmethod
    def _parse_float(raw: str, context: str = "") -> float:
        raw = raw.strip()
        if raw.upper() in ("NAN", "+NAN", "-NAN", "NAN\n"):
            raise InstrumentInvalidReading(f"DMM {context}: NaN válasz: {raw!r}")
        if raw.upper() in ("INF", "+INF", "-INF", "INFINITY"):
            raise InstrumentInvalidReading(f"DMM {context}: Inf válasz: {raw!r}")
        try:
            return float(raw)
        except ValueError as e:
            raise InstrumentInvalidReading(
                f"DMM {context}: nem értelmezhető válasz: {raw!r}"
            ) from e

    def _write(self, cmd: str) -> None:
        last_err: Optional[Exception] = None
        for _ in range(self.max_retries):
            try:
                self._resource.write(cmd)
                return
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"DMM write failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err

    def _query(self, cmd: str) -> str:
        last_err: Optional[Exception] = None
        for _ in range(self.max_retries):
            try:
                return self._resource.query(cmd)
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"DMM query failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err
