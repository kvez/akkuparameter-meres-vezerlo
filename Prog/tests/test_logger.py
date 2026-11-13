"""
Logger unit tesztek — valós fájlrendszer, tmp_path fixture.
"""
import csv
import json
import sqlite3
import threading
import time

import pytest

from Prog.src.logger import CSV_COLUMNS, IEC_DC_RESISTANCE_COLUMNS, LogConfig, Logger


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

    def test_rb_fields_saved_to_csv(self, tmp_path):
        """[H-01] Rb mérési adatok megjelennek a samples.csv-ben."""
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        s = make_sample(rb_1s_mohm=12.5, rb_10s_mohm=11.8,
                        rb_30s_mohm=11.2, rb_delta_v=0.5)
        logger.log_sample(s)
        logger.close()
        import csv as _csv
        with open(tmp_path / "samples.csv") as f:
            row = next(_csv.DictReader(f))
        assert row["rb_1s_mohm"] == "12.5"
        assert row["rb_10s_mohm"] == "11.8"
        assert row["rb_30s_mohm"] == "11.2"
        assert row["rb_delta_v"] == "0.5"

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

    def test_csv_columns_has_phase_elapsed_s_next_to_state(self):
        """[CF-19, governance-v2 kör 2, DFAM-023] a phase_elapsed_s oszlop
        közvetlenül a state oszlop mellett szerepeljen, ne szórtan."""
        assert "phase_elapsed_s" in CSV_COLUMNS
        state_idx = CSV_COLUMNS.index("state")
        assert CSV_COLUMNS[state_idx + 1] == "phase_elapsed_s"

    def test_csv_columns_has_session_elapsed_s_next_to_elapsed_s(self):
        """R2-14 (2026-09-15, DFAM-022 rokona): a session_elapsed_s oszlop
        közvetlenül az elapsed_s mellett szerepeljen -- ez a mező a
        megszakítás(ok)on átívelő TELJES session-időt hordozza, szemben az
        elapsed_s run()-szegmens-idejével. Ugyanebben a tesztben újra-
        ellenőrizzük a state->phase_elapsed_s szomszédosságot is, hogy egy
        jövőbeli, óvatlan beszúrás azt ne mozdítsa el."""
        assert "session_elapsed_s" in CSV_COLUMNS
        elapsed_idx = CSV_COLUMNS.index("elapsed_s")
        assert CSV_COLUMNS[elapsed_idx + 1] == "session_elapsed_s"
        state_idx = CSV_COLUMNS.index("state")
        assert CSV_COLUMNS[state_idx + 1] == "phase_elapsed_s"


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


class TestDeviceErrorLog:
    """device_errors.csv — SCPI error queue napló."""

    def test_device_errors_csv_created(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.close()
        assert (tmp_path / "device_errors.csv").exists()

    def test_device_error_written(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_device_error("PSU", '+222,"Data out of range"')
        logger.close()
        rows = list(csv.DictReader((tmp_path / "device_errors.csv").open()))
        assert len(rows) == 1
        assert rows[0]["device"] == "PSU"
        assert "222" in rows[0]["error_message"]

    def test_multiple_device_errors(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_device_error("PSU", "err1")
        logger.log_device_error("Load", "err2")
        logger.log_device_error("DMM_V", "err3")
        logger.close()
        rows = list(csv.DictReader((tmp_path / "device_errors.csv").open()))
        assert len(rows) == 3
        devices = [r["device"] for r in rows]
        assert "PSU" in devices and "Load" in devices and "DMM_V" in devices

    def test_device_error_has_timestamp(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_device_error("DMM_T", "test error")
        logger.close()
        rows = list(csv.DictReader((tmp_path / "device_errors.csv").open()))
        assert rows[0]["timestamp_iso"] != ""


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

    def test_checkpoint_fsyncs_before_atomic_rename(self, tmp_path, monkeypatch):
        """[Kör 21 F15, SES21-04] `os.replace()` csak a LÁTHATÓSÁGRA
        atomikus, NEM a fizikai tartósságra -- a tmp fájlt flush+fsync-
        elni kell, MIELŐTT a rename megtörténik, különben egy áramkimaradás
        a rename UTÁN is elavult/csonka checkpoint.json-t eredményezhet
        (a FIZIKAI power-loss állítás HIL_PENDING marad, ez a teszt csak
        a kód-oldali hívási sorrendet igazolja, nem a fizikai tartósságot)."""
        import Prog.src.logger as logger_module

        call_order = []
        real_fsync = logger_module.os.fsync
        real_replace = logger_module.os.replace

        def spy_fsync(fd):
            call_order.append("fsync")
            return real_fsync(fd)

        def spy_replace(src, dst):
            call_order.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(logger_module.os, "fsync", spy_fsync)
        monkeypatch.setattr(logger_module.os, "replace", spy_replace)

        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.write_checkpoint({"state": "CHARGE_CC"})

        assert call_order == ["fsync", "replace"], (
            "az fsync-nek a replace ELŐTT kell megtörténnie, különben a "
            "rename lathatosaga megelozheti a tartalom fizikai kiirasat"
        )


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

    def test_event_timestamp_is_utc_aware(self, tmp_path):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P2] a
        timestamp_iso korábban naiv helyi faliórát írt (datetime.now(),
        tzinfo nélkül) — a test_runner.py::_build_sample() (samples.csv)
        UTC-aware konvenciójával ellentétben, ugyanazon session-mappában,
        ugyanazon mezőnév alatt. datetime.fromisoformat() csak akkor ad
        tzinfo-t, ha az explicit szerepel a stringben."""
        from datetime import datetime
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.log_event("TAPER_ENTERED", "Taper feltétel teljesül")
        logger.close()
        rows = list(csv.DictReader((tmp_path / "events.csv").open()))
        parsed = datetime.fromisoformat(rows[0]["timestamp_iso"])
        assert parsed.tzinfo is not None


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


class TestLoggerCommitTiming:
    """V3: Logger SQLite commit wall-clock alapú, nem kumulatív elapsed_s."""

    def test_commit_not_triggered_on_every_sample_after_first(self, tmp_path):
        cfg = LogConfig(sqlite_commit_interval_s=10.0)
        logger = Logger(tmp_path, cfg)

        commit_count = [0]
        original_commit = logger._commit_sqlite
        def counting_commit():
            commit_count[0] += 1
            original_commit()
        logger._commit_sqlite = counting_commit

        for i in range(5):
            sample = {col: None for col in CSV_COLUMNS}
            sample["elapsed_s"] = float((i + 1) * 3600)  # 1h, 2h, ...
            logger.log_sample(sample)

        # Régi kód: elapsed_s kumulatív → minden mintánál commit (5 db)
        # Új kód: wall-clock alapú → 5 gyors hívás << 10s → 0 commit
        assert commit_count[0] <= 1, f"Túl sok commit: {commit_count[0]}"
        logger.close()


class TestAtomicCheckpoint:
    """K4: checkpoint.json write atomi — temp fájl + os.replace."""

    def test_checkpoint_valid_json_after_write(self, tmp_path):
        logger = Logger(tmp_path, LogConfig())
        state = {"status": "RUNNING", "step": "charge", "charge_ah": 1.23}
        logger.write_checkpoint(state)

        content = (tmp_path / "checkpoint.json").read_text(encoding="utf-8")
        parsed = json.loads(content)
        assert parsed["status"] == "RUNNING"
        assert abs(parsed["charge_ah"] - 1.23) < 1e-9
        logger.close()

    def test_no_tmp_file_left_after_write(self, tmp_path):
        logger = Logger(tmp_path, LogConfig())
        logger.write_checkpoint({"status": "TEST"})

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Maradt temp fájl: {tmp_files}"


class TestOcvSocColumns:
    """SUP01-DATA-001/TERM-003: R_pulse_actual_t*_mOhm mezőnevek, nem "Rb";
    method_origin/synchronization_quality/capacity_basis_quality szerepeljen."""

    def _make_ocv_soc_point(self, **overrides) -> dict:
        defaults = {
            "soc_percent": 95.0,
            "grid_mode": "UNIFORM_SOC_STEP",
            "capacity_basis_Ah": 7.0,
            "capacity_basis_quality": "NOMINAL_CAPACITY_BASED_SOC",
            "removed_Ah_total": 0.35,
            "ocv_V": 12.9,
            "R_pulse_actual_t1_mOhm": 15.2,
            "R_pulse_actual_t10_mOhm": 14.1,
            "R_pulse_actual_t30_mOhm": 13.8,
            "t1_actual_s": 1.0,
            "t10_actual_s": 10.0,
            "t30_actual_s": 30.0,
            "pulse_current_set_A": 1.4,
            "pulse_current_measured_mean_A": 1.4,
            "synchronization_quality": "SOFTWARE_TICK_TIMED",
            "method_origin": "PROJECT_ENGINEERING_METHOD",
            "temperature_C": 22.0,
            "relax_duration_s": 7200.0,
            "measurement_quality": "OK",
        }
        defaults.update(overrides)
        return defaults

    def test_ocv_soc_csv_header_uses_r_pulse_naming(self, tmp_path):
        logger = Logger(tmp_path, LogConfig())
        logger.close()
        header = (tmp_path / "ocv_soc_table.csv").read_text().splitlines()[0]
        cols = header.split(",")
        assert "R_pulse_actual_t1_mOhm" in cols
        assert "R_pulse_actual_t10_mOhm" in cols
        assert "R_pulse_actual_t30_mOhm" in cols
        assert "rb_1s_ohm" not in cols

    def test_ocv_soc_csv_includes_method_and_quality_metadata(self, tmp_path):
        logger = Logger(tmp_path, LogConfig())
        logger.close()
        header = (tmp_path / "ocv_soc_table.csv").read_text().splitlines()[0]
        cols = header.split(",")
        for expected in (
            "capacity_basis_Ah", "capacity_basis_quality",
            "synchronization_quality", "method_origin",
            "t1_actual_s", "t10_actual_s", "t30_actual_s",
        ):
            assert expected in cols

    def test_log_ocv_soc_point_writes_row(self, tmp_path):
        logger = Logger(tmp_path, LogConfig())
        logger.log_ocv_soc_point(self._make_ocv_soc_point())
        logger.close()
        rows = list(csv.DictReader((tmp_path / "ocv_soc_table.csv").open()))
        assert len(rows) == 1
        assert rows[0]["soc_percent"] == "95.0"
        assert rows[0]["R_pulse_actual_t1_mOhm"] == "15.2"
        assert rows[0]["capacity_basis_quality"] == "NOMINAL_CAPACITY_BASED_SOC"
        logger.close()

    def test_log_ocv_soc_point_persists_previously_dropped_fields(self, tmp_path):
        """[regresszió, CF-21/MET-R1-19 (batch 4.2/4.6)] a
        t*_time_error_s és a RB Pulse Digitalizálás spec kiegészítő
        mezői (pulse_trigger_mode, measured_baseline_current_A stb.)
        korábban hiányoztak az OCV_SOC_COLUMNS-ból -- a controllerben
        kiszámolt értékek csendben eldobódtak a log_ocv_soc_point()
        `{col: data.get(col, "") for col in OCV_SOC_COLUMNS}` szűrőjében."""
        logger = Logger(tmp_path, LogConfig())
        logger.log_ocv_soc_point(self._make_ocv_soc_point(
            t1_time_error_s=0.141,
            pulse_trigger_mode="HARDWARE_PULSE",
            measured_baseline_current_A=0.05,
            current_trace_incomplete=True,
            fast_transient_cutoff_during_pulse=True,
        ))
        logger.close()
        rows = list(csv.DictReader((tmp_path / "ocv_soc_table.csv").open()))
        assert len(rows) == 1
        assert rows[0]["t1_time_error_s"] == "0.141"
        assert rows[0]["pulse_trigger_mode"] == "HARDWARE_PULSE"
        assert rows[0]["measured_baseline_current_A"] == "0.05"
        assert rows[0]["current_trace_incomplete"] == "True"
        assert rows[0]["fast_transient_cutoff_during_pulse"] == "True"


class TestIecDcResistanceLogging:
    """Governance-v2 kör 2: IEC 60896-21 §6.3 DC belső ellenállás eredmény
    -- saját CSV, az ocv_soc_table.csv mintáját követve, de session-enként
    pontosan egy sorral (nincs sweep/grid)."""

    def _make_result(self, **overrides) -> dict:
        defaults = {
            "Ua_V": 12.6, "Ia_A": 2.48,
            "Ub_V": 12.1, "Ib_A": 12.40,
            "Ri_ohm": 0.0504,
            "Isc_A": 254.4,
            "i10_source_A": 0.62,
            "temperature_C": 22.0,
            "method_origin": "IEC_60896-21_6.3",
            "quality": "OK",
        }
        defaults.update(overrides)
        return defaults

    def test_csv_header_matches_columns(self, tmp_path):
        logger = Logger(tmp_path, LogConfig())
        logger.close()
        header = (tmp_path / "iec_dc_resistance_result.csv").read_text().splitlines()[0]
        assert header.split(",") == IEC_DC_RESISTANCE_COLUMNS

    def test_log_iec_dc_resistance_result_writes_row(self, tmp_path):
        logger = Logger(tmp_path, LogConfig())
        logger.log_iec_dc_resistance_result(self._make_result())
        logger.close()
        rows = list(csv.DictReader((tmp_path / "iec_dc_resistance_result.csv").open()))
        assert len(rows) == 1
        assert rows[0]["Ua_V"] == "12.6"
        assert rows[0]["Ri_ohm"] == "0.0504"
        assert rows[0]["method_origin"] == "IEC_60896-21_6.3"

    def test_missing_field_is_blank_not_dropped_row(self, tmp_path):
        """A {col: data.get(col, "") for col in IEC_DC_RESISTANCE_COLUMNS}
        szűrő ne dobjon KeyErrort, ha egy mező hiányzik a result dict-ből
        -- üresen kell maradnia, nem szabad a sort eldobni."""
        logger = Logger(tmp_path, LogConfig())
        result = self._make_result()
        del result["quality"]
        logger.log_iec_dc_resistance_result(result)
        logger.close()
        rows = list(csv.DictReader((tmp_path / "iec_dc_resistance_result.csv").open()))
        assert len(rows) == 1
        assert rows[0]["quality"] == ""


class TestBqDiagnosticsLogging:
    """WP-AK-07: log_bq_diagnostics() — közös session logging, minden BQ
    mezőhöz timestamp/valid/error (l. Prog.src.bq_diagnostics.BqFieldReading),
    a value SOHA nem "javítható" 0-ra érvénytelen mezőnél."""

    def test_header_has_value_valid_error_triplet_per_field(self, tmp_path):
        logger = Logger(tmp_path, LogConfig())
        logger.close()
        header = (tmp_path / "bq_diagnostics.csv").read_text().splitlines()[0]
        cols = header.split(",")
        assert "timestamp_iso" in cols
        for name in (
            "voltage_mV", "qmax", "update_status", "ra_table_mohm",
            # Block 5 (2026-08-25, SUP03-LEARN-002): korábban a WP-AK-10
            # driver/snapshot-mezők (cycle_count/dod0/avg_i_last_run_mA/
            # avg_p_last_run) hiányoztak a CSV-oszloplistából, plusz az ÚJ
            # dekódolt bitmezők (ocvtaken/vok/qen/rup_dis).
            "cycle_count", "dod0", "avg_i_last_run_mA", "avg_p_last_run",
            "ocvtaken", "vok", "qen", "rup_dis",
        ):
            assert name in cols
            assert f"{name}_valid" in cols
            assert f"{name}_error" in cols
            # Kör 20 Batch E8 (METRO-R20-2, DFAM-021 8. előfordulása): a
            # negyedik, per-mező oszlop -- l. test_field_age_s_reflects_
            # sequential_read_timing_not_row_write_time lejjebb a
            # diszkrimináló tartalmi tesztért.
            assert f"{name}_age_s" in cols

    def test_field_age_s_reflects_sequential_read_timing_not_row_write_time(self, tmp_path, monkeypatch):
        """METRO-R20-2: a `<name>_age_s` oszlopnak a mező TÉNYLEGES, a sor
        közös `timestamp_iso`-jától ELTÉRŐ olvasási pillanatát kell
        tükröznie -- ez az, amit a korábbi (most javított) komment
        "bármikor levezethető"-nek állított a `timestamp_iso`-ból, holott
        NEM: a `timestamp_iso` egyetlen, sor-szintű érték, nem tudja
        visszaadni, hogy a `qmax` mezőt 5 másodperccel a `voltage_mV`
        előtt olvasták."""
        from Prog.src.bq_diagnostics import BqDiagnosticsSnapshot, BqFieldReading

        def _reading(ts: float) -> BqFieldReading:
            return BqFieldReading(value=1, valid=True, error=None, timestamp_monotonic=ts)

        # A `qmax`-ot 5s-mal a tobbi mezo ELOTT "olvastuk" (kisebb
        # timestamp_monotonic = korabbi olvasas = a sor irasakor NAGYOBB
        # age_s-t kell kapnia).
        t_other = 100.0
        t_qmax = t_other - 5.0
        snapshot = BqDiagnosticsSnapshot(
            device_type=_reading(t_other), fw_version=_reading(t_other),
            hw_version=_reading(t_other), chem_id=_reading(t_other),
            control_status=_reading(t_other), voltage_mV=_reading(t_other),
            avg_current_mA=_reading(t_other), temperature_C=_reading(t_other),
            soc_percent=_reading(t_other), remaining_capacity_mAh=_reading(t_other),
            full_charge_capacity_mAh=_reading(t_other), flags=_reading(t_other),
            qmax=_reading(t_qmax), update_status=_reading(t_other),
            ra_table_mohm=_reading(t_other), cycle_count=_reading(t_other),
            dod0=_reading(t_other), avg_i_last_run_mA=_reading(t_other),
            avg_p_last_run=_reading(t_other), ocvtaken=_reading(t_other),
            vok=_reading(t_other), qen=_reading(t_other), rup_dis=_reading(t_other),
        )

        monkeypatch.setattr(time, "monotonic", lambda: 100.0)

        logger = Logger(tmp_path, LogConfig())
        logger.log_bq_diagnostics(snapshot)
        logger.close()

        rows = list(csv.DictReader((tmp_path / "bq_diagnostics.csv").open()))
        assert float(rows[0]["qmax_age_s"]) == pytest.approx(5.0, abs=1e-6)
        assert float(rows[0]["voltage_mV_age_s"]) == pytest.approx(0.0, abs=1e-6)
        # A régi (javított) komment pontosan azt állította, hogy ez a
        # különbség NEM létezik / nem kell tárolni -- ez a teszt bukna a
        # fix előtti kódon, mert az `_age_s` oszlop maga sem létezett.
        assert rows[0]["qmax_age_s"] != rows[0]["voltage_mV_age_s"]

    def test_log_bq_diagnostics_writes_valid_snapshot_row(self, tmp_path):
        from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
        from Prog.src.bq_diagnostics import poll_bq_diagnostics
        from Prog.tests.mock_drivers.mock_bq34z110_transport import (
            MockBq34z110Transport,
        )

        transport = MockBq34z110Transport()
        driver = Bq34z110ReadOnlyDriver(transport=transport)
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        snapshot = poll_bq_diagnostics(driver)

        logger = Logger(tmp_path, LogConfig())
        logger.log_bq_diagnostics(snapshot)
        logger.close()

        rows = list(csv.DictReader((tmp_path / "bq_diagnostics.csv").open()))
        assert len(rows) == 1
        assert rows[0]["voltage_mV"] == "12500"
        assert rows[0]["voltage_mV_valid"] == "True"
        assert rows[0]["voltage_mV_error"] == ""

    def test_log_bq_diagnostics_invalid_field_is_never_silently_zero(self, tmp_path):
        """WP-AK-07 kapu: egy malformed/hiányzó DF blokk (üres MockBq34z110Transport)
        miatt invalid qmax/update_status mezőnek a CSV-ben SEM szabad "0"-nak
        megjelennie — üres value + valid=False + kitöltött error oszlop kell."""
        from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
        from Prog.src.bq_diagnostics import poll_bq_diagnostics
        from Prog.tests.mock_drivers.mock_bq34z110_transport import (
            MockBq34z110Transport,
        )

        transport = MockBq34z110Transport()
        driver = Bq34z110ReadOnlyDriver(transport=transport)
        snapshot = poll_bq_diagnostics(driver)  # DF blokk sosem állítva be -> malformed

        logger = Logger(tmp_path, LogConfig())
        logger.log_bq_diagnostics(snapshot)
        logger.close()

        rows = list(csv.DictReader((tmp_path / "bq_diagnostics.csv").open()))
        assert rows[0]["qmax"] == ""
        assert rows[0]["qmax"] != "0"
        assert rows[0]["qmax_valid"] == "False"
        assert rows[0]["qmax_error"] != ""
        # [Független review I-1 lelet, javítva] ra_table_mohm-nak IS
        # valid=False-nak kell lennie ugyanerre a hiányzó blokkra —
        # korábban itt csendesen "True" + üres value jelent volna meg.
        assert rows[0]["ra_table_mohm"] == ""
        assert rows[0]["ra_table_mohm_valid"] == "False"
        assert rows[0]["ra_table_mohm_error"] != ""

    def test_ra_table_list_serialized_semicolon_joined(self, tmp_path):
        from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
        from Prog.src.bq_diagnostics import poll_bq_diagnostics
        from Prog.tests.mock_drivers.mock_bq34z110_transport import (
            MockBq34z110Transport,
        )

        transport = MockBq34z110Transport()
        driver = Bq34z110ReadOnlyDriver(transport=transport)
        ra_block = bytes([0xFF, 0x55]) + b"".join(
            v.to_bytes(2, "big", signed=True) for v in range(1, 16)
        )
        transport.set_block(driver.DF_SC_RA_TABLE_CELL0, driver.DF_BLOCK_RA_TABLE, ra_block)
        snapshot = poll_bq_diagnostics(driver)

        logger = Logger(tmp_path, LogConfig())
        logger.log_bq_diagnostics(snapshot)
        logger.close()

        rows = list(csv.DictReader((tmp_path / "bq_diagnostics.csv").open()))
        assert rows[0]["ra_table_mohm"] == ";".join(str(float(v)) for v in range(1, 16))

    def test_wp_ak_10_and_decoded_bit_fields_written(self, tmp_path):
        """Block 5 (2026-08-25, SUP03-LEARN-002): a korábban a CSV-
        oszloplistából lemaradt WP-AK-10 mezők (cycle_count/dod0/
        avg_i_last_run_mA/avg_p_last_run) és az új dekódolt bitmezők
        (ocvtaken/vok/qen/rup_dis) ténylegesen kiíródnak."""
        from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
        from Prog.src.bq_diagnostics import poll_bq_diagnostics
        from Prog.tests.mock_drivers.mock_bq34z110_transport import (
            MockBq34z110Transport,
        )

        transport = MockBq34z110Transport()
        driver = Bq34z110ReadOnlyDriver(transport=transport)
        transport.set_register(driver.STD_CMD_FLAGS, 0x0080)  # OCVTAKEN
        transport.set_control_register(driver.MAC_CONTROL_STATUS, 0x0003)  # QEN+VOK
        transport.set_register(driver.STD_CMD_CYCLE_COUNT, 12)
        transport.set_register(driver.STD_CMD_DOD0, 55)
        snapshot = poll_bq_diagnostics(driver)

        logger = Logger(tmp_path, LogConfig())
        logger.log_bq_diagnostics(snapshot)
        logger.close()

        rows = list(csv.DictReader((tmp_path / "bq_diagnostics.csv").open()))
        assert rows[0]["cycle_count"] == "12"
        assert rows[0]["dod0"] == "55"
        assert rows[0]["ocvtaken"] == "True"
        assert rows[0]["ocvtaken_valid"] == "True"
        assert rows[0]["qen"] == "True"
        assert rows[0]["vok"] == "True"
        assert rows[0]["rup_dis"] == "False"

    def test_log_bq_diagnostics_thread_safe_against_worker_flush(self, tmp_path):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), GUI/UX/
        Állapot P2 lelet #1] A __init__ docstring-kommentje ("egyetlen szál
        ír egyszerre, biztonságos") IGAZ samples.csv/session.db-re, de HAMIS
        volt bq_diagnostics.csv-re: a worker-szál (TestRunner tick-ciklusa)
        feltétel nélkül flush_all()-t hív ~2s-enként, a GUI-szál pedig a
        session gauge-lekérdezéséből log_bq_diagnostics()-t hív, UGYANARRA a
        self._bq_diag_writer/_bq_diag_file objektumra — lock nélkül ez
        sorkorrupciót vagy close()-writerow() versenyhelyzetet okozhatott
        egy hosszú TRADITIONAL_LEARNING/GOLDEN_GG sessionben.

        A puszta "hívogassuk versenyezve, nem szabad kivételt dobnia" teszt
        a CPython GIL miatt megbízhatatlan (a valós fájlkorrupció nem
        reprodukálható determinisztikusan, a szűk writerow()/flush() hívások
        közötti interleaving-ablak túl rövid) — ezért a self._bq_diag_writer.
        writerow és self._bq_diag_file.flush hívásait mesterséges
        késleltetéssel és egy megosztott számlálóval instrumentáljuk: a
        számláló sosem léphet 1 fölé, ha a threading.Lock() ténylegesen
        kölcsönösen kizárja a két szálat. A fix előtt (lock nélkül) ez az
        instrumentált mérés megbízhatóan 2-t mutatna."""
        from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
        from Prog.src.bq_diagnostics import poll_bq_diagnostics
        from Prog.tests.mock_drivers.mock_bq34z110_transport import (
            MockBq34z110Transport,
        )

        transport = MockBq34z110Transport()
        driver = Bq34z110ReadOnlyDriver(transport=transport)
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        snapshot = poll_bq_diagnostics(driver)

        logger = Logger(tmp_path, LogConfig())
        errors: list[Exception] = []
        stop = threading.Event()

        counter_lock = threading.Lock()
        state = {"concurrent": 0, "max_concurrent": 0}

        def _instrument(bound_method):
            def wrapper(*args, **kwargs):
                with counter_lock:
                    state["concurrent"] += 1
                    state["max_concurrent"] = max(
                        state["max_concurrent"], state["concurrent"]
                    )
                try:
                    time.sleep(0.002)
                    return bound_method(*args, **kwargs)
                finally:
                    with counter_lock:
                        state["concurrent"] -= 1
            return wrapper

        logger._bq_diag_writer.writerow = _instrument(logger._bq_diag_writer.writerow)
        logger._bq_diag_file.flush = _instrument(logger._bq_diag_file.flush)

        def gui_thread_writer():
            try:
                for _ in range(50):
                    logger.log_bq_diagnostics(snapshot)
            except Exception as exc:  # noqa: BLE001 - szándékosan széles: szál-kivétel átadása a main threadnek
                errors.append(exc)
            finally:
                stop.set()

        def worker_thread_flusher():
            try:
                while not stop.is_set():
                    logger.flush_all()
            except Exception as exc:  # noqa: BLE001 - szándékosan széles: szál-kivétel átadása a main threadnek
                errors.append(exc)

        t_gui = threading.Thread(target=gui_thread_writer)
        t_worker = threading.Thread(target=worker_thread_flusher)
        t_worker.start()
        t_gui.start()
        t_gui.join(timeout=30)
        t_worker.join(timeout=30)
        logger.close()

        assert errors == []
        assert state["max_concurrent"] == 1
        rows = list(csv.DictReader((tmp_path / "bq_diagnostics.csv").open()))
        assert len(rows) == 50


# ------------------------------------------------------------------ #
# Governance-v2 review-kör 1 (MAX verifier V-6, CF-21/MET-R1-19        #
# utóellenőrzés, batch 4.6): strukturális őr a DFAM-012 rokon          #
# mintája ellen -- a BqDiagnosticsSnapshot mezői és a log_bq_          #
# diagnostics()-ot vezérlő _BQ_DIAG_FIELDS lista szét tud csúszni,     #
# ugyanúgy, ahogy az OCV_SOC_COLUMNS az on_soc_point dict-hez képest   #
# (CF-21). Ma NINCS élő eltérés (a verifier ezt önállóan igazolta),    #
# ez a teszt a JÖVŐBELI recidívát akadályozza meg.                    #
# ------------------------------------------------------------------ #

class TestBqDiagFieldsMatchSnapshot:
    def test_bq_diag_fields_match_snapshot_dataclass(self):
        import dataclasses

        from Prog.src.bq_diagnostics import BqDiagnosticsSnapshot
        from Prog.src.logger import _BQ_DIAG_FIELDS

        snapshot_fields = {f.name for f in dataclasses.fields(BqDiagnosticsSnapshot)}
        registry_fields = set(_BQ_DIAG_FIELDS)
        missing_from_registry = snapshot_fields - registry_fields
        assert not missing_from_registry, (
            f"BqDiagnosticsSnapshot mezői hiányoznak a _BQ_DIAG_FIELDS-ből, "
            f"csendben kimaradnak a bq_diagnostics.csv-ből: {sorted(missing_from_registry)}"
        )


# ------------------------------------------------------------------ #
# Kör 18, Batch C7, R17-07 (DFAM-032+DFAM-016): close() korábban a    #
# _closed flaget a munka ELŐTT latchelte, és a 7 erőforrás egyetlen,  #
# izolálatlan láncban záródott -- egy flush_all()/close() hiba a      #
# maradékot nyitva hagyta, és a már True _closed miatt egy retry      #
# azonnal visszatért. Most: per-erőforrás izoláció, _closed a         #
# finally-ben, kudarcok a close_failures property-n olvashatók.       #
# ------------------------------------------------------------------ #

class TestCloseIsExceptionIsolated:
    def test_second_file_close_failure_does_not_abort_remaining(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())

        class _RaisingFile:
            def close(self):
                raise OSError("disk")

        logger._event_file = _RaisingFile()
        logger.close()

        assert logger._csv_file.closed is True
        assert logger._device_error_file.closed is True
        assert logger._ocv_soc_file.closed is True
        assert logger._iec_dc_resistance_file.closed is True
        assert logger._bq_diag_file.closed is True
        with pytest.raises(sqlite3.ProgrammingError):
            logger._conn.execute("select 1")
        assert any("_event_file" in f for f in logger.close_failures)

    def test_flush_all_failure_does_not_abort_closes(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())

        def _raising_flush_all():
            raise RuntimeError("flush boom")

        logger.flush_all = _raising_flush_all
        logger.close()

        assert logger._csv_file.closed is True
        assert logger._event_file.closed is True
        assert logger._device_error_file.closed is True
        assert logger._ocv_soc_file.closed is True
        assert logger._iec_dc_resistance_file.closed is True
        assert logger._bq_diag_file.closed is True
        with pytest.raises(sqlite3.ProgrammingError):
            logger._conn.execute("select 1")
        assert any("flush_all" in f for f in logger.close_failures)

    def test_close_is_idempotent_after_partial_failure(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())

        class _RaisingFile:
            def close(self):
                raise OSError("disk")

        logger._event_file = _RaisingFile()
        logger.close()
        first_failures = logger.close_failures
        logger.close()  # nem szabad kivételt dobnia, a flag mar True
        assert logger.close_failures == first_failures

    def test_no_failures_is_empty_tuple_on_clean_close(self, tmp_path):
        logger = Logger(session_dir=tmp_path, config=LogConfig())
        logger.close()
        assert logger.close_failures == ()

    def test_base_exception_leaves_closed_false_for_retry(self, tmp_path):
        """MAX verifier F1 (Kor 18, Batch C7 R17-07): egy BaseException
        (pl. KeyboardInterrupt) flush_all() kozben a `finally`-t IS
        lefuttatja -- ha `_closed = True` a `finally`-ben allna, HAMISAN
        "kesz"-nek jelezne egy MEGSZAKITOTT teardownt, es egy kesobbi
        retry azonnal no-op lenne, a 6 fajlt/sqlite-ot nyitva hagyva.
        `_closed` ezert a `try` VEGEN allitodik, nem a `finally`-ben."""
        logger = Logger(session_dir=tmp_path, config=LogConfig())

        def _raising_flush_all():
            raise KeyboardInterrupt()

        logger.flush_all = _raising_flush_all
        with pytest.raises(KeyboardInterrupt):
            logger.close()

        assert logger._closed is False
        assert logger._csv_file.closed is False

        # A retry MEGKISERELHETI ujra a teardownt (nem no-op).
        logger.flush_all = lambda: None
        logger.close()
        assert logger._closed is True
        assert logger._csv_file.closed is True


def test_r16b26b_config_fault_flushes_without_files():
    from unittest.mock import Mock

    logger = Logger.__new__(Logger)
    logger._event_writer = Mock()
    logger.flush_all = Mock()
    logger.log_event("DISCHARGE_CONFIG_INVALID", "GOV-029: config")
    assert logger._event_writer.writerow.call_args.args[0]["event_code"] == "DISCHARGE_CONFIG_INVALID"
    logger.flush_all.assert_called_once_with()


def test_b23b_completion_checkpoint_uses_existing_durable_writer(monkeypatch):
    import io
    import json
    from types import SimpleNamespace

    import Prog.src.logger as module

    calls, contents = [], []
    class MemoryFile(io.StringIO):
        def fileno(self):
            return 123
        def close(self):
            contents.append(self.getvalue())
            super().close()
    tmp = SimpleNamespace(open=lambda *a, **k: MemoryFile())
    target = SimpleNamespace(with_suffix=lambda suffix: tmp)
    logger = object.__new__(module.Logger)
    logger._checkpoint_path = target
    monkeypatch.setattr(module.os, "fsync", lambda fd: calls.append(("fsync", fd)))
    monkeypatch.setattr(module.os, "replace", lambda a, b: calls.append(("replace", a is tmp, b is target)))
    record = {
        "safe_boundary": False,
        "tlc_completion": {
            "learning_evidence": {"result_summary": {"result": "BQ_LEARNING_VERIFIED"}},
            "final_recharge": {"status": "RUNNING", "completed_at_utc": None},
        },
    }
    logger.write_checkpoint(record)
    assert json.loads(contents[0]) == record
    assert calls == [("fsync", 123), ("replace", True, True)]
