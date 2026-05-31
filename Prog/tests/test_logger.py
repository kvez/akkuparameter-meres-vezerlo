"""
Logger unit tesztek — valós fájlrendszer, tmp_path fixture.
"""
import csv
import json
import sqlite3
from pathlib import Path

import pytest
from Prog.src.logger import Logger, LogConfig, CSV_COLUMNS


def make_sample(**kwargs) -> dict:
    defaults = {
        "timestamp_iso": "2026-05-31T10:00:00.000",
        "elapsed_s": 1.0,
        "test_name": "CC_CHARGE",
        "step_name": "CHARGE_CC",
        "state": "CHARGE_CC",
        "battery_voltage_V": 12.5,
        "charge_current_A": 1.4,
        "discharge_current_A": 0.0,
        "signed_current_A": 1.4,
        "battery_temperature_C": 22.0,
        "ambient_temperature_C": None,
        "psu_set_voltage_V": 14.2,
        "psu_set_current_A": 1.75,
        "psu_readback_voltage_V": 14.2,
        "psu_readback_current_A": 1.4,
        "psu_output_commanded_on": True,
        "psu_mode": "INDEPENDENT",
        "load_set_current_A": 0.0,
        "load_readback_voltage_V": 0.0,
        "load_readback_current_A": 0.0,
        "load_input_commanded_on": False,
        "isolation_state": "PSU_OUTPUT_ON",
        "accumulated_charge_Ah": 0.001,
        "accumulated_discharge_Ah": 0.0,
        "accumulated_charge_Wh": 0.014,
        "accumulated_discharge_Wh": 0.0,
        "integration_current_source": "PSU_READBACK",
        "u_drop_V": 0.85,
        "regulation_error_V": 0.0,
        "taper_timer_s": 0.0,
        "diode_power_W": 1.19,
        "dmm_voltage_valid": True,
        "dmm_temperature_valid": True,
        "psu_readback_valid": True,
        "load_readback_valid": True,
        "sample_valid": True,
        "integration_valid": True,
        "safety_valid": True,
        "capacity_result_quality": "OK",
        "fault_flags": "",
        "warning_flags": "",
        "event_code": "",
        "event_message": "",
    }
    defaults.update(kwargs)
    return defaults


class TestCsvColumns:
    """[R1] isolation_state kötelező; relay_state tilos."""

    def test_csv_header_has_isolation_state(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.close()
        header = (tmp_path / "samples.csv").read_text().splitlines()[0]
        assert "isolation_state" in header.split(",")

    def test_csv_header_has_no_relay_state(self, tmp_path):
        """[R1] Regression: relay_state soha nem kerülhet vissza a CSV-be."""
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.close()
        header = (tmp_path / "samples.csv").read_text().splitlines()[0]
        assert "relay_state" not in header.split(",")

    def test_csv_columns_match_module_constant(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.close()
        header_cols = (tmp_path / "samples.csv").read_text().splitlines()[0].split(",")
        for col in CSV_COLUMNS:
            assert col in header_cols, f"Hiányzó oszlop: {col}"

    def test_csv_columns_constant_has_no_relay_state(self):
        assert "relay_state" not in CSV_COLUMNS
        assert "isolation_state" in CSV_COLUMNS


class TestCsvLogging:
    def test_log_sample_appends_row(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_sample(make_sample())
        logger.close()
        rows = list(csv.DictReader((tmp_path / "samples.csv").open()))
        assert len(rows) == 1

    def test_log_multiple_samples(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        for i in range(5):
            logger.log_sample(make_sample(elapsed_s=float(i)))
        logger.close()
        rows = list(csv.DictReader((tmp_path / "samples.csv").open()))
        assert len(rows) == 5

    def test_isolation_state_value_written(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_sample(make_sample(isolation_state="PSU_OUTPUT_OFF_ONLY"))
        logger.close()
        rows = list(csv.DictReader((tmp_path / "samples.csv").open()))
        assert rows[0]["isolation_state"] == "PSU_OUTPUT_OFF_ONLY"

    def test_battery_voltage_written_correctly(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_sample(make_sample(battery_voltage_V=12.456))
        logger.close()
        rows = list(csv.DictReader((tmp_path / "samples.csv").open()))
        assert abs(float(rows[0]["battery_voltage_V"]) - 12.456) < 0.001


class TestSqlite:
    def test_sqlite_db_created(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.close()
        assert (tmp_path / "session.db").exists()

    def test_sqlite_contains_sample_after_close(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_sample(make_sample(battery_voltage_V=12.5))
        logger.close()
        conn = sqlite3.connect(tmp_path / "session.db")
        rows = conn.execute("SELECT battery_voltage_V FROM samples").fetchall()
        conn.close()
        assert len(rows) == 1
        assert abs(float(rows[0][0]) - 12.5) < 0.001  # TEXT oszlop → float konverzió

    def test_sqlite_has_isolation_state_column(self, tmp_path):
        """[R1] SQLite sémában isolation_state van, relay_state nincs."""
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_sample(make_sample())
        logger.close()
        conn = sqlite3.connect(tmp_path / "session.db")
        cols = [row[1] for row in conn.execute("PRAGMA table_info(samples)")]
        conn.close()
        assert "isolation_state" in cols
        assert "relay_state" not in cols

    def test_sqlite_immediate_commit_on_critical_event(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig(
            sqlite_commit_interval_s=9999.0  # soha nem commitálna időzítéssel
        ))
        logger.log_sample(make_sample(battery_voltage_V=11.0))
        logger.log_event("EMERGENCY_STOP", "Test fault", is_critical=True)
        # Kritikus esemény után azonnal commitolva van
        conn = sqlite3.connect(tmp_path / "session.db")
        rows = conn.execute("SELECT battery_voltage_V FROM samples").fetchall()
        conn.close()
        assert len(rows) >= 1


class TestCheckpoint:
    def test_checkpoint_file_created_on_write(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.write_checkpoint({"state": "CHARGE_CC", "elapsed_s": 10.0})
        assert (tmp_path / "checkpoint.json").exists()

    def test_checkpoint_contains_state(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.write_checkpoint({"state": "TAPER_HOLD", "taper_timer_s": 120.0})
        data = json.loads((tmp_path / "checkpoint.json").read_text())
        assert data["state"] == "TAPER_HOLD"
        assert abs(data["taper_timer_s"] - 120.0) < 0.001

    def test_checkpoint_overwritten_on_next_write(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.write_checkpoint({"state": "CHARGE_CC"})
        logger.write_checkpoint({"state": "CHARGE_DONE"})
        data = json.loads((tmp_path / "checkpoint.json").read_text())
        assert data["state"] == "CHARGE_DONE"


class TestEventLog:
    def test_event_log_file_created(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_event("TAPER_ENTERED", "Taper feltétel teljesül")
        logger.close()
        assert (tmp_path / "events.csv").exists()

    def test_event_logged_with_code_and_message(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_event("BATTERY_OVERVOLTAGE", "U=14.56V > 14.55V")
        logger.close()
        rows = list(csv.DictReader((tmp_path / "events.csv").open()))
        assert any(r["event_code"] == "BATTERY_OVERVOLTAGE" for r in rows)


class TestCriticalFlush:
    def test_emergency_stop_is_critical_event(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_sample(make_sample())
        logger.log_event("EMERGENCY_STOP", "DMM_FEEDBACK_LOST", is_critical=True)
        # Flush megtörtént — CSV olvasható zárt fájlként is
        logger.close()
        rows = list(csv.DictReader((tmp_path / "samples.csv").open()))
        assert len(rows) == 1

    def test_close_flushes_remaining_samples(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig(
            sqlite_commit_interval_s=9999.0
        ))
        for i in range(3):
            logger.log_sample(make_sample(elapsed_s=float(i)))
        logger.close()
        conn = sqlite3.connect(tmp_path / "session.db")
        count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        conn.close()
        assert count == 3
