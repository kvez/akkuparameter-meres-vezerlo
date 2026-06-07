"""
Ah/Wh numerikus integráló [R8, R14].
Áramirány konvenció: töltés +, kisütés -, relax 0.
"""
from dataclasses import dataclass, field


@dataclass
class Integrator:
    accumulated_charge_Ah: float = field(default=0.0, init=False)
    accumulated_discharge_Ah: float = field(default=0.0, init=False)
    accumulated_charge_Wh: float = field(default=0.0, init=False)
    accumulated_discharge_Wh: float = field(default=0.0, init=False)

    def add_sample(
        self,
        signed_current_A: float,
        voltage_V: float,
        dt_s: float,
    ) -> None:
        """
        Egy mintát integrál. [R14] Előjel konvenció:
          signed_current_A > 0: töltés (PSU readback)
          signed_current_A < 0: kisütés (Load readback, negatív előjellel)
          signed_current_A = 0: relax
        """
        charge_A = max(signed_current_A, 0.0)
        discharge_A = max(-signed_current_A, 0.0)

        self.accumulated_charge_Ah += charge_A * dt_s / 3600.0
        self.accumulated_discharge_Ah += discharge_A * dt_s / 3600.0
        self.accumulated_charge_Wh += charge_A * voltage_V * dt_s / 3600.0
        self.accumulated_discharge_Wh += discharge_A * voltage_V * dt_s / 3600.0

    def reset(self) -> None:
        self.accumulated_charge_Ah = 0.0
        self.accumulated_discharge_Ah = 0.0
        self.accumulated_charge_Wh = 0.0
        self.accumulated_discharge_Wh = 0.0
