"""SessionConfig validáció unit tesztek."""
from Prog.gui.panels.config_panel import SessionConfig


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


class TestSessionConfigNewFields:
    def test_new_fields_have_correct_defaults(self):
        cfg = SessionConfig()
        assert cfg.relax_after_charge_s == 600.0
        assert cfg.charge_current_A_override == 0.0
        assert cfg.discharge_current_A == 0.0
        assert cfg.discharge_terminate_voltage_V == 0.0

    def test_validate_terminate_voltage_too_low(self):
        """12V akku (6 cella): min 6×1.60=9.60V. 9.0V alatt hiba."""
        cfg = SessionConfig(discharge_terminate_voltage_V=9.0)
        # battery_profile_name default = "FIAMM_12V" → cell_count=6 → min=9.60V
        errors = cfg.validate()
        assert any("Végfeszültség" in e for e in errors)

    def test_validate_terminate_voltage_ok(self):
        """10.5V >= 9.60V minimum → nincs hiba."""
        cfg = SessionConfig(discharge_terminate_voltage_V=10.5)
        errors = cfg.validate()
        assert not any("Végfeszültség" in e for e in errors)

    def test_validate_charge_current_above_psu_limit_independent(self):
        """INDEPENDENT mód, 2.0A override > 1.5A limit → hiba."""
        cfg = SessionConfig(charge_current_A_override=2.0, psu_mode="INDEPENDENT")
        errors = cfg.validate()
        assert any("Töltőáram" in e for e in errors)

    def test_validate_charge_current_ok_parallel(self):
        """PARALLEL mód, 2.5A override <= 3.0A limit → nincs hiba."""
        cfg = SessionConfig(charge_current_A_override=2.5, psu_mode="PARALLEL")
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
