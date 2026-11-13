"""
Ah/Wh numerikus integráló [R8, R14].
Áramirány konvenció: töltés +, kisütés -, relax 0.
"""
from dataclasses import dataclass, field

# Teljes kódbázis review 1. kör (2026-08-26), P2 lelet (CLAUDE.md §12: "Első
# és extrém dt_s kapjon helyes kezelést vagy quality flaget") — az első tick
# (dt_s≈0) kezelve van a hívó oldalon, de egy KIUGRÓAN NAGY dt_s (pl. hosszú
# blokkoló VISA I/O, GUI-thread torlódás, OS suspend utáni első tick) eddig
# csendben, teljes súllyal integrálódott. Ez a küszöb (5 perc) SZÁNDÉKOSAN
# kontextus-független, magas érték — a projektben SEHOL nincs olyan
# legitim, tervezett egyetlen mintaköz, ami ezt elérné (a leghosszabb
# ismert: CHEMID/OCV_SOC relax mintavétel ~30s), tehát nem ad hamis
# pozitívot normál működés mellett, csak valódi rendszerakadás esetén jelez.
_EXTREME_DT_S_THRESHOLD_S = 300.0


@dataclass
class Integrator:
    accumulated_charge_Ah: float = field(default=0.0, init=False)
    accumulated_discharge_Ah: float = field(default=0.0, init=False)
    accumulated_charge_Wh: float = field(default=0.0, init=False)
    accumulated_discharge_Wh: float = field(default=0.0, init=False)
    extreme_dt_s_count: int = field(default=0, init=False)
    max_observed_dt_s: float = field(default=0.0, init=False)

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

        self.max_observed_dt_s = max(self.max_observed_dt_s, dt_s)
        if dt_s > _EXTREME_DT_S_THRESHOLD_S:
            self.extreme_dt_s_count += 1

    def add_charge_delta(
        self,
        delta_charge_Ah: float,
        delta_charge_Wh: float,
        dt_s: float,
    ) -> None:
        """R1 B8a, F-20 (DFAM-035): egy MÁSIK integrátor (a composite
        controllerekbe ágyazott ChargeController saját, résztickenként
        integráló `_integrator`-a) által EZEN a ticken már elintegrált
        töltési Ah/Wh-delta átvétele. A szülő így nem integrál újra egy
        végmintából -- a gyermek integrálja az egyetlen numerikus forrás.

        Clamp SZÁNDÉKOSAN NINCS: a forrás-integrátor (add_sample) monoton
        nem csökkenő, tehát negatív delta csak hibából jöhet -- egy
        csendes max(..., 0) ezt elfedné (rules/20-metrology.md).

        A dt-diagnosztika (max_observed_dt_s, extreme_dt_s_count) az
        add_sample()-lel azonosan vezetődik, NULLA delta mellett is (pl.
        PRECHECK/PSU_PRESET vagy kapu-FAULT tick, ahol a gyermek nem
        integrált)."""
        self.accumulated_charge_Ah += delta_charge_Ah
        self.accumulated_charge_Wh += delta_charge_Wh

        self.max_observed_dt_s = max(self.max_observed_dt_s, dt_s)
        if dt_s > _EXTREME_DT_S_THRESHOLD_S:
            self.extreme_dt_s_count += 1

    def reset(self) -> None:
        self.accumulated_charge_Ah = 0.0
        self.accumulated_discharge_Ah = 0.0
        self.accumulated_charge_Wh = 0.0
        self.accumulated_discharge_Wh = 0.0
        self.extreme_dt_s_count = 0
        self.max_observed_dt_s = 0.0
