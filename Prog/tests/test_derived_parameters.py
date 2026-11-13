"""
SUP02 — DynamicSessionModel (reaktív derived-parameter engine) unit tesztek.
Nincs PySide6 import szükséges — pure Python engine, l. SUP02-ARCH-001.
"""
from __future__ import annotations

import pytest

from Prog.gui.derived_parameters import (
    CapacityBasisPolicy,
    ChargeCurrentPolicy,
    DischargeRatePolicy,
    DynamicSessionModel,
    ParamSource,
    PulseCurrentPolicy,
    TerminateVoltagePolicy,
    ValidationState,
    compute_discharge_current_A,
    compute_max_charge_current_A,
    compute_taper_current_A,
    compute_terminate_voltage_pack_V,
)


# ------------------------------------------------------------------ #
# SUP02-TEST-001 — C/10 reaktív frissítés (kötelező regressziós eset) #
# ------------------------------------------------------------------ #

class TestSup02Test001DischargeRateReactive:
    def test_c10_with_1ah_capacity(self):
        model = DynamicSessionModel(c10_capacity_Ah=1.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.C10)
        assert model.discharge_current_A == pytest.approx(0.10)

    def test_capacity_change_to_10ah_updates_automatically_without_reselecting_policy(self):
        model = DynamicSessionModel(c10_capacity_Ah=1.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.C10)
        assert model.discharge_current_A == pytest.approx(0.10)

        model.set_capacity(10.0)  # policy VÁLTOZATLAN marad — nincs újraválasztás

        assert model.discharge_current_A == pytest.approx(1.00)
        assert model.discharge_rate_policy == DischargeRatePolicy.C10

    def test_c5_and_c20_divisors(self):
        model = DynamicSessionModel(c10_capacity_Ah=10.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.C5)
        assert model.discharge_current_A == pytest.approx(2.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.C20)
        assert model.discharge_current_A == pytest.approx(0.5)

    def test_formula_helper_matches_model(self):
        assert compute_discharge_current_A(10.0, 10) == pytest.approx(1.0)


# ------------------------------------------------------------------ #
# SUP02-TEST-002 — 12/24 V reaktív feszültség (kötelező regressziós)  #
# ------------------------------------------------------------------ #

class TestSup02Test002TerminateVoltageReactive:
    def test_12v_profile_1_80_v_per_cell(self):
        model = DynamicSessionModel(battery_profile_name="FIAMM_12V")
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.V_1_80_PER_CELL)
        assert model.terminate_voltage_pack_V == pytest.approx(10.80)

    def test_profile_switch_to_24v_updates_without_reselecting_policy(self):
        model = DynamicSessionModel(battery_profile_name="FIAMM_12V")
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.V_1_80_PER_CELL)
        assert model.terminate_voltage_pack_V == pytest.approx(10.80)

        model.set_battery_profile("FIAMM_24V")  # policy VÁLTOZATLAN

        assert model.terminate_voltage_pack_V == pytest.approx(21.60)
        assert model.cell_count == 12

    def test_formula_helper_matches_model(self):
        assert compute_terminate_voltage_pack_V(12, 1.80) == pytest.approx(21.60)


# ------------------------------------------------------------------ #
# SUP02-TEST-003 — taper és charge current, blocking incompatibility  #
# ------------------------------------------------------------------ #

class TestSup02Test003TaperAndChargeCurrent:
    def test_1ah_max_charge_and_taper(self):
        model = DynamicSessionModel(c10_capacity_Ah=1.0)
        assert model.requested_charge_current_A == pytest.approx(0.25)
        assert model.taper_current_A == pytest.approx(0.03)

    def test_10ah_max_charge_and_taper(self):
        model = DynamicSessionModel(c10_capacity_Ah=10.0)
        assert model.requested_charge_current_A == pytest.approx(2.50)
        assert model.taper_current_A == pytest.approx(0.30)

    def test_independent_1_5a_limit_blocks_not_clamps(self):
        """10 Ah × 0.25 = 2.5 A > 1.5 A INDEPENDENT limit → CONFIG_INCOMPATIBLE,
        NEM csendes 1.5 A-re clampelés (SUP02-VALID-002)."""
        model = DynamicSessionModel(c10_capacity_Ah=10.0, psu_mode="INDEPENDENT")
        assert model.requested_charge_current_A == pytest.approx(2.5)  # nem clampelt!
        assert model.charge_current_hardware_limit_A == pytest.approx(1.5)
        assert model.is_charge_current_incompatible is True

    def test_formula_helpers(self):
        assert compute_max_charge_current_A(10.0) == pytest.approx(2.5)
        assert compute_taper_current_A(10.0) == pytest.approx(0.3)


class TestReviewRound4GuiF6ProfileMaxChargeCurrentOverride:
    """[regresszió, review-kör 4/5 GUI F6 (P2, BAT-001/SRC-003/CFG-001
    sibling)] PROFILE_MAX policy esetén a modell korábban MINDIG a
    generikus 0,25×C10 képlettel számolt a Start-összegzőben -- a
    TÉNYLEGESEN alkalmazott áram (charge_controller.py -> BatteryProfile.
    effective_max_charge_A) viszont a profil YAML `max_charge_current_A`
    mezőjét ELSŐBBSÉGGEL használja, ha az be van állítva. Ma mindkét
    gyári profilban (FIAMM_12V/24V.yaml) null, tehát ma nulla
    viselkedésváltozás -- de a modell maga sosem volt felkészítve rá."""

    def test_profile_override_takes_precedence_over_formula(self):
        model = DynamicSessionModel(c10_capacity_Ah=10.0)
        assert model.requested_charge_current_A == pytest.approx(2.5)  # 0.25×10

        model.set_max_charge_current_A_profile_override(1.2)

        assert model.requested_charge_current_A == pytest.approx(1.2)

    def test_profile_override_none_falls_back_to_formula(self):
        model = DynamicSessionModel(c10_capacity_Ah=10.0)
        model.set_max_charge_current_A_profile_override(1.2)
        model.set_max_charge_current_A_profile_override(None)

        assert model.requested_charge_current_A == pytest.approx(2.5)

    def test_profile_override_does_not_apply_under_custom_policy(self):
        """A felhasználó explicit CUSTOM töltőárama elsőbbséget élvez a
        profil-felülbírálással szemben is -- ugyanaz a policy-sorrend,
        mint a formula-ág esetén."""
        model = DynamicSessionModel(c10_capacity_Ah=10.0)
        model.set_max_charge_current_A_profile_override(1.2)
        model.set_charge_current_policy(ChargeCurrentPolicy.CUSTOM, custom_A=0.5)

        assert model.requested_charge_current_A == pytest.approx(0.5)

    def test_profile_override_is_validated_against_psu_hw_limit(self):
        """A felülbírált értéknek is át kell mennie a meglévő SUP02-VALID-
        002 (nincs csendes clamp) kapun."""
        model = DynamicSessionModel(c10_capacity_Ah=10.0, psu_mode="INDEPENDENT")
        model.set_max_charge_current_A_profile_override(2.0)  # > 1.5A INDEPENDENT limit

        assert model.requested_charge_current_A == pytest.approx(2.0)  # nem clampelt!
        assert model.is_charge_current_incompatible is True


class TestKor20BatchE5ProfileTerminateVoltageAndTaperOverride:
    """[regresszió, Kör 20 Batch E5 (SW20-2, P2, DFAM-001+DFAM-017) -- a
    fenti TestReviewRound4GuiF6ProfileMaxChargeCurrentOverride PONTOS
    mintája, kiterjesztve a két testvér-mezőre] a `terminate_voltage_per_
    cell_default_V` korábban HARDKÓDOLT 1.80 volt (a PROFILE_DEFAULT
    policy egyetlen forrása), a `taper_current_A` pedig MINDIG a generikus
    0,03×C10 képlettel számolt -- egyik sem tükrözte a profil-YAML
    tényleges értékét, amit a BatteryProfile.terminate_voltage_pack_V/
    effective_taper_A ELSŐBBSÉGGEL használ."""

    def test_profile_default_policy_uses_yaml_fed_per_cell_value(self):
        model = DynamicSessionModel(battery_profile_name="FIAMM_12V")
        assert model.cell_count == 6
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.PROFILE_DEFAULT)

        model.set_terminate_voltage_per_cell_default_V(1.75)

        assert model.terminate_voltage_pack_V == pytest.approx(6 * 1.75)

    def test_none_restores_fiamm_default_not_previous_profile_value(self):
        model = DynamicSessionModel(battery_profile_name="FIAMM_12V")
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.PROFILE_DEFAULT)
        model.set_terminate_voltage_per_cell_default_V(1.75)

        model.set_terminate_voltage_per_cell_default_V(None)

        assert model.terminate_voltage_pack_V == pytest.approx(6 * 1.80)

    def test_explicit_preset_policies_are_not_affected(self):
        """KEEP_AS_IS-őr: a profil-alapérték módosítása nem szivároghat át
        az EXPLICIT 1.80/1.75 V/cella presetekbe."""
        model = DynamicSessionModel(battery_profile_name="FIAMM_12V")
        model.set_terminate_voltage_per_cell_default_V(1.75)

        model.set_terminate_voltage_policy(TerminateVoltagePolicy.V_1_80_PER_CELL)
        assert model.terminate_voltage_pack_V == pytest.approx(6 * 1.80)

        model.set_terminate_voltage_policy(TerminateVoltagePolicy.V_1_75_PER_CELL)
        assert model.terminate_voltage_pack_V == pytest.approx(6 * 1.75)

    def test_custom_policy_still_wins_over_profile_default(self):
        model = DynamicSessionModel(battery_profile_name="FIAMM_12V")
        model.set_terminate_voltage_per_cell_default_V(1.75)
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.PROFILE_DEFAULT)

        model.set_terminate_voltage_policy(TerminateVoltagePolicy.CUSTOM, custom_V=9.0)

        assert model.terminate_voltage_pack_V == pytest.approx(9.0)

    def test_profile_switch_recomputes_with_new_cell_count(self):
        model = DynamicSessionModel(battery_profile_name="FIAMM_12V")
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.PROFILE_DEFAULT)
        model.set_terminate_voltage_per_cell_default_V(1.75)

        model.set_battery_profile("FIAMM_24V")

        assert model.cell_count == 12
        assert model.terminate_voltage_pack_V == pytest.approx(12 * 1.75)

    def test_taper_profile_override_takes_precedence_and_none_falls_back(self):
        model = DynamicSessionModel(c10_capacity_Ah=10.0)
        assert model.taper_current_A == pytest.approx(0.03 * 10.0)

        model.set_taper_current_A_profile_override(0.15)
        assert model.taper_current_A == pytest.approx(0.15)

        model.set_taper_current_A_profile_override(None)
        assert model.taper_current_A == pytest.approx(0.03 * 10.0)


# ------------------------------------------------------------------ #
# SUP02-TEST-004 — Custom override                                    #
# ------------------------------------------------------------------ #

class TestSup02Test004CustomOverride:
    def test_custom_value_survives_capacity_change(self):
        model = DynamicSessionModel(c10_capacity_Ah=7.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.CUSTOM, custom_A=0.40)
        assert model.discharge_current_A == pytest.approx(0.40)

        model.set_capacity(14.0)  # kapacitás változik

        assert model.discharge_current_A == pytest.approx(0.40)  # MARAD 0.40 A
        assert model.get("discharge_current_A").source == ParamSource.USER_OVERRIDE

    def test_reselecting_preset_switches_back_to_derived(self):
        """Ha a kezelő újra C/10-et választ, a mező DERIVED_PRESET lesz,
        és azonnal az aktuális kapacitásból számolódik."""
        model = DynamicSessionModel(c10_capacity_Ah=7.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.CUSTOM, custom_A=0.40)
        model.set_discharge_rate_policy(DischargeRatePolicy.C10)

        assert model.get("discharge_current_A").source == ParamSource.DERIVED_PRESET
        assert model.discharge_current_A == pytest.approx(0.7)

    def test_charge_current_custom_override_revalidated(self):
        model = DynamicSessionModel(c10_capacity_Ah=2.0, psu_mode="INDEPENDENT")
        model.set_charge_current_policy(ChargeCurrentPolicy.CUSTOM, custom_A=0.40)
        assert model.requested_charge_current_A == pytest.approx(0.40)
        assert model.is_charge_current_incompatible is False

        model.set_charge_current_policy(ChargeCurrentPolicy.CUSTOM, custom_A=2.5)
        assert model.is_charge_current_incompatible is True  # revalidálva


# ------------------------------------------------------------------ #
# SUP02-TEST-005 — Mód-függő láthatóság (l. test_ui_policy.py is)      #
# ------------------------------------------------------------------ #

class TestSup02Test005EffectiveConfigVisibility:
    def test_hidden_field_not_in_effective_config_charge_only(self):
        """CHARGE_ONLY módban a discharge_rate/pulse_current csoport rejtett —
        nem kerülhet be az effective configba (SUP02-UI-008)."""
        model = DynamicSessionModel(test_type="CHARGE_ONLY", c10_capacity_Ah=7.0)
        cfg = model.effective_config()
        assert "discharge_current_A" not in cfg
        assert "pulse_current_A" not in cfg
        assert "requested_charge_current_A" in cfg

    def test_discharge_only_hides_charge_fields(self):
        model = DynamicSessionModel(test_type="DISCHARGE_ONLY", c10_capacity_Ah=7.0)
        cfg = model.effective_config()
        assert "requested_charge_current_A" not in cfg
        assert "discharge_current_A" in cfg
        assert "terminate_voltage_pack_V" in cfg

    def test_ocv_soc_shows_charge_current_and_pulse(self):
        """[frissítve, Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/
        Állapot P2 lelet #2] requested_charge_current_A korábban hiányzott
        az OCV_SOC_CHARACTERIZATION összefoglalóból — a PRECHARGE
        beágyazott ChargeController élesen a session shared_charge_cfg-jét
        (töltőáram) kapja, ezt a Start-megerősítő dialógusnak is mutatnia
        kell (l. ui_policy.py OCV_SOC_CHARACTERIZATION policy kommentje)."""
        model = DynamicSessionModel(test_type="OCV_SOC_CHARACTERIZATION", c10_capacity_Ah=7.0)
        cfg = model.effective_config()
        assert "requested_charge_current_A" in cfg
        assert "pulse_current_A" in cfg
        assert "capacity_basis_Ah" in cfg


# ------------------------------------------------------------------ #
# SUP02-TEST-006 — PSU mód és wiring                                   #
# ------------------------------------------------------------------ #

class TestSup02Test006PsuModeAndWiring:
    def test_12v_to_24v_forces_series_and_resets_wiring(self):
        model = DynamicSessionModel(battery_profile_name="FIAMM_12V")
        model.wiring_confirmed = True
        model.set_battery_profile("FIAMM_24V")
        assert model.psu_mode == "SERIES"
        assert model.wiring_confirmed is False

    def test_series_to_other_mode_resets_wiring(self):
        model = DynamicSessionModel(battery_profile_name="FIAMM_24V", psu_mode="SERIES")
        model.wiring_confirmed = True
        model.set_psu_mode("PARALLEL")
        assert model.wiring_confirmed is False

    def test_24v_back_to_12v_defaults_to_independent(self):
        model = DynamicSessionModel(battery_profile_name="FIAMM_12V")
        model.set_battery_profile("FIAMM_24V")
        model.set_battery_profile("FIAMM_12V")
        assert model.psu_mode == "INDEPENDENT"
        assert model.wiring_confirmed is False


# ------------------------------------------------------------------ #
# SUP02-TEST-007 — YAML/batch betöltés                                 #
# ------------------------------------------------------------------ #

class TestSup02Test007BatchUpdate:
    def test_batch_update_single_final_revision(self):
        model = DynamicSessionModel(c10_capacity_Ah=1.0)
        rev_before = model.config_revision

        with model.batch_update() as m:
            m.c10_capacity_Ah = 20.0
            m.psu_mode = "PARALLEL"
            m.discharge_rate_policy = DischargeRatePolicy.C10
            # köztes recompute NEM fut — csak a with-blokk végén egy

        assert model.config_revision == rev_before + 1
        assert model.discharge_current_A == pytest.approx(2.0)

    def test_no_batch_multiple_setters_multiple_revisions(self):
        """Regresszió: batch NÉLKÜL minden setter külön recompute-ot vált ki."""
        model = DynamicSessionModel(c10_capacity_Ah=1.0)
        rev_before = model.config_revision
        model.set_capacity(5.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.C10)
        assert model.config_revision == rev_before + 2


# ------------------------------------------------------------------ #
# SUP02-TEST-008 — Signal-loop védelem                                 #
# ------------------------------------------------------------------ #

class TestSup02Test008NoSignalLoop:
    def test_repeated_recompute_is_stable_no_growth(self):
        """A pure engine-nek nincs widget-widget signal-lánca — ismételt
        recompute() hívás stabil, nem növekvő eredményt ad."""
        model = DynamicSessionModel(c10_capacity_Ah=7.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.C10)
        value_1 = model.discharge_current_A
        model.recompute()
        model.recompute()
        value_2 = model.discharge_current_A
        assert value_1 == pytest.approx(value_2)

    def test_single_user_action_causes_single_revision_bump(self):
        model = DynamicSessionModel(c10_capacity_Ah=7.0)
        rev_before = model.config_revision
        model.set_capacity(8.0)
        assert model.config_revision == rev_before + 1


# ------------------------------------------------------------------ #
# SUP02-TEST-009 — Per-mode draft                                       #
# ------------------------------------------------------------------ #

class TestSup02Test009PerModeDraft:
    def test_mode_switch_preserves_custom_value(self):
        model = DynamicSessionModel(test_type="DISCHARGE_ONLY", c10_capacity_Ah=7.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.CUSTOM, custom_A=0.55)

        model.set_test_type("CHARGE_ONLY")
        model.set_test_type("DISCHARGE_ONLY")

        assert model.discharge_rate_policy == DischargeRatePolicy.CUSTOM
        assert model.discharge_current_A == pytest.approx(0.55)

    def test_active_snapshot_only_current_mode(self):
        model = DynamicSessionModel(test_type="CHARGE_ONLY", c10_capacity_Ah=7.0)
        cfg = model.effective_config()
        assert cfg["test_type"] == "CHARGE_ONLY"
        assert "discharge_current_A" not in cfg

    def test_global_capacity_change_updates_all_draft_auto_values(self):
        """Globális capacity-váltás minden mód auto (DERIVED_PRESET) értékét
        frissíti — a draft-ban lévő policy-választás is friss lesz visszaváltáskor."""
        model = DynamicSessionModel(test_type="DISCHARGE_ONLY", c10_capacity_Ah=7.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.C10)  # 0.7A

        model.set_test_type("CHARGE_ONLY")
        model.set_capacity(70.0)  # globális kapacitásváltás egy másik módban
        model.set_test_type("DISCHARGE_ONLY")

        assert model.discharge_current_A == pytest.approx(7.0)  # 70/10, nem 0.7


# ------------------------------------------------------------------ #
# SUP02-TEST-010 — Start csak friss, validált revisionnal                #
# ------------------------------------------------------------------ #

class TestSup02Test010StartRevisionGuard:
    def test_start_blocked_without_validation(self):
        model = DynamicSessionModel(c10_capacity_Ah=7.0)
        assert model.is_start_allowed() is False  # sosem validáltuk

    def test_start_allowed_after_fresh_validation(self):
        model = DynamicSessionModel(c10_capacity_Ah=1.0, psu_mode="INDEPENDENT")
        model.validate_effective_config()
        assert model.is_start_allowed() is True

    def test_start_blocked_after_stale_change(self):
        """Validálás után egy újabb változtatás elavulttá teszi a revisiont."""
        model = DynamicSessionModel(c10_capacity_Ah=1.0, psu_mode="INDEPENDENT")
        model.validate_effective_config()
        assert model.is_start_allowed() is True

        model.set_capacity(2.0)  # stale-lé teszi a validált revisiont

        assert model.is_start_allowed() is False

    def test_start_blocked_when_config_incompatible(self):
        model = DynamicSessionModel(c10_capacity_Ah=10.0, psu_mode="INDEPENDENT")
        model.validate_effective_config()
        assert model.is_start_allowed() is False  # CONFIG_INCOMPATIBLE


# ------------------------------------------------------------------ #
# SUP02-TEST-011 — OCV-SOC derived (pulse current a capacity basisból) #
# ------------------------------------------------------------------ #

class TestSup02Test011OcvSocDerived:
    def test_pulse_current_c5_from_nominal_capacity(self):
        model = DynamicSessionModel(
            test_type="OCV_SOC_CHARACTERIZATION", c10_capacity_Ah=10.0
        )
        model.set_pulse_current_policy(PulseCurrentPolicy.C5)
        assert model.pulse_current_A == pytest.approx(2.0)

    def test_pulse_current_updates_on_measured_capacity_basis(self):
        model = DynamicSessionModel(
            test_type="OCV_SOC_CHARACTERIZATION", c10_capacity_Ah=10.0
        )
        model.set_pulse_current_policy(PulseCurrentPolicy.C10)
        assert model.pulse_current_A == pytest.approx(1.0)

        model.set_measured_capacity(6.5)
        model.set_capacity_basis_policy(CapacityBasisPolicy.MEASURED_USABLE_CAPACITY)

        assert model.capacity_basis_Ah == pytest.approx(6.5)
        assert model.pulse_current_A == pytest.approx(0.65)

    def test_measured_option_falls_back_if_not_available(self):
        model = DynamicSessionModel(test_type="OCV_SOC_CHARACTERIZATION", c10_capacity_Ah=10.0)
        model.set_capacity_basis_policy(CapacityBasisPolicy.MEASURED_USABLE_CAPACITY)
        assert model.capacity_basis_policy == CapacityBasisPolicy.NOMINAL_C10_CAPACITY
        assert model.capacity_basis_quality == "NOMINAL_CAPACITY_BASED_SOC"


# ------------------------------------------------------------------ #
# SUP02-TEST-012 — Session snapshot                                     #
# ------------------------------------------------------------------ #

class TestSup02Test012SessionSnapshot:
    def test_snapshot_has_no_zero_sentinel_for_disabled_policy(self):
        """SUP02-MODEL-003: PROFILE_DEFAULT (nem alkalmazott) policy esetén
        a discharge_current_A None, NEM 0.0."""
        model = DynamicSessionModel(c10_capacity_Ah=7.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.PROFILE_DEFAULT)
        assert model.discharge_current_A is None
        assert model.get("discharge_current_A").source == ParamSource.NOT_APPLICABLE

    def test_snapshot_contains_revision_and_validation(self):
        model = DynamicSessionModel(c10_capacity_Ah=1.0, psu_mode="INDEPENDENT")
        model.validate_effective_config()
        snap = model.snapshot()
        assert snap["revision"] == model.config_revision
        assert snap["validation_state"] == "OK"

    def test_summary_lists_custom_overrides(self):
        model = DynamicSessionModel(c10_capacity_Ah=7.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.CUSTOM, custom_A=0.5)
        summary = model.build_summary()
        assert "discharge_current_A" in summary["custom_overrides"]

    def test_summary_lists_blocking_error_for_incompatible_charge_current(self):
        model = DynamicSessionModel(c10_capacity_Ah=10.0, psu_mode="INDEPENDENT")
        summary = model.build_summary()
        assert len(summary["blocking_errors"]) == 1
        assert "CONFIG_INCOMPATIBLE" in summary["blocking_errors"][0]

    def test_summary_warns_on_series_psu_mode_for_12v_pack(self):
        """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F7 + Szoftver/
        Architektúra F6 (DFAM-026 declared_policy_without_consumer)] a
        "warnings" lista korábban szerkezetileg SOHA nem kapott append-et --
        a SafetyManager::check_psu_mode_compatibility() 12V-pack+SERIES
        figyelmeztetése (WarningCode.PSU_SERIES_MODE_UNUSUAL_FOR_12V_PACK)
        az egyetlen, Start-időben (SafetyManager felépítése előtt) kiértékel-
        hető safety-figyelmeztetés -- mostantól tényleg megjelenik itt."""
        model = DynamicSessionModel(c10_capacity_Ah=7.0, psu_mode="SERIES")
        assert model.cell_count <= 6  # a modell alapértelmezett profilja 12V/6-cellás
        summary = model.build_summary()
        assert len(summary["warnings"]) == 1
        assert "SERIES" in summary["warnings"][0]

    def test_summary_no_warning_for_independent_psu_mode_on_12v_pack(self):
        model = DynamicSessionModel(c10_capacity_Ah=7.0, psu_mode="INDEPENDENT")
        summary = model.build_summary()
        assert summary["warnings"] == []


class TestValidationStateEnum:
    def test_ok_and_config_incompatible_are_distinct(self):
        assert ValidationState.OK != ValidationState.CONFIG_INCOMPATIBLE


class TestR1B8cF24SummaryOverridesFilteredByVisibility:
    """[regresszió, R1 B8c, F-24 (P2), DFAM-026, D-B8-3 (b)] a
    build_summary() `custom_overrides` listája korábban a láthatóságtól
    függetlenül sorolta fel a USER_OVERRIDE forrású mezőket -- módváltáskor
    a CUSTOM policy draft nélkül átöröklődik (set_test_type()), így egy a
    célmódban rejtett, hatástalan érték "egyedi" beállításként jelent meg a
    Start-összegzőben. Mind a 4 mező az effective_config() láthatósági
    csoportjával szűrve."""

    def test_inherited_custom_cutoff_not_listed_in_ocv_soc(self):
        model = DynamicSessionModel(test_type="DISCHARGE_ONLY", c10_capacity_Ah=7.0)
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.CUSTOM, custom_V=11.0)
        assert "terminate_voltage_pack_V" in model.build_summary()["custom_overrides"]

        model.set_test_type("OCV_SOC_CHARACTERIZATION")
        summary = model.build_summary()

        assert "terminate_voltage_pack_V" not in summary
        assert "terminate_voltage_pack_V" not in summary["custom_overrides"]

    def test_inherited_custom_discharge_current_not_listed_in_charge_only(self):
        model = DynamicSessionModel(test_type="DISCHARGE_ONLY", c10_capacity_Ah=7.0)
        model.set_discharge_rate_policy(DischargeRatePolicy.CUSTOM, custom_A=0.5)
        model.set_test_type("CHARGE_ONLY")
        summary = model.build_summary()
        assert "discharge_current_A" not in summary
        assert "discharge_current_A" not in summary["custom_overrides"]

    def test_inherited_custom_charge_current_not_listed_in_discharge_only(self):
        model = DynamicSessionModel(
            test_type="CHARACTERIZATION", c10_capacity_Ah=7.0, psu_mode="INDEPENDENT",
        )
        model.set_charge_current_policy(ChargeCurrentPolicy.CUSTOM, custom_A=1.0)
        assert "requested_charge_current_A" in model.build_summary()["custom_overrides"]
        model.set_test_type("DISCHARGE_ONLY")
        summary = model.build_summary()
        assert "requested_charge_current_A" not in summary
        assert "requested_charge_current_A" not in summary["custom_overrides"]

    def test_inherited_custom_pulse_current_not_listed_in_characterization(self):
        model = DynamicSessionModel(
            test_type="OCV_SOC_CHARACTERIZATION", c10_capacity_Ah=7.0,
        )
        model.set_pulse_current_policy(PulseCurrentPolicy.CUSTOM, custom_A=1.2)
        assert "pulse_current_A" in model.build_summary()["custom_overrides"]
        model.set_test_type("DISCHARGE_ONLY")
        summary = model.build_summary()
        assert "pulse_current_A" not in summary
        assert "pulse_current_A" not in summary["custom_overrides"]

    def test_visible_custom_cutoff_still_listed_in_consumer_modes(self):
        """Nem-regresszió: a fogyasztó módokban a látható CUSTOM cutoff
        továbbra is szerepel."""
        for test_type in ("DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL"):
            model = DynamicSessionModel(test_type=test_type, c10_capacity_Ah=7.0)
            model.set_terminate_voltage_policy(
                TerminateVoltagePolicy.CUSTOM, custom_V=11.0,
            )
            summary = model.build_summary()
            assert summary["terminate_voltage_pack_V"] == pytest.approx(11.0), test_type
            assert "terminate_voltage_pack_V" in summary["custom_overrides"], test_type


class TestR9B1ProfileChargeCurrentValidation:
    """R9-B1: CUSTOM ertek megorzese es ujravalidalasa a profilkorlathoz."""

    @pytest.mark.parametrize(
        "mode,blocked",
        [
            ("CHARGE_ONLY", True), ("CHARACTERIZATION", True),
            ("BQ_LEARNING_PHYSICAL", True), ("TRADITIONAL_LEARNING", True),
            ("GOLDEN_GG", True), ("CHEMID_CHARACTERIZATION_PBA", True),
            ("OCV_SOC_CHARACTERIZATION", True), ("IEC_DC_INTERNAL_RESISTANCE", True),
            ("DISCHARGE_ONLY", False),
        ],
    )
    def test_profile_excess_blocks_only_visible_group(self, mode, blocked):
        model = DynamicSessionModel(c10_capacity_Ah=7.2, psu_mode="PARALLEL")
        model.set_charge_current_policy(ChargeCurrentPolicy.CUSTOM, custom_A=3.0)
        model.set_test_type(mode)
        assert model.requested_charge_current_A == pytest.approx(3.0)
        assert model.get("requested_charge_current_A").validation_state is ValidationState.CONFIG_INCOMPATIBLE
        expected = ValidationState.CONFIG_INCOMPATIBLE if blocked else ValidationState.OK
        assert model.validate_effective_config() is expected
        assert model.is_start_allowed() is (not blocked)
        errors = model.build_summary()["blocking_errors"]
        if blocked:
            assert len(errors) == 1
            assert "CONFIG_INCOMPATIBLE" in errors[0]
            assert "kért töltőáram 3.0 A > profilmaximum 1.8 A" in errors[0]
            assert "profilmaximum" in errors[0]
        else:
            assert errors == []

    @pytest.mark.parametrize("profile_max_A", [None, 1.2])
    @pytest.mark.parametrize("offset_A", [-0.1, 0.0, 0.000001])
    def test_profile_boundary_and_override(self, profile_max_A, offset_A):
        model = DynamicSessionModel(c10_capacity_Ah=7.2, psu_mode="PARALLEL")
        model.set_max_charge_current_A_profile_override(profile_max_A)
        limit = 1.8 if profile_max_A is None else profile_max_A
        model.set_charge_current_policy(ChargeCurrentPolicy.CUSTOM, custom_A=limit + offset_A)
        expected = ValidationState.CONFIG_INCOMPATIBLE if offset_A > 0 else ValidationState.OK
        assert model.validate_effective_config() is expected
        assert model.is_start_allowed() is (offset_A <= 0)
        assert model.requested_charge_current_A == pytest.approx(limit + offset_A)

    @pytest.mark.parametrize("change", ["capacity", "profile"])
    def test_lower_limit_revalidates_preserved_custom(self, change):
        model = DynamicSessionModel(c10_capacity_Ah=12.0, psu_mode="PARALLEL")
        model.set_psu_mode("PARALLEL")
        model.set_charge_current_policy(ChargeCurrentPolicy.CUSTOM, custom_A=2.0)
        assert model.validate_effective_config() is ValidationState.OK
        assert model.is_start_allowed()
        if change == "capacity":
            model.set_capacity(7.2)
        else:
            model.set_battery_profile("FIAMM_12V")
            model.set_max_charge_current_A_profile_override(1.2)
        assert model.requested_charge_current_A == pytest.approx(2.0)
        assert not model.is_start_allowed()
        assert model.validate_effective_config() is ValidationState.CONFIG_INCOMPATIBLE
        assert not model.is_start_allowed()

    def test_hardware_limit_still_blocks_with_larger_profile(self):
        model = DynamicSessionModel(c10_capacity_Ah=20.0, psu_mode="PARALLEL")
        model.set_charge_current_policy(ChargeCurrentPolicy.CUSTOM, custom_A=3.1)
        assert model.validate_effective_config() is ValidationState.CONFIG_INCOMPATIBLE
        errors = model.build_summary()["blocking_errors"]
        assert len(errors) == 1
        assert "PSU PARALLEL limit 3.0 A" in errors[0]


class TestR16B16aRatePresetValidation:
    @pytest.mark.parametrize("profile", ["FIAMM_12V", "FIAMM_24V"])
    @pytest.mark.parametrize("rate,allowed", [
        (DischargeRatePolicy.C20, True), (DischargeRatePolicy.C10, False),
        (DischargeRatePolicy.C5, False), (DischargeRatePolicy.CUSTOM, False),
    ])
    def test_preset_floor_depends_on_proven_rate(self, profile, rate, allowed):
        model = DynamicSessionModel(battery_profile_name=profile, c10_capacity_Ah=7.2, test_type="DISCHARGE_ONLY")
        model.set_battery_model("FG20721")
        model.set_discharge_rate_policy(rate, custom_A=0.36)
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.V_1_75_PER_CELL)
        assert model.terminate_voltage_pack_V == 1.75 * model.cell_count
        model.validate_effective_config()
        assert model.is_start_allowed() is allowed

    @pytest.mark.parametrize("kind", ["current", "cutoff"])
    @pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -float("inf")])
    def test_invalid_custom_is_blocked_without_replacement(self, kind, bad):
        model = DynamicSessionModel(c10_capacity_Ah=7.2, test_type="DISCHARGE_ONLY")
        if kind == "current":
            model.set_discharge_rate_policy(DischargeRatePolicy.CUSTOM, custom_A=bad)
            requested = model.discharge_current_A
        else:
            model.set_terminate_voltage_policy(TerminateVoltagePolicy.CUSTOM, custom_V=bad)
            requested = model.terminate_voltage_pack_V
        assert requested is bad
        assert model.validate_effective_config() == ValidationState.CONFIG_INCOMPATIBLE
        assert not model.is_start_allowed()

    def test_measured_capacity_and_model_change_revoke_c20_permission(self):
        model = DynamicSessionModel(c10_capacity_Ah=7.2, test_type="DISCHARGE_ONLY")
        model.set_battery_model("FG20721")
        model.set_discharge_rate_policy(DischargeRatePolicy.C20)
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.V_1_75_PER_CELL)
        model.validate_effective_config()
        assert model.is_start_allowed()
        model.set_measured_capacity(7.2)
        model.set_capacity_basis_policy(CapacityBasisPolicy.MEASURED_USABLE_CAPACITY)
        model.validate_effective_config()
        assert not model.is_start_allowed()
        model.set_capacity_basis_policy(CapacityBasisPolicy.NOMINAL_C10_CAPACITY)
        model.set_battery_model("OTHER")
        model.validate_effective_config()
        assert not model.is_start_allowed()

    def test_unsafe_profile_blocks_start_even_with_higher_preset(self):
        model = DynamicSessionModel(c10_capacity_Ah=7.2, test_type="DISCHARGE_ONLY")
        model.set_terminate_voltage_per_cell_default_V(1.60)
        model.set_terminate_voltage_policy(TerminateVoltagePolicy.V_1_80_PER_CELL)
        model.validate_effective_config()
        assert not model.is_start_allowed()
