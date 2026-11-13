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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ------------------------------------------------------------------ #
# [R1] Kötelező: isolation_state. relay_state TILTOTT.               #
# ------------------------------------------------------------------ #
CSV_COLUMNS: list[str] = [
    "timestamp_iso", "elapsed_s",
    # R2-14 (2026-09-15, DFAM-022 rokona): az "elapsed_s" SZÁNDÉKOSAN a
    # run()-SZEGMENS kezdetétől számol (test_runner.py, l. self._start_time
    # kommentje) -- egy checkpoint-folytatás vagy cross-process resume UTÁN
    # tehát 0-ról indul újra. Ez a mező a TELJES, megszakítás(ok)on átívelő
    # session-időt hordozza, UGYANABBÓL az origóból (TestRunner._session_
    # start_time), mint a TestResult.total_session_duration_s -- annak
    # eddig KIZÁRÓLAG a report.json volt a fogyasztója, élő (LivePanel
    # "Session idő") NEM volt.
    "session_elapsed_s",
    "test_name", "step_name", "state",
    # CF-19 (governance-v2 kör 2, DFAM-023): mennyi ideje van a controller a
    # "state" oszlopban éppen rögzített belső állapotban -- csak az
    # összetett (OCV_SOC/CHEMID/TRADITIONAL_LEARNING/GOLDEN_GG) módoknál
    # töltött, l. test_runner.py::_build_sample() kommentje.
    "phase_elapsed_s",
    "battery_voltage_V", "charge_current_A", "discharge_current_A", "signed_current_A",
    "battery_temperature_C",
    # M4-07 (review-kor-4, DFAM-021 gyenge változata + DFAM-026): ez a mező
    # MINDEN production úton None marad -- nincs sourced ambient-hőmérséklet
    # DMM/szenzor bekötve ebben a projektben (l. charge_controller.py:81-87
    # dokumentált, TUDATOS hardverkorlát; a CHG-004 BATTERY_TEMPERATURE_
    # RISE_OVER_AMBIENT kapu emiatt ma nem kényszeríthető ki). NEM mért
    # adat -- ugyanolyan placeholder, mint a lenti sample_valid/
    # integration_valid/safety_valid csoport, csak a mért battery_
    # temperature_C mellett maradt (sematikus rokonság), nem oda mozgatva
    # (pozíció-alapú fogyasztók miatt, l. fenti komment ugyanerről az elvről).
    "ambient_temperature_C",
    "psu_set_voltage_V", "psu_set_current_A", "psu_readback_voltage_V", "psu_readback_current_A",
    "psu_output_commanded_on", "psu_mode",
    "load_set_current_A", "load_readback_voltage_V", "load_readback_current_A", "load_input_commanded_on",
    "isolation_state",
    "accumulated_charge_Ah", "accumulated_discharge_Ah",
    "accumulated_charge_Wh", "accumulated_discharge_Wh",
    "integration_current_source",
    # Teljes kódbázis review 3. kör (2026-08-29), Szoftver/Architektúra P2
    # lelet: az Integrator.extreme_dt_s_count/max_observed_dt_s (1. kör
    # javítás, l. integrator.py) korábban frissült, de sehol nem került
    # kiolvasásra — a "láthatóvá tett" kiugró dt_s funkcionálisan
    # továbbra is csendben maradt (CLAUDE.md §12 "Első és extrém dt_s
    # kapjon helyes kezelést vagy quality flaget").
    "extreme_dt_s_count", "max_observed_dt_s",
    "u_drop_V",
    # Review-kör 4 (2026-09-09), Architektúra F7, DFAM-026: ez a mező SOHA
    # nem kap értéket _build_sample()-ben (nincs producer) -- ugyanolyan
    # placeholder, mint a lenti sample_valid/integration_valid/safety_valid
    # csoport, csak a mért u_drop_V mellett maradt (sematikus rokonság,
    # mindkettő PSU-szabályozási minőség), nem oda mozgatva (pozíció-alapú
    # fogyasztók miatt, l. fenti komment ugyanerről az elvről).
    "regulation_error_V",
    "taper_timer_s",
    "diode_power_W",
    "rb_delta_v", "rb_1s_mohm", "rb_10s_mohm", "rb_30s_mohm",
    # Release-audit 2026-08-23 (2.3): tényleges mintavételi idő (nem a
    # névleges 1s/10s/30s cél) + degraded jelző, ha ez a toleranciánál
    # (0.5s) jobban eltér — l. discharge_controller.py.
    "rb_1s_actual_s", "rb_10s_actual_s", "rb_30s_actual_s", "rb_timing_degraded",
    # Governance-v2 review-kör 1 (CF-24/DFAM-001, BAT-RI-001 sértés,
    # batch 4.6): a fenti rb_*_mohm mezőknek korábban nem volt method_
    # origin/címkéje, szemben a helyesen címkézett ocv_soc_table.csv-vel
    # (OCV_SOC_COLUMNS lent) — l. discharge_controller.py::method_origin
    # docstringje.
    "method_origin",
    # MAX verifier P2-1 lelete (CF-24 utóellenőrzés, batch 4.6): a fenti
    # method_origin önmagában nem jelzi, hogy a mérés software-tick-
    # timed (fő runner-tick alapú) vagy instrumentum-időzített — l.
    # discharge_controller.py::synchronization_quality docstringje.
    # Ugyanaz a mezőnév, mint az OCV_SOC_COLUMNS-ban (lent), a
    # PulseMeasurementService/ocv_soc_controller.py-ra vonatkozó
    # dokumentált SUP01-PULSE-005 taxonómia szerint.
    "synchronization_quality",
    "dmm_voltage_valid", "dmm_temperature_valid",
    # Teljes kódbázis review 6. kör (2026-08-29), Metrológia/Adatintegritás
    # P3 lelet: psu_readback_valid/load_readback_valid kiemelve az alábbi
    # placeholder-csoportból — ezeket a release-audit 2026-08-23 (2.4) óta
    # test_runner.py::_build_sample() ténylegesen kitölti (871-872. sor:
    # sample["psu_readback_valid"] = i_psu is not None), tehát NEM
    # placeholder/None mezők többé.
    "psu_readback_valid", "load_readback_valid",
    # Placeholder mezők — jövőbeli bővíthetőségre fenntartva; _build_sample() nem tölti fel (None marad).
    "sample_valid", "integration_valid", "safety_valid",
    "capacity_result_quality",
    "fault_flags", "warning_flags",
    "event_code", "event_message",
]

OCV_SOC_COLUMNS: list[str] = [
    # SUP01-DATA-001/TERM-003: R_pulse_actual_t*_mOhm — NEM "Rb", és mOhm egység;
    # method_origin/synchronization_quality/capacity_basis_quality — l.
    # kiegeszito_szabalyok_01_ocv_soc_pulse_resistance_v1_0_20260723.txt
    "timestamp_iso", "soc_percent", "grid_mode",
    "capacity_basis_Ah", "capacity_basis_quality",
    "removed_Ah_total", "ocv_V", "ocv_semantic_type",
    "R_pulse_actual_t1_mOhm", "R_pulse_actual_t10_mOhm", "R_pulse_actual_t30_mOhm",
    "t1_actual_s", "t10_actual_s", "t30_actual_s",
    # Governance-v2 review-kör 1 (CF-21/MET-R1-19, DFAM-012, batch 4.2/4.6,
    # a CF-22 MAX verifier kör azonosította, l. DECISION_REGISTER.md
    # GOV-012): a 3 t*_time_error_s mező az OcvSocController::
    # on_soc_point dict-jében MINDEN ponton kiszámolódik, de eddig
    # csendben elveszett — a `t1/t10/t30_actual_s` MELLETT a tényleges
    # időzítési hiba (a `_pulse_timing_out_of_tolerance` mögötti nyers
    # adat) sosem került az ocv_soc_table.csv-be, tehát utólag nem volt
    # rekonstruálható, PONTOSAN mekkora volt az eltérés a névleges
    # célidőponttól egy DEGRADED_TIMING_TOLERANCE minősítésű ponton.
    "t1_time_error_s", "t10_time_error_s", "t30_time_error_s",
    "pulse_current_set_A", "pulse_current_measured_mean_A",
    # Review-kör 5, Metrológia M5-03 (P2, DFAM-021 computed_but_never_
    # persisted): a PulseMeasurementService MÁR kiszámolja a tényleges
    # impulzus-terhelési időt, de ez a controller korábban sosem olvasta
    # ki a PulseAcquisitionResult-ból — l. ocv_soc_controller.py
    # azonos hivatkozású kommentje.
    "pulse_duration_set_s", "pulse_duration_actual_s",
    "synchronization_quality", "method_origin",
    "temperature_C", "relax_duration_s",
    "measurement_quality",
    # Governance-v2 review-kör 1 (CF-21/MET-R1-19 folytatás): a RB Pulse
    # Digitalizálás spec kiegészítő mezői (trigger-mód, mért baseline-
    # áram, trace-teljesség, recovery-minták száma) SZINTÉN MINDEN
    # ponton kiszámolódnak (nem csak fast_transient_enabled=True esetén),
    # de ugyanígy csendben elvesztek. Alapértelmezett (INSTRUMENT_TIMED,
    # HARDWARE_PULSE-t nem használó) konfigurációban ezek az oszlopok
    # üresek/alapérték-tartalmúak maradnak. MAX verifier pontosítás: az
    # új mezők a listában NEM a végére, hanem a `measurement_quality`
    # elé, KÖZÉPRE kerültek — a meglévő elemző eszközök EBBŐL a
    # szempontból csak akkor változatlanok, ha FEJLÉC-alapúak
    # (csv.DictWriter/DictReader mezőnév-alapú) — egy szigorúan
    # POZÍCIÓ-alapú (nem fejléc-alapú) külső eszköznél a korábbi
    # `pulse_current_set_A`…`measurement_quality` oszlopok pozíciója
    # eltolódik. A repóban ma nincs pozíció-alapú olvasó, és minden
    # session friss fájlt+fejlécet ír (nincs régi/új fejléc keveredés).
    "pulse_trigger_mode", "measured_baseline_current_A",
    "baseline_current_degraded",
    "current_trace_incomplete", "voltage_trace_incomplete",
    "recovery_sample_count",
    # Governance-v2 review-kör 1 (CF-21/MET-R1-19 folytatás, a CF-22 fix
    # kiegészítéseként): a fast-transient réteg SAJÁT, elkülönített
    # minőségjelzői (l. ocv_soc_controller.py::_determine_point_quality()
    # CF-22 kommentje — ezek a jelzők TUDATOSAN NEM keverednek a fő
    # `measurement_quality` mezőbe) eddig ide sem jutottak el, tehát a
    # CF-22 fix óta EZEK VOLTAK AZ EGYETLEN hely, ahol a fast-transient
    # degradáció egyáltalán megjelenhetett volna — enélkül teljesen
    # nyomtalanul veszett volna. A per-célidőpont dict-értékű mezők
    # (fast_transient_R_mOhm/_actual_s/_time_error_s) NEM kerülnek ide —
    # azok flatten-elése fix célidőpont-oszlopokat igényelne, ami a
    # jelenleg konfigurálható `fast_transient_target_times_s`-szel nem
    # egyértelmű séma-döntés. MAX verifier V-4 pontosítás: ez NEM
    # SOURCE_REQUIRED (nincs gyártói forrás, ami blokkolna) — PROJECT_
    # POLICY jellegű, nyitott tervezési döntés (l. CLAIM_LEDGER.yaml
    # nyitott rekordja), ami előtt már van precedens a fájlban: a
    # `ra_table_mohm` mező `";".join(...)`-jal sorosít egy változó
    # hosszúságú értéksort egyetlen oszlopba (l. lent, `log_bq_
    # diagnostics()`) — egy hasonló, `"0.02:15.2;0.1:14.8"`-szerű
    # sorosítás itt is séma-stabil lenne, de bugfixben új sorosítási
    # formátum bevezetése nem indokolt (CLAUDE.md §10) — külön, dedikált
    # tervezési kör dönt majd róla.
    "fast_transient_synchronization_quality",
    "fast_transient_cutoff_during_pulse", "fast_transient_current_degraded",
    "fast_transient_invalid_resistance",
    "fast_transient_current_trace_incomplete",
    "fast_transient_voltage_trace_incomplete",
    "fast_transient_skipped_low_voltage",
    # Review-kör 2 (2026-09-08), Session/Workflow/Állapotgép S-5 lelet:
    # ha a végfeszültség a racs szerinti köztes cél ELŐTT érkezik,
    # ocv_soc_controller.py a racs-pozíciót egyenesen az utolsó elemre
    # ugratja -- a köztes racspontok kimaradnak, korábban semmilyen
    # jelzés nélkül. A listavégre kerül (nem középre), hogy a meglévő,
    # pozíció-alapú (nem csak fejléc-alapú) eszközök oszlop-sorrendje ne
    # változzon (l. fenti MAX verifier pontosítás indoklása).
    "grid_truncated_by_terminate_voltage", "grid_points_skipped",
    # M4-04 (review-kör-4, DFAM-001): temperature_C sticky-jelzése soronként
    # (l. ocv_soc_controller.py::on_soc_point kommentje). Listavégre kerül,
    # ugyanazon pozíció-stabilitási indokból, mint fent.
    "temperature_valid",
    # 2026-09-12, FAST_TRANSIENT NPLC-javítás: a ms-tartományú réteg
    # időzítés-megvalósíthatósági minősítése. A `*_timing_infeasible` azt
    # jelenti, hogy a műszer SAJÁT visszaolvasott képessége (SAMPle:TIMer?
    # MIN az ideiglenes, alacsony NPLC beállítása UTÁN) szerint a kért
    # mintavételi köz nem teljesíthető — ilyenkor az impulzus el sem indult,
    # és a `fast_transient_*` R-értékek ÜRESEK, nem 0-ák. A
    # `*_nplc_restore_failed` viszont a sor UTÁNI mérésekre is figyelmeztet
    # (a DMM a gyors, hálózati zajelnyomás nélküli integrációs időn ragadt).
    # Listavégre kerül, ugyanazon pozíció-stabilitási indokból, mint fent.
    "fast_transient_timing_infeasible",
    "fast_transient_timing_infeasible_reason",
    "fast_transient_nplc_restore_failed",
    # Kör 17 MAX-effort review, R17-13 (DFAM-021+DFAM-003+DFAM-001): a
    # nem-fast-transient pulse_current_degraded jelző korábban hiányzott
    # innen, holott a fast_transient_current_degraded testvér (fent) már
    # exportálva volt. Listavégre kerül, ugyanazon pozíció-stabilitási
    # indokból, mint a fenti mezők.
    "pulse_current_degraded",
    # Kör 19 Batch D3a (F-1/DFAM-021, 7. előfordulás): a fenti R_pulse_
    # actual_t*_mOhm = dU/dI hányados nyers bemenetei korábban sosem
    # kerültek naplózásra -- a hányados önmagában nem ellenőrizhető/
    # rekonstruálható utólag. `ocv_V` NEM a dU-referencia (más mintavételi
    # időpont, l. ocv_soc_controller.py::_run_impulse_prep() vs.
    # begin_pulse() kommentje), és `pulse_current_measured_mean_A` NEM a
    # dI-nevező (a hányados a legközelebbi EGYETLEN mintából, nem az
    # átlagból számol). Listavégre kerül, ugyanazon pozíció-stabilitási
    # indokból, mint a fenti mezők. [MAX verifier P3-1 lelet] a dU/dI-ből
    # a hányados NEM rekonstruálható MINDEN soron -- egy INVALID_PULSE_
    # RESISTANCE minőségű ponton a dU/dI kitöltődik, de R_pulse_actual_
    # t*_mOhm SZÁNDÉKOSAN None marad (fizikailag lehetetlen/negatív
    # eredmény lenne) -- l. ocv_soc_controller.py::__init__ dU/dI-
    # mezőinek kommentje.
    "dU_t1_V", "dU_t10_V", "dU_t30_V",
    "dI_t1_A", "dI_t10_A", "dI_t30_A",
    "pulse_v_baseline_V",
]

# Governance-v2 kör 2: IEC 60896-21 §6.3 kétpontos DC belső ellenállás mérés
# egyetlen eredmény-sora -- az ocv_soc_table.csv mintáját követi (saját
# fájl, azonnali flush), de itt session-enként PONTOSAN EGY sor íródik
# (nincs SOC-rács/sweep) -- l. iec_dc_resistance_controller.py::on_result.
IEC_DC_RESISTANCE_COLUMNS: list[str] = [
    "timestamp_iso",
    "Ua_V", "Ia_A", "Ub_V", "Ib_A",
    "Ri_ohm", "Isc_A",
    "i10_source_A", "temperature_C", "method_origin", "quality",
    # MAX verifier lelet (governance-v2 kör 2, F1) -- a tényleges, load-on
    # pillanattól mért mintavételi időpontok + a software-tick-timed
    # jelleg őszinte címkézése (a discharge_controller.py rb_timing_
    # degraded/synchronization_quality mintáját követve, l.
    # iec_dc_resistance_controller.py::_run_compute_result() kommentje).
    "t_sample_1_s", "t_sample_2_s", "timing_degraded", "synchronization_quality",
    # MAX verifier lelet (review-kör 2, 45ea824 P2-1, DFAM-021/026): a
    # temperature_C mellé bevezetett temperature_valid mező (l.
    # iec_dc_resistance_controller.py result dict-je) korábban a
    # produkciós export-úton HOLT volt -- a log_iec_dc_resistance_result()
    # whitelistje (lent) nem tartalmazta, tehát a mező csendben eldobódott
    # mielőtt bármely CSV-sor elkészült volna. A lista VÉGÉN, pozíció-
    # alapú fogyasztók miatt (ugyanaz a minta, mint a fenti t_sample_1_s
    # csoport bővítésénél).
    "temperature_valid",
    # M4-06 (review-kör-4, DFAM-021+DFAM-001): a fenti temperature_C/
    # temperature_valid KIZÁRÓLAG az Ua (LOAD_1) mintavételi pillanatára
    # vonatkozik -- a Ub (LOAD_2) saját mintavételi pillanata (rest_
    # duration_s + egy teljes terhelési ciklussal később) korábban semmilyen
    # hőmérséklet-jelzést nem kapott. Listavégre kerül, pozíció-stabilitás
    # miatt.
    "temperature_2_C", "temperature_2_valid",
    "load_windows",
]

# WP-AK-07 (BQ Diagnostics GUI + közös session logging): "Közös timestamp
# domain: reference / BQ, de minden BQ értékhez: timestamp, valid, error,
# age/freshness." — a timestamp_iso oszlop közös (ugyanaz a session), és
# minden mező saját <name>/<name>_valid/<name>_error/<name>_age_s négyest
# kap (a Prog.src.bq_diagnostics.BqFieldReading szerkezetének megfelelően).
# Sorrend megegyezik BqDiagnosticsSnapshot mezősorrendjével.
#
# Kör 20 Batch E8 (METRO-R20-2, 2026-09-19, DFAM-021/8. előfordulás +
# DFAM-043): a fenti bekezdés KORÁBBAN azt állította, hogy "a freshness/age
# a timestamp_iso-ból bármikor levezethető, nem kell külön tárolni" -- ez
# BIZONYÍTHATÓAN HAMIS: a `poll_bq_diagnostics()` 19 egymást követő,
# szekvenciális `_read()` hívást tesz (bq_diagnostics.py:223-247), tehát a
# sor egyes mezői a tényleges hardver-olvasás pillanatában akár tíz(ek)
# másodperccel is eltérhetnek egymástól és a sor `timestamp_iso`
# (ÍRÁSI idő) mezőjétől -- a `timestamp_iso` egyetlen, sor-szintű értéke
# ezt a szórást NEM tudja visszaadni. A `BqFieldReading.timestamp_monotonic`
# mezőt a producer MÁR kiszámítja mezőnként, csak a logger korábban
# eldobta. Ezért minden mező most saját `<name>_age_s`-t is kap (a sor
# közös írási pillanatához, nem `time.monotonic()`-hoz mérve -- l.
# log_bq_diagnostics()).
_BQ_DIAG_FIELDS: list[str] = [
    "device_type", "fw_version", "hw_version", "chem_id", "control_status",
    "voltage_mV", "avg_current_mA", "temperature_C", "soc_percent",
    "remaining_capacity_mAh", "full_charge_capacity_mAh", "flags",
    "qmax", "update_status", "ra_table_mohm",
    # Block 5 (2026-08-25, SUP03-LEARN-002): cycle_count/dod0/avg_i_last_run_mA/
    # avg_p_last_run MÁR benne voltak a BqDiagnosticsSnapshot-ban (WP-AK-10),
    # csak ez a lista maradt le a bővítésükről — a driver/snapshot nem
    # változott, ez tisztán a CSV-oszloplista pótlása. ocvtaken/vok/qen/
    # rup_dis ÚJ, dekódolt bitmezők (l. bq_diagnostics.py poll_bq_diagnostics()) —
    # a SUP03-LEARN-002 kötelező listájának eddig hiányzó tételei, a
    # meglévő decode_flags()/decode_control_status()-ból, mapping dokumentálva.
    "cycle_count", "dod0", "avg_i_last_run_mA", "avg_p_last_run",
    "ocvtaken", "vok", "qen", "rup_dis",
    # R16-energy-04: csak új oszlopok a végén, a régi értékek és pozíciók maradnak.
    "design_energy_scale", "pack_configuration_x10",
]

BQ_DIAGNOSTICS_COLUMNS: list[str] = ["timestamp_iso"] + [
    col
    for name in _BQ_DIAG_FIELDS
    for col in (name, f"{name}_valid", f"{name}_error", f"{name}_age_s")
]

_CRITICAL_EVENTS = frozenset({
    "EMERGENCY_STOP",
    "DMM_FEEDBACK_LOST",
    "BATTERY_OVERVOLTAGE",
    "BATTERY_OVERTEMPERATURE",  # R11-B4
    "DISCHARGE_CONFIG_INVALID",  # Q-46: azonnali kritikus flush
    "CONCURRENT_PSU_LOAD_ON",
    "LOAD_POWER_LIMIT",
    "PSU_COMM_LOST",
    "MAX_CHARGE_TIME_REACHED",
    "MAX_CHARGE_AH_REACHED",
    "TEMPERATURE_MONITOR_LOST_CRITICAL",
    "INTEGRATION_FALLBACK_TOO_LONG",
    "BATTERY_TEMPERATURE_RISE_OVER_AMBIENT",  # CHG-004
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


class Logger:
    def __init__(self, session_dir: Path, config: LogConfig):
        self._dir = Path(session_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._config = config
        self._last_commit_wall_t: float = time.monotonic()
        self._pending_rows: list[tuple] = []
        self._closed: bool = False
        # R17-07 (Kor 17, DFAM-032+DFAM-016): ures tuple = a close() meg
        # nem futott, vagy hibatlanul futott le. L. close() docstringjet.
        self._close_failures: tuple[str, ...] = ()

        # CSV
        self._csv_path = self._dir / "samples.csv"
        self._csv_file = self._csv_path.open("w", newline="", encoding="utf-8")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=CSV_COLUMNS)
        self._csv_writer.writeheader()
        self._csv_file.flush()

        # SQLite
        self._sqlite_path = self._dir / "session.db"
        # check_same_thread=False: a Logger a főszálon jön létre, de a TestRunner
        # QThread-ben írja — egyetlen szál ír egyszerre, így biztonságos.
        self._conn = sqlite3.connect(str(self._sqlite_path), check_same_thread=False)
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

        # Device errors
        self._device_error_path = self._dir / "device_errors.csv"
        self._device_error_file = self._device_error_path.open(
            "w", newline="", encoding="utf-8"
        )
        self._device_error_writer = csv.DictWriter(
            self._device_error_file,
            fieldnames=["timestamp_iso", "device", "error_message"],
        )
        self._device_error_writer.writeheader()
        self._device_error_file.flush()

        # Checkpoint
        self._checkpoint_path = self._dir / "checkpoint.json"

        # OCV-SOC táblázat
        self._ocv_soc_path = self._dir / "ocv_soc_table.csv"
        self._ocv_soc_file = self._ocv_soc_path.open("w", newline="", encoding="utf-8")
        self._ocv_soc_writer = csv.DictWriter(
            self._ocv_soc_file, fieldnames=OCV_SOC_COLUMNS
        )
        self._ocv_soc_writer.writeheader()
        self._ocv_soc_file.flush()

        # Governance-v2 kör 2: IEC DC belső ellenállás eredménytábla.
        self._iec_dc_resistance_path = self._dir / "iec_dc_resistance_result.csv"
        self._iec_dc_resistance_file = self._iec_dc_resistance_path.open(
            "w", newline="", encoding="utf-8"
        )
        self._iec_dc_resistance_writer = csv.DictWriter(
            self._iec_dc_resistance_file, fieldnames=IEC_DC_RESISTANCE_COLUMNS
        )
        self._iec_dc_resistance_writer.writeheader()
        self._iec_dc_resistance_file.flush()

        # WP-AK-07: BQ diagnosztika (közös session logging, "reference"
        # samples.csv-vel megosztott session könyvtár/timestamp domain).
        # Teljes kódbázis review 6. kör (2026-08-29), GUI/UX/Állapot P2
        # lelet #1: a __init__ docstring-kommentje ("egyetlen szál ír
        # egyszerre, biztonságos") IGAZ samples.csv/session.db-re, de HAMIS
        # bq_diagnostics.csv-re — a worker-szál a fő tick-ciklusban
        # feltétel nélkül flush_all()-t hív, a GUI-szál pedig a session
        # gauge-lekérdezéséből log_bq_diagnostics()-t hív, UGYANARRA a
        # self._bq_diag_writer/_bq_diag_file objektumra, lock nélkül, két
        # szálról — sorkorrupció vagy close()-writerow() versenyhelyzet
        # kockázata egy hosszú TRADITIONAL_LEARNING/GOLDEN_GG sessionben.
        self._bq_diag_lock = threading.Lock()
        self._bq_diag_path = self._dir / "bq_diagnostics.csv"
        self._bq_diag_file = self._bq_diag_path.open("w", newline="", encoding="utf-8")
        self._bq_diag_writer = csv.DictWriter(
            self._bq_diag_file, fieldnames=BQ_DIAGNOSTICS_COLUMNS
        )
        self._bq_diag_writer.writeheader()
        self._bq_diag_file.flush()

    def log_sample(self, sample: dict) -> None:
        row = {col: sample.get(col, "") for col in CSV_COLUMNS}
        self._csv_writer.writerow(row)
        self._pending_rows.append(tuple(str(row[c]) if row[c] is not None else "" for c in CSV_COLUMNS))

        if time.monotonic() - self._last_commit_wall_t >= self._config.sqlite_commit_interval_s:
            self._commit_sqlite()

    def log_event(self, event_code: str, message: str, is_critical: bool = False) -> None:
        # Teljes kódbázis review 2. kör (2026-08-28), Szoftver/Architektúra
        # P2 lelet: minden `timestamp_iso` generálás itt korábban naiv,
        # helyi faliórát használt (`datetime.now()`, tzinfo/offszet nélkül),
        # miközben test_runner.py::_build_sample() (samples.csv) UTC-aware
        # időt ír ugyanazon mezőnévvel — ugyanabban a session-mappában, nyári
        # időszámítás idején (CEST, UTC+2) 2 órás eltérést okozva az
        # events.csv/ocv_soc_table.csv/bq_diagnostics.csv/device_errors.csv
        # és a samples.csv korrelálásakor (CLAUDE.md §12).
        self._event_writer.writerow({
            "timestamp_iso": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event_code": event_code,
            "event_message": message,
            "is_critical": is_critical,
        })
        if is_critical or event_code in _CRITICAL_EVENTS:
            self.flush_all()

    def log_ocv_soc_point(self, data: dict) -> None:
        """OCV-SOC lépési eredmény rögzítése ocv_soc_table.csv-be."""
        row = {col: data.get(col, "") for col in OCV_SOC_COLUMNS}
        row["timestamp_iso"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        # impulse_current_A opcionális — ha nincs az adatban, üresen marad
        self._ocv_soc_writer.writerow(row)
        self._ocv_soc_file.flush()

    def log_iec_dc_resistance_result(self, data: dict) -> None:
        """IEC 60896-21 §6.3 DC belső ellenállás egyszeri eredményének
        rögzítése iec_dc_resistance_result.csv-be -- l. IEC_DC_RESISTANCE_
        COLUMNS/log_ocv_soc_point() ugyanazon mintája."""
        row = {col: data.get(col, "") for col in IEC_DC_RESISTANCE_COLUMNS}
        row["timestamp_iso"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._iec_dc_resistance_writer.writerow(row)
        self._iec_dc_resistance_file.flush()

    def log_bq_diagnostics(self, snapshot) -> None:
        """WP-AK-07: egy Prog.src.bq_diagnostics.BqDiagnosticsSnapshot
        naplózása bq_diagnostics.csv-be. Nem importálja a bq_diagnostics
        modult típusannotációként (elkerülve a kör-import kockázatát és
        hogy a Logger ne kényszerítsen egy adott BQ-réteg-implementációt) —
        a snapshot bármely objektum lehet, aminek a mezei
        `Prog.src.bq_diagnostics.BqFieldReading`-szerűek (value/valid/error/
        timestamp_monotonic). A `value` SOHA nem "javított" 0-ra — pontosan
        az van a sorban, amit a BqFieldReading hordoz (érvénytelen mezőnél
        üres string).

        Kör 20 Batch E8 (METRO-R20-2, DFAM-021 8. előfordulása): minden
        mező saját `<name>_age_s`-t is kap -- a `poll_bq_diagnostics()`
        19 szekvenciális `_read()` hívása miatt a mezők tényleges olvasási
        pillanata akár tíz(ek) másodperccel eltérhet egymástól ÉS ettől a
        sor közös `timestamp_iso` írási pillanatától, amit az egyetlen,
        sor-szintű `timestamp_iso` NEM tud visszaadni. A `now` egyetlen
        `time.monotonic()` hívásból származik minden mezőre (a sor egy
        közös referenciaponthoz mérve, nem mezőnként újra lekérdezve)."""
        now = time.monotonic()
        row: dict = {"timestamp_iso": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        for name in _BQ_DIAG_FIELDS:
            reading = getattr(snapshot, name)
            if isinstance(reading.value, list):
                value_str = ";".join(str(v) for v in reading.value)
            else:
                value_str = "" if reading.value is None else str(reading.value)
            row[name] = value_str
            row[f"{name}_valid"] = reading.valid
            row[f"{name}_error"] = reading.error or ""
            row[f"{name}_age_s"] = round(reading.age_s(now), 3)
        with self._bq_diag_lock:
            self._bq_diag_writer.writerow(row)
            self._bq_diag_file.flush()

    def log_device_error(self, device: str, error: str) -> None:
        self._device_error_writer.writerow({
            "timestamp_iso": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "device": device,
            "error_message": error,
        })
        self._device_error_file.flush()

    def write_checkpoint(self, state: dict) -> None:
        tmp = self._checkpoint_path.with_suffix(".json.tmp")
        # Kör 21 F15 (SES21-04, DFAM-021 durability variant): korábban a
        # `tmp.write_text()` után NEM volt flush+fsync az `os.replace()`
        # ELŐTT -- az `os.replace()` csak a LÁTHATÓSÁGRA atomikus, NEM a
        # fizikai tartósságra: egy áramkimaradás a rename UTÁN is
        # 0-hosszú/elavult checkpoint.json-t eredményezhetett volna egy
        # több-napos fizikai session során, mert a `tmp` tartalma még
        # csak az OS oldalpuffer-cache-ében élt, a fizikai adathordozón
        # nem. A FIZIKAI (power-loss) állítás HIL_PENDING marad -- ez a
        # fix kizárólag a kód-oldali garanciát adja (fsync a rename
        # ELŐTT); nincs könyvtár-fsync (Windows-on nem elérhető azonos
        # módon).
        #
        # KORREKCIÓ (MAX verifier, 2026-09-19): az eredeti archívum-sweep
        # ("grep os.replace") NEM az egyetlen ilyen hely -- `Path.replace()`
        # (CPython-ban szó szerint `os.replace(self, target)`) egy MÁSODIK
        # helyen is előfordul: `main_window.py` (a Kör 21 F5/SES21-02
        # `report.json` resume-marker írása, ami ÉPP EZT a mintát vette át
        # 1:1, l. ottani komment) -- most UGYANEZZEL a fsync-javítással
        # kiegészítve.
        with tmp.open("w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._checkpoint_path)

    def flush_all(self) -> None:
        self._csv_file.flush()
        self._commit_sqlite()
        self._event_file.flush()
        self._device_error_file.flush()
        self._ocv_soc_file.flush()
        self._iec_dc_resistance_file.flush()
        with self._bq_diag_lock:
            self._bq_diag_file.flush()

    def close(self) -> None:
        """R17-07 (Kor 17, DFAM-032+DFAM-016): a `_closed` flag KORABBAN a
        munka ELOTT latchelt, es a 7 eroforras EGYETLEN, izolalatlan
        lancban zarodott -- egy `flush_all()` (vagy egy korai `.close()`)
        hibaja a maradek eroforrasokat NYITVA hagyta, es a MAR `True`
        `_closed` miatt minden ujraprobalkozas azonnal visszatert: a
        session adatfajljai soha nem zarodtak. CLAUDE.md 6. szakasz "egy
        OFF-hiba ne akadalyozza a masik megkiserleset" -- ugyanaz az elv
        teardownra alkalmazva.

        Per-eroforras izolacioval MINDEN lezaras mindig megkiserlodik.
        A kudarcok a `close_failures` property-n keresztul olvashatok.

        MAX verifier F1 lelete (Kor 18, Batch C7): a `_closed = True`
        NEM lehet a `finally`-ben -- egy `BaseException` (pl. Ctrl+C
        `flush_all()` kozben) a `finally`-t IS lefuttatja, tehat
        `_closed` HAMISAN "kesz"-nek jelezne egy MEGSZAKITOTT (nem
        vegrehajtott) teardownt, mikozben a `close_failures` ures tuple-
        je is "hibatlanul lezajlott"-nak olvasodna -- pontosan azt a
        felreveszeto allapotot reprodukalva, amit ez a fix maga celzott
        megszuntetni, csak Exception-rol BaseException-re szukitve. A
        `_closed = True` ezert a `try` blokk VEGE, nem a `finally` --
        Exception-nel (a normal eset) igy is mindig lefut, BaseException
        megszakitasnal viszont `_closed` False marad, es egy kesobbi
        retry ujra megkiserelheti a teardownt."""
        if self._closed:
            return
        failures: list[str] = []
        try:
            try:
                self.flush_all()
            except Exception as exc:  # noqa: BLE001 - teardown: minden eroforrast megkiserlunk
                failures.append(f"flush_all: {exc!r}")
            for attr in (
                "_csv_file",
                "_event_file",
                "_device_error_file",
                "_ocv_soc_file",
                "_iec_dc_resistance_file",
            ):
                try:
                    getattr(self, attr).close()
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{attr}: {exc!r}")
            try:
                with self._bq_diag_lock:
                    self._bq_diag_file.close()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"_bq_diag_file: {exc!r}")
            try:
                self._conn.close()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"_conn: {exc!r}")
            self._closed = True
        finally:
            self._close_failures = tuple(failures)

    @property
    def close_failures(self) -> tuple[str, ...]:
        """R17-07 -- l. close() docstringje. A teardown kozben elbukott
        eroforras-lezarasok (es a flush_all()) leirasai."""
        return self._close_failures

    def _commit_sqlite(self) -> None:
        if self._pending_rows:
            self._conn.executemany(_SQLITE_INSERT, self._pending_rows)
            self._pending_rows.clear()
        self._conn.commit()
        self._last_commit_wall_t = time.monotonic()
