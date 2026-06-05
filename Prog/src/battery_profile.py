from dataclasses import dataclass
from Prog.src.exceptions import ProfileValidationError


@dataclass
class BatteryProfile:
    battery_name: str
    manufacturer: str
    model: str
    nominal_voltage_V: float = 12.0
    cell_count: int = 6
    nominal_capacity_Ah: float = 0.0
    chemistry: str = "AGM_VRLA"
    profile_source: str = ""
    charge_voltage_per_cell_V: float = 2.40
    terminate_voltage_per_cell_V: float = 1.80
    float_voltage_per_cell_V: float = 2.27
    max_charge_current_A: float | None = None
    taper_current_A: float | None = None
    temp_comp_mV_per_cell_per_degC: float = -2.5
    test_temperature_min_C: float = 15.0
    test_temperature_max_C: float = 30.0

    def __post_init__(self):
        self._validate()

    def _validate(self) -> None:
        if not self.model or not self.model.strip():
            raise ProfileValidationError(
                f"BatteryProfile.model mező kötelező (pl. 'FG20721'). "
                f"Kapott: {self.model!r}"
            )
        if self.nominal_capacity_Ah <= 0:
            raise ProfileValidationError(
                f"nominal_capacity_Ah > 0 kötelező. Kapott: {self.nominal_capacity_Ah}"
            )
        if not (0.1 <= self.nominal_capacity_Ah <= 1000):
            raise ProfileValidationError(
                f"nominal_capacity_Ah tartomány: 0.1–1000 Ah. "
                f"Kapott: {self.nominal_capacity_Ah}"
            )
        if not (2.00 <= self.charge_voltage_per_cell_V <= 2.60):
            raise ProfileValidationError(
                f"charge_voltage_per_cell_V tartomány: 2.00–2.60 V. "
                f"Kapott: {self.charge_voltage_per_cell_V}"
            )
        if not (1.60 <= self.terminate_voltage_per_cell_V <= 2.00):
            raise ProfileValidationError(
                f"terminate_voltage_per_cell_V tartomány: 1.60–2.00 V. "
                f"Kapott: {self.terminate_voltage_per_cell_V}"
            )
        if not (1 <= self.cell_count <= 24):
            raise ProfileValidationError(
                f"cell_count tartomány: 1–24. Kapott: {self.cell_count}"
            )

    # ------------------------------------------------------------------ #
    # Safety határok                                                       #
    # ------------------------------------------------------------------ #

    @property
    def batt_absolute_max_V_per_cell(self) -> float:
        return 2.425

    @property
    def batt_absolute_max_V(self) -> float:
        return round(self.batt_absolute_max_V_per_cell * self.cell_count, 6)

    # ------------------------------------------------------------------ #
    # Pack feszültségek                                                    #
    # ------------------------------------------------------------------ #

    @property
    def charge_voltage_pack_V(self) -> float:
        return self.charge_voltage_per_cell_V * self.cell_count

    @property
    def terminate_voltage_pack_V(self) -> float:
        return self.terminate_voltage_per_cell_V * self.cell_count

    @property
    def float_voltage_pack_V(self) -> float:
        return self.float_voltage_per_cell_V * self.cell_count

    # ------------------------------------------------------------------ #
    # C-ráta számítás [R2]                                                 #
    # FIAMM: 0.25 × nominal_capacity_Ah [A] — NEM 0.25 × C10 kisütőáram! #
    # ------------------------------------------------------------------ #

    @property
    def C5_discharge_current_A(self) -> float:
        return self.nominal_capacity_Ah / 5

    @property
    def C10_discharge_current_A(self) -> float:
        """Kisütőáram C/10 rátán — NEM azonos az effective_max_charge_A-val!"""
        return self.nominal_capacity_Ah / 10

    @property
    def C20_discharge_current_A(self) -> float:
        return self.nominal_capacity_Ah / 20

    @property
    def effective_max_charge_A(self) -> float:
        """FIAMM: 0.25 × nominal_capacity_Ah [A]"""
        if self.max_charge_current_A is not None:
            return self.max_charge_current_A
        return 0.25 * self.nominal_capacity_Ah

    @property
    def effective_taper_A(self) -> float:
        """FIAMM: 0.03 × nominal_capacity_Ah [A]"""
        if self.taper_current_A is not None:
            return self.taper_current_A
        return 0.03 * self.nominal_capacity_Ah

    # ------------------------------------------------------------------ #
    # Hőkompenzáció [N1]                                                   #
    # Clamp: [2.30, batt_absolute_max_V_per_cell] V/cella                 #
    # ------------------------------------------------------------------ #

    def compensated_charge_voltage_V(self, temperature_C: float) -> float:
        corrected = self.charge_voltage_per_cell_V + (
            self.temp_comp_mV_per_cell_per_degC / 1000.0
        ) * (temperature_C - 20.0)
        corrected = max(2.30, min(self.batt_absolute_max_V_per_cell, corrected))
        return corrected * self.cell_count
