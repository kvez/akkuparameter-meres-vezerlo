"""
InstrumentManager — négy műszer egységes kezelése.
connect_all(), safe_all_off(), is_all_connected().
"""
from __future__ import annotations
from dataclasses import dataclass



@dataclass
class InstrumentConfig:
    psu_resource: str = "PLACEHOLDER"
    load_resource: str = "PLACEHOLDER"
    dmm_voltage_resource: str = "PLACEHOLDER"
    dmm_temperature_resource: str = "PLACEHOLDER"


class InstrumentManager:
    def __init__(self, psu, load, dmm_voltage, dmm_temperature):
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature

    @property
    def psu(self):
        return self._psu

    @property
    def load(self):
        return self._load

    @property
    def dmm_voltage(self):
        return self._dmm_v

    @property
    def dmm_temperature(self):
        return self._dmm_t

    def connect_all(self, config: InstrumentConfig) -> None:
        instruments = [
            (self._psu,   config.psu_resource),
            (self._load,  config.load_resource),
            (self._dmm_v, config.dmm_voltage_resource),
            (self._dmm_t, config.dmm_temperature_resource),
        ]
        connected: list = []
        try:
            for inst, resource in instruments:
                inst.connect(resource)
                connected.append(inst)
        except Exception:
            for inst in reversed(connected):
                try:
                    inst.disconnect()
                except Exception:
                    pass
            raise

    def disconnect_all(self) -> None:
        """Fordított connect sorrend: DMM_T → DMM_V → Load → PSU."""
        for inst in (self._dmm_t, self._dmm_v, self._load, self._psu):
            try:
                inst.disconnect()
            except Exception:
                pass

    def safe_all_off(self) -> None:
        """Sorrend: LOAD OFF → PSU OFF → DMM-ek (no-op). [R1] Relay nincs."""
        for instrument in (self._load, self._psu, self._dmm_v, self._dmm_t):
            try:
                instrument.safe_off()
            except Exception:
                pass

    def is_all_connected(self) -> bool:
        return all(
            i.is_connected()
            for i in (self._psu, self._load, self._dmm_v, self._dmm_t)
        )

    def read_all(self) -> dict:
        """Egyszerre olvas minden csatornából; hibás érték None."""
        result = {}
        for name, instrument, method in [
            ("battery_voltage_V", self._dmm_v, "read_voltage"),
            ("battery_temperature_C", self._dmm_t, "read_temperature"),
            ("psu_readback_voltage_V", self._psu, "measure_output_voltage"),
            ("psu_readback_current_A", self._psu, "measure_output_current"),
            ("load_readback_voltage_V", self._load, "measure_voltage"),
            ("load_readback_current_A", self._load, "measure_current"),
        ]:
            try:
                result[name] = getattr(instrument, method)()
            except Exception:
                result[name] = None
        return result
