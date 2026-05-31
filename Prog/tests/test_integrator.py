"""
Integrator unit tesztek — Ah/Wh numerikus integráló [R8, R14].
"""
import pytest
from Prog.src.integrator import Integrator


class TestChargeIntegration:
    """[R12/R14] Töltés: signed_current_A pozitív → accumulated_charge_Ah nő."""

    def test_known_charge_integral(self):
        """100 minta × 1s × 1.4A = 0.38889 Ah"""
        ig = Integrator()
        for _ in range(100):
            ig.add_sample(signed_current_A=1.4, voltage_V=13.5, dt_s=1.0,
                          source="PSU_READBACK")
        expected = 1.4 * 100 / 3600
        assert abs(ig.accumulated_charge_Ah - expected) < 0.0001

    def test_charge_does_not_accumulate_discharge(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=1.4, voltage_V=13.5, dt_s=3600.0,
                      source="PSU_READBACK")
        assert abs(ig.accumulated_discharge_Ah - 0.0) < 1e-9

    def test_charge_wh_integration(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=1.4, voltage_V=14.0, dt_s=3600.0,
                      source="PSU_READBACK")
        expected_Wh = 1.4 * 14.0 * 1.0  # 1h × 1.4A × 14V
        assert abs(ig.accumulated_charge_Wh - expected_Wh) < 0.01


class TestDischargeIntegration:
    """[R14] Kisütés: signed_current_A negatív → accumulated_discharge_Ah nő."""

    def test_known_discharge_integral(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=-1.4, voltage_V=12.5, dt_s=3600.0,
                      source="LOAD_READBACK")
        assert abs(ig.accumulated_discharge_Ah - 1.4) < 0.001

    def test_discharge_does_not_accumulate_charge(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=-1.4, voltage_V=12.5, dt_s=3600.0,
                      source="LOAD_READBACK")
        assert abs(ig.accumulated_charge_Ah - 0.0) < 1e-9

    def test_discharge_wh_integration(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=-1.4, voltage_V=12.0, dt_s=3600.0,
                      source="LOAD_READBACK")
        expected_Wh = 1.4 * 12.0 * 1.0
        assert abs(ig.accumulated_discharge_Wh - expected_Wh) < 0.01


class TestRelaxIntegration:
    """Relax: signed_current_A = 0 → semmi nem nő."""

    def test_zero_current_no_accumulation(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=0.0, voltage_V=13.0, dt_s=3600.0,
                      source="ZERO")
        assert ig.accumulated_charge_Ah == 0.0
        assert ig.accumulated_discharge_Ah == 0.0


class TestSignConvention:
    """[R14] Előjel konvenció: töltés +, kisütés -, relax 0."""

    def test_mixed_samples_separate_correctly(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=1.0, voltage_V=13.0, dt_s=3600.0,
                      source="PSU_READBACK")
        ig.add_sample(signed_current_A=-0.5, voltage_V=12.0, dt_s=3600.0,
                      source="LOAD_READBACK")
        assert abs(ig.accumulated_charge_Ah - 1.0) < 0.001
        assert abs(ig.accumulated_discharge_Ah - 0.5) < 0.001

    def test_small_negative_current_not_counted_as_charge(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=-0.001, voltage_V=12.5, dt_s=3600.0,
                      source="LOAD_READBACK")
        assert ig.accumulated_charge_Ah == 0.0

    def test_small_positive_current_not_counted_as_discharge(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=0.001, voltage_V=13.5, dt_s=3600.0,
                      source="PSU_READBACK")
        assert ig.accumulated_discharge_Ah == 0.0


class TestFallback:
    """[N5] SETPOINT_FALLBACK számlálás és quality degradálódás."""

    def test_fallback_samples_counted(self):
        ig = Integrator()
        ig.add_sample(1.0, 13.5, 5.0, source="SETPOINT_FALLBACK")
        ig.add_sample(1.0, 13.5, 5.0, source="SETPOINT_FALLBACK")
        assert ig.fallback_elapsed_s == 10.0
        assert ig.fallback_samples == 2

    def test_normal_samples_not_counted_as_fallback(self):
        ig = Integrator()
        ig.add_sample(1.0, 13.5, 5.0, source="PSU_READBACK")
        assert ig.fallback_elapsed_s == 0.0
        assert ig.fallback_samples == 0

    def test_quality_ok_without_fallback(self):
        ig = Integrator()
        ig.add_sample(1.0, 13.5, 5.0, source="PSU_READBACK")
        assert ig.capacity_result_quality == "OK"

    def test_quality_degraded_after_fallback_timeout(self):
        ig = Integrator(fallback_max_duration_s=30.0)
        for _ in range(7):
            ig.add_sample(1.0, 13.5, 5.0, source="SETPOINT_FALLBACK")
        assert ig.fallback_elapsed_s == 35.0
        assert ig.capacity_result_quality == "DEGRADED"

    def test_integration_invalid_after_fallback_timeout(self):
        ig = Integrator(fallback_max_duration_s=30.0)
        for _ in range(7):
            ig.add_sample(1.0, 13.5, 5.0, source="SETPOINT_FALLBACK")
        assert ig.integration_valid is False


class TestReset:
    def test_reset_clears_all_accumulators(self):
        ig = Integrator()
        ig.add_sample(1.4, 13.5, 3600.0, source="PSU_READBACK")
        ig.reset()
        assert ig.accumulated_charge_Ah == 0.0
        assert ig.accumulated_discharge_Ah == 0.0
        assert ig.accumulated_charge_Wh == 0.0
        assert ig.accumulated_discharge_Wh == 0.0
        assert ig.fallback_elapsed_s == 0.0
        assert ig.fallback_samples == 0
        assert ig.capacity_result_quality == "OK"
        assert ig.integration_valid is True
