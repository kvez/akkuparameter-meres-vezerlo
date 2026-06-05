"""
ReportGenerator unit tesztek — kötelező mezők és MEASUREMENT_LIMITATIONS.
"""
from Prog.src.report_generator import ReportGenerator


def make_session_meta(**kwargs) -> dict:
    defaults = {
        "psu_mode": "INDEPENDENT",
        "psu_mode_max_voltage_V": 30.0,
        "psu_mode_max_current_A": 1.5,
        "temperature_compensation_mode": "MONITOR_ONLY",
        "interrupted_session_recovered": False,
        "charge_Ah_total": 7.2,
        "discharge_Ah_total": 0.0,
        "capacity_result_quality": "OK",
        "fallback_integration_duration_s": 0.0,
        "fallback_samples_count": 0,
        "communication_faults_count": 0,
        "emergency_stop_occurred": False,
        "test_name": "CC_CHARGE",
    }
    defaults.update(kwargs)
    return defaults


class TestMandatoryHardwareFields:
    """Minden riportban kötelező hardver- és konfig-mezők."""

    def test_no_galvanic_isolation_is_true(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["no_galvanic_isolation"] is True

    def test_isolation_method_is_psu_output_off_only(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["isolation_method"] == "PSU_OUTPUT_OFF_ONLY"

    def test_series_diode_installed_is_true(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["series_diode_installed"] is True

    def test_series_diode_type_is_silicon_rectifier(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert "silicon" in report["series_diode_type"].lower()

    def test_series_diode_part_number_is_by550(self):
        """[BY550] A riportban a BY550 azonosítja a diódát."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["series_diode_part_number"] == "BY550"

    def test_battery_temperature_source_is_dmm2(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert "DMM2" in report["battery_temperature_source"]

    def test_ocv_isolation_method_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert "ocv_isolation_method" in report

    def test_galvanic_isolation_note_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert "galvanic_isolation_note" in report
        assert len(report["galvanic_isolation_note"]) > 10


class TestNoRelayFieldInReport:
    """[R1] relay_state vagy relay bármi tilos a riportban."""

    def test_no_relay_state_in_report(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        keys_str = " ".join(str(k).lower() for k in report.keys())
        assert "relay_state" not in keys_str

    def test_no_relay_in_isolation_method(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert "relay" not in report["isolation_method"].lower()


class TestSessionFields:
    def test_psu_mode_from_session(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(psu_mode="SERIES"))
        assert report["psu_mode"] == "SERIES"

    def test_temp_compensation_mode_from_session(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            temperature_compensation_mode="ENABLED"
        ))
        assert report["temperature_compensation_mode"] == "ENABLED"

    def test_interrupted_session_recovered_false(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(interrupted_session_recovered=False))
        assert report["interrupted_session_recovered"] is False

    def test_interrupted_session_recovered_true(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(interrupted_session_recovered=True))
        assert report["interrupted_session_recovered"] is True


class TestMeasurementLimitations:
    """[N13] MEASUREMENT_LIMITATIONS dedikált szekció."""

    def test_measurement_limitations_block_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert "MEASUREMENT_LIMITATIONS" in report

    def test_no_galvanic_isolation_during_ocv_is_true(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert lim["no_galvanic_isolation_during_ocv"] is True

    def test_no_external_calibrated_shunt_is_true(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert lim["no_external_calibrated_shunt"] is True

    def test_current_measurement_source_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert "current_measurement_source" in lim

    def test_capacity_result_quality_ok_by_default(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(capacity_result_quality="OK"))
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert lim["capacity_result_quality"] == "OK"

    def test_capacity_result_quality_degraded(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            capacity_result_quality="DEGRADED",
            fallback_integration_duration_s=35.0,
            fallback_samples_count=7,
        ))
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert lim["capacity_result_quality"] == "DEGRADED"
        assert lim["fallback_integration_duration_s"] == 35.0

    def test_emergency_stop_occurred_false(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(emergency_stop_occurred=False))
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert lim["emergency_stop_occurred"] is False

    def test_emergency_stop_occurred_true(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(emergency_stop_occurred=True))
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert lim["emergency_stop_occurred"] is True

    def test_communication_faults_count_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(communication_faults_count=3))
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert lim["communication_faults_count"] == 3

    def test_charge_ah_accuracy_note_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert "charge_Ah_accuracy_note" in lim

    def test_discharge_ah_accuracy_note_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert "discharge_Ah_accuracy_note" in lim
