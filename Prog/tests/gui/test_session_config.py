"""SessionConfig validáció unit tesztek."""
import pytest

from Prog.gui.panels.config_panel import SessionConfig


class TestConfigHashFieldsMatchRealDataclass:
    """R2-04 (2026-09-15, P1): meta-teszt `dataclasses.fields()`
    introspekcióval -- megelőzi, hogy a session_recovery.py
    CONFIG_HASH_FIELDS whitelist egy elgépelt/átnevezett mezőnevet
    tartalmazzon, amit a fail-closed `_stable_hash()` egy VALÓDI
    `SessionConfig`-gal azonnal AttributeError-ra váltana. Ez a meta-teszt
    a GUI suite-ban él, mert `SessionConfig` (config_panel.py) PySide6-ot
    importál -- l. `test_session_recovery.py` a `BatteryProfile`/
    PROFILE_HASH_FIELDS nem-GUI oldali analóg meta-tesztjéért."""

    def test_every_config_hash_field_exists_on_session_config(self):
        import dataclasses

        from Prog.src.session_recovery import CONFIG_HASH_FIELDS

        real_field_names = {f.name for f in dataclasses.fields(SessionConfig)}
        missing = set(CONFIG_HASH_FIELDS) - real_field_names
        assert not missing, f"CONFIG_HASH_FIELDS elgepelt/nemletezo mezot tartalmaz: {missing}"

    def test_every_session_config_field_is_hashed_or_explicitly_excluded(self):
        """R2-04-F7 (2026-09-16, DFAM-003 STRUCTURAL_PREVENTION_REVIEW):
        a MÁSIK irány -- egy ÚJ SessionConfig mező mostantól nem tud
        csendben a "nem hashelt" defaultra esni. Mutáció, amin bukik: egy
        új mező felvétele a dataclass-ba anélkül, hogy a CONFIG_HASH_
        FIELDS-be vagy a _CONFIG_FIELDS_INTENTIONALLY_NOT_HASHED-be
        kerülne."""
        import dataclasses

        from Prog.src.session_recovery import (
            _CONFIG_FIELDS_INTENTIONALLY_NOT_HASHED,
            CONFIG_HASH_FIELDS,
        )

        real_field_names = {f.name for f in dataclasses.fields(SessionConfig)}
        classified = set(CONFIG_HASH_FIELDS) | set(_CONFIG_FIELDS_INTENTIONALLY_NOT_HASHED)
        unclassified = real_field_names - classified
        assert not unclassified, (
            f"SessionConfig uj mezo(k) nincs(nek) osztalyozva: {unclassified} -- "
            "vagy CONFIG_HASH_FIELDS-be, vagy _CONFIG_FIELDS_INTENTIONALLY_"
            "NOT_HASHED-be kell kerulniuk, indoklassal"
        )


class TestStartDialogDisplaysEveryHashedField:
    """Kor 18, Batch C7 R17-15 (DFAM-003+DFAM-017): a Start-megerosito
    dialogus mezolistaja korabban KEZZEL tartott, MAS lista volt, mint a
    resume-kapu CONFIG_HASH_FIELDS -- semmi nem kototte ossze a kettot.
    A d10cee0 (R2-04-F7) teszt-only strukturalis prevencio harmadik
    alkalmazasa: egy jovobeli hashelt mezo, amit itt nem jelenitunk meg
    ES nem sorolunk fel kifejezetten kizartkent, bukja a suite-ot."""

    def test_every_hashed_config_field_is_displayed_or_explicitly_excluded(self):
        from Prog.gui.main_window import (
            _START_DIALOG_DISPLAYED_CONFIG_FIELDS,
            _START_DIALOG_FIELDS_INTENTIONALLY_NOT_DISPLAYED,
        )
        from Prog.src.session_recovery import CONFIG_HASH_FIELDS

        classified = (
            set(_START_DIALOG_DISPLAYED_CONFIG_FIELDS)
            | set(_START_DIALOG_FIELDS_INTENTIONALLY_NOT_DISPLAYED)
        )
        unclassified = set(CONFIG_HASH_FIELDS) - classified
        assert not unclassified, (
            f"Hashelt SessionConfig mezo(k) nincs(nek) a Start-dialogusban "
            f"megjelenitve/kizarva: {unclassified} -- vagy "
            "_START_DIALOG_DISPLAYED_CONFIG_FIELDS-be, vagy "
            "_START_DIALOG_FIELDS_INTENTIONALLY_NOT_DISPLAYED-be kell "
            "kerulniuk, indoklassal"
        )

    def test_displayed_field_names_exist_on_session_config(self):
        import dataclasses

        from Prog.gui.main_window import _START_DIALOG_DISPLAYED_CONFIG_FIELDS

        real_field_names = {f.name for f in dataclasses.fields(SessionConfig)}
        missing = set(_START_DIALOG_DISPLAYED_CONFIG_FIELDS) - real_field_names
        assert not missing, (
            f"_START_DIALOG_DISPLAYED_CONFIG_FIELDS elgepelt/nemletezo "
            f"mezot tartalmaz: {missing}"
        )

    def test_profile_hash_fields_are_all_classified_as_not_displayed(self):
        """A PROFILE_HASH_FIELDS 17 mezoje mind a battery_profile.py-bol
        szarmazik (nem operator-input a Start-dialogus pillanataban) --
        a battery_profile_name (mar megjelenitve) az azonositojuk. Ez a
        teszt rogziti ezt a mernoki dontest, hogy egy jovobeli olvaso ne
        ertelmezze felre veletlenul."""
        from Prog.gui.main_window import _START_DIALOG_DISPLAYED_CONFIG_FIELDS
        from Prog.src.session_recovery import PROFILE_HASH_FIELDS

        assert set(PROFILE_HASH_FIELDS).isdisjoint(
            set(_START_DIALOG_DISPLAYED_CONFIG_FIELDS)
        )


class TestSessionConfigValidation:
    def _valid(self, **overrides) -> SessionConfig:
        cfg = SessionConfig(
            battery_profile_name="FIAMM_12V",
            battery_model="FG20721",
            nominal_capacity_ah=20.0,
            sample_id="SN001",
            psu_resource="USB0::0x05E6::0x2220::0::INSTR",
            load_resource="USB0::0x05E6::0x2380::0::INSTR",
            dmm_voltage_resource="TCPIP0::192.168.1.10::inst0::INSTR",
            dmm_temperature_resource="TCPIP0::192.168.1.11::inst0::INSTR",
            psu_mode="INDEPENDENT",
            hardware_wiring_confirmed=False,
            test_type="CHARACTERIZATION",
            runner_tick_s=2.0,
            taper_hold_s=600.0,
            temperature_compensation_mode="MONITOR_ONLY",
        )
        for k, v in overrides.items():
            object.__setattr__(cfg, k, v)
        return cfg

    def test_valid_config_has_no_errors(self):
        assert self._valid().validate() == []

    def test_zero_capacity_is_error(self):
        errors = self._valid(nominal_capacity_ah=0.0).validate()
        assert any("nominal_capacity_ah" in e for e in errors)

    def test_negative_capacity_is_error(self):
        errors = self._valid(nominal_capacity_ah=-1.0).validate()
        assert any("nominal_capacity_ah" in e for e in errors)

    def test_empty_model_is_error(self):
        errors = self._valid(battery_model="").validate()
        assert any("battery_model" in e for e in errors)

    def test_whitespace_model_is_error(self):
        errors = self._valid(battery_model="   ").validate()
        assert any("battery_model" in e for e in errors)

    @pytest.mark.parametrize("model", ["FG20721/AGM", "FG20721\\AGM", "a/../../x"])
    def test_model_with_path_separator_is_error(self, model):
        """[regresszió, R1 F-18] a battery_model a session-mappa nevébe
        kerül (session_{battery_model}_{stamp}); egy '/' vagy '\\' a
        Sessions gyökér ALÁ beágyazott (vagy '..'-tal azon kívüli) mappát
        hozna létre, amit a nem rekurzív discovery/resume nem lát."""
        errors = self._valid(battery_model=model).validate()
        assert any(
            "battery_model" in e and "útvonal-elválasztó" in e for e in errors
        ), errors

    @pytest.mark.parametrize("model", ["FG20721_AGM", "FG 20721-v2", ".."])
    def test_model_without_path_separator_is_accepted(self, model, tmp_path):
        assert self._valid(battery_model=model).validate() == []
        session_dir = tmp_path / f"session_{model}_20261025_120000"
        assert session_dir.resolve().parent == tmp_path.resolve()

    def test_empty_psu_resource_is_error(self):
        errors = self._valid(psu_resource="").validate()
        assert any("psu_resource" in e for e in errors)

    def test_empty_load_resource_is_error(self):
        errors = self._valid(load_resource="").validate()
        assert any("load_resource" in e for e in errors)

    def test_empty_dmm_voltage_resource_is_error(self):
        errors = self._valid(dmm_voltage_resource="").validate()
        assert any("dmm_voltage_resource" in e for e in errors)

    def test_empty_dmm_temperature_resource_is_error(self):
        errors = self._valid(dmm_temperature_resource="").validate()
        assert any("dmm_temperature_resource" in e for e in errors)

    def test_parallel_mode_requires_wiring_confirmed(self):
        errors = self._valid(
            psu_mode="PARALLEL", hardware_wiring_confirmed=False
        ).validate()
        assert any("hardware_wiring_confirmed" in e for e in errors)

    def test_series_mode_requires_wiring_confirmed(self):
        errors = self._valid(
            psu_mode="SERIES", hardware_wiring_confirmed=False
        ).validate()
        assert any("hardware_wiring_confirmed" in e for e in errors)

    def test_parallel_mode_with_wiring_confirmed_ok(self):
        errors = self._valid(
            psu_mode="PARALLEL", hardware_wiring_confirmed=True
        ).validate()
        assert errors == []

    def test_24v_profile_requires_series_mode(self):
        errors = self._valid(
            battery_profile_name="FIAMM_24V", psu_mode="INDEPENDENT",
            hardware_wiring_confirmed=False,
        ).validate()
        assert any("SERIES" in e or "24V" in e for e in errors)

    def test_24v_profile_with_series_ok(self):
        errors = self._valid(
            battery_profile_name="FIAMM_24V",
            psu_mode="SERIES",
            hardware_wiring_confirmed=True,
        ).validate()
        assert errors == []

    def test_multiple_errors_returned(self):
        errors = self._valid(
            battery_model="", nominal_capacity_ah=0.0
        ).validate()
        assert len(errors) >= 2

    def test_traditional_learning_requires_bq_port(self):
        """[regresszió, D9/F-01, 2026-09-13] korábban egy hardkódolt
        `test_type in ("TRADITIONAL_LEARNING", "GOLDEN_GG")` ágból élt --
        mostantól `ui_policy.is_group_required()`-en keresztül, a registry
        TRADITIONAL_LEARNING `required_groups={"bq_port"}` bejegyzésével
        (nulla viselkedésváltozás)."""
        errors = self._valid(test_type="TRADITIONAL_LEARNING", bq_port="").validate()
        assert any("bq_port" in e for e in errors)

    def test_traditional_learning_with_bq_port_has_no_bq_port_error(self):
        errors = self._valid(test_type="TRADITIONAL_LEARNING", bq_port="COM8").validate()
        assert not any("bq_port" in e for e in errors)

    def test_golden_gg_requires_bq_port_and_system_rate_current(self):
        errors = self._valid(
            test_type="GOLDEN_GG", bq_port="", golden_gg_system_rate_current_A=0.0,
        ).validate()
        assert any("bq_port" in e for e in errors)
        assert any("golden_gg_system_rate_current_A" in e for e in errors)

    def test_golden_gg_with_both_fields_set_has_no_errors_for_them(self):
        errors = self._valid(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.72,
        ).validate()
        assert not any("bq_port" in e for e in errors)
        assert not any("golden_gg_system_rate_current_A" in e for e in errors)

    def test_characterization_does_not_require_bq_port(self):
        """CHARACTERIZATION nincs a TRADITIONAL_LEARNING/GOLDEN_GG
        required_groups listáján -- bq_port hiánya itt NEM hiba."""
        errors = self._valid(test_type="CHARACTERIZATION", bq_port="").validate()
        assert not any("bq_port" in e for e in errors)

    def test_auto_mode_zero_charge_current_is_never_an_error(self):
        """D9/F-01 tudatos döntés: a `charge_current` csoport policy-
        vezérelt, MINDIG van auto/default értéke (a spinbox "auto
        (számított)" speciális értéke) -- a `charge_current_A_override=0.0`
        SOHA nem lehet hiba, még olyan módban sem, ahol a csoport
        `required_groups`-ban szerepel (pl. CHARGE_ONLY/CHARACTERIZATION/
        BQ_LEARNING_PHYSICAL)."""
        for test_type in ("CHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL"):
            errors = self._valid(
                test_type=test_type, charge_current_A_override=0.0,
            ).validate()
            assert not any("charge_current" in e for e in errors), test_type


class TestSessionConfigNewFields:
    def test_new_fields_have_correct_defaults(self):
        cfg = SessionConfig()
        assert cfg.relax_after_charge_s == 600.0
        assert cfg.charge_current_A_override == 0.0
        assert cfg.discharge_current_A == 0.0
        assert cfg.discharge_terminate_voltage_V == 0.0

    def test_validate_terminate_voltage_too_low(self):
        """GOV-029: a 9,0 V a rátabizonyíték nélküli 10,80 V minimum alatt blokkoló."""
        cfg = SessionConfig(
            battery_profile_name="FIAMM_12V",
            discharge_terminate_voltage_V=9.0,
        )
        errors = cfg.validate()
        assert any("Végfeszültség" in e for e in errors)

    def test_validate_terminate_voltage_ok(self):
        """GOV-029: a rátabizonyíték nélküli minimum 10,80 V."""
        cfg = SessionConfig(
            battery_profile_name="FIAMM_12V",
            discharge_terminate_voltage_V=10.8,
        )
        errors = cfg.validate()
        assert not any("Végfeszültség" in e for e in errors)

    def test_get_warnings_terminate_voltage_below_recommended(self):
        """GOV-029: a 10,0 V blokkoló hiba; a kiegészítő figyelmeztetés is megmarad."""
        cfg = SessionConfig(
            battery_profile_name="FIAMM_12V",
            discharge_terminate_voltage_V=10.0,
        )
        assert any("Végfeszültség" in e for e in cfg.validate())
        warnings = cfg.get_warnings()
        assert any("Végfeszültség" in w for w in warnings)

    def test_get_warnings_no_warning_above_recommended(self):
        """10.5V = 1.75V/cella → nincs figyelmeztetés."""
        cfg = SessionConfig(
            battery_profile_name="FIAMM_12V",
            discharge_terminate_voltage_V=10.5,
        )
        assert not cfg.get_warnings()

    def test_validate_charge_current_above_psu_limit_independent(self):
        """INDEPENDENT mód, 2.0A override > 1.5A limit → hiba."""
        cfg = SessionConfig(charge_current_A_override=2.0, psu_mode="INDEPENDENT")
        errors = cfg.validate()
        assert any("Töltőáram" in e for e in errors)

    def test_validate_charge_current_ok_parallel(self):
        """PARALLEL mód, 2.5A override <= 3.0A limit → nincs Töltőáram hiba."""
        cfg = SessionConfig(
            charge_current_A_override=2.5,
            psu_mode="PARALLEL",
            hardware_wiring_confirmed=True,
        )
        errors = cfg.validate()
        assert not any("Töltőáram" in e for e in errors)

    def test_validate_zero_override_skips_checks(self):
        """0.0 értékek esetén (profil default) ne legyen validációs hiba."""
        cfg = SessionConfig(
            discharge_terminate_voltage_V=0.0,
            charge_current_A_override=0.0,
        )
        errors = cfg.validate()
        assert not any("Végfeszültség" in e for e in errors)
        assert not any("Töltőáram" in e for e in errors)


class TestD8gDischargeRateDivisorAlignedDefault:
    """[regresszió, Kör 19 D8g, SW19-05, DFAM-017] a SessionConfig.
    discharge_rate_divisor dataclass-alapértéke korábban 5 volt, miközben
    a GUI-n át az EGYETLEN produkciós konstrukció (ConfigPanel.
    get_session_config()) mindig explicit 10-et adott át -- a régi 5-ös
    alapérték SOHA nem volt elérhető a GUI-n keresztül, csak egy közvetlen,
    GUI-n kívüli SessionConfig()-hívásnál (pl. Prog/tools/ szkript),
    ahol csendben eltért az OcvSocConfig testvér-mező 10-es
    alapértékétől. Ez a teszt a régi kódon BUKNA (cfg.discharge_rate_
    divisor == 5 lenne)."""

    def test_bare_session_config_discharge_rate_divisor_matches_gui_value(self):
        cfg = SessionConfig()
        assert cfg.discharge_rate_divisor == 10

    def test_matches_ocv_soc_config_sibling_default(self):
        from Prog.src.ocv_soc_controller import OcvSocConfig
        assert SessionConfig().discharge_rate_divisor == OcvSocConfig().discharge_rate_divisor


class TestCF15SessionConfigReadsCanonicalDerivedParametersSources:
    """[regresszió, Governance-v2 review-kör 1 (CF-15/DFAM-017 3.
    előfordulás, batch 4.5)] validate()/get_warnings() cell_count-ja és a
    töltőáram-override PSU-limitje korábban SessionConfig-specifikus
    duplikált dict-ekből jött (_PROFILE_DEFAULTS, inline "3.0 if PARALLEL
    else 1.5") — mostantól a kanonikus derived_parameters.PROFILE_META/
    PSU_HW_MAX_CURRENT_A-ból. A monkeypatch bizonyítja, hogy a forrás
    TÉNYLEGESEN ez, nem egy másolat (egy másolat nem reagálna a
    monkeypatch-re)."""

    def test_validate_terminate_voltage_reads_canonical_profile_meta(self, monkeypatch):
        from Prog.gui import derived_parameters
        monkeypatch.setitem(
            derived_parameters.PROFILE_META, "FIAMM_12V",
            {"cell_count": 20, "nominal_voltage_V": 12.0, "manufacturer": "FIAMM"},
        )
        # 20 cella × 1.60V = 32.0V abszolút minimum -- a valós (6 cellás)
        # FIAMM_12V-hez képest jóval magasabb, tehát diszkrimináló.
        cfg = SessionConfig(
            battery_profile_name="FIAMM_12V", discharge_terminate_voltage_V=25.0,
        )
        errors = cfg.validate()
        assert any("Végfeszültség" in e for e in errors)

    def test_get_warnings_terminate_voltage_reads_canonical_profile_meta(self, monkeypatch):
        from Prog.gui import derived_parameters
        monkeypatch.setitem(
            derived_parameters.PROFILE_META, "FIAMM_12V",
            {"cell_count": 20, "nominal_voltage_V": 12.0, "manufacturer": "FIAMM"},
        )
        # 20 cella × 1.75V = 35.0V ajánlott minimum.
        cfg = SessionConfig(
            battery_profile_name="FIAMM_12V", discharge_terminate_voltage_V=32.0,
        )
        warnings = cfg.get_warnings()
        assert any("Végfeszültség" in w for w in warnings)

    def test_validate_charge_current_override_reads_canonical_psu_limit(self, monkeypatch):
        from Prog.gui import derived_parameters
        monkeypatch.setitem(derived_parameters.PSU_HW_MAX_CURRENT_A, "INDEPENDENT", 0.5)
        cfg = SessionConfig(
            battery_profile_name="FIAMM_12V", psu_mode="INDEPENDENT",
            charge_current_A_override=1.0,  # > 0.5 mockolt limit, DE < a valós 1.5A limit
        )
        errors = cfg.validate()
        assert any("Töltőáram" in e for e in errors)


_R2B8_FORBIDDEN = ["<", ">", ":", '"', "|", "?", "*"] + [chr(i) for i in range(32)]
_R2B8_LEGIT = [
    "FG20721_AGM", "FG 20721-v2", "..", "ÁRVÍZ-tűrő", "A.", "CON", "NUL",
    "A\x7fB", "FG20721 (12V, 7.2Ah)", "x" * 231, "\U0001F600" * 115 + "x",
]


class TestR2B8BatteryModelWindowsComponentRules:
    """[regresszió, R2-B8 / R2-ARCH-01, DFAM-046 2. előfordulás] A
    battery_model a session-mappa nevébe kerül
    (main_window.py::_build_runner(): session_{battery_model}_{stamp}); a
    validate() a Windows-fájlnévben tiltott karaktereket ('<>:"|?*' és
    U+0000..U+001F) és a komponens-hosszkorlátot (255 UTF-16 kódegység -
    24 = 231) a Start ELŐTT utasítsa el, ne a mkdir bukjon később."""

    @staticmethod
    def _cfg(model):
        return SessionConfig(
            battery_model=model, nominal_capacity_ah=7.2, psu_resource="a",
            load_resource="b", dmm_voltage_resource="c", dmm_temperature_resource="d",
        )

    @pytest.mark.parametrize("ch", _R2B8_FORBIDDEN, ids=lambda c: f"U+{ord(c):04X}")
    def test_forbidden_char_is_error(self, ch):
        errors = self._cfg(f"FG{ch}20721").validate()
        assert any("battery_model" in e and "tiltott karakter" in e for e in errors), errors

    @pytest.mark.parametrize("model", ["x" * 232, "\U0001F600" * 116], ids=["x232", "emoji116"])
    def test_too_long_is_error(self, model):
        errors = self._cfg(model).validate()
        assert any("battery_model" in e and "túl hosszú" in e for e in errors), errors

    @pytest.mark.parametrize(
        "model", _R2B8_LEGIT, ids=[f"legit{i}" for i in range(len(_R2B8_LEGIT))]
    )
    def test_legit_model_accepted_and_creatable(self, model, tmp_path):
        assert self._cfg(model).validate() == []
        d = tmp_path / f"session_{model}_20260101_000000"
        d.mkdir()
        (d / "checkpoint.json").write_text("{}", encoding="utf-8")
        assert [p.name for p in tmp_path.iterdir()] == [d.name]
        assert d.resolve().parent == tmp_path.resolve()

    def test_every_single_char_accepted_by_validate_is_creatable(self, tmp_path):
        """Strukturális őr (SPR): U+0001..U+02FF és a felsorolt extra kódpontok esetén a validate()-en átmenő A{ch}B modellből képzett session-mappa létrehozható."""
        bad = []
        cps = list(range(0x0001, 0x0300)) + [0x200B, 0x2028, 0xFEFF, 0xFFFF, 0x1F600, 0x10FFFF]
        for cp in cps:
            model = f"A{chr(cp)}B"
            if self._cfg(model).validate():
                continue
            d = tmp_path / f"session_{model}_20260101_000000"
            try:
                d.mkdir()
                d.rmdir()
            except (OSError, ValueError):
                bad.append(f"U+{cp:04X}")
        assert bad == [], bad[:20]

    def test_lone_surrogate_validate_returns_list_without_raising(self):
        result = self._cfg("A\ud800B").validate()
        assert isinstance(result, list)


class TestR16B16aSessionFloor:
    @pytest.mark.parametrize("profile,cells", [("FIAMM_12V", 6), ("FIAMM_24V", 12)])
    @pytest.mark.parametrize("value", [1.0, 1.60, 1.75, float("nan"), float("inf")])
    def test_direct_config_cannot_bypass_floor(self, profile, cells, value):
        cfg = SessionConfig(battery_profile_name=profile, discharge_terminate_voltage_V=value * cells)
        assert any("Végfeszültség" in e for e in cfg.validate())
        assert repr(cfg.discharge_terminate_voltage_V) == repr(value * cells)

    def test_c20_proof_allows_175_and_custom_revokes_it(self):
        from Prog.src.battery_profile import DischargeRateOrigin

        cfg = SessionConfig(
            battery_model="FG20721", nominal_capacity_ah=7.2, discharge_current_A=0.36,
            discharge_terminate_voltage_V=10.5,
            discharge_rate_origin=DischargeRateOrigin("C/20", "NOMINAL_C10_CAPACITY", 7.2),
        )
        assert not any("Végfeszültség" in e for e in cfg.validate())
        cfg.discharge_rate_origin = DischargeRateOrigin("CUSTOM", "NOMINAL_C10_CAPACITY", 7.2)
        assert any("Végfeszültség" in e for e in cfg.validate())

    def test_measured_capacity_at_180_is_still_allowed(self):
        from Prog.src.battery_profile import DischargeRateOrigin

        cfg = SessionConfig(
            battery_model="FG20721", nominal_capacity_ah=7.2, discharge_current_A=0.35,
            discharge_terminate_voltage_V=10.8,
            discharge_rate_origin=DischargeRateOrigin("C/20", "MEASURED_USABLE_CAPACITY", 7.0),
        )
        assert not any("GOV-029" in e for e in cfg.validate())


class TestR16B17WarningText:
    def test_low_cutoff_warning_describes_binding_rate_minimum(self):
        cfg = SessionConfig(
            battery_profile_name="FIAMM_12V", discharge_terminate_voltage_V=10.0,
        )
        assert any("Végfeszültség" in error for error in cfg.validate())
        warning, = cfg.get_warnings()
        assert warning == (
            "Végfeszültség (10.00V) < az igazolt FG20721 C/20 minimuma (10.50V). "
            "GOV-029: más vagy bizonytalan rátánál 1.80V/cella a minimum; "
            "a minimum alatti érték normál kisütésben blokkoló hiba."
        )
