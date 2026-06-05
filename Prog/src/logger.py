"""
Logger — CSV + SQLite + checkpoint.json + events.csv.
[R1] isolation_state oszlop; relay_state NINCS.
[N5] Kritikus esemény → azonnali flush + commit.
"""
from __future__ import annotations
import csv
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# ------------------------------------------------------------------ #
# [R1] Kötelező: isolation_state. relay_state TILTOTT.               #
# ------------------------------------------------------------------ #
CSV_COLUMNS: list[str] = [
    "timestamp_iso", "elapsed_s", "test_name", "step_name", "state",
    "battery_voltage_V", "charge_current_A", "discharge_current_A", "signed_current_A",
    "battery_temperature_C", "ambient_temperature_C",
    "psu_set_voltage_V", "psu_set_current_A", "psu_readback_voltage_V", "psu_readback_current_A",
    "psu_output_commanded_on", "psu_mode",
    "load_set_current_A", "load_readback_voltage_V", "load_readback_current_A", "load_input_commanded_on",
    "isolation_state",
    "accumulated_charge_Ah", "accumulated_discharge_Ah",
    "accumulated_charge_Wh", "accumulated_discharge_Wh",
    "integration_current_source",
    "u_drop_V", "regulation_error_V", "taper_timer_s",
    "diode_power_W",
    "dmm_voltage_valid", "dmm_temperature_valid",
    "psu_readback_valid", "load_readback_valid",
    "sample_valid", "integration_valid", "safety_valid",
    "capacity_result_quality",
    "fault_flags", "warning_flags",
    "event_code", "event_message",
]

_CRITICAL_EVENTS = frozenset({
    "EMERGENCY_STOP",
    "DMM_FEEDBACK_LOST",
    "BATTERY_OVERVOLTAGE",
    "CONCURRENT_PSU_LOAD_ON",
    "LOAD_POWER_LIMIT",
    "PSU_COMM_LOST",
    "MAX_CHARGE_TIME_REACHED",
    "MAX_CHARGE_AH_REACHED",
    "TEMPERATURE_MONITOR_LOST_CRITICAL",
    "INTEGRATION_FALLBACK_TOO_LONG",
})

_SQLITE_CREATE = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {cols}
)
""".format(cols=",\n    ".join(f"{c} TEXT" for c in CSV_COLUMNS))

_SQLITE_INSERT = "INSERT INTO samples ({cols}) VALUES ({vals})".format(
    cols=", ".join(CSV_COLUMNS),
    vals=", ".join("?" for _ in CSV_COLUMNS),
)


@dataclass
class LogConfig:
    sqlite_commit_interval_s: float = 10.0
    csv_flush_interval_s: float = 5.0
    checkpoint_period_s: float = 10.0


class Logger:
    def __init__(self, session_dir: Path, config: LogConfig):
        self._dir = Path(session_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._config = config
        self._last_commit_wall_t: float = time.monotonic()
        self._pending_rows: list[tuple] = []
        self._closed: bool = False

        # CSV
        self._csv_path = self._dir / "samples.csv"
        self._csv_file = self._csv_path.open("w", newline="", encoding="utf-8")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=CSV_COLUMNS)
        self._csv_writer.writeheader()
        self._csv_file.flush()

        # SQLite
        self._sqlite_path = self._dir / "session.db"
        self._conn = sqlite3.connect(str(self._sqlite_path))
        self._conn.execute(_SQLITE_CREATE)
        self._conn.commit()

        # Events
        self._event_path = self._dir / "events.csv"
        self._event_file = self._event_path.open("w", newline="", encoding="utf-8")
        self._event_writer = csv.DictWriter(
            self._event_file,
            fieldnames=["timestamp_iso", "event_code", "event_message", "is_critical"],
        )
        self._event_writer.writeheader()
        self._event_file.flush()

        # Checkpoint
        self._checkpoint_path = self._dir / "checkpoint.json"

    def log_sample(self, sample: dict) -> None:
        row = {col: sample.get(col, "") for col in CSV_COLUMNS}
        self._csv_writer.writerow(row)
        self._pending_rows.append(tuple(str(row[c]) if row[c] is not None else "" for c in CSV_COLUMNS))

        if time.monotonic() - self._last_commit_wall_t >= self._config.sqlite_commit_interval_s:
            self._commit_sqlite()

    def log_event(self, event_code: str, message: str, is_critical: bool = False) -> None:
        self._event_writer.writerow({
            "timestamp_iso": datetime.now().isoformat(timespec="milliseconds"),
            "event_code": event_code,
            "event_message": message,
            "is_critical": is_critical,
        })
        if is_critical or event_code in _CRITICAL_EVENTS:
            self.flush_all()

    def write_checkpoint(self, state: dict) -> None:
        tmp = self._checkpoint_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._checkpoint_path)

    def flush_all(self) -> None:
        self._csv_file.flush()
        self._commit_sqlite()
        self._event_file.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.flush_all()
        self._csv_file.close()
        self._event_file.close()
        self._conn.close()

    def _commit_sqlite(self) -> None:
        if self._pending_rows:
            self._conn.executemany(_SQLITE_INSERT, self._pending_rows)
            self._pending_rows.clear()
        self._conn.commit()
        self._last_commit_wall_t = time.monotonic()
