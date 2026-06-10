"""
ReportGenerator — összefoglaló JSON riport kötelező mezőkkel.
[N13] MEASUREMENT_LIMITATIONS dedikált szekció.
[R1] relay_state TILOS a riportban.
[BY550] series_diode_part_number = "BY550"
"""
from __future__ import annotations


class ReportGenerator:

    def generate(self, session_meta: dict) -> dict:
        """
        Összefoglaló riportot állít elő session_meta dict-ből.
        Minden kötelező mező teljesítve a v1.2.1 / 4.20 szekció alapján.
        """
        report = {
            # PSU konfiguráció
            "psu_mode": session_meta.get("psu_mode", "UNKNOWN"),
            "psu_mode_max_voltage_V": session_meta.get("psu_mode_max_voltage_V"),
            "psu_mode_max_current_A": session_meta.get("psu_mode_max_current_A"),

            # Galvanikus leválasztás — [R1] relay_state tilos
            "no_galvanic_isolation": True,
            "isolation_method": "PSU_OUTPUT_OFF_ONLY",
            "galvanic_isolation_note": (
                "Nincs mechanikus relé. PSU output OFF szintű leválasztás."
            ),

            # BY550 soros dióda — [BY550]
            "series_diode_installed": True,
            "series_diode_type": "Silicon rectifier",
            "series_diode_part_number": "BY550",

            # Hőmérés
            "battery_temperature_source": "DMM2_surface_PT100",
            "ambient_temperature_C_source": session_meta.get(
                "ambient_temperature_C_source", None
            ),

            # Működési konfiguráció
            "temperature_compensation_mode": session_meta.get(
                "temperature_compensation_mode", "MONITOR_ONLY"
            ),
            "interrupted_session_recovered": session_meta.get(
                "interrupted_session_recovered", False
            ),
            "SOC_estimate_method": "coulomb_counting_from_measured_capacity",

            # OCV
            "ocv_isolation_method": "PSU_OUTPUT_OFF_ONLY",
            "ocv_accuracy_limitation": (
                "Az OCV mérés PSU_OUTPUT_OFF_ONLY leválasztás mellett történt "
                "(nincs galvanikus leválasztás). BY550 fordított szivárgás ~10 nA "
                "@30V (mért érték, 2026-05-31 lab session) — adatlap max ~10 µA."
            ),

            # [N13] Mérési korlátozások dedikált szekció
            "MEASUREMENT_LIMITATIONS": self._build_measurement_limitations(session_meta),
        }
        return report

    def _build_measurement_limitations(self, session_meta: dict) -> dict:
        """
        [N13] MEASUREMENT_LIMITATIONS szekció összeállítása.

        Implementáld ide a session_meta alapján feltöltött limitation dict-et.
        Kötelező mezők (v1.2.1 / 4.20):
          - current_measurement_source
          - no_external_calibrated_shunt
          - no_galvanic_isolation_during_ocv
          - temperature_source
          - capacity_result_quality
          - fallback_integration_duration_s
          - fallback_samples_count
          - communication_faults_count
          - emergency_stop_occurred
          - charge_Ah_accuracy_note
          - discharge_Ah_accuracy_note

        session_meta mezők amik ide kerülhetnek:
          session_meta["capacity_result_quality"]    → "OK" / "DEGRADED"
          session_meta["fallback_integration_duration_s"]
          session_meta["fallback_samples_count"]
          session_meta["communication_faults_count"]
          session_meta["emergency_stop_occurred"]    → bool
        """
        return {
            "current_measurement_source": "PSU_READBACK_OR_LOAD_READBACK",
            "no_external_calibrated_shunt": True,
            "no_galvanic_isolation_during_ocv": True,
            "temperature_source": "surface_DMM_not_internal",
            "capacity_result_quality": session_meta.get("capacity_result_quality", "OK"),
            "fallback_integration_duration_s": session_meta.get(
                "fallback_integration_duration_s", 0.0
            ),
            "fallback_samples_count": session_meta.get("fallback_samples_count", 0),
            "communication_faults_count": session_meta.get(
                "communication_faults_count", 0
            ),
            "emergency_stop_occurred": session_meta.get("emergency_stop_occurred", False),
            "charge_Ah_accuracy_note": (
                "PSU readback alapú; kis taper áramnál korlátozott pontosság."
            ),
            "discharge_Ah_accuracy_note": "Load readback alapú.",
            "rb_current_source": (
                "Rb kalkuláció terhelés beállított értéke (LOAD_SETPOINT) alapján, "
                "nem mért readback — kis eltérés lehetséges."
            ),
        }
