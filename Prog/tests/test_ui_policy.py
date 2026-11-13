"""SUP02-ARCH-003 — TestModePolicy registry unit tesztek."""
from __future__ import annotations

import re
from pathlib import Path

from Prog.gui.ui_policy import (
    ALWAYS_VISIBLE_GROUPS,
    TEST_MODE_POLICIES,
    is_group_required,
    is_group_visible,
    visible_groups_for,
)
from Prog.src.test_runner import TestType


class TestAlwaysVisibleGroups:
    def test_always_visible_present_in_every_mode(self):
        # C14/SW-P3-2: a TestType enum a ground truth, NEM a
        # TEST_MODE_POLICIES kulcsai -- ha egy TestType kimaradna a
        # registry-ből, a régi `for test_type in TEST_MODE_POLICIES`
        # tautologikusan zöld maradt volna (csak a meglévő kulcsokon
        # futott végig).
        for test_type in TestType:
            visible = visible_groups_for(test_type.value)
            assert ALWAYS_VISIBLE_GROUPS <= visible, f"{test_type} hiányos alap-csoportokkal"


class TestC14TestTypeRegistryCompleteness:
    def test_every_test_type_has_a_policy(self):
        for test_type in TestType:
            assert test_type.value in TEST_MODE_POLICIES, (
                f"{test_type} nincs regisztrálva a TEST_MODE_POLICIES-ban"
            )

    def test_no_orphan_policy(self):
        test_type_values = {t.value for t in TestType}
        for key in TEST_MODE_POLICIES:
            assert key in test_type_values, (
                f"{key} a TEST_MODE_POLICIES-ban van, de nincs TestType tagja"
            )

    def test_config_panel_combo_lists_exactly_the_registered_types(self):
        # Nincs PySide6-import a nem-GUI suite-ban -- a config_panel.py-t
        # nyers szövegként olvassuk, csak a addItems([...]) hívás listáját
        # keresve.
        config_panel_path = (
            Path(__file__).resolve().parent.parent / "gui" / "panels" / "config_panel.py"
        )
        source = config_panel_path.read_text(encoding="utf-8")
        match = re.search(
            r"self\._test_type_combo\.addItems\(\s*\[(.*?)\]\s*\)", source, re.DOTALL
        )
        assert match is not None, "nem található a teszttípus-combo addItems() hívása"
        combo_values = set(re.findall(r'"([A-Z_0-9]+)"', match.group(1)))
        test_type_values = {t.value for t in TestType}
        assert combo_values == test_type_values, (
            f"combo/TestType eltérés: csak combo-ban={combo_values - test_type_values}, "
            f"csak TestType-ban={test_type_values - combo_values}"
        )

    def test_visible_groups_for_is_fail_closed_on_unknown_test_type(self):
        """[MAX verifier P3-4 lelete, Kör 19 Batch D4] `visible_groups_for()`
        SZÁNDÉKOSAN fail-CLOSED (üres halmazt ad ismeretlen test_type-ra),
        szemben `is_group_visible()` SZÁNDÉKOS fail-OPEN viselkedésével
        (l. main_window.py::_make_relax_ctrl(), ahol ez a különbség
        terhelő -- egy fail-open választás egy ismeretlen módot a RÖVIDEBB,
        operátor-konfigurált relaxidőre engedett volna esni a
        konzervatívabb alapérték helyett). Ma strukturálisan elérhetetlen
        (_build_runner() `raise ValueError` egy ismeretlen test_type-ra,
        jóval a relax-dispatch előtt), de ez a teszt rögzíti a szerződést,
        hogy egy jövőbeli mutáció (fail-open-ra cserélve) ne maradjon
        észrevétlen."""
        assert visible_groups_for("NINCS_ILYEN_TEST_TYPE") == frozenset()


class TestSup02Ui002ChargeOnly:
    def test_charge_fields_visible(self):
        assert is_group_visible("CHARGE_ONLY", "charge_current")
        assert is_group_visible("CHARGE_ONLY", "taper")

    def test_discharge_and_ocv_soc_hidden(self):
        assert not is_group_visible("CHARGE_ONLY", "discharge_rate")
        assert not is_group_visible("CHARGE_ONLY", "soc_grid_mode")
        assert not is_group_visible("CHARGE_ONLY", "pulse_current")


class TestTraditionalLearningAndGoldenGgChargeCurrentVisibility:
    """2026-08-23 (felhasználói review, 2. kör — traditional_learning OV
    incidens): a töltőáram mostantól LÁTHATÓ (de nem kötelező) ezekben a
    módokban — korábban a kezelő nem látta/állíthatta, holott a controller
    valódi, többórás fizikai töltést végez (l. main_window.py
    shared_charge_cfg)."""

    def test_traditional_learning_charge_current_visible_not_required(self):
        assert is_group_visible("TRADITIONAL_LEARNING", "charge_current")
        assert not is_group_required("TRADITIONAL_LEARNING", "charge_current")
        assert is_group_required("TRADITIONAL_LEARNING", "bq_port")

    def test_golden_gg_charge_current_visible_not_required(self):
        assert is_group_visible("GOLDEN_GG", "charge_current")
        assert not is_group_required("GOLDEN_GG", "charge_current")
        assert is_group_required("GOLDEN_GG", "bq_port")
        assert is_group_required("GOLDEN_GG", "golden_gg_system_rate_current")


class TestSharedChargeCfgModesShowTaperAndChargeCurrent:
    """[regresszió, Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/
    Állapot P2 lelet #2] main_window.py::_build_runner() egyetlen közös
    shared_charge_cfg-et (taper_hold_s=cfg.taper_hold_s, charge_current_
    A_override=cfg.charge_current_A_override) ad át MIND a 6 fizikai
    töltő módnak (CHARGE_ONLY, CHARACTERIZATION, TRADITIONAL_LEARNING,
    GOLDEN_GG, OCV_SOC_CHARACTERIZATION, CHEMID_CHARACTERIZATION_PBA) —
    "taper" korábban TRADITIONAL_LEARNING/GOLDEN_GG/OCV_SOC_
    CHARACTERIZATION/CHEMID_CHARACTERIZATION_PBA egyikében sem volt
    látható, "charge_current" pedig OCV_SOC_CHARACTERIZATION/CHEMID_
    CHARACTERIZATION_PBA-ban hiányzott — egy rejtett widgeten álló, akár
    egy korábbi módból visszamaradt érték élesen befolyásolta a fizikai
    töltésvezérlést, anélkül, hogy a kezelő látta/állította volna
    (ugyanaz a hibaosztály, mint a 2026-08-23 OV-incidens után
    TRADITIONAL_LEARNING/GOLDEN_GG-nél már javított charge_current-hiány)."""

    _SHARED_CHARGE_CFG_TEST_TYPES = (
        "CHARGE_ONLY", "CHARACTERIZATION", "TRADITIONAL_LEARNING",
        "GOLDEN_GG", "OCV_SOC_CHARACTERIZATION", "CHEMID_CHARACTERIZATION_PBA",
    )

    def test_taper_visible_in_every_shared_charge_cfg_mode(self):
        for test_type in self._SHARED_CHARGE_CFG_TEST_TYPES:
            assert is_group_visible(test_type, "taper"), (
                f"{test_type}: 'taper' rejtve, holott shared_charge_cfg."
                f"taper_hold_s élesen vezérli a beágyazott ChargeControllert"
            )

    def test_charge_current_visible_in_every_shared_charge_cfg_mode(self):
        for test_type in self._SHARED_CHARGE_CFG_TEST_TYPES:
            assert is_group_visible(test_type, "charge_current"), (
                f"{test_type}: 'charge_current' rejtve, holott shared_charge_cfg."
                f"charge_current_A_override élesen vezérli a beágyazott ChargeControllert"
            )


class TestSup02Ui003DischargeOnly:
    def test_discharge_fields_visible(self):
        assert is_group_visible("DISCHARGE_ONLY", "discharge_rate")
        assert is_group_visible("DISCHARGE_ONLY", "terminate_voltage")
        assert is_group_visible("DISCHARGE_ONLY", "capacity_basis")

    def test_charge_taper_and_target_hidden(self):
        assert not is_group_visible("DISCHARGE_ONLY", "taper")
        assert not is_group_visible("DISCHARGE_ONLY", "charge_target")
        assert not is_group_visible("DISCHARGE_ONLY", "soc_grid_mode")


class TestSup02Ui004Characterization:
    def test_both_charge_and_discharge_visible(self):
        assert is_group_visible("CHARACTERIZATION", "charge_current")
        assert is_group_visible("CHARACTERIZATION", "discharge_rate")

    def test_ocv_soc_and_bq_hidden(self):
        assert not is_group_visible("CHARACTERIZATION", "soc_grid_mode")
        assert not is_group_visible("CHARACTERIZATION", "bq_precondition_checklist")


class TestSup02Ui005BqLearningPhysical:
    def test_bq_specific_fields_visible(self):
        assert is_group_visible("BQ_LEARNING_PHYSICAL", "bq_precondition_checklist")
        assert is_group_visible("BQ_LEARNING_PHYSICAL", "manual_checkpoint")

    def test_uniform_ocv_soc_step_hidden(self):
        assert not is_group_visible("BQ_LEARNING_PHYSICAL", "soc_grid_mode")
        assert not is_group_visible("BQ_LEARNING_PHYSICAL", "pulse_current")


class TestSup02Ui006OcvSocCharacterization:
    def test_grid_and_pulse_fields_visible(self):
        assert is_group_visible("OCV_SOC_CHARACTERIZATION", "soc_grid_mode")
        assert is_group_visible("OCV_SOC_CHARACTERIZATION", "pulse_current")
        assert is_group_visible("OCV_SOC_CHARACTERIZATION", "capacity_basis")

    def test_charge_current_and_taper_visible(self):
        """[frissítve, Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/
        Állapot P2 lelet #2] korábban rejtve volt — l.
        TestSharedChargeCfgModesShowTaperAndChargeCurrent docstringje: a
        PRECHARGE beágyazott ChargeController élesen a session
        shared_charge_cfg-jét kapja, ezt a kezelőnek látnia/állítania
        kell tudnia."""
        assert is_group_visible("OCV_SOC_CHARACTERIZATION", "charge_current")
        assert is_group_visible("OCV_SOC_CHARACTERIZATION", "taper")


class TestRequiredGroups:
    def test_charge_only_requires_charge_current(self):
        assert is_group_required("CHARGE_ONLY", "charge_current")
        assert not is_group_required("CHARGE_ONLY", "discharge_rate")

    def test_discharge_only_does_not_require_charge_current(self):
        """SUP02-VALID-001 explicit példa: CHARGE_ONLY ne bukjon el hiányzó
        discharge current miatt — itt a fordítottja: DISCHARGE_ONLY ne
        követeljen meg töltő-mezőt."""
        assert not is_group_required("DISCHARGE_ONLY", "charge_current")


class TestSup03ChemIdCharacterizationPba:
    """SUP03-APP-004/TEST-008: ChemID PbA mód csak a saját (C20 áram,
    kondicionálás, 5h relax, gyártói cutoff, GPCCHEM export) mezőit mutassa."""

    def test_chemid_fields_visible(self):
        assert is_group_visible("CHEMID_CHARACTERIZATION_PBA", "chemid_c20_current")
        assert is_group_visible("CHEMID_CHARACTERIZATION_PBA", "chemid_conditioning")
        assert is_group_visible("CHEMID_CHARACTERIZATION_PBA", "chemid_gpcchem_export")

    def test_learning_fields_hidden_but_charge_current_and_taper_visible(self):
        """[frissítve, Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/
        Állapot P2 lelet #2] charge_current/taper korábban rejtve volt — l.
        TestSharedChargeCfgModesShowTaperAndChargeCurrent docstringje: a
        FULL_CHARGE_MANUFACTURER_METHOD beágyazott ChargeController élesen
        a session shared_charge_cfg-jét kapja, ezt a kezelőnek látnia/
        állítania kell tudnia. soc_grid_mode/pulse_current (OCV_SOC-
        specifikus mezők) továbbra is rejtve maradnak."""
        assert is_group_visible("CHEMID_CHARACTERIZATION_PBA", "charge_current")
        assert is_group_visible("CHEMID_CHARACTERIZATION_PBA", "taper")
        assert not is_group_visible("CHEMID_CHARACTERIZATION_PBA", "soc_grid_mode")
        assert not is_group_visible("CHEMID_CHARACTERIZATION_PBA", "pulse_current")

    def test_other_modes_hide_chemid_fields(self):
        for mode in (
            "CHARGE_ONLY", "DISCHARGE_ONLY", "CHARACTERIZATION",
            "BQ_LEARNING_PHYSICAL", "OCV_SOC_CHARACTERIZATION",
        ):
            assert not is_group_visible(mode, "chemid_c20_current"), mode
            assert not is_group_visible(mode, "chemid_gpcchem_export"), mode

    def test_required_fields(self):
        assert is_group_required("CHEMID_CHARACTERIZATION_PBA", "chemid_c20_current")
        assert is_group_required("CHEMID_CHARACTERIZATION_PBA", "chemid_manufacturer_cutoff")
        assert not is_group_required("CHEMID_CHARACTERIZATION_PBA", "charge_current")


class TestUnknownTestType:
    def test_unknown_type_defaults_to_visible_true(self):
        assert is_group_visible("SOME_FUTURE_MODE", "anything")

    def test_unknown_type_defaults_to_not_required(self):
        assert not is_group_required("SOME_FUTURE_MODE", "anything")


class TestCf34RechargeRequiredWarningPolicy:
    """[regresszió, governance-v2 review-kör 1, CF-34, DFAM-026
    declared_policy_without_consumer] a "recharge_required_warning"
    csoport minden, dokumentáltan lemerítéssel záruló módnál (DISCHARGE_ONLY/
    CHARACTERIZATION/OCV_SOC_CHARACTERIZATION/CHEMID_CHARACTERIZATION_PBA/
    TRADITIONAL_LEARNING/GOLDEN_GG) kell hogy látható legyen -- a tényleges
    GUI-fogyasztó (main_window.py::_on_finished() DONE-ág) tesztje
    test_build_runner.py-ban van.

    Review-kör 2 (2026-09-09), friss GUI/UX F3 (DFAM-001): a TRADITIONAL_
    LEARNING/GOLDEN_GG felvéve -- az eredeti CF-34 sibling-keresés egy fix,
    4-elemű módlistára szűkült, nem a tényleges állapotgép-végállapotra,
    pedig mindkét mód lemerített akkuval zárul (l. live_panel.py
    _PHASE_MAP).

    Review-kör 4 (2026-09-09), friss GUI/UX F1 (P2, DFAM-001): a GUI F2
    fix (main_window.py "not keep_thread and result.status in (DONE,
    CHECKPOINT_STOPPED)" gate-bővítés) időközben megtörtént, tehát a
    korábbi blokkoló feltétel elhárult -- BQ_LEARNING_PHYSICAL is
    lemerített akkuval zár (utolsó energiaátadó lépése egy DISCHARGE),
    most már ez is felvéve."""

    def test_declared_for_all_discharge_ending_modes(self):
        for test_type in (
            "DISCHARGE_ONLY", "CHARACTERIZATION",
            "OCV_SOC_CHARACTERIZATION", "CHEMID_CHARACTERIZATION_PBA",
            "TRADITIONAL_LEARNING", "GOLDEN_GG", "BQ_LEARNING_PHYSICAL",
        ):
            assert is_group_visible(test_type, "recharge_required_warning"), test_type

    def test_not_declared_for_charge_ending_modes(self):
        """CHARGE_ONLY teljes töltéssel zár (nem lemerítve) -- itt a
        figyelmeztetés hamis riasztás lenne."""
        for test_type in ("CHARGE_ONLY",):
            assert not is_group_visible(test_type, "recharge_required_warning"), test_type


class TestIecDcInternalResistancePolicy:
    """Governance-v2 kör 2: az IEC_DC_INTERNAL_RESISTANCE mód a PRECHARGE
    fázis miatt "charge_current"/"taper"-t örökli a testvér összetett
    módoktól, és egy saját, csak-olvasható "i10_source_display" csoportot
    kap (a forrás nem enged szabad numerikus GUI-paramétert)."""

    def test_visible_groups(self):
        for group in ("charge_current", "taper", "temperature_monitoring", "i10_source_display"):
            assert is_group_visible("IEC_DC_INTERNAL_RESISTANCE", group), group

    def test_no_free_numeric_params_visible(self):
        """A forrás (IEC 60896-21 §6.3) minden időzítést rögzít -- nincs
        pl. "discharge_rate"/"pulse_current"-hoz hasonló szabad paraméter."""
        for group in ("discharge_rate", "pulse_current", "pulse_duration", "soc_grid_mode"):
            assert not is_group_visible("IEC_DC_INTERNAL_RESISTANCE", group), group

    def test_not_required_groups(self):
        """Nincs kötelező mező -- az I10 a profilból jön, nem GUI-bemenet."""
        assert not is_group_required("IEC_DC_INTERNAL_RESISTANCE", "i10_source_display")


# D9 (2026-09-13, Riportok/ui_policy_audit_20260913.md): teljes audit
# mind a 39 deklarált csoportnévre (DEFECT_FAMILY_REGISTRY.yaml DFAM-026
# `prevention` mezője ezt követelte meg, mielőtt a lenti meta-teszt
# bevezethető lenne fail-closed módon). A besorolás:
#   A = van production `is_group_visible()`/`is_group_required()`
#       hívóhelye (config_panel.py/derived_parameters.py) -- D9 F-02/
#       F-03/F-04/F-07 fixek után ide tartozik chemid_c20_current/
#       chemid_manufacturer_cutoff/partial_discharge_rate/
#       chemid_conditioning/chemid_relax_5h/charge_safety_limits/
#       discharge_safety_limits is.
#   B = ALWAYS_VISIBLE_GROUPS tag, szándékosan sosem gate-elt (a
#       QGroupBox feltétel nélkül épül, l. ui_policy.py kommentje).
#   C = nincs is_group_visible()-hívóhely, de EKVIVALENS, szigorúbb/
#       szélesebb fogyasztója van (l. az egyes csoportok kommentje).
#   D = tudatosan halasztott SUP02-funkció (Riportok/sup02_gui_gap_
#       matrix.md), nincs widget/backend-implementáció -- a csoport
#       deklarációja egy placeholder a jövőbeli munkához.
#   E = VALÓDI nyitott tétel: chemid_gpcchem_export (D9/F-06 más úton,
#       session_meta/popup-on keresztül oldta meg a láthatóságot, NEM
#       is_group_visible()-en át -- a csoport maga technikailag
#       fogyasztó nélkül marad, de a mögötte álló kockázat lezárva);
#       bq_precondition_checklist (D9/F-05, SOURCE_REQUIRED, felhasználói
#       döntés 2026-09-13: "visszatérünk rá", külön körre halasztva).
_GROUP_CATEGORY: dict[str, str] = {
    # A -- eredeti 15
    "temperature_compensation": "A", "charge_current": "A", "charge_target": "A",
    "discharge_rate": "A", "terminate_voltage": "A", "taper": "A",
    "relax_after_charge": "A", "uniform_step": "A", "bq_port": "A",
    "golden_gg_system_rate_current": "A", "i10_source_display": "A",
    "pulse_current": "A", "capacity_basis": "A", "recharge_required_warning": "A",
    "physical_cycle_only_warning": "A",
    # A -- D9 fixek után (F-02/F-03/F-04/F-07)
    "chemid_c20_current": "A", "chemid_manufacturer_cutoff": "A",
    "partial_discharge_rate": "A", "chemid_conditioning": "A", "chemid_relax_5h": "A",
    "charge_safety_limits": "A", "discharge_safety_limits": "A",
    # A -- R1 B8c (F-24): OCV_SOC csak-olvasható profil-cutoff sor
    # (config_panel.py _update_visibility() fogyasztó).
    "ocv_soc_profile_cutoff": "A",
    # B -- ALWAYS_VISIBLE_GROUPS (temperature_compensation kivételével,
    # az A-ban van, mert ÖNÁLLÓ is_group_visible()-hívóhelye is van)
    "battery": "B", "instruments": "B", "psu_mode_and_wiring": "B",
    "effective_summary": "B", "validation_status": "B",
    # C -- ekvivalens fogyasztó
    "manual_checkpoint": "C", "temperature_monitoring": "C",
    "report_quality_summary": "C",
    # D -- tudatosan halasztott SUP02-funkció
    "soc_grid_mode": "D", "relax_policy": "D", "pulse_duration": "D",
    "target_evaluation_times": "D", "acquisition_mode": "D",
    "estimated_duration": "D", "output_table_summary": "D",
    # E -- nyitott/más-úton-megoldott
    "chemid_gpcchem_export": "E", "bq_precondition_checklist": "E",
}


class TestD9StructuralPreventionAllGroupsClassified:
    """[D9, 2026-09-13, Riportok/ui_policy_audit_20260913.md,
    DEFECT_FAMILY_REGISTRY.yaml DFAM-026 `prevention` mező] fail-closed
    meta-teszt: minden ÚJ, a jövőben `TEST_MODE_POLICIES`-hoz adott
    csoportnévnek explicit be KELL kerülnie a `_GROUP_CATEGORY`
    allowlistbe (production-fogyasztóval VAGY dokumentált indokkal) --
    különben ez a teszt bukik, mielőtt a csoport csendben, fogyasztó
    nélkül a registryben ragadna (pont ez volt az F-01..F-08 findingek
    gyökeroka)."""

    def test_every_declared_group_is_classified(self):
        from Prog.gui.ui_policy import ALWAYS_VISIBLE_GROUPS
        all_groups: set[str] = set(ALWAYS_VISIBLE_GROUPS)
        for policy in TEST_MODE_POLICIES.values():
            all_groups |= policy.visible_groups
        unclassified = all_groups - set(_GROUP_CATEGORY)
        assert not unclassified, (
            f"Új, osztályozatlan ui_policy csoport(ok): {sorted(unclassified)} -- "
            "sorold be a _GROUP_CATEGORY allowlistbe (A/B/C/D/E), l. a fenti "
            "kategória-leírást, mielőtt production-kódban felhasználnád."
        )

    def test_classification_allowlist_has_no_stale_entries(self):
        """Fordított irány: a _GROUP_CATEGORY ne hordozzon egy törölt
        csoportnevet sem (ha egy csoport kikerül a registryből, az
        allowlist-bejegyzését is törölni kell, hogy ne maradjon
        megtévesztő, nem létező csoportra utaló dokumentáció)."""
        from Prog.gui.ui_policy import ALWAYS_VISIBLE_GROUPS
        all_groups: set[str] = set(ALWAYS_VISIBLE_GROUPS)
        for policy in TEST_MODE_POLICIES.values():
            all_groups |= policy.visible_groups
        stale = set(_GROUP_CATEGORY) - all_groups
        assert not stale, f"Elavult _GROUP_CATEGORY bejegyzés(ek): {sorted(stale)}"

    def test_category_a_groups_have_a_real_consumer_call_site(self):
        """A "A" kategóriájú csoportoknak VALÓDI production hívóhelye
        legyen -- nem csak a tesztekben. Ez a teszt a forrásfájlokat
        grep-eli, hogy a kategorizálás ne csak jóhiszemű állítás legyen."""
        import pathlib
        prod_text = ""
        for path in (
            pathlib.Path("Prog/gui/panels/config_panel.py"),
            pathlib.Path("Prog/gui/derived_parameters.py"),
            pathlib.Path("Prog/gui/main_window.py"),
        ):
            prod_text += path.read_text(encoding="utf-8")
        for group, category in _GROUP_CATEGORY.items():
            if category != "A":
                continue
            assert (
                f'"{group}"' in prod_text or f"'{group}'" in prod_text
            ), f"{group} A-kategóriás, de nincs rá idézett hivatkozás config_panel.py/derived_parameters.py-ban"


class TestR1B8cF24TerminateVoltageOnlyWhereConsumed:
    """[regresszió, R1 B8c, F-24 (P2), DFAM-026] az OCV_SOC policy a
    szerkeszthető "terminate_voltage" csoportot látta, holott az
    OcvSocController a profil cutoffját használja (ocv_soc_controller.py
    `profile.terminate_voltage_pack_V`) -- a hatástalan érték a Start-
    összegzőbe és a config-hashbe került. A cfg-szintű cutoffot KIZÁRÓLAG a
    DischargeController fogyasztja (main_window.py::_make_discharge_ctrl()),
    tehát csak azokban a módokban lehet látható, amelyek tervében van
    StepKind.DISCHARGE lépés."""

    def test_ocv_soc_hides_editable_cutoff_and_shows_readonly_profile_cutoff(self):
        assert not is_group_visible("OCV_SOC_CHARACTERIZATION", "terminate_voltage")
        assert is_group_visible("OCV_SOC_CHARACTERIZATION", "ocv_soc_profile_cutoff")

    def test_readonly_profile_cutoff_group_only_in_ocv_soc(self):
        for test_type in TestType:
            expected = test_type.value == "OCV_SOC_CHARACTERIZATION"
            assert (
                is_group_visible(test_type.value, "ocv_soc_profile_cutoff") is expected
            ), test_type

    def test_terminate_voltage_visible_exactly_in_modes_with_discharge_step(self):
        from Prog.src.test_runner import StepKind, TestPlan

        consumer_modes: set[str] = set()
        for test_type in TestType:
            plan = getattr(TestPlan, test_type.value.lower())()
            assert plan.test_type == test_type
            if any(step.kind == StepKind.DISCHARGE for step in plan.steps):
                consumer_modes.add(test_type.value)
        assert consumer_modes == {
            "DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL",
        }
        visible_modes = {
            t.value for t in TestType if is_group_visible(t.value, "terminate_voltage")
        }
        assert visible_modes == consumer_modes
