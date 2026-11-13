"""
ReportGenerator — összefoglaló JSON riport kötelező mezőkkel.
[N13] MEASUREMENT_LIMITATIONS dedikált szekció.
[R1] relay_state TILOS a riportban.
[BY550] series_diode_part_number = "BY550"
"""
from __future__ import annotations


class ReportGenerator:

    def generate(self, session_meta: dict) -> dict:
        """
        Összefoglaló riportot állít elő session_meta dict-ből.
        Minden kötelező mező teljesítve a v1.2.1 / 4.20 szekció alapján.
        """
        report = {
            # DATA-003: minden terminális állapotra kötelező alapmezők — a
            # generate() korábban csak DONE esetén hívódott, session_meta
            # ezeket a mezőket kapta, de sosem kerültek be a kimeneti JSON-ba.
            "status": session_meta.get("status", "UNKNOWN"),
            "reason": session_meta.get("reason", ""),
            "error_queue_deferred_s": session_meta.get("error_queue_deferred_s", {}),
            "relax_missed_deadline_count": session_meta.get("relax_missed_deadline_count", 0),
            "total_charge_ah": session_meta.get("total_charge_ah", 0.0),
            "total_discharge_ah": session_meta.get("total_discharge_ah", 0.0),
            # Teljes kódbázis review 17. kör (2026-09-05), Metrológia/
            # Adatintegritás P2 lelet: az Ah-testvérpár Wh-oldala.
            "total_charge_wh": session_meta.get("total_charge_wh", 0.0),
            "total_discharge_wh": session_meta.get("total_discharge_wh", 0.0),
            # Governance-v2 review-kör 1 (CF-18/DFAM-022, batch 4.3): a
            # TELJES, megszakítás(ok)on átívelő session időtartama.
            "total_session_duration_s": session_meta.get("total_session_duration_s", 0.0),
            "active_phase": session_meta.get("active_phase", ""),
            # CF-19 (governance-v2 kör 2, DFAM-023): l. main_window.py
            # _write_report() azonos hivatkozású kommentje -- az
            # "active_phase" fenti mezővel szemben ez a kettő a controller
            # TÉNYLEGES utolsó belső állapotát/az abban eltöltött időt adja,
            # összetett (OCV_SOC/CHEMID/TRADITIONAL_LEARNING/GOLDEN_GG)
            # módoknál; egyszerű módoknál mindkettő None.
            "active_internal_phase": session_meta.get("active_internal_phase"),
            "active_internal_phase_elapsed_s": session_meta.get("active_internal_phase_elapsed_s"),
            "safe_off_attempted": session_meta.get("safe_off_attempted", False),
            "safe_off_result": session_meta.get("safe_off_result", {}),
            # Governance-v2 review-kör (MAX verifier V4-1, P2): a BQ
            # checkpoint ELÉRÉSEKOR (test_runner.py::_run_manual_checkpoint())
            # kiadott safe-off külön, nem összekeverhető
            # bizonyítéka -- l. main_window.py::_write_report() azonos
            # hivatkozású kommentje.
            #
            # PONTOSÍTÁS (független MAX verifier, 2026-09-12): ma MINDEN
            # session ezt a False/{} alapértéket kapja -- a checkpoint-kori
            # útvonal (main_window.py::_on_checkpoint_close()) egyetlen
            # elérhető checkpoint-típusnál sem fut le ténylegesen, mert a
            # MANUAL_CHECKPOINT (test_runner.py::bq_learning_physical())
            # mindig terminális, tehát `_pending_checkpoint_result` sosem
            # íródik (l. main_window.py NOT_REACHABLE-jelölése a
            # closeEvent()-ben). NEM "nem-checkpointos session"-t jelöl,
            # hanem a jelenlegi implementáció mellett MINDIG False/{}-t.
            "checkpoint_safe_off_attempted": session_meta.get(
                "checkpoint_safe_off_attempted", False
            ),
            "checkpoint_safe_off_result": session_meta.get(
                "checkpoint_safe_off_result", {}
            ),

            # PSU konfiguráció
            "psu_mode": session_meta.get("psu_mode", "UNKNOWN"),
            "psu_mode_max_voltage_V": session_meta.get("psu_mode_max_voltage_V"),
            "psu_mode_max_current_A": session_meta.get("psu_mode_max_current_A"),

            # Galvanikus leválasztás — [R1] relay_state tilos
            "no_galvanic_isolation": True,
            "isolation_method": "PSU_OUTPUT_OFF_ONLY",
            "galvanic_isolation_note": (
                "Nincs mechanikus relé. PSU output OFF szintű leválasztás."
            ),

            # BY550 soros dióda — [BY550]
            "series_diode_installed": True,
            "series_diode_type": "Silicon rectifier",
            "series_diode_part_number": "BY550",

            # Hőmérés
            # Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: korábban
            # feltétel nélkül "DMM2_surface_PT100" (hardkódolt), függetlenül
            # attól, hogy a session ténylegesen PT100 (4-eres RTD) vagy NTC
            # termisztor pótszonda módban futott-e (CLAUDE.md §12 — ne
            # ígérj más mérési láncot, mint ami ténylegesen aktív volt).
            "battery_temperature_source": session_meta.get(
                "battery_temperature_source", "DMM2_surface_PT100"
            ),
            "ambient_temperature_C_source": session_meta.get(
                "ambient_temperature_C_source", None
            ),

            # Működési konfiguráció
            "temperature_compensation_mode": session_meta.get(
                "temperature_compensation_mode", "MONITOR_ONLY"
            ),
            "interrupted_session_recovered": session_meta.get(
                "interrupted_session_recovered", False
            ),
            # WP-AK-02 (REC-003, "session folytonosság"): melyik megszakadt
            # session cross-process folytatása ez — üres, ha nem resume.
            "parent_session_id": session_meta.get("parent_session_id", ""),
            # R9-B4: controlleradat nelkul nincs kapacitasalap-allitas.
            "SOC_estimate_method": session_meta.get("SOC_estimate_method"),

            # Release-audit 2026-08-23 (2.2): TRADITIONAL_LEARNING/GOLDEN_GG
            # session esetén a BQ learning eredmény-klasszifikáció (CLAUDE.md
            # §11.6 — "a fizikai ciklus nem egyenlő sikeres BQ learninggel")
            # korábban sosem jutott el a riportig, csendben elveszett. None
            # marad más teszttípusnál (nincs BQ learning eredmény).
            "bq_learning_result": session_meta.get("bq_learning_result"),
            "bq_learning_quality_flags": session_meta.get("bq_learning_quality_flags"),

            # DATA-006 (2026-08-24): a ténylegesen aktív profil/config-
            # paraméterek értékei (nem csak egy visszafejthetetlen hash,
            # l. NOT_IMPLEMENTED_FIELDS lent) — enélkül utólag nem volt
            # rekonstruálható a session mappából, milyen kapacitás/
            # feszültség/limit-paraméterek voltak ténylegesen érvényben.
            "profile_snapshot": session_meta.get("profile_snapshot"),
            "charge_config_snapshot": session_meta.get("charge_config_snapshot"),
            "learning_config_snapshot": session_meta.get("learning_config_snapshot"),
            # R1 B8c, F-25 (P2, DFAM-021/DFAM-043, D-B8-6): a DischargeController-
            # utak (DISCHARGE_ONLY/CHARACTERIZATION/BQ_LEARNING_PHYSICAL)
            # nyers DischargeConfig-ja + feloldott effektív áram-SETPOINT és
            # cutoff (nem műszer-readback) -- l. main_window.py::
            # _write_report(). None, ha a sessionben nem jött létre
            # DischargeController.
            "discharge_config_snapshot": session_meta.get("discharge_config_snapshot"),

            # Governance-v2 kör 2: IEC 60896-21 §6.3 DC belső ellenállás
            # EGYETLEN, session-szintű eredménye — None marad más
            # test_type-nál (l. main_window.py::_write_report() iec_result
            # kommentje). NEM sweep/tábla, ezért a fő report.json tetején,
            # nem csak a dedikált iec_dc_resistance_result.csv-ben.
            "Ri_DC_IEC_ohm": session_meta.get("Ri_DC_IEC_ohm"),
            "Isc_A": session_meta.get("Isc_A"),
            # Kör 17 MAX-effort review, R17-12 (DFAM-021+DFAM-018): a mérés
            # érvényessége/időzítés-minősítése korábban KIZÁRÓLAG a dedikált
            # iec_dc_resistance_result.csv-be jutott el -- l. main_window.py
            # azonos hivatkozású kommentje a popup-gate döntés indoklásáért
            # (csak quality, NEM timing_degraded -- forrás-ellenőrzött,
            # timing_degraded strukturálisan garantált alapértelmezett
            # runner_tick_s mellett).
            "Ri_DC_IEC_quality": session_meta.get("Ri_DC_IEC_quality"),
            "Ri_DC_IEC_timing_degraded": session_meta.get("Ri_DC_IEC_timing_degraded"),
            "Ri_DC_IEC_synchronization_quality": session_meta.get(
                "Ri_DC_IEC_synchronization_quality"
            ),
            "Ri_DC_IEC_t_sample_1_s": session_meta.get("Ri_DC_IEC_t_sample_1_s"),
            "Ri_DC_IEC_t_sample_2_s": session_meta.get("Ri_DC_IEC_t_sample_2_s"),
            # Kör 21 F13 (METRO-R21-3, DFAM-021, R17-12 mintáját 1:1
            # követve): a mérési hőmérséklet (Ua/Ia pont + Ub/Ib pont,
            # mindkettő saját validity-flaggel) korábban KIZÁRÓLAG a
            # dedikált iec_dc_resistance_result.csv-be jutott el -- l.
            # main_window.py::_write_report() azonos hivatkozású kommentje.
            "Ri_DC_IEC_temperature_C": session_meta.get("Ri_DC_IEC_temperature_C"),
            "Ri_DC_IEC_temperature_valid": session_meta.get("Ri_DC_IEC_temperature_valid"),
            "Ri_DC_IEC_temperature_2_C": session_meta.get("Ri_DC_IEC_temperature_2_C"),
            "Ri_DC_IEC_temperature_2_valid": session_meta.get("Ri_DC_IEC_temperature_2_valid"),
            # Kor 18, Batch C12 (METRO-P2-1, DFAM-021, R17-12 mintajat 1:1
            # koveteve): az OcvSocController session-szintu, quality-string
            # -> darabszam osszesitese a per-pont measurement_quality-rol
            # -- l. main_window.py::_write_report() azonos hivatkozasu
            # kommentje.
            "ocv_soc_point_quality_counts": session_meta.get("ocv_soc_point_quality_counts"),

            # Block 6 (2026-08-25) — Block 2 (PROFILE<->SESSION<->BQ Data
            # Flash konzisztencia-gate) és Block 3 (Cell BL tranzakciós
            # evidence) eredménye. Ugyanaz a minta, mint a fenti *_snapshot
            # mezőknél: session_meta.get() -> None, ha a hívó nem adja meg
            # (más test-típusnál/módban), zéró hatással azokra.
            "provisioning_consistency": session_meta.get("provisioning_consistency"),
            "learning_result_summary_full": session_meta.get("learning_result_summary_full"),
            "tlc_completion": session_meta.get("tlc_completion"),

            # Release-audit 2026-08-23 (2.1): GOLDEN_GG session esetén a TI
            # Golden GG Maker submission csomag ténylegesen elkészült-e —
            # korábban a GUI sosem adott át export_config-ot, a csomag sosem
            # jött létre, de erről a riport sem adott jelzést.
            "golden_gg_package_ready_for_ti_submission": session_meta.get(
                "golden_gg_package_ready_for_ti_submission"
            ),
            "golden_gg_export_dir": session_meta.get("golden_gg_export_dir"),
            "golden_gg_export_zip_path": session_meta.get("golden_gg_export_zip_path"),
            # Teljes kódbázis review 2. kör (2026-09-15, R2-11a, 14.
            # writer-kör, DFAM-026 4. előfordulása): l. main_window.py
            # azonos hivatkozású kommentje -- korábban ez a mező a
            # session_meta-ban sem létezett, tehát a whitelist hiánya
            # önmagában NEM lett volna elég ok, de a whitelist megléte itt
            # kötelező, különben egy jövőbeli session_meta-bővítés is
            # csendben elveszne a report.json-ig.
            "golden_gg_quality_flags": session_meta.get("golden_gg_quality_flags"),
            # D9/F-06 (2026-09-13, Riportok/ui_policy_audit_20260913.md):
            # a sikeres GPCCHEM-export (CHEMID_CHARACTERIZATION_PBA) útvonala
            # korábban sehol nem jelent meg a riportban -- a GOLDEN_GG
            # testvér mezője (fent) már exponálta a sajátját.
            "chemid_export_dir": session_meta.get("chemid_export_dir"),
            "chemid_export_zip_path": session_meta.get("chemid_export_zip_path"),

            # OCV
            "ocv_isolation_method": "PSU_OUTPUT_OFF_ONLY",
            "ocv_accuracy_limitation": (
                "Az OCV mérés PSU_OUTPUT_OFF_ONLY leválasztás mellett történt "
                "(nincs galvanikus leválasztás). BY550 fordított szivárgás ~10 nA "
                "@30V (mért érték, 2026-05-31 lab session) — adatlap max ~10 µA."
            ),

            # [N13] Mérési korlátozások dedikált szekció
            "MEASUREMENT_LIMITATIONS": self._build_measurement_limitations(session_meta),

            # DATA-003: a szabályrendszer által FAULT esetén kötelezőnek jelölt
            # mezők közül ami MÉG NEM implementált — explicit, forrás-hivatkozott
            # jelzés, nem hallgatólagos hiány (l. CLAUDE.md §7 "nem állapítható meg").
            "NOT_IMPLEMENTED_FIELDS": {
                "last_valid_sample": (
                    "Nincs külön duplikálva itt — l. samples.csv a session mappában "
                    "(DATA-001 szerint folyamatosan naplózva)."
                ),
                # R5-B3 (R5-MET-2, DFAM-043): a GUI 7954421 (WP-AK-02)
                # óta connect_all() után identify_all()-t hív; az eredményt
                # a test_runner.py _checkpoint_traceability_fields()
                # adja át a checkpointnak.
                "instrument_identity": (
                    "A report.json nem tartalmaz műszeridentitásokat. A "
                    "GUI-session sikeres identify_all() eredménye az elkészült "
                    "checkpoint.json instrument_identities mezőjében található."
                ),
                "config_snapshot_hash": (
                    "A visszafejthetetlen hash maga nincs a report.json-ban "
                    "(l. checkpoint.json config_hash/profile_hash, resume-"
                    "egyezés ellenőrzésére) — DE 2026-08-24 óta a tényleges "
                    "profil/config ÉRTÉKEK a profile_snapshot/"
                    "charge_config_snapshot/learning_config_snapshot "
                    "mezőkben szerepelnek. A charge_config_snapshot a "
                    "beágyazott ChargeController konfigurációja "
                    "TRADITIONAL_LEARNING/GOLDEN_GG/CHEMID/OCV_SOC/"
                    "IEC_DC_INTERNAL_RESISTANCE session-öknél; a "
                    "discharge_config_snapshot a DischargeController "
                    "(DISCHARGE_ONLY/CHARACTERIZATION/BQ_LEARNING_PHYSICAL) "
                    "nyers konfigurációja + effektív áram-setpoint és cutoff "
                    "(nem műszer-readback). NYITOTT (NS-08): az önálló CHARGE "
                    "lépés (CHARGE_ONLY, ill. CHARACTERIZATION/"
                    "BQ_LEARNING_PHYSICAL töltési lépései) töltési "
                    "override-jainak snapshotja nincs a report.json-ban."
                ),
                "setpoint_fallback_integration": (
                    "MEASUREMENT_LIMITATIONS.fallback_integration_duration_s/"
                    "fallback_samples_count mindig 0.0/0 — egyik controller "
                    "sem vált setpoint-alapú áramintegrációra readback-hiba "
                    "esetén, minden ilyen hiba emergency_stop()-ot vált ki "
                    "(CLAUDE.md §10.4-nek megfelelő, konzervatívabb döntés)."
                ),
                # Kör 17 MAX-effort review, R17-26 (DFAM-026 inverz alakja --
                # deklarált mező producer nélkül, nem producer deklarált
                # policy nélkül): a fenti session_meta.get("ambient_
                # temperature_C_source", None) mindig None marad -- nincs
                # sehol a Prog/-ban producer, ami feltöltené. A hardver
                # ténylegesen NEM rendelkezik ambiens-hőmérséklet szenzorral
                # (l. charge_controller.py::ChargeConfig.ambient_temperature_
                # source docstringje, CHG-004) -- a GUI sosem állítja be ezt
                # az értéket, tehát a "nem implementált" jelzés pontos, nem
                # egy meglévő, csak be nem kötött adatforrásra vonatkozik.
                "ambient_temperature_C_source": (
                    "Mindig None a report.json-ban -- nincs Prog/-beli producer, "
                    "és a hardver nem rendelkezik ambiens-hőmérséklet szenzorral "
                    "(l. charge_controller.py ChargeConfig.ambient_temperature_"
                    "source docstringje, CHG-004). NEM ugyanaz, mint a mért "
                    "battery_temperature_C (surface DMM/RTD, l. battery_"
                    "temperature_source mező)."
                ),
                # Kapcsolódó, ugyanabba a report-mező/producer-teljességi
                # felületbe tartozó, önállóan regisztrált tétel (l.
                # WORK_STATE.json open_findings[41] item 1): conditioning_
                # actual_s (a konditálás TÉNYLEGES időtartama, SUP03-CHEMID-
                # 012 által kötelezőnek jelölt) sehol nincs implementálva/
                # exportálva sem GOLDEN_GG-n, sem ChemID-en -- SOURCE_
                # REQUIRED, nem ez a kör oldja fel.
                "conditioning_actual_s": (
                    "SOURCE_REQUIRED -- a SUP03-CHEMID-012 kötelezőnek jelöli, "
                    "de sem GOLDEN_GG-n, sem ChemID-en nincs implementálva/"
                    "exportálva. L. WORK_STATE.json open_findings, korábbi "
                    "CF-31 utáni tétel (1)."
                ),
            },
        }
        return report

    def _build_measurement_limitations(self, session_meta: dict) -> dict:
        """
        [N13] MEASUREMENT_LIMITATIONS szekció összeállítása.

        Implementáld ide a session_meta alapján feltöltött limitation dict-et.
        Kötelező mezők (v1.2.1 / 4.20):
          - current_measurement_source
          - no_external_calibrated_shunt
          - no_galvanic_isolation_during_ocv
          - temperature_source
          - capacity_result_quality
          - fallback_integration_duration_s
          - fallback_samples_count
          - communication_faults_count
          - emergency_stop_occurred
          - charge_Ah_accuracy_note
          - discharge_Ah_accuracy_note

        session_meta mezők amik ide kerülhetnek:
          session_meta["capacity_result_quality"]    → "OK" / "DEGRADED"
          session_meta["fallback_integration_duration_s"]
          session_meta["fallback_samples_count"]
          session_meta["communication_faults_count"]
          session_meta["emergency_stop_occurred"]    → bool

        Teljes kódbázis review 9. kör (2026-08-30), Metrológia/Adatintegritás
        P3 lelet: fallback_integration_duration_s/fallback_samples_count a
        CLAUDE.md §11.3 normatív szabályát fedi le ("Setpoint-fallback csak
        explicit degraded minősítéssel, időtartam- és mintaszám-naplózással
        fogadható el") — ez a mezőpár és a hozzá tartozó safety.py
        FaultCode.INTEGRATION_FALLBACK_TOO_LONG/WarningCode.INTEGRATION_
        FALLBACK_TO_SETPOINT enum-tag SZÁNDÉKOSAN nincs producer-oldalról
        bekötve: egyik controller sem vált setpoint-alapú áramintegrációra
        readback-hiba esetén, minden ilyen hiba emergency_stop()-ot vált ki
        (CLAUDE.md §10.4-nek megfelelő, KONZERVATÍVABB döntés, mint egy
        tényleges fallback lenne). E két mező ezért minden valaha generált
        report.json-ban 0.0/0 marad — ez NEM hiba, hanem a jelenleg
        implementált, biztonságosabb viselkedés következménye; l.
        NOT_IMPLEMENTED_FIELDS a report generate()-jében.
        """
        return {
            "current_measurement_source": "PSU_READBACK_OR_LOAD_READBACK",
            "no_external_calibrated_shunt": True,
            "no_galvanic_isolation_during_ocv": True,
            "temperature_source": "surface_DMM_not_internal",
            "capacity_result_quality": session_meta.get("capacity_result_quality", "OK"),
            "fallback_integration_duration_s": session_meta.get(
                "fallback_integration_duration_s", 0.0
            ),
            "fallback_samples_count": session_meta.get("fallback_samples_count", 0),
            "communication_faults_count": session_meta.get(
                "communication_faults_count", 0
            ),
            # Teljes kódbázis review 9. kör (2026-08-30), Metrológia/
            # Adatintegritás P2 lelet: a capacity_result_quality mostantól
            # ezeket is figyelembe veszi (l. main_window.py::_write_report())
            # — a nyers számokat is fel kell venni a MEASUREMENT_LIMITATIONS
            # szekcióba, hogy a "miért DEGRADED" kérdés a report.json-ból
            # magából is megválaszolható legyen, ne csak a samples.csv
            # soronkénti átvizsgálásával.
            "extreme_dt_s_count": session_meta.get("extreme_dt_s_count", 0),
            "max_observed_dt_s": session_meta.get("max_observed_dt_s", 0.0),
            # Teljes kódbázis review 21. kör (2026-09-07), Session/Workflow/
            # Állapotgép P2 lelet: a safety.check_test_temperature_range()
            # (20. kör) session-szintű összege korábban sosem jutott el a
            # riportig — az extreme_dt_s_count mintáját követi, ugyanazon
            # tulajdonságnéven (temperature_out_of_range_count) minden
            # controlleren, ezért a test_runner.py generikus delta-
            # összegzése (_flush_totals()) már önmagában helyesen összegzi.
            "temperature_out_of_range_count": session_meta.get(
                "temperature_out_of_range_count", 0
            ),
            # Review-kör 3 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb)
            # P2, DFAM-001 4. előfordulása -- a temperature_out_of_range_
            # count fenti mintáját követő, MINDEN StepKind-et lefedő
            # session-szintű összeg (test_runner.py generikus
            # _flush_totals()-e).
            "temp_dmm_fault_ticks": session_meta.get("temp_dmm_fault_ticks", 0),
            # Governance-v2 review-kör 1 (CF-20/DFAM-021, batch 4.6): a 13
            # lehetséges safety.WarningCode session-szintű előfordulás-
            # száma — korábban csak az UTOLSÓ aktív tick jelzése volt
            # elérhető (samples.csv warning_flags oszlopa).
            "warning_code_counts": session_meta.get("warning_code_counts", {}),
            # Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
            # Adatintegritás P2 lelet: a RelaxController.dmm_fault_count
            # (önálló StepKind.RELAX úton) session-szintű összege — a
            # golden_gg_relax_dmm_fault_total/chemid_relax_dmm_fault_total
            # mintáját követi, de külön mezőként (nem ütközik velük).
            "standalone_relax_dmm_fault_count": session_meta.get(
                "standalone_relax_dmm_fault_count", 0
            ),
            "emergency_stop_occurred": session_meta.get("emergency_stop_occurred", False),
            "charge_Ah_accuracy_note": (
                "PSU readback alapú; kis taper áramnál korlátozott pontosság."
            ),
            "discharge_Ah_accuracy_note": "Load readback alapú.",
            # Release-audit 2026-08-23 (2.3): korábban statikus, mindig
            # ugyanazt állító szöveg volt ("kis eltérés lehetséges"),
            # függetlenül a tényleges hibától — most a session ténylegesen
            # mért árामforrását és a valós időzítési degraded-állapotot
            # tükrözi (l. discharge_controller.py rb_timing_degraded).
            "rb_current_source": (
                "Rb kalkuláció mért terhelésáram (LOAD_READBACK) alapján. "
                + (
                    "FIGYELEM: legalább egy pont (1s/10s/30s) tényleges "
                    "mintavételi ideje a névleges célidőponttól 0.5s-nál "
                    "jobban eltért — l. rb_timing_degraded és a "
                    "rb_*_actual_s mezők a samples.csv-ben."
                    if session_meta.get("rb_timing_degraded")
                    else "Minden elért pont mintavételi ideje a névleges "
                    "célidőpont ±0.5s tolerancián belül volt."
                )
            ),
            "rb_timing_degraded": session_meta.get("rb_timing_degraded", False),
            # Teljes kódbázis review 10. kör (2026-08-31), Metrológia/
            # Adatintegritás P2 lelet: a GoldenGgController/
            # ChemIdCharacterizationController relax-fázisai (RELAX_2H/
            # RELAX_5H/RELAX_5H_FULL/RELAX_5H_EMPTY) alatti feszültség-DMM-
            # olvasási hibák session-szintű összege korábban csak az
            # export-zip belsejében jelent meg (csak sikeres exportnál) —
            # a fő report.json-ban egyáltalán nem, FAULT-tal végződő
            # session esetén nyomtalanul elveszett. session_meta.get()
            # default 0 -> más teszttípusnál (nincs golden_gg/chemid
            # controller ebben a sessionben) nulla hatású.
            "golden_gg_relax_dmm_fault_total": session_meta.get(
                "golden_gg_relax_dmm_fault_total", 0
            ),
            "chemid_relax_dmm_fault_total": session_meta.get(
                "chemid_relax_dmm_fault_total", 0
            ),
            # Kör 17 MAX-effort review, R17-24a (DFAM-021+DFAM-026): a
            # RELAX_5H_FULL fázis végi feszültsége -- a teljes ChemID
            # karakterizáció legjelentősebb OCV-adata -- korábban
            # kiszámolódott, de sehova nem jutott el (l. chemid_
            # controller.py::relaxed_full_voltage_V docstringje). None
            # marad más test_type-nál/FAULT-tal a RELAX_5H_FULL vég előtt.
            "chemid_relaxed_full_voltage_V": session_meta.get(
                "chemid_relaxed_full_voltage_V"
            ),
            "chemid_relaxed_full_voltage_valid": session_meta.get(
                "chemid_relaxed_full_voltage_valid"
            ),
            # Teljes kódbázis review 12. kör (2026-08-31), Szoftver/
            # Architektúra P2 lelet: a TraditionalLearningController
            # RELAX_1/RELAX_2H fázisai ugyanazt a mintát követik, mint a
            # fenti golden_gg/chemid mezők — korábban hiányzott róla a
            # relax_dmm_fault_total property, ezért ez a mező sosem jutott
            # el idáig.
            "traditional_learning_relax_dmm_fault_total": session_meta.get(
                "traditional_learning_relax_dmm_fault_total", 0
            ),
            # Teljes kódbázis review 14. kör (2026-09-04), Szoftver/
            # Architektúra + Session/Workflow/Állapotgép P2 lelet: az
            # OcvSocController PRECHARGE_RELAX/STEP_RELAX fázisai ugyanazt a
            # mintát követik, mint a fenti golden_gg/chemid/traditional_
            # learning mezők — ez a negyedik testvér, amelyről korábban
            # hiányzott a relax_dmm_fault_total property.
            "ocv_soc_relax_dmm_fault_total": session_meta.get(
                "ocv_soc_relax_dmm_fault_total", 0
            ),
            # Review-kör 2 (2026-09-09), friss Szoftver/Architektúra F7
            # (DFAM-001, 5. testvér): az IecDcResistanceController
            # PRECHARGE_RELAX/OPEN_CIRCUIT_REST fázisai ugyanazt a mintát
            # követik, mint a fenti golden_gg/chemid/traditional_learning/
            # ocv_soc mezők — korábban ez volt az egyetlen, amelyről
            # hiányzott a per-controller attribúciós kulcs a riportban
            # (az összesített relax_dmm_fault_total-ba MÁR beleszámított).
            "iec_dc_resistance_relax_dmm_fault_total": session_meta.get(
                "iec_dc_resistance_relax_dmm_fault_total", 0
            ),
            # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás
            # NEW-2 (DFAM-024 2. előfordulás + DFAM-021): a chemid/
            # golden_gg/iec_dc_resistance controllerek egy sikertelen
            # hőmérséklet-DMM-olvasást a korábbi (vagy konstruktor-default
            # 20.0) értékkel helyettesítettek CSENDBEN, quality="OK"-ként
            # exportálva a roomtemp_rel_dis_rel.csv/roomtemp.csv/
            # iec_dc_resistance_result.csv fájlokba -- ez a session-szintű
            # összeg (a golden_gg/chemid/iec_dc_resistance_controller.py
            # temp_dmm_fault_ticks property-jéből) az első jelzés a
            # report.json-ban arról, hogy ilyen kiesés történt.
            "golden_gg_temp_dmm_fault_ticks": session_meta.get(
                "golden_gg_temp_dmm_fault_ticks", 0
            ),
            "chemid_temp_dmm_fault_ticks": session_meta.get(
                "chemid_temp_dmm_fault_ticks", 0
            ),
            "iec_dc_resistance_temp_dmm_fault_ticks": session_meta.get(
                "iec_dc_resistance_temp_dmm_fault_ticks", 0
            ),
            # Review-kör 3 (2026-09-09), friss Metrológia M3-02 / Szoftver/
            # Architektúra F2 (P2): a traditional_learning/ocv_soc
            # controllerek korábban hiányoztak erről a property-ről (l.
            # temp_dmm_fault_ticks_total main_window.py-beli komment) —
            # ez a két mező a fenti golden_gg/chemid/iec_dc_resistance
            # mintát egészíti ki, ugyanazzal a session-szintű, tick-alapú
            # jelentéssel.
            "traditional_learning_temp_dmm_fault_ticks": session_meta.get(
                "traditional_learning_temp_dmm_fault_ticks", 0
            ),
            "ocv_soc_temp_dmm_fault_ticks": session_meta.get(
                "ocv_soc_temp_dmm_fault_ticks", 0
            ),
            # DIS-003: 24V pack (2× soros 12V blokk) esetén csak a teljes
            # stringfeszültség mérhető, nincs egyedi blokk-szintű felügyelet —
            # ezt explicit dokumentálni kell, nem hardvert kitalálni hozzá
            # (Konszolidált terv "Phase 1-ben B út elfogadható").
            "block_voltage_monitoring": self._block_voltage_monitoring_status(session_meta),
        }

    def _block_voltage_monitoring_status(self, session_meta: dict) -> str:
        # Teljes kódbázis review 21. kör (2026-09-07), Metrológia/
        # Adatintegritás P3 lelet: korábban "cell_count > 6" volt a
        # küszöb, ami a safety.py::check_psu_mode_compatibility()/
        # check_precheck_voltage() egyetlen hiteles "24V+ pack" definí-
        # ciójától (cell_count >= 12) eltért — egy 7-11 cellás pack (pl.
        # a 20. kör 8-cellás precheck-tesztje) tévesen "NOT_AVAILABLE —
        # 24V+ pack" jelzést kapott, holott safety.py szerint még nem
        # 24V+ pack.
        cell_count = session_meta.get("cell_count")
        if cell_count is not None and cell_count >= 12:
            return (
                "NOT_AVAILABLE — 24V (vagy nagyobb) pack esetén csak a teljes "
                "stringfeszültség mérhető (1 feszültség-DMM), egyedi 12V blokk "
                "nincs külön felügyelve. Ez a mérés emiatt NEM nevezhető teljes "
                "EN 60896 kompatibilis string capacity tesztnek."
            )
        return "NOT_APPLICABLE — 12V (egyetlen blokk) pack, nincs blokk-felosztási kérdés."
