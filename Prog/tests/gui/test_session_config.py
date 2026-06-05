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
