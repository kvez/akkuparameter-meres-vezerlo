"""
Ah/Wh numerikus integráló [R8, R14].
Áramirány konvenció: töltés +, kisütés -, relax 0.
[N5] SETPOINT_FALLBACK időkorlát és DEGRADED quality flag.
"""
from dataclasses import dataclass, field


@dataclass
class Integrator:
    fallback_max_duration_s: float = 30.0

    accumulated_charge_Ah: float = field(default=0.0, init=False)
    accumulated_discharge_Ah: float = field(default=0.0, init=False)
    accumulated_charge_Wh: float = field(default=0.0, init=False)
    accumulated_discharge_Wh: float = field(default=0.0, init=False)
    fallback_elapsed_s: float = field(default=0.0, init=False)
    fallback_samples: int = field(default=0, init=False)
    capacity_result_quality: str = field(default="OK", init=False)
    integration_valid: bool = field(default=True, init=False)

    def add_sample(
        self,
        signed_current_A: float,
        voltage_V: float,
        dt_s: float,
        source: str = "PSU_READBACK",
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

        if source == "SETPOINT_FALLBACK":
            self.fallback_elapsed_s += dt_s
            self.fallback_samples += 1
            if self.fallback_elapsed_s > self.fallback_max_duration_s:
                self.integration_valid = False
                self.capacity_result_quality = "DEGRADED"

    def reset(self) -> None:
        self.accumulated_charge_Ah = 0.0
        self.accumulated_discharge_Ah = 0.0
        self.accumulated_charge_Wh = 0.0
        self.accumulated_discharge_Wh = 0.0
        self.fallback_elapsed_s = 0.0
        self.fallback_samples = 0
        self.capacity_result_quality = "OK"
        self.integration_valid = True
