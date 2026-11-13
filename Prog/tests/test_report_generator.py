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

    def test_battery_temperature_source_default_is_pt100(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Explicit
        session_meta nélkül a régi, PT100-alapú alapérték marad — a korábbi
        tautologikus teszt (fenti) csak ezt igazolta vissza, nem azt, hogy
        más forrás esetén NEM ezt adja."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["battery_temperature_source"] == "DMM2_surface_PT100"

    def test_battery_temperature_source_reflects_ntc_mode_when_configured(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Ha a
        session_meta explicit NTC-forrást ad át (a GUI-oldali
        temperature_sensor_mode='THERMISTOR_2W_NTC_10K_B3950' leképezéséből),
        a riport NEM adhatja vissza tévesen a PT100-at."""
        rg = ReportGenerator()
        report = rg.generate(
            make_session_meta(battery_temperature_source="DMM2_surface_NTC_10K_B3950")
        )
        assert report["battery_temperature_source"] == "DMM2_surface_NTC_10K_B3950"
        assert "PT100" not in report["battery_temperature_source"]

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

    def test_parent_session_id_defaults_empty(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["parent_session_id"] == ""

    def test_parent_session_id_forwarded(self):
        """WP-AK-02 (REC-003): melyik megszakadt session cross-process
        folytatása ez a session — traceability, ne vesszen el."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            parent_session_id="session_STALE_20260101_000000",
        ))
        assert report["parent_session_id"] == "session_STALE_20260101_000000"


class TestReleaseAudit20260823BqLearningAndGoldenGgFields:
    """Release-audit 2026-08-23 (2.1/2.2): a BQ learning eredmény-
    klasszifikáció (§11.6) és a GOLDEN_GG export-státusz korábban sosem
    jutott el a report.json-ig — session_meta-ból explicit átvéve."""

    def test_bq_learning_fields_default_none(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["bq_learning_result"] is None
        assert report["bq_learning_quality_flags"] is None

    def test_bq_learning_result_forwarded(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            bq_learning_result="BQ_LEARNING_VERIFIED",
            bq_learning_quality_flags=["QEN_NOT_CONFIRMED_AFTER_IT_ENABLE"],
        ))
        assert report["bq_learning_result"] == "BQ_LEARNING_VERIFIED"
        assert report["bq_learning_quality_flags"] == ["QEN_NOT_CONFIRMED_AFTER_IT_ENABLE"]

    def test_golden_gg_export_fields_default_none(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["golden_gg_package_ready_for_ti_submission"] is None
        assert report["golden_gg_export_dir"] is None
        assert report["golden_gg_export_zip_path"] is None

    def test_golden_gg_export_fields_forwarded(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            golden_gg_package_ready_for_ti_submission=True,
            golden_gg_export_dir="/sessions/x/golden_gg_export",
            golden_gg_export_zip_path="/sessions/x/golden_gg_export/package.zip",
        ))
        assert report["golden_gg_package_ready_for_ti_submission"] is True
        assert report["golden_gg_export_dir"] == "/sessions/x/golden_gg_export"
        assert report["golden_gg_export_zip_path"] == "/sessions/x/golden_gg_export/package.zip"

    def test_golden_gg_quality_flags_default_none(self):
        """R2-11a (2026-09-15, 14. writer-kör, DFAM-026 4. előfordulása):
        a golden_gg_quality_flags mezőnek korábban NEM volt whitelist-
        bejegyzése a report_generator.py-ban -- a session_meta-ban jelen
        lévő érték csendben elveszett volna a report.json-ig."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["golden_gg_quality_flags"] is None

    def test_golden_gg_quality_flags_reaches_report_json(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            golden_gg_quality_flags=["BQ_SEAL_RESTORE_FAILED_AT_EXPORT_PACKAGE"],
        ))
        assert report["golden_gg_quality_flags"] == [
            "BQ_SEAL_RESTORE_FAILED_AT_EXPORT_PACKAGE"
        ]

    def test_chemid_export_fields_default_none(self):
        """[regresszió, D9/F-06, 2026-09-13] a chemid_export_dir/
        chemid_export_zip_path mezők korábban EGYÁLTALÁN NEM léteztek a
        ReportGenerator kimenetében -- a main_window.py::_write_report()
        session_meta-jában megjelentek, de a generate() explicit allowlist-
        jéből hiányoztak, ezért sosem jutottak el a tényleges report.json-ig."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["chemid_export_dir"] is None
        assert report["chemid_export_zip_path"] is None

    def test_chemid_export_fields_forwarded(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            chemid_export_dir="/sessions/x/chemid_export",
            chemid_export_zip_path="/sessions/x/chemid_export/gpcchem_submission.zip",
        ))
        assert report["chemid_export_dir"] == "/sessions/x/chemid_export"
        assert (
            report["chemid_export_zip_path"]
            == "/sessions/x/chemid_export/gpcchem_submission.zip"
        )

    def test_rb_current_source_text_ok_by_default(self):
        """Release-audit 2026-08-23 (2.3): korábban statikus, mindig
        ugyanazt állító szöveg ("kis eltérés lehetséges") volt, függetlenül
        a tényleges hibától — most a mért readback-forrást és a valós
        degraded-státuszt tükrözi."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        text = report["MEASUREMENT_LIMITATIONS"]["rb_current_source"]
        assert "LOAD_READBACK" in text
        assert "0.5s tolerancián belül" in text
        assert report["MEASUREMENT_LIMITATIONS"].get("rb_timing_degraded", False) is False

    def test_rb_current_source_text_reflects_timing_degraded(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(rb_timing_degraded=True))
        text = report["MEASUREMENT_LIMITATIONS"]["rb_current_source"]
        assert "FIGYELEM" in text
        assert "rb_timing_degraded" in text

    def test_golden_gg_chemid_relax_dmm_fault_total_zero_by_default(self):
        """[regresszió, Teljes kódbázis review 10. kör (2026-08-31),
        Metrológia/Adatintegritás P2 lelet] a relax-fázisok alatti
        feszültség-DMM-olvasási hibák session-szintű összege korábban
        egyáltalán nem jutott el a fő report.json-ig."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["golden_gg_relax_dmm_fault_total"] == 0
        assert limitations["chemid_relax_dmm_fault_total"] == 0

    def test_golden_gg_chemid_relax_dmm_fault_total_reflects_session_meta(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            golden_gg_relax_dmm_fault_total=3, chemid_relax_dmm_fault_total=5,
        ))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["golden_gg_relax_dmm_fault_total"] == 3
        assert limitations["chemid_relax_dmm_fault_total"] == 5

    def test_standalone_relax_dmm_fault_count_zero_by_default(self):
        """[regresszió, Teljes kódbázis review 11. kör (2026-08-31),
        Metrológia/Adatintegritás P2 lelet] a RelaxController.dmm_fault_
        count (önálló StepKind.RELAX úton) session-szintű összege korábban
        egyáltalán nem jutott el a fő report.json-ig."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["standalone_relax_dmm_fault_count"] == 0

    def test_standalone_relax_dmm_fault_count_reflects_session_meta(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(standalone_relax_dmm_fault_count=4))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["standalone_relax_dmm_fault_count"] == 4

    def test_temperature_out_of_range_count_zero_by_default(self):
        """[regresszió, Teljes kódbázis review 21. kör (2026-09-07), Session/
        Workflow/Állapotgép P2 lelet] a safety.check_test_temperature_range()
        (20. kör) session-szintű összege korábban sosem jutott el a fő
        report.json-ig — az extreme_dt_s_count mintáját követi."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["temperature_out_of_range_count"] == 0

    def test_temperature_out_of_range_count_reflects_session_meta(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(temperature_out_of_range_count=6))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["temperature_out_of_range_count"] == 6


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


class TestDataMandatoryTerminalFields:
    """DATA-003: minden terminális állapotra kötelező alapmezők a JSON-ban."""

    def test_status_present_and_forwarded(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(status="FAULT"))
        assert report["status"] == "FAULT"

    def test_reason_present_and_forwarded(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(reason="DMM_FEEDBACK_LOST"))
        assert report["reason"] == "DMM_FEEDBACK_LOST"

    def test_total_charge_ah_present_in_top_level_report(self):
        """Korábbi hiba: session_meta kapta az értéket, de sosem került a JSON-ba."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(total_charge_ah=3.21))
        assert report["total_charge_ah"] == 3.21

    def test_total_discharge_ah_present_in_top_level_report(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(total_discharge_ah=1.11))
        assert report["total_discharge_ah"] == 1.11

    def test_active_phase_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(active_phase="discharge"))
        assert report["active_phase"] == "discharge"

    def test_safe_off_attempted_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(safe_off_attempted=True))
        assert report["safe_off_attempted"] is True

    def test_status_defaults_unknown_when_missing(self):
        rg = ReportGenerator()
        report = rg.generate({})
        assert report["status"] == "UNKNOWN"

    def test_not_implemented_fields_block_present(self):
        """Explicit, forrás-hivatkozott jelzés a még nem implementált DATA-003
        mezőkről (instrument identity) — nem hallgatott el."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert "NOT_IMPLEMENTED_FIELDS" in report
        nif = report["NOT_IMPLEMENTED_FIELDS"]
        assert "instrument_identity" in nif
        assert "config_snapshot_hash" in nif
        # [regresszió, Kör 17 MAX-effort review, R17-26, DFAM-026 inverz
        # alakja] az ambient_temperature_C_source mező (report.json
        # tetején, mindig None) korábban NEM volt itt dokumentálva -- egy
        # olvasó nem tudta megkülönböztetni a "nem mért" és a "hiba miatt
        # None" eseteket. A conditioning_actual_s (SOURCE_REQUIRED,
        # WORK_STATE.json open_findings) ugyanabba a producer-teljességi
        # felületbe tartozik, egyben regisztrálva.
        assert "ambient_temperature_C_source" in nif
        assert "conditioning_actual_s" in nif

    def test_profile_snapshot_present_when_given(self):
        """DATA-006 (2026-08-24): a session_meta profile_snapshot/
        charge_config_snapshot/learning_config_snapshot mezői a report.json-ba
        is bekerülnek — enélkül utólag nem volt rekonstruálható, milyen
        kapacitás/feszültség-paraméterek voltak ténylegesen aktívak egy adott
        futáshoz (l. session_fg1_20260823_200337 elemzés)."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            profile_snapshot={"nominal_capacity_Ah": 6.8, "model": "FG20721"},
            charge_config_snapshot={"max_charge_Ah_factor": 1.5},
            learning_config_snapshot={"rebound_tolerance_V": 0.1},
        ))
        assert report["profile_snapshot"] == {"nominal_capacity_Ah": 6.8, "model": "FG20721"}
        assert report["charge_config_snapshot"] == {"max_charge_Ah_factor": 1.5}
        assert report["learning_config_snapshot"] == {"rebound_tolerance_V": 0.1}

    def test_profile_snapshot_none_when_not_given(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["profile_snapshot"] is None
        assert report["charge_config_snapshot"] is None
        assert report["learning_config_snapshot"] is None

    def test_provisioning_consistency_present_when_given(self):
        """Block 6 (2026-08-25): a Block 2 konzisztencia-gate eredménye és
        a Block 3 Cell BL restore evidence-et is tartalmazó teljes
        LearningResultSummary a report.json-ba is bekerül, ugyanazzal a
        session_meta.get() mintával, mint a *_snapshot mezők."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            provisioning_consistency={"decision": "CONSISTENT", "reason": "ok", "comparisons": []},
            learning_result_summary_full={"result": "BQ_LEARNING_VERIFIED", "cell_bl_restore_confirmed": True},
        ))
        assert report["provisioning_consistency"] == {
            "decision": "CONSISTENT", "reason": "ok", "comparisons": [],
        }
        assert report["learning_result_summary_full"] == {
            "result": "BQ_LEARNING_VERIFIED", "cell_bl_restore_confirmed": True,
        }

    def test_provisioning_consistency_none_when_not_given(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["provisioning_consistency"] is None
        assert report["learning_result_summary_full"] is None


class TestDis003BlockVoltageMonitoring:
    """DIS-003: 24V pack (2× soros 12V blokk) esetén explicit jelzés kell,
    hogy nincs blokk-szintű feszültségfelügyelet — nem hardvert kitalálni."""

    def test_12v_pack_not_applicable(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(cell_count=6))
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert "NOT_APPLICABLE" in lim["block_voltage_monitoring"]

    def test_24v_pack_not_available(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(cell_count=12))
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert "NOT_AVAILABLE" in lim["block_voltage_monitoring"]
        assert "EN 60896" in lim["block_voltage_monitoring"]

    def test_missing_cell_count_treated_as_not_applicable(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert "NOT_APPLICABLE" in lim["block_voltage_monitoring"]

    def test_8_cell_pack_not_treated_as_24v_pack(self):
        """[regresszió, Teljes kódbázis review 21. kör (2026-09-07),
        Metrológia/Adatintegritás P3 lelet] a küszöb korábban "cell_count >
        6" volt, ami eltért a safety.py::check_psu_mode_compatibility()/
        check_precheck_voltage() egyetlen hiteles "24V+ pack" definíciójától
        (cell_count >= 12) — egy 8 cellás pack (l. 20. kör precheck-tesztje)
        tévesen "NOT_AVAILABLE — 24V+ pack" jelzést kapott volna."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(cell_count=8))
        lim = report["MEASUREMENT_LIMITATIONS"]
        assert "NOT_APPLICABLE" in lim["block_voltage_monitoring"]
        assert "NOT_AVAILABLE" not in lim["block_voltage_monitoring"]


class TestOcvSocPointQualityCountsForwarding:
    """Kor 18, Batch C12 (METRO-P2-1, DFAM-021, R17-12 mintajat 1:1
    koveteve): az OcvSocController session-szintu point_quality_counts
    osszesitese a session_meta-bol a report.json-ba is eljusson."""

    def test_forwards_point_quality_counts_when_present(self):
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            ocv_soc_point_quality_counts={"OK": 8, "CUTOFF_DURING_PULSE": 2},
        ))
        assert report["ocv_soc_point_quality_counts"] == {
            "OK": 8, "CUTOFF_DURING_PULSE": 2,
        }

    def test_forwards_timing_tolerance_key_unfiltered(self):
        """Fuggetlen re-verifikacio P3-2 lelete: a main_window.py popup-
        kapu SZANDEKOSAN kizarja a DEGRADED_TIMING_TOLERANCE-t (l. ott a
        kommentet), DE ez a szures KIZAROLAG a popup-dontesre vonatkozik
        -- a report.json-nak TELJES, szuretlen kepet kell adnia a post-hoc
        elemzeshez. Ez a teszt zarolja, hogy egy jovobeli kor a szurest
        NE mozgassa fel a producer/forwarder oldalra."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta(
            ocv_soc_point_quality_counts={"OK": 5, "DEGRADED_TIMING_TOLERANCE": 15},
        ))
        assert report["ocv_soc_point_quality_counts"] == {
            "OK": 5, "DEGRADED_TIMING_TOLERANCE": 15,
        }

    def test_none_when_absent(self):
        """Nem-OCV/SOC test_type-on a mezo None marad, nem KeyError."""
        rg = ReportGenerator()
        report = rg.generate(make_session_meta())
        assert report["ocv_soc_point_quality_counts"] is None


class TestR1B8cF25DischargeConfigSnapshot:
    """[regresszió, R1 B8c, F-25 (P2), DFAM-021/DFAM-043, D-B8-6] a
    DischargeController-utak (DISCHARGE_ONLY/CHARACTERIZATION/
    BQ_LEARNING_PHYSICAL) effektív kisütési áram-SETPOINTja és cutoffja
    korábban nem jutott el a report.json-ig. A NOT_IMPLEMENTED_FIELDS
    magyarázata a megmaradó rést (önálló CHARGE lépés override-snapshotja,
    NS-08) őszintén megnevezi."""

    def test_discharge_config_snapshot_passed_through_allowlist(self):
        snapshot = {
            "discharge_current_A": 0.36,
            "terminate_voltage_V_override": 10.5,
            "effective_discharge_current_set_A": 0.36,
            "effective_terminate_voltage_V": 10.5,
        }
        report = ReportGenerator().generate(
            make_session_meta(discharge_config_snapshot=snapshot),
        )
        assert report["discharge_config_snapshot"] == snapshot

    def test_discharge_config_snapshot_none_when_not_given(self):
        report = ReportGenerator().generate(make_session_meta())
        assert "discharge_config_snapshot" in report
        assert report["discharge_config_snapshot"] is None

    def test_not_implemented_text_names_snapshot_coverage_and_remaining_gap(self):
        text = ReportGenerator().generate(make_session_meta())[
            "NOT_IMPLEMENTED_FIELDS"
        ]["config_snapshot_hash"]
        assert "discharge_config_snapshot" in text
        assert "setpoint" in text
        # az önálló CHARGE lépés override-snapshotja (NS-08) őszintén nyitott
        assert "CHARGE" in text
        assert "NS-08" in text
        # a régi, pontatlan állítás ("más teszttípusnál egyelőre csak a
        # profile_snapshot") nem maradhat benne
        assert "egyelőre csak a profile_snapshot" not in text


class TestR5B3InstrumentIdentityTextPointsToCheckpoint:
    """[regresszió, Claude-Codex loop R5 B3: R5-MET-2, DFAM-043] a
    NOT_IMPLEMENTED_FIELDS["instrument_identity"] korábban azt állította,
    hogy az éles session nem kérdez *IDN?-t -- holott a GUI bring-up
    identify_all()-t hív, és az eredmény a checkpoint.json
    instrument_identities mezőjébe kerül. A szöveg most a report.json
    valós hiányát és a checkpoint megfelelő mezőjét nevezi meg."""

    def test_text_names_checkpoint_field_and_not_absent_idn_query(self):
        text = ReportGenerator().generate(make_session_meta())[
            "NOT_IMPLEMENTED_FIELDS"
        ]["instrument_identity"]
        assert "report.json nem tartalmaz" in text
        assert "identify_all()" in text
        assert "checkpoint.json" in text
        assert "instrument_identities" in text
        assert "sosem hív idn" not in text
        assert "Nincs implementálva" not in text
        # R5-B3 verifier OBS-R5B3-V1: az átfogalmazott hamis állítást vagy a
        # minősítők ("sikeres", "elkészült", "GUI-session") elvesztését a
        # részszöveg-assertek nem fognák -- a teljes, egyeztetett szöveg
        # (R5/AGREED_FINDINGS.md R5-B3) pontos egyezése igen.
        assert text == (
            "A report.json nem tartalmaz műszeridentitásokat. A "
            "GUI-session sikeres identify_all() eredménye az elkészült "
            "checkpoint.json instrument_identities mezőjében található."
        )


class TestR9B4SocEstimateMethod:
    """R9-B4: a generator csak a kapott modszeradatot kozvetiti."""

    def test_missing_soc_method_defaults_to_none(self):
        report = ReportGenerator().generate(make_session_meta())
        assert report["SOC_estimate_method"] is None

    def test_explicit_soc_methods_are_preserved(self):
        for method in (
            None,
            "coulomb_counting_from_nominal_capacity",
            "coulomb_counting_from_measured_capacity",
            "CUSTOM",
        ):
            report = ReportGenerator().generate(
                make_session_meta(SOC_estimate_method=method)
            )
            assert report["SOC_estimate_method"] == method


def test_b23b_report_keeps_learning_evidence_separate_from_incomplete_recharge():
    record = {
        "learning_evidence": {"result_summary": {"result": "BQ_LEARNING_VERIFIED"}},
        "final_recharge": {"status": "INCOMPLETE", "completed_at_utc": None},
    }
    report = ReportGenerator().generate(make_session_meta(
        tlc_completion=record, bq_learning_result="BQ_LEARNING_VERIFIED",
    ))
    assert report["tlc_completion"] == record
    assert report["bq_learning_result"] == "BQ_LEARNING_VERIFIED"
