"""
ConfigPanel widget-szintű regressziós tesztek — SUP02-REACT-006 (wiring reset)
és SUP02-VALID-002 (nincs csendes hardware-clamp).
"""
from __future__ import annotations

import pytest

from Prog.gui.panels.config_panel import ConfigPanel


class TestP2SessionActiveDisablesDeviceSearch:
    """Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet: a
    "Eszközök keresése…" gomb korábban futó session/worker alatt is
    elérhető maradt — VISA erőforrás-versengést okozhatott volna egy aktív
    mérés kritikus DMM-visszacsatolású CC/CV szabályozási hurokjában."""

    def test_search_button_enabled_by_default(self, qapp):
        panel = ConfigPanel()
        assert panel._search_btn.isEnabled() is True

    def test_session_active_disables_search_button(self, qapp):
        panel = ConfigPanel()
        panel.set_session_active(True)
        assert panel._search_btn.isEnabled() is False

    def test_session_inactive_re_enables_search_button(self, qapp):
        panel = ConfigPanel()
        panel.set_session_active(True)
        panel.set_session_active(False)
        assert panel._search_btn.isEnabled() is True


class TestCF30SessionActiveLocksWholeConfigTab:
    """Governance-v2 review-kör 1 (CF-30, GUI-R1-12): korábban a
    set_session_active() KIZÁRÓLAG a _search_btn-t tiltotta le -- a
    Konfiguráció fül minden más mezője (profil, kapacitás, teszttípus,
    töltő-/kisütőáram, resource stringek, stb.) TOVÁBBRA IS szabadon
    szerkeszthető maradt egy futó session alatt, félrevezetve az
    operátort (a riport-snapshot maga helyes, csak a felület nem)."""

    def test_all_fields_enabled_by_default(self, qapp):
        panel = ConfigPanel()
        assert panel.isEnabled() is True
        assert panel._profile_combo.isEnabled() is True
        assert panel._capacity_spin.isEnabled() is True
        assert panel._test_type_combo.isEnabled() is True
        assert panel._discharge_current_spin.isEnabled() is True
        assert panel._psu_res_edit.isEnabled() is True

    def test_session_active_disables_whole_panel(self, qapp):
        panel = ConfigPanel()

        panel.set_session_active(True)

        assert panel.isEnabled() is False
        # Qt: egy letiltott ős alatt a gyerek widget isEnabled()-je is
        # False-t ad vissza, függetlenül a saját belső enabled flag-jétől.
        assert panel._profile_combo.isEnabled() is False
        assert panel._capacity_spin.isEnabled() is False
        assert panel._test_type_combo.isEnabled() is False
        assert panel._discharge_current_spin.isEnabled() is False
        assert panel._psu_res_edit.isEnabled() is False

    def test_session_inactive_re_enables_whole_panel(self, qapp):
        panel = ConfigPanel()
        panel.set_session_active(True)

        panel.set_session_active(False)

        assert panel.isEnabled() is True
        assert panel._profile_combo.isEnabled() is True
        assert panel._capacity_spin.isEnabled() is True
        assert panel._test_type_combo.isEnabled() is True
        assert panel._discharge_current_spin.isEnabled() is True
        assert panel._psu_res_edit.isEnabled() is True


class TestSup02WiringConfirmationReset:
    def test_profile_switch_to_24v_resets_wiring_confirmed(self, qapp):
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._psu_mode_combo.setCurrentText("SERIES")
        panel._wiring_confirmed_cb.setChecked(True)

        panel._profile_combo.setCurrentText("FIAMM_24V")

        assert panel._wiring_confirmed_cb.isChecked() is False

    def test_profile_switch_between_12v_variants_resets_wiring(self, qapp):
        """Bármilyen profilváltás reseteli a wiring confirmation-t, nemcsak a 24V ág."""
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("PARALLEL")
        panel._wiring_confirmed_cb.setChecked(True)

        panel._profile_combo.setCurrentText("FIAMM_24V")
        panel._profile_combo.setCurrentText("FIAMM_12V")

        assert panel._wiring_confirmed_cb.isChecked() is False


class TestTemperatureSensorModeSelection:
    """Sérült 4-wire PT100 RTD pótlása 2-wire NTC termisztorral — GUI-választó."""

    def test_default_reflects_local_config_yaml(self, qapp):
        """ConfigPanel.__init__() mindig a VALÓS local_config.yaml-t olvassa
        be (fejlesztői módban nincs izolálva, l. app_paths.local_config_path()) —
        a teszt ezért nem egy fix literált, hanem a config tényleges tartalmát
        (vagy annak hiányában az RTD_4W_PT100 alapértéket) várja el."""
        import yaml
        from Prog import app_paths
        cfg: dict = {}
        for path in (app_paths.default_config_path(), app_paths.local_config_path()):
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    cfg.update(yaml.safe_load(f) or {})
        expected = cfg.get("temperature_sensor_mode", "RTD_4W_PT100")

        panel = ConfigPanel()

        assert panel._temp_sensor_combo.currentText() == expected

    def test_selecting_thermistor_reflected_in_session_config(self, qapp):
        panel = ConfigPanel()
        panel._temp_sensor_combo.setCurrentText("THERMISTOR_2W_NTC_10K_B3950")
        assert panel.get_session_config().temperature_sensor_mode == (
            "THERMISTOR_2W_NTC_10K_B3950"
        )


class TestDefaultConfigYamlOnlyContainsConsumedKeys:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Metrológia/
    Adatintegritás P2 lelet] default_config.yaml korábban a lenti
    whitelisten KÍVÜL sok szekciót (timing/dmm_voltage/safety/
    psu_limits_by_mode/series_diode/taper) tartalmazott, amiket
    ConfigPanel._load_yaml() SEHOL nem olvas ki — a dmm_voltage.nplc_*
    értékek pedig AKTÍVAN eltértek a ténylegesen futó TestRunnerConfig.
    charge_discharge_nplc/relax_ocv_nplc-től (félrevezető, nem csak
    hatástalan). Ez a teszt megakadályozza, hogy a jövőben csendben
    visszakerüljön egy nem-fogyasztott kulcs — ha egy paramétert
    ténylegesen yaml-konfigurálhatóvá kell tenni, ahhoz ConfigPanel.
    _load_yaml()-t (és ezt a whitelistet) is bővíteni kell."""

    _CONSUMED_TOP_LEVEL_KEYS = frozenset({"instruments", "temperature_compensation_mode"})
    _CONSUMED_INSTRUMENT_PSU_KEYS = frozenset({"resource", "combination_mode"})
    _CONSUMED_INSTRUMENT_OTHER_KEYS = frozenset({"resource"})

    def _load_default_config(self) -> dict:
        import yaml

        from Prog import app_paths
        with app_paths.default_config_path().open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def test_top_level_keys_are_all_consumed(self):
        cfg = self._load_default_config()
        assert set(cfg.keys()) <= self._CONSUMED_TOP_LEVEL_KEYS

    def test_instruments_psu_keys_are_all_consumed(self):
        cfg = self._load_default_config()
        psu_keys = set(cfg.get("instruments", {}).get("psu", {}).keys())
        assert psu_keys <= self._CONSUMED_INSTRUMENT_PSU_KEYS

    def test_instruments_load_and_dmm_keys_are_all_consumed(self):
        cfg = self._load_default_config()
        instr = cfg.get("instruments", {})
        for name in ("load", "dmm_voltage", "dmm_temperature"):
            keys = set(instr.get(name, {}).keys())
            assert keys <= self._CONSUMED_INSTRUMENT_OTHER_KEYS, (
                f"instruments.{name} nem-fogyasztott kulcsot tartalmaz: "
                f"{keys - self._CONSUMED_INSTRUMENT_OTHER_KEYS}"
            )

    def test_default_config_still_loads_without_error(self, qapp):
        """Defense-in-depth: a szűkített yaml is hibátlanul betöltődik a
        valós ConfigPanel-en keresztül."""
        panel = ConfigPanel()
        assert panel is not None


class TestKor21BatchF8LocalConfigReadModifyWriteAndDeepMerge:
    """Kör 21 Batch F8 (ARC21-1 + ARC21-4(b), DFAM-017): (a)
    `_save_local_config()` korábban a 4 resource-stringet tartalmazó
    dict-et `yaml.dump()`-olta a TELJES `local_config.yaml` fölé -- minden
    MÁS kulcs (pl. `temperature_sensor_mode`,
    `instruments.psu.combination_mode`) egy routin mentésre CSENDBEN
    megsemmisült. (b) `_load_yaml()` `cfg.update(loaded)`-je sekély merge
    volt -- `local_config.yaml` `instruments`-blokkja EGÉSZBEN felülírta a
    `default_config.yaml`-ét, akkor is, ha nem tartalmazta MINDEN
    almezőt. MINDKÉT teszt SAJÁT, izolált `tmp_path`-alapú konfig-fájlokat
    használ (`app_paths.local_config_path`/`default_config_path`
    monkeypatch-elve) -- a `local_config_path()` fejlesztői módban NEM a
    `bundle_dir()`-en megy át, tehát a valós repo-fájl védtelen maradna
    enélkül."""

    def _isolate_config_paths(self, monkeypatch, tmp_path):
        from Prog import app_paths
        local_path = tmp_path / "local_config.yaml"
        default_path = tmp_path / "default_config.yaml"
        monkeypatch.setattr(app_paths, "local_config_path", lambda: local_path)
        monkeypatch.setattr(app_paths, "default_config_path", lambda: default_path)
        return local_path, default_path

    def test_save_preserves_existing_unrelated_keys(self, qapp, monkeypatch, tmp_path):
        import yaml

        local_path, default_path = self._isolate_config_paths(monkeypatch, tmp_path)
        default_path.write_text("instruments: {}\n", encoding="utf-8")
        local_path.write_text(
            "temperature_sensor_mode: THERMISTOR_2W_NTC_10K_B3950\n"
            "instruments:\n"
            "  psu:\n"
            "    resource: USB0::OLD::INSTR\n"
            "    combination_mode: SERIES\n",
            encoding="utf-8",
        )
        panel = ConfigPanel()
        panel._psu_res_edit.setText("USB0::NEW::INSTR")

        panel._save_local_config()

        with local_path.open(encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["temperature_sensor_mode"] == "THERMISTOR_2W_NTC_10K_B3950"
        assert saved["instruments"]["psu"]["combination_mode"] == "SERIES"
        assert saved["instruments"]["psu"]["resource"] == "USB0::NEW::INSTR"

    def test_save_when_local_config_did_not_exist_yet(self, qapp, monkeypatch, tmp_path):
        """Counter-teszt: az ELSŐ mentés (nincs még helyi fájl) ne
        hibázzon -- a read-modify-write ág `existing={}`-re essen vissza."""
        import yaml

        local_path, default_path = self._isolate_config_paths(monkeypatch, tmp_path)
        default_path.write_text("instruments: {}\n", encoding="utf-8")
        assert not local_path.exists()
        panel = ConfigPanel()
        panel._psu_res_edit.setText("USB0::FIRST::INSTR")

        panel._save_local_config()

        with local_path.open(encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["instruments"]["psu"]["resource"] == "USB0::FIRST::INSTR"

    def test_load_deep_merges_default_instruments_block_into_local(
        self, qapp, monkeypatch, tmp_path
    ):
        """DISZKRIMINÁLÓ: a `default_config.yaml`
        `instruments.psu.combination_mode: SERIES`-e a fix ELŐTTI sekély
        merge alatt eltűnt volna, mert `local_config.yaml`
        `instruments.psu`-ja NEM tartalmazza ezt a kulcsot -- csak a
        `resource`-t."""
        default_path_content = (
            "instruments:\n"
            "  psu:\n"
            "    combination_mode: SERIES\n"
        )
        local_path, default_path = self._isolate_config_paths(monkeypatch, tmp_path)
        default_path.write_text(default_path_content, encoding="utf-8")
        local_path.write_text(
            "instruments:\n"
            "  psu:\n"
            "    resource: USB0::LOCAL::INSTR\n",
            encoding="utf-8",
        )

        panel = ConfigPanel()

        idx = panel._psu_mode_combo.findText("SERIES")
        assert idx >= 0
        assert panel._psu_mode_combo.currentIndex() == idx
        assert panel._psu_res_edit.text() == "USB0::LOCAL::INSTR"


class TestSup02NoSilentHardwareClamp:
    def test_charge_current_spin_not_silently_overwritten(self, qapp):
        """Korábban: PARALLEL módban beírt 2.5A, majd INDEPENDENT-re váltva a
        spinbox értékét csendben 1.5A-re írta át. Most: az érték MEGMARAD."""
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("PARALLEL")
        panel._charge_current_spin.setValue(2.5)

        panel._psu_mode_combo.setCurrentText("INDEPENDENT")

        assert panel._charge_current_spin.value() == 2.5  # NEM lett átírva

    def test_charge_current_spin_maximum_not_capped_to_hw_limit(self, qapp):
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("INDEPENDENT")
        panel._update_charge_current_info()
        assert panel._charge_current_spin.maximum() > 1.5

    def test_label_shows_config_incompatible_when_over_hw_limit(self, qapp):
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("INDEPENDENT")
        panel._capacity_spin.setValue(10.0)  # 0.25*10 = 2.5A > 1.5A INDEPENDENT limit

        panel._update_charge_current_info()

        assert "CONFIG_INCOMPATIBLE" in panel._charge_current_label.text()

    def test_label_shows_plain_calculated_when_compatible(self, qapp):
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("INDEPENDENT")
        panel._capacity_spin.setValue(1.0)  # 0.25*1 = 0.25A < 1.5A

        panel._update_charge_current_info()

        assert "CONFIG_INCOMPATIBLE" not in panel._charge_current_label.text()
        assert "0.25" in panel._charge_current_label.text()

    def test_hw_max_uses_shared_engine_constant(self, qapp):
        """SUP02-ARCH-004: a PSU hardver-limit egy közös forrásból jön, nem
        egy önálló, a config_panel.py-ban külön másolt konstansból."""
        from Prog.gui.derived_parameters import PSU_HW_MAX_CURRENT_A
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("PARALLEL")
        assert panel._psu_hw_max_A() == PSU_HW_MAX_CURRENT_A["PARALLEL"]


class TestWpAk03ReactiveWiring:
    """WP-AK-03: a ConfigPanel mostantól a DynamicSessionModel-en keresztül
    számol — SUP02-ACCEPT-001 2/3/5 pontjai."""

    def test_capacity_change_updates_discharge_current_reactively(self, qapp):
        """SUP02-ACCEPT-001/2: C-rate preset dependency-változásra automatikusan
        frissül, ÚJRA-VÁLASZTÁS (preset gomb) nélkül."""
        panel = ConfigPanel()
        panel._capacity_spin.setValue(1.0)
        assert panel._discharge_current_spin.value() == pytest.approx(0.2, abs=0.01)  # 1.0/5 (alap C5)

        panel._capacity_spin.setValue(10.0)  # nincs újra preset-kattintás

        assert panel._discharge_current_spin.value() == pytest.approx(2.0, abs=0.01)  # 10.0/5

    def test_profile_change_updates_terminate_voltage_reactively(self, qapp):
        """SUP02-ACCEPT-001/3: 12/24V profilváltás minden pack-feszültséget frissít."""
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        v_12 = panel._model.terminate_voltage_pack_V
        assert v_12 == pytest.approx(6 * 1.80, abs=0.01)

        panel._profile_combo.setCurrentText("FIAMM_24V")

        v_24 = panel._model.terminate_voltage_pack_V
        assert v_24 == pytest.approx(12 * 1.80, abs=0.01)

    def test_custom_discharge_current_not_overwritten_by_capacity_change(self, qapp):
        """SUP02-ACCEPT-001/5: custom override nem iródik felül."""
        panel = ConfigPanel()
        panel._discharge_current_spin.setValue(3.33)  # egyedi, nem preset érték

        panel._capacity_spin.setValue(20.0)  # kapacitásváltozás

        assert panel._discharge_current_spin.value() == pytest.approx(3.33, abs=0.01)

    def test_custom_terminate_voltage_not_overwritten_by_profile_change(self, qapp):
        panel = ConfigPanel()
        panel._discharge_term_v_spin.setValue(11.5)  # egyedi érték

        panel._capacity_spin.setValue(5.0)  # nem érinti a végfeszültséget

        assert panel._discharge_term_v_spin.value() == pytest.approx(11.5, abs=0.01)

    def test_preset_button_sets_reactive_policy_not_one_shot_value(self, qapp):
        """A preset gomb policy-t választ (nem egyszeri számbeírást) — utána a
        kapacitásváltozás ÚJRA-KATTINTÁS nélkül is frissíti az értéket."""
        panel = ConfigPanel()
        panel._capacity_spin.setValue(10.0)
        panel._apply_discharge_preset(10, 1.80)  # C/10 preset
        assert panel._discharge_current_spin.value() == pytest.approx(1.0, abs=0.01)

        panel._capacity_spin.setValue(20.0)

        assert panel._discharge_current_spin.value() == pytest.approx(2.0, abs=0.01)  # 20/10, ÚJRA C/10

    def test_get_session_config_discharge_current_always_resolved(self, qapp):
        """A kisütőáram a SessionConfig-ba mindig a modell felbontott (nem
        0.0-sentinel) értékét kapja — a régi main_window.py hardcodolt
        divisor=10 fallback útvonal innentől halott kód, de nem törik."""
        panel = ConfigPanel()
        panel._capacity_spin.setValue(7.2)
        cfg = panel.get_session_config()
        assert cfg.discharge_current_A == pytest.approx(7.2 / 5, abs=0.01)  # alap C5

    def test_get_session_config_charge_current_profile_max_stays_zero(self, qapp):
        """PROFILE_MAX policy esetén a charge_current_A_override VÁLTOZATLANUL
        0.0 — a ChargeController saját, profil-specifikus auto-logikáját nem
        szabad felülírni a modell generikus 0.25×C10 formulájával."""
        panel = ConfigPanel()
        panel._capacity_spin.setValue(7.2)  # nincs egyedi töltőáram megadva
        cfg = panel.get_session_config()
        assert cfg.charge_current_A_override == 0.0

    def test_get_session_config_charge_current_custom_resolved(self, qapp):
        panel = ConfigPanel()
        panel._charge_current_spin.setValue(1.2)
        cfg = panel.get_session_config()
        assert cfg.charge_current_A_override == pytest.approx(1.2, abs=0.01)

    def test_wiring_confirmed_checkbox_syncs_to_model(self, qapp):
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("SERIES")
        panel._wiring_confirmed_cb.setChecked(True)
        assert panel._model.wiring_confirmed is True

    def test_test_type_change_preserves_custom_draft_per_mode(self, qapp):
        """SUP02-CONFIG-001: mód-specifikus draft — a modell mostantól
        ténylegesen bekötve, a widget is tükrözi a visszaváltáskori draftot."""
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        panel._discharge_current_spin.setValue(3.33)

        panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")

        assert panel._discharge_current_spin.value() == pytest.approx(3.33, abs=0.01)

    def test_apply_discharge_preset_known_divisors_still_work(self, qapp):
        """Kör 19 D8b (SW19-06, DFAM-004): a 3 valós preset-gomb (C5/C10/
        C20) hívóértéke NEM változott -- csak az ismeretlen divisor-ág
        (lent) dob most explicit hibát a korábbi csendes CUSTOM-fallback
        helyett."""
        from Prog.gui.derived_parameters import DischargeRatePolicy

        panel = ConfigPanel()
        panel._apply_discharge_preset(5, 1.80)
        assert panel._model.discharge_rate_policy == DischargeRatePolicy.C5
        panel._apply_discharge_preset(10, 1.80)
        assert panel._model.discharge_rate_policy == DischargeRatePolicy.C10
        panel._apply_discharge_preset(20, 1.75)
        assert panel._model.discharge_rate_policy == DischargeRatePolicy.C20

    def test_apply_discharge_preset_unknown_divisor_raises(self, qapp):
        """[regresszió, Kör 19 D8b, SW19-06, DFAM-004 silent_catchall_
        dispatch] korábban `policy_by_divisor.get(divisor,
        DischargeRatePolicy.CUSTOM)` egy ismeretlen divisor-t CSENDBEN
        CUSTOM-ra váltott volna -- ez a teszt a régi kódon (mock/monkeypatch
        nélkül, közvetlenül hívva egy nem-létező divisor-ral) BUKNA (nem
        dobna semmit, a modell csendben CUSTOM-ra váltana)."""
        panel = ConfigPanel()
        with pytest.raises(ValueError, match="Unknown discharge preset divisor"):
            panel._apply_discharge_preset(7, 1.80)


class TestWpAk03UiPolicyVisibility:
    """WP-AK-03 (SUP02-ACCEPT-001/1/8): tesztmódváltás után csak a releváns
    csoportok látszanak, `ui_policy.py` a mérvadó forrás."""

    def test_characterization_shows_charge_and_discharge_boxes(self, qapp):
        # widget.isVisible() csak akkor True, ha a szülő is látható (ablak
        # nincs show()-olva a tesztben) — ezért isVisibleTo(panel)-t nézzük.
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert panel._charge_box.isVisibleTo(panel) is True
        assert panel._discharge_box.isVisibleTo(panel) is True
        assert panel._temp_box.isVisibleTo(panel) is True

    def test_discharge_only_hides_charge_box(self, qapp):
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        assert panel._charge_box.isVisibleTo(panel) is False
        assert panel._discharge_box.isVisibleTo(panel) is True
        # 2026-08-23 (felhasználói kérés): a "temperature_compensation" (a
        # hőmérséklet-DMM szenzortípus-választóval együtt) mostantól
        # ALWAYS_VISIBLE_GROUPS tagja — minden módban látható/állítható,
        # hogy a kezelő a fizikailag bekötött szondát (pl. 2-eres NTC
        # pótszonda) bármelyik teszttípusnál kiválaszthassa (l.
        # ui_policy.py ALWAYS_VISIBLE_GROUPS kommentje).
        assert panel._temp_box.isVisibleTo(panel) is True

    def test_charge_only_hides_discharge_box(self, qapp):
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARGE_ONLY")
        assert panel._charge_box.isVisibleTo(panel) is True
        assert panel._discharge_box.isVisibleTo(panel) is False

    def test_ocv_soc_shows_step_row_and_charge_box(self, qapp):
        """[frissítve, Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/
        Állapot P2 lelet #2] _charge_box korábban rejtve volt itt — a
        PRECHARGE beágyazott ChargeController élesen a session
        shared_charge_cfg-jét (töltőáram/taper) kapja, ezt a kezelőnek
        látnia/állítania kell tudnia (l. ui_policy.py
        OCV_SOC_CHARACTERIZATION policy kommentje)."""
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("OCV_SOC_CHARACTERIZATION")
        assert panel._charge_box.isVisibleTo(panel) is True
        assert panel._test_form.isRowVisible(panel._taper_spin) is True
        assert panel._test_form.isRowVisible(panel._ocv_soc_step_spin) is True

    def test_characterization_hides_ocv_soc_step_row(self, qapp):
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert panel._test_form.isRowVisible(panel._ocv_soc_step_spin) is False

    def test_bq_learning_hides_relax_after_charge_row(self, qapp):
        """BQ_LEARNING_PHYSICAL a 'relax_policy' csoportot használja, nem a
        CHARGE_ONLY-stílusú 'relax_after_charge'-ot."""
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("BQ_LEARNING_PHYSICAL")
        assert panel._test_form.isRowVisible(panel._relax_after_charge_spin) is False
        assert panel._charge_box.isVisibleTo(panel) is True  # töltés releváns marad

    def test_chemid_selectable_and_hides_unrelated_groups(self, qapp):
        """WP-AK-05: CHEMID_CHARACTERIZATION_PBA választható a comboból, és
        egyetlen fix (C/20, 5h relax) paraméterű módként a discharge/
        ocv_soc/relax_after_charge widgetek nem relevánsak rá (ui_policy.py
        CHEMID_CHARACTERIZATION_PBA policy-ja ezeket nem sorolja fel) — a
        generikus, meglévő láthatósági mechanizmus enélkül is helyesen
        elrejti őket, nem kell hozzá új widget ehhez a körhöz.

        [frissítve, Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/
        Állapot P2 lelet #2] _charge_box/taper_spin korábban tévesen a
        "nem releváns" listában szerepelt — a FULL_CHARGE_MANUFACTURER_
        METHOD beágyazott ChargeController élesen a session shared_charge_
        cfg-jét (töltőáram/taper) kapja, ezt a kezelőnek látnia/állítania
        kell tudnia (l. ui_policy.py CHEMID_CHARACTERIZATION_PBA policy
        kommentje)."""
        panel = ConfigPanel()
        assert panel._test_type_combo.findText("CHEMID_CHARACTERIZATION_PBA") >= 0

        panel._test_type_combo.setCurrentText("CHEMID_CHARACTERIZATION_PBA")

        assert panel._model.test_type == "CHEMID_CHARACTERIZATION_PBA"
        assert panel._charge_box.isVisibleTo(panel) is True
        assert panel._discharge_box.isVisibleTo(panel) is False
        assert panel._test_form.isRowVisible(panel._taper_spin) is True
        assert panel._test_form.isRowVisible(panel._ocv_soc_step_spin) is False
        assert panel._test_form.isRowVisible(panel._relax_after_charge_spin) is False

    def test_switching_away_from_chemid_restores_other_mode_widgets(self, qapp):
        """Regresszió: a CHEMID-re váltás után a visszaváltás ne ragadjon be
        elrejtett állapotban egy másik módnál."""
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHEMID_CHARACTERIZATION_PBA")
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert panel._charge_box.isVisibleTo(panel) is True
        assert panel._discharge_box.isVisibleTo(panel) is True

    def test_ocv_soc_hides_both_discharge_rows_and_shows_readonly_profile_cutoff(
        self, qapp
    ):
        """[regresszió, D9/F-03, 2026-09-13, Riportok/ui_policy_audit_
        20260913.md] a _discharge_box korábban EGÉSZBEN, box-szinten volt
        gate-elve a két csoport OR-jával, tehát az "Áram" sor is látható/
        szerkeszthető maradt OCV_SOC alatt, noha az OcvSocController ezt
        az értéket sosem olvassa -- mostantól a két sor ÖNÁLLÓAN gate-elt.

        [frissítve, R1 B8c, F-24 (P2), DFAM-026] a szerkeszthető
        "Végfeszültség" sor sem fogyasztott OCV_SOC alatt (a controller a
        profil cutoffját használja, ocv_soc_controller.py `profile.
        terminate_voltage_pack_V`) -- a policy-ból kikerült a
        "terminate_voltage", így a teljes kisütési doboz rejtett; helyette
        a tesztformában a csak-olvasható "ocv_soc_profile_cutoff" sor
        látszik. Korábbi elvárás: box True + TV-sor True (a D9 idején a
        policy még látta a csoportot)."""
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("OCV_SOC_CHARACTERIZATION")
        assert panel._discharge_box.isVisibleTo(panel) is False
        assert panel._discharge_form.isRowVisible(panel._discharge_current_spin) is False
        assert panel._discharge_form.isRowVisible(panel._discharge_term_v_spin) is False
        assert panel._discharge_preset_widget.isVisibleTo(panel) is False
        assert panel._test_form.isRowVisible(panel._ocv_soc_profile_cutoff_label) is True

    def test_characterization_shows_both_discharge_rows(self, qapp):
        """Diszkrimináló: CHARACTERIZATION mindkét csoportot (discharge_rate
        ÉS terminate_voltage) deklarálja -- mindkét sornak láthatónak kell
        maradnia, a preset-gombsorral együtt."""
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert panel._discharge_form.isRowVisible(panel._discharge_current_spin) is True
        assert panel._discharge_form.isRowVisible(panel._discharge_term_v_spin) is True
        assert panel._discharge_preset_widget.isVisibleTo(panel) is True

    def test_charge_only_hides_both_discharge_rows(self, qapp):
        """CHARGE_ONLY-nál a teljes discharge_box rejtett (l. fenti
        test_charge_only_hides_discharge_box) -- a sor-szintű gate ezzel
        konzisztens marad (mindkét sor is rejtett, nem csak a box)."""
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARGE_ONLY")
        assert panel._discharge_form.isRowVisible(panel._discharge_current_spin) is False
        assert panel._discharge_form.isRowVisible(panel._discharge_term_v_spin) is False


class TestWpAk03RevisionGateAndSummary:
    """WP-AK-03 (SUP02-ACCEPT-001/11/12): ConfigPanel-szintű wrapper metódusok."""

    def test_is_start_allowed_true_for_compatible_config(self, qapp):
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("INDEPENDENT")
        panel._capacity_spin.setValue(1.0)  # 0.25*1=0.25A, kompatibilis
        panel.refresh_validation()
        assert panel.is_start_allowed() is True

    def test_is_start_allowed_false_for_incompatible_charge_current(self, qapp):
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("INDEPENDENT")
        panel._capacity_spin.setValue(10.0)  # 0.25*10=2.5A > 1.5A limit
        panel.refresh_validation()
        assert panel.is_start_allowed() is False

    def test_build_start_summary_contains_key_fields(self, qapp):
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        summary = panel.build_start_summary()
        assert summary["test_type"] == "CHARACTERIZATION"
        assert "psu_mode" in summary
        assert "wiring_confirmed" in summary


class TestCF15PsuModeInfoLabelReadsCanonicalDerivedParametersSource:
    """[regresszió, Governance-v2 review-kör 1 (CF-15/DFAM-017 3.
    előfordulás, batch 4.5 MAX verifier P2-1 lelete)] a "Max: <V>V / <A>A"
    kijelző korábban egy SAJÁT, hardkódolt "info" dict-ből jött — a MÁR
    egyesített PSU_HW_MAX_VOLTAGE_V/PSU_HW_MAX_CURRENT_A 6. testvér-
    másolata, UGYANEBBEN a fájlban/osztályban. A monkeypatch bizonyítja,
    hogy a forrás TÉNYLEGESEN a kanonikus dict, nem egy másolat."""

    def test_independent_mode_label_reads_canonical_source(self, qapp, monkeypatch):
        from Prog.gui import derived_parameters
        monkeypatch.setitem(derived_parameters.PSU_HW_MAX_VOLTAGE_V, "INDEPENDENT", 111.0)
        monkeypatch.setitem(derived_parameters.PSU_HW_MAX_CURRENT_A, "INDEPENDENT", 2.25)
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("INDEPENDENT")
        assert panel._psu_mode_info_label.text() == "Max: 111V / 2.2A per csatorna"

    def test_parallel_mode_label_has_no_per_channel_suffix(self, qapp):
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("PARALLEL")
        assert panel._psu_mode_info_label.text() == "Max: 30V / 3.0A"

    def test_series_mode_label_reads_canonical_source(self, qapp, monkeypatch):
        from Prog.gui import derived_parameters
        monkeypatch.setitem(derived_parameters.PSU_HW_MAX_VOLTAGE_V, "SERIES", 777.0)
        panel = ConfigPanel()
        panel._psu_mode_combo.setCurrentText("SERIES")
        assert panel._psu_mode_info_label.text() == "Max: 777V / 1.5A"


class TestIecDcResistanceI10SourceLabel:
    """Governance-v2 kör 2: IEC 60896-21 §6.3 -- a "IEC_DC_INTERNAL_
    RESISTANCE" mód egyetlen mód-specifikus, csak-olvasható eleme a
    profilból jövő I10 megjelenítése (BAT-001/SRC-003/CFG-001: a
    versioned battery profile YAML az egyetlen hiteles forrás, ugyanaz a
    fájl, amit main_window.py::_build_runner() olvas)."""

    def _make_profile_yaml(self, tmp_path, i10=None):
        from Prog import app_paths
        profiles_dir = tmp_path / "Prog" / "config" / "battery_profiles"
        profiles_dir.mkdir(parents=True)
        body = "chemistry: AGM_VRLA\nmanufacturer: FIAMM\ncell_count: 6\n"
        if i10 is not None:
            body += f"i10_discharge_current_A: {i10}\n"
        (profiles_dir / "FIAMM_12V.yaml").write_text(body, encoding="utf-8")
        return app_paths

    def test_i10_label_shows_value_from_profile_yaml(self, qapp, monkeypatch, tmp_path):
        app_paths = self._make_profile_yaml(tmp_path, i10=0.62)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        assert panel._i10_source_label.text() == "0.62 A (profil)"

    def test_i10_label_shows_missing_marker_without_profile_field(
        self, qapp, monkeypatch, tmp_path
    ):
        app_paths = self._make_profile_yaml(tmp_path, i10=None)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        assert "HIÁNYZIK" in panel._i10_source_label.text()

    def test_i10_label_row_visible_only_for_iec_mode(self, qapp, monkeypatch, tmp_path):
        app_paths = self._make_profile_yaml(tmp_path, i10=0.62)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert panel._test_form.isRowVisible(panel._i10_source_label) is False
        panel._test_type_combo.setCurrentText("IEC_DC_INTERNAL_RESISTANCE")
        assert panel._test_form.isRowVisible(panel._i10_source_label) is True


class TestD9F02ChemIdReadonlyCutoffAndC20CurrentLabels:
    """[regresszió, D9/F-02, 2026-09-13, Riportok/ui_policy_audit_
    20260913.md] a ui_policy.py CHEMID_CHARACTERIZATION_PBA policy a
    "chemid_c20_current"/"chemid_manufacturer_cutoff" csoportokat
    láthatónak/kötelezőnek deklarálja, de korábban nem volt GUI-
    megjelenítésük -- a chemid_controller.py ténylegesen a profil C10-
    kapacitásából (/20) és a profil YAML terminate_voltage_per_cell_V×
    cell_count-jából számolja ezeket, az operátor indítás előtt nem
    látta, milyen kisütőáramra/cutoff-feszültségre indul egy ~20-24 órás
    ciklus. Az i10_source_label PONTOS mintáját követi."""

    def _make_profile_yaml(self, tmp_path, cell_count=6, terminate_v_per_cell=1.80):
        from Prog import app_paths
        profiles_dir = tmp_path / "Prog" / "config" / "battery_profiles"
        profiles_dir.mkdir(parents=True)
        body = (
            f"chemistry: AGM_VRLA\nmanufacturer: FIAMM\ncell_count: {cell_count}\n"
            f"terminate_voltage_per_cell_V: {terminate_v_per_cell}\n"
        )
        (profiles_dir / "FIAMM_12V.yaml").write_text(body, encoding="utf-8")
        return app_paths

    def test_c20_current_label_derived_from_c10_capacity_spinbox(
        self, qapp, monkeypatch, tmp_path
    ):
        app_paths = self._make_profile_yaml(tmp_path)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._capacity_spin.setValue(7.2)
        assert panel._chemid_c20_current_label.text() == "0.36 A (profil, C10/20)"

    def test_cutoff_label_uses_profile_terminate_voltage_per_cell_times_cell_count(
        self, qapp, monkeypatch, tmp_path
    ):
        app_paths = self._make_profile_yaml(
            tmp_path, cell_count=6, terminate_v_per_cell=1.75
        )
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        assert panel._chemid_cutoff_label.text() == "10.50 V (profil)"

    def test_labels_visible_only_for_chemid_mode(self, qapp, monkeypatch, tmp_path):
        app_paths = self._make_profile_yaml(tmp_path)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert panel._test_form.isRowVisible(panel._chemid_c20_current_label) is False
        assert panel._test_form.isRowVisible(panel._chemid_cutoff_label) is False
        panel._test_type_combo.setCurrentText("CHEMID_CHARACTERIZATION_PBA")
        assert panel._test_form.isRowVisible(panel._chemid_c20_current_label) is True
        assert panel._test_form.isRowVisible(panel._chemid_cutoff_label) is True

    def test_c20_current_label_updates_when_capacity_changes(
        self, qapp, monkeypatch, tmp_path
    ):
        """Diszkrimináló: a régi kódon (nincs _on_capacity_changed-kötés) a
        label a konstrukciókori kapacitás-alapértéken ragadna."""
        app_paths = self._make_profile_yaml(tmp_path)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._capacity_spin.setValue(7.2)
        assert panel._chemid_c20_current_label.text() == "0.36 A (profil, C10/20)"
        panel._capacity_spin.setValue(20.0)
        assert panel._chemid_c20_current_label.text() == "1.00 A (profil, C10/20)"


class TestD9F06ChemIdConditioningAndRelaxReadonlyLabels:
    """[regresszió, D9/F-06, 2026-09-13, Riportok/ui_policy_audit_
    20260913.md] a "chemid_conditioning"/"chemid_relax_5h" csoportok
    láthatónak deklarálva, de a ChemIdConfig() fix kondicionálási/relax-
    időtartamai (2h + 5h/5h, sosem override-olva GUI-ból) korábban sehol
    nem jelentek meg -- az operátor nem látta az előre meghatározott
    minimumidőt egy ~24h+ ciklus előtt."""

    def test_labels_show_chemidconfig_default_durations(self, qapp):
        from Prog.src.chemid_controller import ChemIdConfig
        defaults = ChemIdConfig()
        panel = ConfigPanel()
        assert str(int(defaults.conditioning_min_s / 3600)) in panel._chemid_conditioning_label.text()
        assert str(int(defaults.relax_full_min_s / 3600)) in panel._chemid_relax_label.text()
        assert str(int(defaults.relax_empty_min_s / 3600)) in panel._chemid_relax_label.text()

    def test_labels_visible_only_for_chemid_mode(self, qapp):
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert panel._test_form.isRowVisible(panel._chemid_conditioning_label) is False
        assert panel._test_form.isRowVisible(panel._chemid_relax_label) is False
        panel._test_type_combo.setCurrentText("CHEMID_CHARACTERIZATION_PBA")
        assert panel._test_form.isRowVisible(panel._chemid_conditioning_label) is True
        assert panel._test_form.isRowVisible(panel._chemid_relax_label) is True


class TestD9F04OcvSocPartialDischargeRateReadonlyLabel:
    """[regresszió, D9/F-04, 2026-09-13, Riportok/ui_policy_audit_
    20260913.md] a "partial_discharge_rate" csoport láthatónak
    deklarálva, de az OCV_SOC lépcsős (C/10) kisütőárama korábban sehol
    nem jelent meg -- a `get_session_config()` hardkódolt divisor=10-zel
    számol, az operátor nem látta a tényleges áramot."""

    def test_label_shows_c10_of_capacity_spinbox(self, qapp):
        panel = ConfigPanel()
        panel._capacity_spin.setValue(7.2)
        panel._refresh_ocv_soc_partial_discharge_rate_label()
        assert panel._ocv_soc_partial_discharge_rate_label.text() == "0.72 A (kapacitásalapból)"

    def test_label_updates_when_capacity_changes(self, qapp):
        """Diszkrimináló: a régi kódon (nincs _on_capacity_changed-kötés)
        a label a konstrukciókori kapacitás-alapértéken ragadna."""
        panel = ConfigPanel()
        panel._capacity_spin.setValue(7.2)
        assert panel._ocv_soc_partial_discharge_rate_label.text() == "0.72 A (kapacitásalapból)"
        panel._capacity_spin.setValue(20.0)
        assert panel._ocv_soc_partial_discharge_rate_label.text() == "2.00 A (kapacitásalapból)"

    def test_label_visible_only_for_ocv_soc_mode(self, qapp):
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert (
            panel._test_form.isRowVisible(panel._ocv_soc_partial_discharge_rate_label)
            is False
        )
        panel._test_type_combo.setCurrentText("OCV_SOC_CHARACTERIZATION")
        assert (
            panel._test_form.isRowVisible(panel._ocv_soc_partial_discharge_rate_label)
            is True
        )


class TestD9F07SafetyLimitReadonlyLabels:
    """[regresszió, D9/F-07, 2026-09-13, Riportok/ui_policy_audit_
    20260913.md] a "charge_safety_limits"/"discharge_safety_limits"
    csoportok láthatónak deklarálva, de a tényleges safety-backstopok
    (BatteryProfile.batt_absolute_max_V_per_cell, SafetyManager.
    max_discharge_time_s/max_discharge_Ah_factor) korábban nem voltak
    láthatók a GUI-n -- NEM szerkeszthetők (ez helyes), csak
    tájékoztató jellegűek."""

    def test_charge_safety_limit_label_shows_fixed_pack_voltage_limit(self, qapp):
        # R16-B2: a teljes cimke kizarja a szamelotagos mutansokat.
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        assert panel._charge_safety_limit_label.text() == (
            "2.425 V/cella (14.55 V pack, fix)"
        )

    def test_discharge_safety_limit_label_shows_fixed_time_and_ah_factor(self, qapp):
        panel = ConfigPanel()
        assert panel._discharge_safety_limit_label.text() == (
            "24 óra / 1.10×kapacitás (fix)"
        )

    def test_charge_safety_limit_visible_only_for_charge_only_mode(self, qapp):
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert panel._test_form.isRowVisible(panel._charge_safety_limit_label) is False
        panel._test_type_combo.setCurrentText("CHARGE_ONLY")
        assert panel._test_form.isRowVisible(panel._charge_safety_limit_label) is True

    def test_discharge_safety_limit_visible_only_for_discharge_only_mode(self, qapp):
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARACTERIZATION")
        assert panel._test_form.isRowVisible(panel._discharge_safety_limit_label) is False
        panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        assert panel._test_form.isRowVisible(panel._discharge_safety_limit_label) is True

    def test_charge_safety_limit_label_updates_with_profile_cell_count(
        self, qapp, monkeypatch
    ):
        from pathlib import Path

        from Prog import app_paths

        # R16-B2: a kovetett profilokkal 6 -> 12 -> 6, fajliras nelkul.
        repo_root = Path(__file__).resolve().parents[3]
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: repo_root)
        panel = ConfigPanel()
        for profile, expected in (
            ("FIAMM_12V", "2.425 V/cella (14.55 V pack, fix)"),
            ("FIAMM_24V", "2.425 V/cella (29.10 V pack, fix)"),
            ("FIAMM_12V", "2.425 V/cella (14.55 V pack, fix)"),
        ):
            panel._profile_combo.setCurrentText(profile)
            assert panel._charge_safety_limit_label.text() == expected


class TestReviewRound4GuiF6ProfileMaxChargeCurrentOverrideWiring:
    """[regresszió, review-kör 4/5 GUI F6 (P2, BAT-001/SRC-003/CFG-001
    sibling -- l. TestIecDcResistanceI10SourceLabel fenti pontos mintája
    ugyanerről a hibaosztályról)] a Start-összegző töltőáram-kijelzése
    korábban MINDIG a generikus 0,25×C10 képletet mutatta, még akkor is,
    ha a profil YAML-ja explicit `max_charge_current_A` felülbírálást
    adott meg -- ami viszont a TÉNYLEGESEN alkalmazott (charge_
    controller.py -> BatteryProfile.effective_max_charge_A) érték."""

    def _make_profile_yaml(self, tmp_path, max_charge_current_A=None):
        from Prog import app_paths
        profiles_dir = tmp_path / "Prog" / "config" / "battery_profiles"
        profiles_dir.mkdir(parents=True)
        body = "chemistry: AGM_VRLA\nmanufacturer: FIAMM\ncell_count: 6\n"
        if max_charge_current_A is not None:
            body += f"max_charge_current_A: {max_charge_current_A}\n"
        (profiles_dir / "FIAMM_12V.yaml").write_text(body, encoding="utf-8")
        return app_paths

    def test_profile_yaml_override_reaches_the_model(self, qapp, monkeypatch, tmp_path):
        app_paths = self._make_profile_yaml(tmp_path, max_charge_current_A=1.2)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._capacity_spin.setValue(10.0)  # 0.25×10 = 2.5 A -- a felülbírálás NÉLKÜL ezt adná

        panel._profile_combo.setCurrentText("FIAMM_12V")

        assert panel._model.requested_charge_current_A == pytest.approx(1.2)

    def test_profile_yaml_without_override_uses_formula(self, qapp, monkeypatch, tmp_path):
        app_paths = self._make_profile_yaml(tmp_path, max_charge_current_A=None)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._capacity_spin.setValue(10.0)

        panel._profile_combo.setCurrentText("FIAMM_12V")

        assert panel._model.requested_charge_current_A == pytest.approx(2.5)


class TestKor20BatchE5ProfileTerminateVoltageAndTaperWiring:
    """[regresszió, Kör 20 Batch E5 (SW20-2, P2, DFAM-001+DFAM-017) -- a
    fenti TestReviewRound4GuiF6ProfileMaxChargeCurrentOverrideWiring
    PONTOS mintája] a `discharge_terminate_voltage_V` és a
    `taper_current_A` korábban NEM voltak a profil-YAML-ból a GUI-modellbe
    vezetve -- egyetlen YAML-sor szerkesztésével, kód nélkül a DISCHARGE
    lépés a hardkódolt 1.80×N-ig sütött volna ki, miközben a ChemID
    cutoff-címke és a TÉNYLEGESEN alkalmazott (BatteryProfile.
    terminate_voltage_pack_V) érték a helyes YAML-értéket mutatta."""

    def _make_profile_yaml(
        self, tmp_path, cell_count=6, terminate_v_per_cell=1.75, taper_current_A=0.15,
    ):
        from Prog import app_paths
        profiles_dir = tmp_path / "Prog" / "config" / "battery_profiles"
        profiles_dir.mkdir(parents=True)
        body = (
            f"chemistry: AGM_VRLA\nmanufacturer: FIAMM\ncell_count: {cell_count}\n"
            f"terminate_voltage_per_cell_V: {terminate_v_per_cell}\n"
            f"taper_current_A: {taper_current_A}\n"
        )
        (profiles_dir / "FIAMM_12V.yaml").write_text(body, encoding="utf-8")
        return app_paths

    def test_session_config_terminate_voltage_uses_yaml_value(
        self, qapp, monkeypatch, tmp_path
    ):
        """DISZKRIMINÁLÓ: a fix ELŐTTI kódon `get_session_config().
        discharge_terminate_voltage_V == approx(6*1.80) == 10.80` -- a
        hardkódolt alapérték, NEM a profil-YAML 1.75-e."""
        app_paths = self._make_profile_yaml(tmp_path, terminate_v_per_cell=1.75)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()

        panel._profile_combo.setCurrentText("FIAMM_12V")

        cfg = panel.get_session_config()
        assert cfg.discharge_terminate_voltage_V == pytest.approx(6 * 1.75)

    def test_term_v_spin_shows_yaml_value_at_construction(self, qapp, monkeypatch, tmp_path):
        """A Diff-2 __init__-sorrend-javítást fedi: az override-nak a
        KONSTRUKCIÓKORI render ELŐTT kell megtörténnie."""
        app_paths = self._make_profile_yaml(tmp_path, terminate_v_per_cell=1.75)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)

        panel = ConfigPanel()

        assert panel._discharge_term_v_spin.value() == pytest.approx(6 * 1.75)

    def test_chemid_cutoff_label_and_session_config_agree(self, qapp, monkeypatch, tmp_path):
        """Parity-őr a _refresh_chemid_readonly_labels() KEEP_AS_IS saját
        YAML-olvasásával szemben -- a két útnak azonos eredményt kell
        adnia, még ha külön kódúton is jutnak oda."""
        app_paths = self._make_profile_yaml(tmp_path, terminate_v_per_cell=1.75)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()

        panel._profile_combo.setCurrentText("FIAMM_12V")

        assert panel._chemid_cutoff_label.text() == "10.50 V (profil)"
        cfg = panel.get_session_config()
        assert cfg.discharge_terminate_voltage_V == pytest.approx(10.50)

    def test_matches_build_runner_battery_profile(self, qapp, monkeypatch, tmp_path):
        """Kereszt-ellenőrzés a main_window.py::_build_runner() által
        UGYANABBÓL a YAML-ból épített BatteryProfile-lal -- a két útvonal
        (GUI-modell vs. produkciós session-összeállítás) nem térhet el."""
        from Prog.src.battery_profile import BatteryProfile

        app_paths = self._make_profile_yaml(
            tmp_path, terminate_v_per_cell=1.85, taper_current_A=0.15,
        )
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")

        profile = BatteryProfile(
            battery_name="FIAMM_12V", manufacturer="FIAMM", model="TEST",
            nominal_capacity_Ah=panel._capacity_spin.value() or 1.0,
            cell_count=6,
            terminate_voltage_per_cell_V=1.85,
            taper_current_A=0.15,
        )

        cfg = panel.get_session_config()
        assert profile.terminate_voltage_pack_V == pytest.approx(
            cfg.discharge_terminate_voltage_V
        )
        assert profile.effective_taper_A == pytest.approx(panel._model.taper_current_A)

    def test_yaml_without_terminate_field_falls_back_to_1_80(self, qapp, monkeypatch, tmp_path):
        from Prog import app_paths
        profiles_dir = tmp_path / "Prog" / "config" / "battery_profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "FIAMM_12V.yaml").write_text(
            "chemistry: AGM_VRLA\nmanufacturer: FIAMM\ncell_count: 6\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()

        panel._profile_combo.setCurrentText("FIAMM_12V")

        cfg = panel.get_session_config()
        assert cfg.discharge_terminate_voltage_V == pytest.approx(6 * 1.80)


class TestKor20BatchE5FactoryYamlNonRegression:
    """Nem-regressziós teszt (rules/60-testing "diszkrimináló ÉS nem-
    regressziós is kell") -- a KÉT GYÁRI YAML-lel (bundle_dir monkeypatch
    NÉLKÜL, a valódi Prog/config/battery_profiles/*.yaml-t használva) a
    SessionConfig minden mezője bitre azonos maradjon a fix előttivel."""

    def test_fiamm_12v_session_config_unchanged(self, qapp):
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        cfg = panel.get_session_config()
        assert cfg.discharge_terminate_voltage_V == pytest.approx(6 * 1.80)
        assert panel._model.taper_current_A == pytest.approx(0.03 * panel._capacity_spin.value())

    def test_fiamm_24v_session_config_unchanged(self, qapp):
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_24V")
        cfg = panel.get_session_config()
        assert cfg.discharge_terminate_voltage_V == pytest.approx(12 * 1.80)
        assert panel._model.taper_current_A == pytest.approx(0.03 * panel._capacity_spin.value())


_F24_HIDDEN_CUTOFF_MODES = (
    "CHARGE_ONLY", "OCV_SOC_CHARACTERIZATION", "CHEMID_CHARACTERIZATION_PBA",
    "TRADITIONAL_LEARNING", "GOLDEN_GG", "IEC_DC_INTERNAL_RESISTANCE",
)
_F24_CONSUMER_CUTOFF_MODES = (
    "DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL",
)


def _f24_panel_with_inherited_custom_cutoff(target_mode: str, cutoff_V: float):
    """DISCHARGE_ONLY-ban CUSTOM cutoff (spinbox), majd ELSŐ váltás a
    célmódba -- a CUSTOM policy draft nélkül átöröklődik
    (DynamicSessionModel.set_test_type())."""
    panel = ConfigPanel()
    panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
    panel._discharge_term_v_spin.setValue(cutoff_V)
    assert panel._model.terminate_voltage_pack_V == pytest.approx(cutoff_V)
    panel._test_type_combo.setCurrentText(target_mode)
    return panel


def _f24_panel_with_visible_cutoff(consumer_mode: str, cutoff_V: float):
    panel = ConfigPanel()
    panel._test_type_combo.setCurrentText(consumer_mode)
    panel._discharge_term_v_spin.setValue(cutoff_V)
    return panel


class TestR1B8cF24OcvSocReadonlyProfileCutoffAndHashSentinel:
    """[regresszió, R1 B8c, F-24 (P2), DFAM-026, D-B8-3] OCV_SOC alatt a
    szerkeszthető végfeszültségnek nem volt fogyasztója (a controller a
    profil cutoffját használja), mégis bekerült a SessionConfig-ba és a
    config-hashbe; CUSTOM-öröklés miatt a sor elrejtése önmagában nem lett
    volna elég. Mostantól: readonly profil-cutoff sor OCV_SOC-ban;
    get_session_config() a cfg-cutoffot csak a "terminate_voltage"
    csoportot látó (fogyasztó) módokban adja át, egyébként a meglévő 0.0
    auto/profil szentinel."""

    def test_readonly_cutoff_row_shows_profile_pack_voltage_and_refreshes(self, qapp):
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._test_type_combo.setCurrentText("OCV_SOC_CHARACTERIZATION")
        assert panel._test_form.isRowVisible(panel._ocv_soc_profile_cutoff_label) is True
        assert panel._ocv_soc_profile_cutoff_label.text() == "10.80 V (profil)"

        panel._profile_combo.setCurrentText("FIAMM_24V")

        assert panel._ocv_soc_profile_cutoff_label.text() == "21.60 V (profil)"

    def test_readonly_cutoff_row_uses_profile_yaml_terminate_per_cell(
        self, qapp, monkeypatch, tmp_path,
    ):
        from Prog import app_paths
        profiles_dir = tmp_path / "Prog" / "config" / "battery_profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "FIAMM_12V.yaml").write_text(
            "chemistry: AGM_VRLA\nmanufacturer: FIAMM\ncell_count: 6\n"
            "terminate_voltage_per_cell_V: 1.75\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._test_type_combo.setCurrentText("OCV_SOC_CHARACTERIZATION")
        assert panel._ocv_soc_profile_cutoff_label.text() == "10.50 V (profil)"

    def test_readonly_cutoff_row_hidden_outside_ocv_soc(self, qapp):
        panel = ConfigPanel()
        for mode in _F24_CONSUMER_CUTOFF_MODES + (
            "CHARGE_ONLY", "CHEMID_CHARACTERIZATION_PBA", "TRADITIONAL_LEARNING",
            "GOLDEN_GG", "IEC_DC_INTERNAL_RESISTANCE",
        ):
            panel._test_type_combo.setCurrentText(mode)
            assert (
                panel._test_form.isRowVisible(panel._ocv_soc_profile_cutoff_label)
                is False
            ), mode

    def test_inherited_custom_cutoff_not_passed_to_session_config_in_ocv_soc(self, qapp):
        panel = _f24_panel_with_inherited_custom_cutoff("OCV_SOC_CHARACTERIZATION", 11.0)
        # a modell továbbra is őrzi a (draft-)értéket ...
        assert panel._model.terminate_voltage_pack_V == pytest.approx(11.0)
        # ... de a hatástalan érték nem kerül a SessionConfig-ba.
        assert panel.get_session_config().discharge_terminate_voltage_V == 0.0

    def test_hash_equal_when_only_inherited_custom_cutoff_differs(self, qapp):
        """Csak a cutoff örökölt CUSTOM értéke tér el; a discharge_current_A
        (külön nyilvántartott rejtett hash-maradék) mindkét panelen azonos."""
        from Prog.src.session_recovery import compute_config_hash
        cfg_a = _f24_panel_with_inherited_custom_cutoff(
            "OCV_SOC_CHARACTERIZATION", 11.0,
        ).get_session_config()
        cfg_b = _f24_panel_with_inherited_custom_cutoff(
            "OCV_SOC_CHARACTERIZATION", 11.5,
        ).get_session_config()
        assert cfg_a.discharge_current_A == cfg_b.discharge_current_A
        assert compute_config_hash(cfg_a) == compute_config_hash(cfg_b)

    @pytest.mark.parametrize("mode", _F24_HIDDEN_CUTOFF_MODES)
    def test_mode_matrix_hidden_cutoff_does_not_change_hash(self, qapp, mode):
        from Prog.src.session_recovery import compute_config_hash
        cfg_a = _f24_panel_with_inherited_custom_cutoff(mode, 11.0).get_session_config()
        cfg_b = _f24_panel_with_inherited_custom_cutoff(mode, 11.5).get_session_config()
        assert cfg_a.discharge_terminate_voltage_V == 0.0
        assert cfg_b.discharge_terminate_voltage_V == 0.0
        assert compute_config_hash(cfg_a) == compute_config_hash(cfg_b)

    @pytest.mark.parametrize("mode", _F24_CONSUMER_CUTOFF_MODES)
    def test_mode_matrix_consumer_cutoff_still_changes_hash(self, qapp, mode):
        from Prog.src.session_recovery import compute_config_hash
        cfg_a = _f24_panel_with_visible_cutoff(mode, 11.0).get_session_config()
        cfg_b = _f24_panel_with_visible_cutoff(mode, 11.5).get_session_config()
        assert cfg_a.discharge_terminate_voltage_V == pytest.approx(11.0)
        assert cfg_b.discharge_terminate_voltage_V == pytest.approx(11.5)
        assert compute_config_hash(cfg_a) != compute_config_hash(cfg_b)

    def _checkpoint(self, config_hash: str) -> dict:
        return {
            "safe_boundary": True,
            "config_hash": config_hash,
            "profile_hash": "PROFILE_HASH",
            "instrument_identities": {"PSU": "MOCK_PSU"},
            "sample_id": "",
            "next_safe_step_index": 0,
        }

    def _evaluate(self, checkpoint: dict, cfg):
        from Prog.src.session_recovery import compute_config_hash, evaluate_resume
        return evaluate_resume(
            checkpoint,
            current_config_hash=compute_config_hash(cfg),
            current_profile_hash="PROFILE_HASH",
            current_instrument_identities={"PSU": "MOCK_PSU"},
            current_sample_id="",
            total_step_count=1,
        )

    def test_old_checkpoint_hashing_nonzero_hidden_cutoff_stays_blocked(self, qapp):
        """Fix-induced resume-hatás (D-B8-3, FINAL): a fix előtti kód OCV_SOC
        alatt a profil pack-feszültséget (FIAMM_12V: 10,80 V) hashelte; az
        ilyen checkpoint az új kóddal TARTÓSAN BLOCKED_CONFIG_MISMATCH --
        migráció/fallback nincs (minden ismételt kiértékelés blokkol)."""
        import dataclasses

        from Prog.src.session_recovery import ResumeDecision, compute_config_hash
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._test_type_combo.setCurrentText("OCV_SOC_CHARACTERIZATION")
        cfg_new = panel.get_session_config()
        cfg_old = dataclasses.replace(cfg_new, discharge_terminate_voltage_V=6 * 1.80)
        old_checkpoint = self._checkpoint(compute_config_hash(cfg_old))

        for _ in range(2):
            evaluation = self._evaluate(old_checkpoint, cfg_new)
            assert evaluation.decision is ResumeDecision.BLOCKED_CONFIG_MISMATCH

    def test_new_checkpoint_resumes(self, qapp):
        from Prog.src.session_recovery import ResumeDecision, compute_config_hash
        panel = ConfigPanel()
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._test_type_combo.setCurrentText("OCV_SOC_CHARACTERIZATION")
        new_checkpoint = self._checkpoint(compute_config_hash(panel.get_session_config()))

        # A kezelő a crash után egy (rejtett, örökölt) cutoffot állítana --
        # a resume-azonosítást nem befolyásolja.
        panel2 = _f24_panel_with_inherited_custom_cutoff("OCV_SOC_CHARACTERIZATION", 11.5)
        panel2._profile_combo.setCurrentText("FIAMM_12V")
        for cfg in (panel.get_session_config(), panel2.get_session_config()):
            evaluation = self._evaluate(new_checkpoint, cfg)
            assert evaluation.decision is ResumeDecision.RESUME_ALLOWED, evaluation.reason


class TestR8B3DischargeCurrentLimit:
    """R8-B3: kapacitas -> modell -> render -> Start, valos I/O nelkul."""

    @pytest.fixture
    def start_window(self, qapp, monkeypatch):
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QMessageBox

        from Prog.gui import main_window as mw

        monkeypatch.setattr(mw.MainWindow, "_review_incomplete_sessions_on_startup", lambda self: None)
        worker = MagicMock()
        thread = MagicMock()
        warning = MagicMock()
        monkeypatch.setattr(mw, "_InstrumentConnectWorker", worker)
        monkeypatch.setattr(mw, "QThread", thread)
        monkeypatch.setattr(QMessageBox, "warning", warning)
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        window = mw.MainWindow()
        window._config_panel._model_edit.setText("R8B3")
        try:
            yield window, worker, thread, warning
        finally:
            window._cleanup_connect_thread()
            window.close()
            window.deleteLater()
            qapp.processEvents()

    @pytest.mark.parametrize(
        "mode,capacity,divisor,requested,allowed",
        [
            ("DISCHARGE_ONLY", 300.1, 5, 60.02, False),
            ("DISCHARGE_ONLY", 500.0, 5, 100.0, False),
            ("DISCHARGE_ONLY", 1000.0, 10, 100.0, False),
            ("DISCHARGE_ONLY", 300.0, 5, 60.0, True),
            ("DISCHARGE_ONLY", 1000.0, 20, 50.0, True),
            ("DISCHARGE_ONLY", 100.0, 5, 20.0, True),
            ("CHARGE_ONLY", 500.0, 5, 100.0, True),
        ],
    )
    def test_capacity_signal_to_start(
        self, start_window, mode, capacity, divisor, requested, allowed,
    ):
        from Prog.gui.derived_parameters import ValidationState

        window, worker, thread, warning = start_window
        panel = window._config_panel
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        panel._apply_discharge_preset(divisor, 1.80)
        panel._capacity_spin.setValue(capacity)
        if mode != "DISCHARGE_ONLY":
            panel._test_type_combo.setCurrentText(mode)
            panel._charge_current_spin.setValue(1.0)

        assert panel._model.discharge_current_A == pytest.approx(requested)
        cfg = panel.get_session_config()
        assert cfg.discharge_current_A == pytest.approx(requested)
        assert cfg.validate() == []
        assert panel._discharge_current_spin.value() == pytest.approx(min(requested, 60.0))

        window._live_panel._start_btn.click()

        if allowed:
            worker.assert_called_once()
            thread.return_value.start.assert_called_once()
            assert worker.call_args.args[0].discharge_current_A == pytest.approx(requested)
            warning.assert_not_called()
            assert panel.refresh_validation() is ValidationState.OK
            assert panel.build_start_summary()["blocking_errors"] == []
        else:
            worker.assert_not_called()
            thread.assert_not_called()
            assert window._connect_worker is None
            warning.assert_called_once()
            assert panel.refresh_validation() is ValidationState.CONFIG_INCOMPATIBLE
            label = panel._discharge_current_label
            assert panel._discharge_form.isRowVisible(label)
            assert "CONFIG_INCOMPATIBLE" in label.text()
            assert f"{requested:.2f} A" in label.text()
            assert "60.00 A" in label.text()
            errors = panel.build_start_summary()["blocking_errors"]
            assert len(errors) == 1
            assert "CONFIG_INCOMPATIBLE" in errors[0]
            assert f"{requested} A" in errors[0]
            assert "60.0 A" in errors[0]
            assert errors[0] in warning.call_args.args[2]
        assert panel.is_start_allowed() is allowed
        assert panel._model.discharge_current_A == pytest.approx(requested)

    @pytest.mark.parametrize("mode", ["CHARACTERIZATION", "BQ_LEARNING_PHYSICAL"])
    def test_other_visible_discharge_groups_block(self, qapp, mode):
        from Prog.gui.derived_parameters import ValidationState

        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText(mode)
        panel._charge_current_spin.setValue(1.0)
        panel._capacity_spin.setValue(500.0)

        assert panel._model.requested_charge_current_A == pytest.approx(1.0)
        assert panel.refresh_validation() is ValidationState.CONFIG_INCOMPATIBLE
        assert panel.is_start_allowed() is False
        assert len(panel.build_start_summary()["blocking_errors"]) == 1
        assert panel._model.get("discharge_current_A").validation_state is ValidationState.CONFIG_INCOMPATIBLE

    @pytest.mark.parametrize(
        "mode",
        [
            "CHARGE_ONLY", "OCV_SOC_CHARACTERIZATION", "CHEMID_CHARACTERIZATION_PBA",
            "TRADITIONAL_LEARNING", "GOLDEN_GG", "IEC_DC_INTERNAL_RESISTANCE",
        ],
    )
    def test_hidden_discharge_request_does_not_block(self, qapp, mode):
        from Prog.gui.derived_parameters import ValidationState

        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        panel._capacity_spin.setValue(500.0)
        panel._test_type_combo.setCurrentText(mode)
        panel._charge_current_spin.setValue(1.0)

        assert panel._model.discharge_current_A == pytest.approx(100.0)
        assert panel._discharge_form.isRowVisible(panel._discharge_current_spin) is False
        assert panel._discharge_form.isRowVisible(panel._discharge_current_label) is False
        assert panel.refresh_validation() is ValidationState.OK
        assert panel.is_start_allowed() is True
        summary = panel.build_start_summary()
        assert summary["blocking_errors"] == []
        assert "discharge_current_A" not in summary

    def test_capacity_correction_clears_error_and_allows_start(self, start_window):
        from Prog.gui.derived_parameters import ValidationState

        window, worker, thread, warning = start_window
        panel = window._config_panel
        panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        panel._capacity_spin.setValue(500.0)
        assert panel.refresh_validation() is ValidationState.CONFIG_INCOMPATIBLE

        panel._capacity_spin.setValue(300.0)

        assert panel._model.discharge_current_A == pytest.approx(60.0)
        assert panel._discharge_current_spin.value() == pytest.approx(60.0)
        assert panel._discharge_current_label.text() == ""
        assert panel._discharge_current_label.styleSheet() == ""
        window._live_panel._start_btn.click()
        worker.assert_called_once()
        thread.return_value.start.assert_called_once()
        warning.assert_not_called()

    def test_spinbox_validation_and_messages_share_limit(self, qapp, monkeypatch):
        from Prog.gui import derived_parameters as dp

        assert dp.LOAD_HW_MAX_CURRENT_A == 60.0
        monkeypatch.setattr(dp, "LOAD_HW_MAX_CURRENT_A", 55.0)
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        panel._capacity_spin.setValue(280.0)

        assert panel._discharge_current_spin.maximum() == 55.0
        assert panel._model.discharge_current_A == pytest.approx(56.0)
        assert panel.refresh_validation() is dp.ValidationState.CONFIG_INCOMPATIBLE
        assert "56.00 A" in panel._discharge_current_label.text()
        assert "55.00 A" in panel._discharge_current_label.text()
        assert "55.0 A" in panel.build_start_summary()["blocking_errors"][0]


class TestR9B1ChargeProfileLimit:
    """R9-B1: spinbox -> modell -> render -> Start, mockolt kapcsolodas."""

    start_window = TestR8B3DischargeCurrentLimit.start_window

    @pytest.mark.parametrize(
        "mode,requested,allowed",
        [
            ("CHARGE_ONLY", 3.0, False),
            ("CHARGE_ONLY", 1.8, True),
            ("CHARGE_ONLY", 1.7, True),
            ("DISCHARGE_ONLY", 3.0, True),
        ],
    )
    def test_custom_current_to_start(self, start_window, mode, requested, allowed):
        from Prog.gui.derived_parameters import ValidationState

        window, worker, thread, warning = start_window
        panel = window._config_panel
        panel._test_type_combo.setCurrentText("CHARGE_ONLY")
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._psu_mode_combo.setCurrentText("PARALLEL")
        panel._capacity_spin.setValue(7.2)
        panel._charge_current_spin.setValue(requested)
        if mode != "CHARGE_ONLY":
            panel._test_type_combo.setCurrentText(mode)
        assert panel._model.requested_charge_current_A == pytest.approx(requested)
        assert panel._charge_current_spin.value() == pytest.approx(requested)
        panel._wiring_confirmed_cb.setChecked(True)
        window._live_panel._start_btn.click()
        assert panel.is_start_allowed() is allowed
        errors = panel.build_start_summary()["blocking_errors"]
        if allowed:
            worker.assert_called_once()
            thread.return_value.start.assert_called_once()
            warning.assert_not_called()
            assert errors == []
        else:
            worker.assert_not_called()
            thread.assert_not_called()
            assert panel.refresh_validation() is ValidationState.CONFIG_INCOMPATIBLE
            label = panel._charge_current_label.text()
            assert "CONFIG_INCOMPATIBLE" in label
            assert "3.00 A" in label and "1.8 A" in label
            assert "profilmaximum" in label
            assert len(errors) == 1
            assert "3.0 A" in errors[0] and "1.8 A" in errors[0]
            warning.assert_called_once()
            assert errors[0] in warning.call_args.args[2]

    def test_capacity_signal_revalidates_custom_and_recovers(self, start_window):
        from Prog.gui.derived_parameters import ValidationState

        window, worker, _, warning = start_window
        panel = window._config_panel
        panel._test_type_combo.setCurrentText("CHARGE_ONLY")
        panel._psu_mode_combo.setCurrentText("PARALLEL")
        panel._capacity_spin.setValue(12.0)
        panel._charge_current_spin.setValue(2.0)
        assert panel.refresh_validation() is ValidationState.OK
        panel._capacity_spin.setValue(7.2)
        assert panel._charge_current_spin.value() == pytest.approx(2.0)
        assert panel.refresh_validation() is ValidationState.CONFIG_INCOMPATIBLE
        assert "2.00 A" in panel._charge_current_label.text()
        assert "1.8 A" in panel._charge_current_label.text()
        panel._wiring_confirmed_cb.setChecked(True)
        window._live_panel._start_btn.click()
        worker.assert_not_called()
        warning.assert_called_once()
        panel._capacity_spin.setValue(12.0)
        assert panel.refresh_validation() is ValidationState.OK
        assert panel._charge_current_spin.value() == pytest.approx(2.0)
        assert "CONFIG_INCOMPATIBLE" not in panel._charge_current_label.text()
        window._live_panel._start_btn.click()
        worker.assert_called_once()

    def test_profile_signal_revalidates_custom(self, qapp, monkeypatch):
        from io import StringIO
        from pathlib import Path

        from Prog.gui.derived_parameters import ValidationState

        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText("CHARGE_ONLY")
        panel._capacity_spin.setValue(7.2)
        panel._charge_current_spin.setValue(1.0)
        assert panel.refresh_validation() is ValidationState.OK
        real_open = Path.open

        def profile_open(path, *args, **kwargs):
            if path.name == "FIAMM_24V.yaml" and path.parent.name == "battery_profiles":
                return StringIO("cell_count: 12\nmax_charge_current_A: 0.8\n")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", profile_open)
        panel._profile_combo.setCurrentText("FIAMM_24V")
        assert panel._charge_current_spin.value() == pytest.approx(1.0)
        assert panel._model.charge_current_hardware_limit_A == pytest.approx(1.5)
        assert panel.refresh_validation() is ValidationState.CONFIG_INCOMPATIBLE
        assert not panel.is_start_allowed()
        label = panel._charge_current_label.text()
        assert "1.00 A" in label and "0.8 A" in label
        errors = panel.build_start_summary()["blocking_errors"]
        assert len(errors) == 1
        assert "1.0 A" in errors[0] and "0.8 A" in errors[0]
        panel._profile_combo.setCurrentText("FIAMM_12V")
        assert panel._charge_current_spin.value() == pytest.approx(1.0)
        assert panel.refresh_validation() is ValidationState.OK
        assert panel.build_start_summary()["blocking_errors"] == []


def test_r9b1_profile_limit_label_keeps_third_decimal(qapp):
    from Prog.gui.derived_parameters import ValidationState

    panel = ConfigPanel()
    panel._test_type_combo.setCurrentText("CHARGE_ONLY")
    panel._profile_combo.setCurrentText("FIAMM_12V")
    panel._psu_mode_combo.setCurrentText("PARALLEL")
    panel._capacity_spin.setValue(4.7)
    panel._charge_current_spin.setValue(1.18)

    assert panel._model.requested_charge_current_A == pytest.approx(1.18)
    assert panel._model.charge_current_profile_limit_A == pytest.approx(1.175)
    assert panel._charge_current_spin.value() == pytest.approx(1.18)
    assert panel.refresh_validation() is ValidationState.CONFIG_INCOMPATIBLE
    assert not panel.is_start_allowed()
    assert panel._charge_current_label.text() == (
        "kért: 1.18 A — CONFIG_INCOMPATIBLE (profilmaximum: 1.175 A)"
    )


def test_r9b1_profile_limit_label_keeps_yaml_override_precision(qapp, monkeypatch):
    from io import StringIO
    from pathlib import Path

    from Prog.gui.derived_parameters import ValidationState

    panel = ConfigPanel()
    panel._test_type_combo.setCurrentText("CHARGE_ONLY")
    panel._profile_combo.setCurrentText("FIAMM_12V")
    panel._capacity_spin.setValue(7.2)
    panel._charge_current_spin.setValue(1.0)
    real_open = Path.open

    def profile_open(path, *args, **kwargs):
        if path.name == "FIAMM_24V.yaml" and path.parent.name == "battery_profiles":
            return StringIO("cell_count: 12\nmax_charge_current_A: 0.8006\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", profile_open)
    panel._profile_combo.setCurrentText("FIAMM_24V")

    assert panel._model.max_charge_current_A_profile_override == 0.8006
    assert panel._model.charge_current_profile_limit_A == 0.8006
    assert panel._model.requested_charge_current_A == 1.0
    assert panel._charge_current_spin.value() == 1.0
    assert panel.refresh_validation() is ValidationState.CONFIG_INCOMPATIBLE
    assert not panel.is_start_allowed()
    assert panel.build_start_summary()["blocking_errors"] == [
        "CONFIG_INCOMPATIBLE: kért töltőáram 1.0 A > profilmaximum 0.8006 A"
    ]
    assert panel._charge_current_label.text() == (
        "kért: 1.00 A — CONFIG_INCOMPATIBLE (profilmaximum: 0.8006 A)"
    )


class TestR10B1HiddenChargeCurrent:
    """R10-B1: rejtett CUSTOM toltoaram, Start es config-hash."""

    start_window = TestR8B3DischargeCurrentLimit.start_window

    @pytest.fixture(autouse=True)
    def _isolate_sessions_root(self, monkeypatch):
        from pathlib import Path

        from Prog import app_paths

        monkeypatch.setattr(
            app_paths, "exe_dir",
            lambda: Path(__file__).parent / "_r10b1_unused_sessions",
        )

    def _set_custom_charge_current(self, panel, requested=1.6):
        panel._model_edit.setText("R10B1")
        panel._test_type_combo.setCurrentText("CHARGE_ONLY")
        panel._profile_combo.setCurrentText("FIAMM_12V")
        panel._psu_mode_combo.setCurrentText("INDEPENDENT")
        panel._capacity_spin.setValue(7.2)
        panel._charge_current_spin.setValue(requested)

    def test_hidden_custom_current_does_not_block_discharge_start(self, start_window):
        from Prog.gui.derived_parameters import ValidationState

        window, worker, thread, warning = start_window
        panel = window._config_panel
        self._set_custom_charge_current(panel)
        panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        assert panel._charge_box.isHidden()
        assert panel.refresh_validation() is ValidationState.OK
        assert panel.is_start_allowed()
        assert panel.build_start_summary()["blocking_errors"] == []

        window._live_panel._start_btn.click()

        warning.assert_not_called()
        worker.assert_called_once()
        thread.return_value.start.assert_called_once()
        cfg = worker.call_args.args[0]
        assert cfg.test_type == "DISCHARGE_ONLY"
        assert cfg.charge_current_A_override == 0.0
        assert cfg.validate() == []
        assert panel._model.requested_charge_current_A == pytest.approx(1.6)

    def test_visible_custom_current_still_blocks_start(self, start_window):
        from Prog.gui.derived_parameters import ValidationState

        window, worker, thread, warning = start_window
        panel = window._config_panel
        self._set_custom_charge_current(panel)
        cfg = panel.get_session_config()
        assert cfg.charge_current_A_override == pytest.approx(1.6)
        errors = cfg.validate()
        assert len(errors) == 1
        assert "Töltőáram (1.60A) > PSU INDEPENDENT limit (1.5A)" == errors[0]
        assert panel.refresh_validation() is ValidationState.CONFIG_INCOMPATIBLE
        assert not panel.is_start_allowed()
        assert panel.build_start_summary()["blocking_errors"]

        window._live_panel._start_btn.click()

        worker.assert_not_called()
        thread.assert_not_called()
        warning.assert_called_once()
        assert warning.call_args.args[2] == errors[0]

    @pytest.mark.parametrize("requested,allowed", [(1.2, True), (1.6, False)])
    def test_custom_draft_applies_again_when_charge_group_returns(
        self, qapp, requested, allowed,
    ):
        from Prog.gui.derived_parameters import ChargeCurrentPolicy, ValidationState

        panel = ConfigPanel()
        self._set_custom_charge_current(panel, requested)
        panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        assert panel._model.charge_current_policy is ChargeCurrentPolicy.CUSTOM
        assert panel._model.requested_charge_current_A == pytest.approx(requested)
        assert panel.get_session_config().charge_current_A_override == 0.0

        panel._test_type_combo.setCurrentText("CHARGE_ONLY")

        assert not panel._charge_box.isHidden()
        assert panel._model.charge_current_policy is ChargeCurrentPolicy.CUSTOM
        assert panel._charge_current_spin.value() == pytest.approx(requested)
        assert panel.get_session_config().charge_current_A_override == pytest.approx(requested)
        state = panel.refresh_validation()
        assert state is (ValidationState.OK if allowed else ValidationState.CONFIG_INCOMPATIBLE)
        assert panel.is_start_allowed() is allowed
        assert bool(panel.get_session_config().validate()) is (not allowed)

    def test_hidden_custom_currents_produce_equal_configs_and_hashes(self, qapp):
        from Prog.src.session_recovery import compute_config_hash

        configs = []
        for requested in (1.2, 1.6):
            panel = ConfigPanel()
            self._set_custom_charge_current(panel, requested)
            panel._test_type_combo.setCurrentText("DISCHARGE_ONLY")
            assert panel._model.requested_charge_current_A == pytest.approx(requested)
            configs.append(panel.get_session_config())

        assert configs[0] == configs[1]
        assert configs[0].charge_current_A_override == 0.0
        assert compute_config_hash(configs[0]) == compute_config_hash(configs[1])


class TestR16B16aGuiStartChain:
    @pytest.mark.parametrize("mode", ["DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL"])
    def test_preset_custom_profile_revalidation_chain(self, qapp, mode):
        panel = ConfigPanel()
        panel._test_type_combo.setCurrentText(mode)
        panel._model_edit.setText("FG20721")
        panel._capacity_spin.setValue(7.2)
        panel._psu_mode_combo.setCurrentText("PARALLEL")
        if mode != "DISCHARGE_ONLY":
            panel._charge_current_spin.setValue(0.67)
        panel._apply_discharge_preset(20, 1.75)
        panel.refresh_validation()
        assert panel.is_start_allowed()
        cfg = panel.get_session_config()
        assert cfg.discharge_rate_origin.rate_policy == "C/20"
        assert cfg.discharge_terminate_voltage_V == 10.5
        panel._discharge_current_spin.setValue(0.36)
        # Azonos számértéknél a widget nem küld új signalt: explicit szerkesztés.
        panel._on_discharge_current_spin_changed(0.36)
        assert not panel.is_start_allowed()
        panel.refresh_validation()
        assert not panel.is_start_allowed()
        assert panel.get_session_config().discharge_terminate_voltage_V == 10.5
        panel._apply_discharge_preset(20, 1.75)
        panel._profile_combo.setCurrentText("FIAMM_24V")
        assert not panel.is_start_allowed()
        panel.refresh_validation()
        assert panel.is_start_allowed()
        assert panel.get_session_config().discharge_terminate_voltage_V == 21.0
        panel._model_edit.setText("OTHER")
        panel.refresh_validation()
        assert not panel.is_start_allowed()
