"""
RelaxController — relaxációs várakozási időszak.
Állapotok: INIT → RELAXING → RELAX_DONE
dV/dt számítás opcionálisan korai kilépéshez (v1.2.1: alapból kikapcsolt).
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class RelaxState(Enum):
    INIT = "INIT"
    RELAXING = "RELAXING"
    RELAX_DONE = "RELAX_DONE"


@dataclass
class RelaxConfig:
    min_relax_s: float = 7200.0     # 2h töltés után; 18000s (5h) kisütés után
    dvdt_threshold_V_per_s: float = 0.0   # 0 = nincs korai kilépés dV/dt alapján
    sample_period_s: float = 30.0


class RelaxController:
    def __init__(self, dmm_voltage, config: RelaxConfig):
        self._dmm_v = dmm_voltage
        self._config = config
        self._state = RelaxState.INIT
        self._elapsed_s: float = 0.0
        self._last_voltage: float = 0.0
        self._dvdt: float = 0.0
        self._dmm_fault_count: int = 0
        self.on_event: Optional[Callable[[dict], None]] = None

    @property
    def state(self) -> RelaxState:
        return self._state

    @property
    def elapsed_s(self) -> float:
        return self._elapsed_s

    @property
    def dvdt_V_per_s(self) -> float:
        return self._dvdt

    def advance(self, dt_s: float) -> RelaxState:
        if self._state == RelaxState.RELAX_DONE:
            return self._state

        self._elapsed_s += dt_s

        if self._state == RelaxState.INIT:
            try:
                self._last_voltage = self._dmm_v.read_voltage()
            except Exception:
                self._dmm_fault_count += 1
                if self.on_event is not None:
                    self.on_event({
                        "event_code": "RELAX_DMM_FAULT",
                        "event_message": f"DMM olvasási hiba relaxáció INIT fázisában (hiba #{self._dmm_fault_count})",
                    })
            self._state = RelaxState.RELAXING
            return self._state

        # RELAXING
        try:
            v = self._dmm_v.read_voltage()
            if dt_s > 0:
                self._dvdt = (v - self._last_voltage) / dt_s
            self._last_voltage = v
        except Exception:
            self._dmm_fault_count += 1
            if self.on_event is not None:
                self.on_event({
                    "event_code": "RELAX_DMM_FAULT",
                    "event_message": f"DMM olvasási hiba relaxáció közben (hiba #{self._dmm_fault_count})",
                })

        if self._elapsed_s >= self._config.min_relax_s:
            self._state = RelaxState.RELAX_DONE

        return self._state
