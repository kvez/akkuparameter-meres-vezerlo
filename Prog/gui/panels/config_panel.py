"""
ConfigPanel — SessionConfig dataclass + Qt konfiguráló panel.
SessionConfig: GUI által kitöltött paraméterek, validate() visszaadja a hibákat.
Qt widget: Task 4-ben kerül ide.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class SessionConfig:
    # Akkumulátor
    battery_profile_name: str = "FIAMM_12V"
    battery_model: str = ""
    nominal_capacity_ah: float = 0.0
    sample_id: str = ""

    # Műszerek
    psu_resource: str = ""
    load_resource: str = ""
    dmm_voltage_resource: str = ""
    dmm_temperature_resource: str = ""

    # PSU mód
    psu_mode: str = "INDEPENDENT"
    hardware_wiring_confirmed: bool = False

    # Teszt
    test_type: str = "CHARACTERIZATION"
    runner_tick_s: float = 2.0
    taper_hold_s: float = 600.0

    # Hőkompenzáció
    temperature_compensation_mode: str = "MONITOR_ONLY"

    def validate(self) -> list[str]:
        """Visszaadja a validációs hibaüzenetek listáját. Üres lista = OK."""
        errors: list[str] = []

        if self.nominal_capacity_ah <= 0:
            errors.append("nominal_capacity_ah > 0 kötelező")
        if not self.battery_model.strip():
            errors.append("battery_model nem lehet üres")
        if not self.psu_resource.strip():
            errors.append("psu_resource nem lehet üres")
        if not self.load_resource.strip():
            errors.append("load_resource nem lehet üres")
        if not self.dmm_voltage_resource.strip():
            errors.append("dmm_voltage_resource nem lehet üres")
        if not self.dmm_temperature_resource.strip():
            errors.append("dmm_temperature_resource nem lehet üres")
        if self.psu_mode in ("PARALLEL", "SERIES") and not self.hardware_wiring_confirmed:
            errors.append(
                f"hardware_wiring_confirmed = True kötelező {self.psu_mode} módban"
            )
        if self.battery_profile_name == "FIAMM_24V" and self.psu_mode != "SERIES":
            errors.append("FIAMM_24V (24V pack) csak SERIES PSU módban indítható")

        return errors
