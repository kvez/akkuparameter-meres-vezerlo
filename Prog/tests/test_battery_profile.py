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


class TestReleaseAudit20260823C10DischargeCurrentUsesC10Base:
    """Release-audit 2026-08-23 (2.9): C10_discharge_current_A korábban
    nominal_capacity_Ah-t osztott, NEM _c10_base_Ah-t — inkonzisztens a
    testvér effective_max_charge_A/effective_taper_A property-kkel.
    FG-sorozatnál (C20-nominal), ahol C10_capacity_Ah &lt; nominal_capacity_Ah,
    ez a deklaráltnál magasabb BQ golden-image tanulási kisütőáramot
    eredményezett volna."""

    def test_uses_c10_capacity_when_explicitly_set_and_lower_than_nominal(self):
        p = make_profile(nominal_capacity_Ah=100.0, C10_capacity_Ah=80.0)
        assert p.C10_discharge_current_A == pytest.approx(8.0)  # 80/10, NEM 100/10=10.0

    def test_matches_effective_max_charge_a_base_capacity(self):
        """C10_discharge_current_A és effective_max_charge_A ugyanazt a
        kapacitásbázist (_c10_base_Ah) kell hogy használja."""
        p = make_profile(nominal_capacity_Ah=100.0, C10_capacity_Ah=80.0)
        assert p.C10_discharge_current_A == pytest.approx(p._c10_base_Ah / 10)
        assert p.effective_max_charge_A == pytest.approx(0.25 * p._c10_base_Ah)

    def test_falls_back_to_nominal_when_c10_capacity_not_set(self):
        """FGH/FGHL-sorozat (nominal=C10): a fallback változatlan marad."""
        p = make_profile(nominal_capacity_Ah=7.0)
        assert p.C10_discharge_current_A == pytest.approx(0.70)

    def test_custom_max_charge_overrides_auto(self):
        p = make_profile(nominal_capacity_Ah=7.0, max_charge_current_A=1.40)
        assert abs(p.effective_max_charge_A - 1.40) < 0.001


class TestSession11C5DischargeCurrentUsesC10Base:
    """[regresszió, Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
    Adatintegritás P2 lelet] C5_discharge_current_A ugyanabba a hibaosztályba
    tartozott, mint a C10_discharge_current_A release-audit 2.9 fixje fent
    (TestReleaseAudit20260823C10DischargeCurrentUsesC10Base) — korábban
    nominal_capacity_Ah/5-öt osztott, NEM _c10_base_Ah/5-öt, de ez a
    testvér-fix sosem lett rá alkalmazva. FG-sorozatnál (C20-nominal), ahol
    C10_capacity_Ah &lt; nominal_capacity_Ah, ez a deklaráltnál magasabb
    kisütőáramot eredményezett volna (discharge_controller.py::
    _run_cc_setup() önálló StepKind.DISCHARGE alapértelmezett árama)."""

    def test_uses_c10_capacity_when_explicitly_set_and_lower_than_nominal(self):
        p = make_profile(nominal_capacity_Ah=100.0, C10_capacity_Ah=80.0)
        assert p.C5_discharge_current_A == pytest.approx(16.0)  # 80/5, NEM 100/5=20.0

    def test_matches_c10_discharge_current_a_base_capacity(self):
        """C5_discharge_current_A és C10_discharge_current_A ugyanazt a
        kapacitásbázist (_c10_base_Ah) kell hogy használja."""
        p = make_profile(nominal_capacity_Ah=100.0, C10_capacity_Ah=80.0)
        assert p.C5_discharge_current_A == pytest.approx(p._c10_base_Ah / 5)
        assert p.C10_discharge_current_A == pytest.approx(p._c10_base_Ah / 10)

    def test_falls_back_to_nominal_when_c10_capacity_not_set(self):
        """FGH/FGHL-sorozat (nominal=C10): a fallback változatlan marad."""
        p = make_profile(nominal_capacity_Ah=7.0)
        assert p.C5_discharge_current_A == pytest.approx(1.40)

    def test_custom_taper_overrides_auto(self):
        p = make_profile(nominal_capacity_Ah=7.0, taper_current_A=0.15)
        assert abs(p.effective_taper_A - 0.15) < 0.001


class TestCF16C20DischargeCurrentUsesC10Base:
    """[regresszió, Governance-v2 review-kör 1 (CF-16/MET-R1-04+16+18,
    DFAM-001 6. előfordulás + DFAM-017, batch 4.4)] ugyanabba a
    hibaosztályba tartozik, mint a C10/C5_discharge_current_A fenti fixjei
    (TestReleaseAudit20260823C10DischargeCurrentUsesC10Base,
    TestSession11C5DischargeCurrentUsesC10Base) — a kódbázis egységes
    konvenciója szerint MINDEN rate-alapú áram-setpoint a kanonikus
    `_c10_base_Ah`-ból származik, függetlenül a konkrét osztó (5/10/20)
    értékétől. C20_discharge_current_A korábban nyers nominal_capacity_Ah-t
    osztott, NEM _c10_base_Ah-t."""

    def test_uses_c10_capacity_when_explicitly_set_and_lower_than_nominal(self):
        p = make_profile(nominal_capacity_Ah=100.0, C10_capacity_Ah=80.0)
        assert p.C20_discharge_current_A == pytest.approx(4.0)  # 80/20, NEM 100/20=5.0

    def test_matches_c10_discharge_current_a_base_capacity(self):
        """C20_discharge_current_A és C10_discharge_current_A ugyanazt a
        kapacitásbázist (_c10_base_Ah) kell hogy használja."""
        p = make_profile(nominal_capacity_Ah=100.0, C10_capacity_Ah=80.0)
        assert p.C20_discharge_current_A == pytest.approx(p._c10_base_Ah / 20)
        assert p.C10_discharge_current_A == pytest.approx(p._c10_base_Ah / 10)

    def test_falls_back_to_nominal_when_c10_capacity_not_set(self):
        """FGH/FGHL-sorozat (nominal=C10): a fallback változatlan marad."""
        p = make_profile(nominal_capacity_Ah=7.0)
        assert p.C20_discharge_current_A == pytest.approx(0.35)


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

    def test_at_15c_capped_by_q26(self):
        p = make_profile(cell_count=6, charge_voltage_per_cell_V=2.40)
        assert p.compensated_charge_voltage_V(15.0) == pytest.approx(14.40, abs=1e-12)

    def test_clamp_never_exceeds_safety_limit_at_minus_10c(self):
        """Q-44/Q-48: -10 °C-on is legfeljebb 2,400 V/cella → 14,40 V a cél."""
        p = make_profile(cell_count=6)
        target = p.compensated_charge_voltage_V(-10.0)
        assert target <= p.batt_absolute_max_V + 1e-6

    def test_clamp_never_exceeds_safety_limit_at_minus_20c(self):
        """Q-44/Q-48: -20 °C-on is legfeljebb 2,400 V/cella → 14,40 V a cél."""
        p = make_profile(cell_count=6)
        target = p.compensated_charge_voltage_V(-20.0)
        assert target <= p.batt_absolute_max_V + 1e-6

    def test_lower_clamp_at_extreme_heat(self):
        """Extrém magas hőmérsékleten 2.30 V/cella minimumot tart."""
        p = make_profile(cell_count=6)
        target = p.compensated_charge_voltage_V(100.0)
        assert target >= p.cell_count * 2.30 - 1e-6

    def test_clamp_24v_pack(self):
        """Q-44/Q-48: 24 V-os pakk célplafonja 12×2,400 = 28,80 V."""
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


class TestI10DischargeCurrentField:
    """[IEC 60896-21 DC belső ellenállás mód] i10_discharge_current_A -- a
    modell saját C10 kisütési táblázatából (10h, 1,80V/cella), NEM nominal/10
    közelítésből. Forrás: Folyamatok/tervek/
    AGM_VRLA_belsoellenallas_IEC60896_ACIR_HRM10.txt (FG20721 példa)."""

    def test_defaults_to_none(self):
        p = make_profile()
        assert p.i10_discharge_current_A is None

    def test_accepts_explicit_value(self):
        """[Kör 21 F17, SRC21-04 sibling] I10=0,62A a forrásdokumentum
        (AGM_VRLA_belsoellenallas_IEC60896_ACIR_HRM10.txt 8. szakasz)
        elavult munkapélda-értéke -- a primer FIAMM adatlap azóta
        I10=0,67A-ra korrigálta (l. battery_profile.py 41-43. sor
        komment, CLAIM_LEDGER.yaml BAT-FG20721-I10-001). Itt csak
        generikus teszt-input, nem a FG20721 jelenlegi értékét állítja."""
        p = make_profile(i10_discharge_current_A=0.62)
        assert p.i10_discharge_current_A == pytest.approx(0.62)

    def test_rejects_zero_or_negative(self):
        with pytest.raises(ProfileValidationError):
            make_profile(i10_discharge_current_A=0.0)
        with pytest.raises(ProfileValidationError):
            make_profile(i10_discharge_current_A=-0.5)

    def test_rejects_implausibly_large_value(self):
        """Tipikus adatbeviteli hiba: Ah/10 helyett Ah-t adnak meg, vagy
        Ah helyett mAh-t -- mindkettő a nominal_capacity_Ah-nál nagyobb
        I10-et eredményezne, ami fizikailag implauzibilis."""
        with pytest.raises(ProfileValidationError):
            make_profile(nominal_capacity_Ah=7.2, i10_discharge_current_A=7.2 * 1000)

    def test_rejects_value_between_capacity_and_half_capacity(self):
        """[regresszió, MAX verifier lelet, governance-v2 kör 2, F7] a
        korábbi felső korlát (i10 > nominal_capacity_Ah, azaz akár 1×C is
        átment) implauzibilisan laza volt egy ~0,1×C tipikus C10 rátához
        képest -- egy nagyságrendekkel túl nagy, de a régi korláton BELÜL
        eső érték (itt 0,8×C) a REGI kódon átment volna, és a belőle
        számolt Ib=20×I10 terhelőáram-parancsot ellenőrzés nélkül küldte
        volna a load felé (l. iec_dc_resistance_controller.py::_run_load_1/2
        kommentje: a safety power-check csak input_on() UTÁN, a MÉRT
        áramra fut)."""
        with pytest.raises(ProfileValidationError):
            make_profile(nominal_capacity_Ah=7.2, i10_discharge_current_A=7.2 * 0.8)

    def test_accepts_typical_c10_rate_ratio(self):
        """FG20721 arány (0,62/7,2 ≈ 0,086×C) és egy bőséges, de még
        plauzibilis 0,3×C érték is átmenjen -- a szigorítás ne legyen
        túl agresszív a valós C10 rátatartományhoz képest."""
        p = make_profile(nominal_capacity_Ah=7.2, i10_discharge_current_A=7.2 * 0.3)
        assert p.i10_discharge_current_A == pytest.approx(7.2 * 0.3)


class TestR16B16aDischargeFloor:
    @pytest.mark.parametrize("value", [1.60, 1.75, float("nan"), float("inf"), -float("inf")])
    def test_profile_without_rate_proof_rejects_low_or_nonfinite_cutoff(self, value):
        with pytest.raises(ProfileValidationError):
            make_profile(terminate_voltage_per_cell_V=value)

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("rate,basis,capacity,current,minimum", [
        ("C/20", "NOMINAL_C10_CAPACITY", 7.2, 0.36, 1.75),
        ("C/10", "NOMINAL_C10_CAPACITY", 7.2, 0.72, 1.80),
        ("C/5", "NOMINAL_C10_CAPACITY", 7.2, 1.44, 1.80),
        ("CUSTOM", "NOMINAL_C10_CAPACITY", 7.2, 0.36, 1.80),
        ("C/20", "MEASURED_USABLE_CAPACITY", 7.2, 0.36, 1.80),
        ("C/20", "NOMINAL_C10_CAPACITY", 6.7, 0.335, 1.80),
        ("UNKNOWN", "UNKNOWN", 0.0, 0.36, 1.80),
    ])
    def test_rate_origin_is_required(self, cells, rate, basis, capacity, current, minimum):
        from Prog.src.battery_profile import (
            DischargeRateOrigin,
            validate_normal_discharge,
        )

        profile = make_profile(cell_count=cells, nominal_capacity_Ah=capacity or 7.2)
        origin = DischargeRateOrigin(rate, basis, capacity)
        assert validate_normal_discharge(profile, current, minimum * cells, origin) == minimum * cells
        with pytest.raises(ValueError):
            validate_normal_discharge(profile, current, minimum * cells - 0.001, origin)

    def test_source_is_model_specific(self):
        from Prog.src.battery_profile import (
            DischargeRateOrigin,
            validate_normal_discharge,
        )

        for changes in ({"model": "OTHER"}, {"manufacturer": "OTHER"}):
            profile = make_profile(nominal_capacity_Ah=7.2, **changes)
            with pytest.raises(ValueError):
                validate_normal_discharge(
                    profile, 0.36, 10.5,
                    DischargeRateOrigin("C/20", "NOMINAL_C10_CAPACITY", 7.2),
                )

    def test_measured_capacity_can_use_the_conservative_floor(self):
        from Prog.src.battery_profile import (
            DischargeRateOrigin,
            validate_normal_discharge,
        )

        profile = make_profile(nominal_capacity_Ah=7.2)
        origin = DischargeRateOrigin("C/20", "MEASURED_USABLE_CAPACITY", 7.0)
        assert validate_normal_discharge(profile, 0.35, 10.8, origin) == 10.8
        with pytest.raises(ValueError):
            validate_normal_discharge(profile, 0.35, 10.5, origin)


class TestR16B7ExplicitBqCapacity:
    @pytest.mark.parametrize("attribute", [
        "design_capacity_mAh", "design_energy_cWh", "bq_learning_discharge_current_A",
    ])
    def test_missing_c10_blocks(self, attribute):
        with pytest.raises(ProfileValidationError, match="SOURCE_REQUIRED"):
            getattr(make_profile(nominal_capacity_Ah=7.2), attribute)

    @pytest.mark.parametrize("voltage,cells,energy", [(12.0, 6, 8040), (24.0, 12, 16080)])
    def test_fg20721_source_values(self, voltage, cells, energy):
        p = make_profile(
            nominal_capacity_Ah=7.2, C10_capacity_Ah=6.7,
            nominal_voltage_V=voltage, cell_count=cells,
        )
        # FG_FOLDER_EN Rev.11.2025 p2; Q-9/Q-23 tudatosan eltérő bázisok.
        assert p.design_capacity_mAh == 6700
        assert p.design_energy_cWh == energy
        assert p.qmax_cell0_initial_mAh == 7200
        assert p.bq_learning_discharge_current_A == pytest.approx(0.67)

    @pytest.mark.parametrize("model,nominal", [("FG20722", 7.2), ("FG20721", 7.0)])
    def test_unsourced_model_or_parallel_count_blocks(self, model, nominal):
        p = make_profile(model=model, nominal_capacity_Ah=nominal)
        with pytest.raises(ProfileValidationError, match="SOURCE_REQUIRED"):
            _ = p.fg20721_c10_capacity_Ah

    @pytest.mark.parametrize("c10,voltage,attribute", [
        (32.7671, 2.0, "design_capacity_mAh"),
        (13.653, 24.0, "design_energy_cWh"),
        (27.306, 12.0, "design_energy_cWh"),
    ])
    def test_raw_overflow_is_not_rounded_or_clamped(self, c10, voltage, attribute):
        p = make_profile(nominal_capacity_Ah=40.0, C10_capacity_Ah=c10, nominal_voltage_V=voltage)
        with pytest.raises(ProfileValidationError, match="Q-22"):
            getattr(p, attribute)


class TestR16B7F1DecimalEndpoints:
    @pytest.mark.parametrize("attribute,voltage", [
        ("design_capacity_mAh", 2.0), ("design_energy_cWh", 10.0),
    ])
    @pytest.mark.parametrize("c10,accepted", [(32.767, True), (32.7670001, False)])
    def test_exact_endpoint_without_rounding_overflow(self, attribute, voltage, c10, accepted):
        profile = make_profile(
            nominal_capacity_Ah=40.0, C10_capacity_Ah=c10, nominal_voltage_V=voltage,
        )
        if accepted:
            assert getattr(profile, attribute) == 32767
        else:
            with pytest.raises(ProfileValidationError, match="Q-22"):
                getattr(profile, attribute)


class TestR16B26aChargeCap:
    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("temperature,per_cell", [
        (0.0, 2.400), (5.0, 2.400), (10.0, 2.400), (15.0, 2.400),
        (19.9, 2.400), (20.0, 2.400), (25.0, 2.3875), (35.0, 2.3625),
    ])
    def test_temperature_matrix(self, cells, temperature, per_cell):
        profile = make_profile(cell_count=cells)
        assert profile.compensated_charge_voltage_V(temperature) == pytest.approx(per_cell * cells, abs=1e-12)
        assert profile.batt_absolute_max_V_per_cell == 2.425

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("nominal", [2.38, 2.40, 2.60])
    def test_uncompensated_profile_cannot_bypass_cap(self, cells, nominal):
        profile = make_profile(cell_count=cells, charge_voltage_per_cell_V=nominal)
        assert profile.charge_voltage_pack_V == pytest.approx(min(nominal, 2.400) * cells, abs=1e-12)
