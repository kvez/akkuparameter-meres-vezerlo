import pytest
from Prog.src.exceptions import ProfileValidationError
from Prog.src.battery_profile import BatteryProfile


def make_profile(**kwargs) -> BatteryProfile:
    defaults = dict(
        battery_name="Teszt akku",
        manufacturer="FIAMM",
        model="FG20721",
        nominal_capacity_Ah=7.0,
        cell_count=6,
        nominal_voltage_V=12.0,
    )
    defaults.update(kwargs)
    return BatteryProfile(**defaults)  # type: ignore[arg-type]


class TestCRateCalculation:
    """[R2] C-ráta: 0.25 * nominal_capacity_Ah — NEM 0.25 * C10_discharge_current_A"""

    def test_effective_max_charge_7ah(self):
        p = make_profile(nominal_capacity_Ah=7.0)
        assert abs(p.effective_max_charge_A - 1.75) < 0.001

    def test_effective_max_charge_18ah(self):
        p = make_profile(nominal_capacity_Ah=18.0)
        assert abs(p.effective_max_charge_A - 4.50) < 0.001

    def test_effective_max_charge_6ah(self):
        p = make_profile(nominal_capacity_Ah=6.0)
        assert abs(p.effective_max_charge_A - 1.50) < 0.001

    def test_effective_taper_7ah(self):
        p = make_profile(nominal_capacity_Ah=7.0)
        assert abs(p.effective_taper_A - 0.21) < 0.001

    def test_effective_taper_18ah(self):
        p = make_profile(nominal_capacity_Ah=18.0)
        assert abs(p.effective_taper_A - 0.54) < 0.001

    def test_c10_discharge_current_not_same_as_charge_current(self):
        """Tízszeres különbség — ez a v1.1 kritikus hibájának regressziós tesztje."""
        p = make_profile(nominal_capacity_Ah=7.0)
        assert abs(p.C10_discharge_current_A - 0.70) < 0.001
        assert abs(p.effective_max_charge_A - 1.75) < 0.001
        assert p.C10_discharge_current_A != pytest.approx(p.effective_max_charge_A)

    def test_c5_discharge_current(self):
        p = make_profile(nominal_capacity_Ah=7.0)
        assert abs(p.C5_discharge_current_A - 1.40) < 0.001

    def test_c20_discharge_current(self):
        p = make_profile(nominal_capacity_Ah=7.0)
        assert abs(p.C20_discharge_current_A - 0.35) < 0.001

    def test_custom_max_charge_overrides_auto(self):
        p = make_profile(nominal_capacity_Ah=7.0, max_charge_current_A=1.40)
        assert abs(p.effective_max_charge_A - 1.40) < 0.001

    def test_custom_taper_overrides_auto(self):
        p = make_profile(nominal_capacity_Ah=7.0, taper_current_A=0.15)
        assert abs(p.effective_taper_A - 0.15) < 0.001


class TestPackVoltages:
    def test_charge_voltage_pack_12v(self):
        p = make_profile(cell_count=6, charge_voltage_per_cell_V=2.40)
        assert abs(p.charge_voltage_pack_V - 14.40) < 0.001

    def test_terminate_voltage_pack_12v(self):
        p = make_profile(cell_count=6, terminate_voltage_per_cell_V=1.80)
        assert abs(p.terminate_voltage_pack_V - 10.80) < 0.001

    def test_float_voltage_pack_12v(self):
        p = make_profile(cell_count=6, float_voltage_per_cell_V=2.27)
        assert abs(p.float_voltage_pack_V - 13.62) < 0.001

    def test_charge_voltage_pack_24v(self):
        p = make_profile(cell_count=12, charge_voltage_per_cell_V=2.40)
        assert abs(p.charge_voltage_pack_V - 28.80) < 0.001

    def test_terminate_voltage_pack_24v(self):
        p = make_profile(cell_count=12, terminate_voltage_per_cell_V=1.80)
        assert abs(p.terminate_voltage_pack_V - 21.60) < 0.001


class TestSafetyLimits:
    def test_batt_absolute_max_v_per_cell(self):
        p = make_profile()
        assert abs(p.batt_absolute_max_V_per_cell - 2.425) < 0.001

    def test_batt_absolute_max_v_12v_pack(self):
        p = make_profile(cell_count=6)
        assert abs(p.batt_absolute_max_V - 14.55) < 0.001

    def test_batt_absolute_max_v_24v_pack(self):
        p = make_profile(cell_count=12)
        assert abs(p.batt_absolute_max_V - 29.10) < 0.001


class TestTemperatureCompensation:
    """[N1] A kompenzált célfeszültség soha nem haladhatja meg a batt_absolute_max_V-t."""

    def test_at_20c_no_correction(self):
        p = make_profile(cell_count=6, charge_voltage_per_cell_V=2.40)
        assert abs(p.compensated_charge_voltage_V(20.0) - 14.40) < 0.001

    def test_at_30c_lower_target(self):
        p = make_profile(cell_count=6, charge_voltage_per_cell_V=2.40)
        # 2.40 + (-0.0025)*(30-20) = 2.375 V/cell → 6*2.375 = 14.25 V
        assert abs(p.compensated_charge_voltage_V(30.0) - 14.25) < 0.001

    def test_at_15c_slightly_higher(self):
        p = make_profile(cell_count=6, charge_voltage_per_cell_V=2.40)
        # 2.40 + (-0.0025)*(15-20) = 2.4125 V/cell → 6*2.4125 = 14.475 V
        assert abs(p.compensated_charge_voltage_V(15.0) - 14.475) < 0.001

    def test_clamp_never_exceeds_safety_limit_at_minus_10c(self):
        """[N1] -10°C: corrected=2.4775 → clamped=2.425 → pack=14.55 V"""
        p = make_profile(cell_count=6)
        target = p.compensated_charge_voltage_V(-10.0)
        assert target <= p.batt_absolute_max_V + 1e-6

    def test_clamp_never_exceeds_safety_limit_at_minus_20c(self):
        """[N1] -20°C: corrected=2.50 → clamped=2.425 → pack=14.55 V (volt: 2.50→15.00V bug)"""
        p = make_profile(cell_count=6)
        target = p.compensated_charge_voltage_V(-20.0)
        assert target <= p.batt_absolute_max_V + 1e-6

    def test_lower_clamp_at_extreme_heat(self):
        """Extrém magas hőmérsékleten 2.30 V/cella minimumot tart."""
        p = make_profile(cell_count=6)
        target = p.compensated_charge_voltage_V(100.0)
        assert target >= p.cell_count * 2.30 - 1e-6

    def test_clamp_24v_pack(self):
        """24V pack: clamp 12*2.425 = 29.10 V-nál"""
        p = make_profile(cell_count=12)
        target = p.compensated_charge_voltage_V(-20.0)
        assert target <= p.batt_absolute_max_V + 1e-6


class TestValidation:
    def test_empty_model_raises(self):
        with pytest.raises(ProfileValidationError, match="model"):
            make_profile(model="")

    def test_whitespace_model_raises(self):
        with pytest.raises(ProfileValidationError, match="model"):
            make_profile(model="   ")

    def test_zero_capacity_raises(self):
        with pytest.raises(ProfileValidationError):
            make_profile(nominal_capacity_Ah=0.0)

    def test_negative_capacity_raises(self):
        with pytest.raises(ProfileValidationError):
            make_profile(nominal_capacity_Ah=-5.0)

    def test_charge_voltage_too_high_raises(self):
        with pytest.raises(ProfileValidationError):
            make_profile(charge_voltage_per_cell_V=3.0)

    def test_charge_voltage_too_low_raises(self):
        with pytest.raises(ProfileValidationError):
            make_profile(charge_voltage_per_cell_V=1.5)

    def test_terminate_voltage_out_of_range_raises(self):
        with pytest.raises(ProfileValidationError):
            make_profile(terminate_voltage_per_cell_V=1.0)

    def test_valid_profile_does_not_raise(self):
        p = make_profile()
        assert p is not None

    def test_valid_24v_profile_does_not_raise(self):
        p = make_profile(cell_count=12, nominal_voltage_V=24.0, nominal_capacity_Ah=18.0,
                         model="FGH21803")
        assert p is not None


class TestC10CapacityField:
    """[K-1] FIAMM Boost charge spec: 0.25 C10 max áram, 0.03 C10 taper stop.
    FG akkuknál nominal = C20, ezért C10_capacity_Ah külön mező kell.
    Engineering Manual line 907-909: "0.25 C10" és "0.03 C10" az Ah kapacitás %-a."""

    def test_effective_taper_uses_c10_capacity_when_specified(self):
        """FG20721: C20=7.2Ah, C10=6.7Ah → taper = 0.03 × 6.7 = 0.201A"""
        p = make_profile(nominal_capacity_Ah=7.2, C10_capacity_Ah=6.7)
        assert abs(p.effective_taper_A - 0.201) < 0.001

    def test_effective_max_charge_uses_c10_capacity_when_specified(self):
        """FG20721: C20=7.2Ah, C10=6.7Ah → max charge = 0.25 × 6.7 = 1.675A"""
        p = make_profile(nominal_capacity_Ah=7.2, C10_capacity_Ah=6.7)
        assert abs(p.effective_max_charge_A - 1.675) < 0.001

    def test_effective_taper_falls_back_to_nominal_when_c10_not_set(self):
        """Ha nincs C10_capacity_Ah megadva, fallback: 0.03 × nominal (FGH: nominal=C10)"""
        p = make_profile(nominal_capacity_Ah=7.0)
        assert abs(p.effective_taper_A - 0.21) < 0.001

    def test_effective_max_charge_falls_back_to_nominal_when_c10_not_set(self):
        p = make_profile(nominal_capacity_Ah=7.0)
        assert abs(p.effective_max_charge_A - 1.75) < 0.001

    def test_c10_capacity_must_not_exceed_nominal(self):
        """C10 < C20 (C20=nominal): C10 fizikailag nem lehet nagyobb a C20-nál."""
        with pytest.raises(ProfileValidationError):
            make_profile(nominal_capacity_Ah=7.0, C10_capacity_Ah=8.0)

    def test_explicit_taper_still_overrides_c10(self):
        """Ha taper_current_A manuálisan be van állítva, az élvez elsőbbséget."""
        p = make_profile(nominal_capacity_Ah=7.2, C10_capacity_Ah=6.7,
                         taper_current_A=0.15)
        assert abs(p.effective_taper_A - 0.15) < 0.001
