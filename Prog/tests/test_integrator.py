"""
Integrator unit tesztek — Ah/Wh numerikus integráló [R8, R14].
"""
from Prog.src.integrator import Integrator


class TestChargeIntegration:
    """[R12/R14] Töltés: signed_current_A pozitív → accumulated_charge_Ah nő."""

    def test_known_charge_integral(self):
        """100 minta × 1s × 1.4A = 0.38889 Ah"""
        ig = Integrator()
        for _ in range(100):
            ig.add_sample(signed_current_A=1.4, voltage_V=13.5, dt_s=1.0)
        expected = 1.4 * 100 / 3600
        assert abs(ig.accumulated_charge_Ah - expected) < 0.0001

    def test_charge_does_not_accumulate_discharge(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=1.4, voltage_V=13.5, dt_s=3600.0)
        assert abs(ig.accumulated_discharge_Ah - 0.0) < 1e-9

    def test_charge_wh_integration(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=1.4, voltage_V=14.0, dt_s=3600.0)
        expected_Wh = 1.4 * 14.0 * 1.0  # 1h × 1.4A × 14V
        assert abs(ig.accumulated_charge_Wh - expected_Wh) < 0.01


class TestDischargeIntegration:
    """[R14] Kisütés: signed_current_A negatív → accumulated_discharge_Ah nő."""

    def test_known_discharge_integral(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=-1.4, voltage_V=12.5, dt_s=3600.0)
        assert abs(ig.accumulated_discharge_Ah - 1.4) < 0.001

    def test_discharge_does_not_accumulate_charge(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=-1.4, voltage_V=12.5, dt_s=3600.0)
        assert abs(ig.accumulated_charge_Ah - 0.0) < 1e-9

    def test_discharge_wh_integration(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=-1.4, voltage_V=12.0, dt_s=3600.0)
        expected_Wh = 1.4 * 12.0 * 1.0
        assert abs(ig.accumulated_discharge_Wh - expected_Wh) < 0.01


class TestRelaxIntegration:
    """Relax: signed_current_A = 0 → semmi nem nő."""

    def test_zero_current_no_accumulation(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=0.0, voltage_V=13.0, dt_s=3600.0)
        assert ig.accumulated_charge_Ah == 0.0
        assert ig.accumulated_discharge_Ah == 0.0


class TestSignConvention:
    """[R14] Előjel konvenció: töltés +, kisütés -, relax 0."""

    def test_mixed_samples_separate_correctly(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=1.0, voltage_V=13.0, dt_s=3600.0)
        ig.add_sample(signed_current_A=-0.5, voltage_V=12.0, dt_s=3600.0)
        assert abs(ig.accumulated_charge_Ah - 1.0) < 0.001
        assert abs(ig.accumulated_discharge_Ah - 0.5) < 0.001

    def test_small_negative_current_not_counted_as_charge(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=-0.001, voltage_V=12.5, dt_s=3600.0)
        assert ig.accumulated_charge_Ah == 0.0

    def test_small_positive_current_not_counted_as_discharge(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=0.001, voltage_V=13.5, dt_s=3600.0)
        assert ig.accumulated_discharge_Ah == 0.0


class TestReset:
    def test_reset_clears_all_accumulators(self):
        ig = Integrator()
        ig.add_sample(1.4, 13.5, 3600.0)
        ig.reset()
        assert ig.accumulated_charge_Ah == 0.0
        assert ig.accumulated_discharge_Ah == 0.0
        assert ig.accumulated_charge_Wh == 0.0
        assert ig.accumulated_discharge_Wh == 0.0
