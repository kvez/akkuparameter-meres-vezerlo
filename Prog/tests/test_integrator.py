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
        assert ig.extreme_dt_s_count == 0
        assert ig.max_observed_dt_s == 0.0


class TestExtremeDtSTracking:
    """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] CLAUDE.md §12:
    "Első és extrém dt_s kapjon helyes kezelést vagy quality flaget" — korábban
    egy kiugróan nagy dt_s (pl. hosszú rendszerakadás utáni első tick) csendben,
    teljes súllyal integrálódott, nyom nélkül."""

    def test_normal_dt_s_does_not_count_as_extreme(self):
        ig = Integrator()
        for _ in range(50):
            ig.add_sample(signed_current_A=1.0, voltage_V=13.0, dt_s=2.0)
        assert ig.extreme_dt_s_count == 0

    def test_legitimate_long_relax_sample_does_not_count_as_extreme(self):
        """A projekt leghosszabb ismert, tervezett egyetlen mintaköze (CHEMID/
        OCV_SOC relax mintavétel ~30s) NEM válthat ki hamis pozitívot."""
        ig = Integrator()
        ig.add_sample(signed_current_A=0.0, voltage_V=13.0, dt_s=30.0)
        assert ig.extreme_dt_s_count == 0

    def test_extreme_dt_s_increments_counter(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=1.0, voltage_V=13.0, dt_s=1.0)
        ig.add_sample(signed_current_A=1.0, voltage_V=13.0, dt_s=450.0)  # rendszerakadás
        assert ig.extreme_dt_s_count == 1

    def test_max_observed_dt_s_tracks_largest_sample(self):
        ig = Integrator()
        ig.add_sample(signed_current_A=1.0, voltage_V=13.0, dt_s=1.0)
        ig.add_sample(signed_current_A=1.0, voltage_V=13.0, dt_s=5.0)
        ig.add_sample(signed_current_A=1.0, voltage_V=13.0, dt_s=2.0)
        assert ig.max_observed_dt_s == 5.0

    def test_extreme_sample_still_integrates_normally(self):
        """A kiugró minta jelzése NEM helyettesíti/módosítja az integrációt —
        csak látható lesz, nem szűrődik ki csendben."""
        ig = Integrator()
        ig.add_sample(signed_current_A=1.0, voltage_V=13.0, dt_s=450.0)
        expected_Ah = 1.0 * 450.0 / 3600.0
        assert abs(ig.accumulated_charge_Ah - expected_Ah) < 1e-9


class TestAddChargeDelta:
    """[regresszió, R1 B8a, F-20/DFAM-035] add_charge_delta(): egy másik
    integrátor (a beágyazott ChargeController) által már elintegrált töltési
    Ah/Wh-delta átvétele -- clamp NÉLKÜL, a dt-diagnosztika az add_sample()-
    lel azonosan, nulla delta mellett is."""

    def test_delta_added_exactly_discharge_untouched(self):
        ig = Integrator()
        ig.add_charge_delta(0.0012345, 0.0177777, dt_s=3.75)
        ig.add_charge_delta(0.0001, 0.0014, dt_s=2.0)
        assert ig.accumulated_charge_Ah == 0.0012345 + 0.0001
        assert ig.accumulated_charge_Wh == 0.0177777 + 0.0014
        assert ig.accumulated_discharge_Ah == 0.0
        assert ig.accumulated_discharge_Wh == 0.0

    def test_negative_delta_is_not_clamped(self):
        """A forrás-integrátor monoton nem csökkenő -- negatív delta csak
        hibából jöhet; egy csendes max(..., 0) elfedné (l. a docstringet)."""
        ig = Integrator()
        ig.add_charge_delta(0.010, 0.140, dt_s=1.0)
        ig.add_charge_delta(-0.004, -0.056, dt_s=1.0)
        assert ig.accumulated_charge_Ah == 0.010 - 0.004
        assert ig.accumulated_charge_Wh == 0.140 - 0.056

    def test_zero_delta_still_records_dt_diagnostics(self):
        ig = Integrator()
        ig.add_charge_delta(0.0, 0.0, dt_s=7.0)
        assert ig.max_observed_dt_s == 7.0
        assert ig.extreme_dt_s_count == 0
        ig.add_charge_delta(0.0, 0.0, dt_s=400.0)
        assert ig.max_observed_dt_s == 400.0
        assert ig.extreme_dt_s_count == 1
        assert ig.accumulated_charge_Ah == 0.0
        assert ig.accumulated_charge_Wh == 0.0
