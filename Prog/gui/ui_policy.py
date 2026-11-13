"""
SUP02-ARCH-003: teszt-mód szerinti mező-láthatósági/kötelezőségi policy registry.
Nincs PySide6 import — pure Python, GUI-független (SUP02-ARCH-001).

Ahelyett, hogy minden panel/widget saját `if test_type == ...` elágazást tartana
fenn (amit a SUP02 dokumentum kifejezetten tilt — l. SUP02-ARCH-003), a
láthatósági/kötelezőségi szabályok egyetlen, központi táblázatban élnek.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TestModePolicy:
    visible_groups: frozenset[str]
    # D9/F-01 (2026-09-13, Riportok/ui_policy_audit_20260913.md): a
    # "required" itt KÉTFÉLE csoportra vonatkozhat, és ez szándékosan nem
    # egyetlen enforcement-szemantika. (1) Hard input-kötelező, nincs
    # legitim auto/default -- pl. bq_port, golden_gg_system_rate_current
    # -- EZEKET a SessionConfig.validate() tényleg ">0"/"nem üres"-ként
    # ellenőrzi, is_group_required()-en keresztül (egyetlen forrás). (2)
    # Policy-vezérelt, MINDIG van auto/default értéke (pl.
    # charge_current/discharge_rate/terminate_voltage/pulse_current --
    # spinbox "auto" speciális érték vagy DynamicSessionModel default
    # policy) -- ezeknél a "required" azt jelenti, hogy a csoportnak
    # LÁTHATÓNAK/mérlegelhetőnek kell lennie indítás előtt (l.
    # is_group_visible()), NEM azt, hogy a kezelőnek explicit felül
    # kellene írnia az auto-értéket. A SessionConfig.validate() ezért
    # TUDATOSAN nem hív ezekre ">0"-enforcementet -- egy ilyen check egy
    # legitim auto-módot utasítana el hibaként.
    required_groups: frozenset[str] = field(default_factory=frozenset)


# SUP02-UI-001: mindig látható alapadatok — minden teszttípusnál jelen van.
# 2026-08-23 (felhasználói kérés): "temperature_compensation" (a
# hőmérséklet-DMM szenzortípus-választója, RTD_4W_PT100/THERMISTOR_2W_NTC_
# 10K_B3950 is benne) mostantól MINDEN módnál látható/állítható — korábban
# csak CHARGE_ONLY/CHARACTERIZATION/BQ_LEARNING_PHYSICAL módban volt
# elérhető, ezért TRADITIONAL_LEARNING/GOLDEN_GG (és a "temperature_
# monitoring" csoportot használó DISCHARGE_ONLY/CHEMID/OCV_SOC) alatt a
# kezelő nem tudta a helyi szondát (2-eres NTC pótszonda a sérült 4-eres
# RTD helyett) kiválasztani — ez okozott egy valós HIL futtatásnál
# TEMPERATURE_MONITOR_LOST_CRITICAL faultot (a GUI a rejtett widget
# alapértékét, RTD_4W_PT100-at küldte, miközben ténylegesen a THERMISTOR
# szonda volt bekötve).
ALWAYS_VISIBLE_GROUPS: frozenset[str] = frozenset({
    "battery", "instruments", "psu_mode_and_wiring",
    "effective_summary", "validation_status", "temperature_compensation",
})


TEST_MODE_POLICIES: dict[str, TestModePolicy] = {
    # SUP02-UI-002
    "CHARGE_ONLY": TestModePolicy(
        visible_groups=ALWAYS_VISIBLE_GROUPS | {
            "charge_current", "charge_target", "taper",
            "relax_after_charge", "charge_safety_limits",
        },
        required_groups=frozenset({"charge_current"}),
    ),
    # SUP02-UI-003
    "DISCHARGE_ONLY": TestModePolicy(
        visible_groups=ALWAYS_VISIBLE_GROUPS | {
            "discharge_rate", "terminate_voltage", "capacity_basis",
            "temperature_monitoring", "discharge_safety_limits", "recharge_required_warning",
        },
        required_groups=frozenset({"discharge_rate", "terminate_voltage"}),
    ),
    # SUP02-UI-004
    "CHARACTERIZATION": TestModePolicy(
        visible_groups=ALWAYS_VISIBLE_GROUPS | {
            "charge_current", "charge_target", "taper",
            "discharge_rate", "terminate_voltage", "capacity_basis",
            "relax_after_charge", "report_quality_summary",
            # CF-34 (governance-v2 review-kör 1, DFAM-026
            # declared_policy_without_consumer): a session utolsó lépése
            # kisütés+relax — az akku lemerítve marad a teszt végén, l.
            # main_window.py::_on_finished() DONE-ági fogyasztóját.
            "recharge_required_warning",
        },
        required_groups=frozenset({"charge_current", "discharge_rate", "terminate_voltage"}),
    ),
    # SUP02-UI-005
    "BQ_LEARNING_PHYSICAL": TestModePolicy(
        visible_groups=ALWAYS_VISIBLE_GROUPS | {
            "charge_current", "charge_target", "taper",
            "discharge_rate", "terminate_voltage",
            "relax_policy",
            "bq_precondition_checklist", "manual_checkpoint", "physical_cycle_only_warning",
            # Review-kör 4 (2026-09-09), friss GUI/UX F1 (P2, DFAM-001): a
            # BQ_LEARNING_PHYSICAL terv utolsó energiaátadó lépése egy
            # DISCHARGE (discharge_2/relax_after_discharge_2/manual_bq_
            # checkpoint, l. test_runner.py::TestPlan), tehát a session
            # lemerített akkuval zár -- pontosan ugyanaz a helyzet, mint
            # DISCHARGE_ONLY/CHARACTERIZATION/OCV_SOC/CHEMID/TRADITIONAL_
            # LEARNING/GOLDEN_GG-nél, amik már mind megkapták ezt a
            # csoportot (CF-34 + round-2 GUI F3). A CF-34/GUI-F3 sibling-
            # keresése egy rögzített módlistára szűkült, nem a tényleges
            # végállapotra -- ez a mód kimaradt.
            "recharge_required_warning",
        },
        required_groups=frozenset({"charge_current", "discharge_rate"}),
    ),
    # SUP03-APP-004 (kiegeszito_szabalyok_03): ChemID PbA karakterizáció —
    # C20 áram, szobahőmérséklet-kondicionálás, 5h relax, gyártói cutoff,
    # GPCCHEM export. NEM azonos a többi teszttípussal (SUP03-CHEMID-010).
    # Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/Állapot P2 lelet
    # #2: "taper"/"charge_current" mostantól LÁTHATÓ — main_window.py::
    # _build_runner() shared_charge_cfg-je (taper_hold_s/charge_current_A_
    # override) a FULL_CHARGE_MANUFACTURER_METHOD beágyazott ChargeController-
    # jét élesen vezérli, ugyanazon a fizikai (TF1/soros dióda) útvonalon,
    # mint CHARGE_ONLY/TRADITIONAL_LEARNING/GOLDEN_GG — enélkül a kezelő nem
    # látta/állíthatta, milyen taper-küszöbbel/töltőárammal fut a saját,
    # több órás fizikai ciklusa (ugyanaz a hibaosztály, mint a 2026-08-23
    # OV-incidens után TRADITIONAL_LEARNING/GOLDEN_GG-nél már javított
    # "charge_current" hiány, l. a TRADITIONAL_LEARNING/GOLDEN_GG policy
    # kommentje lentebb).
    "CHEMID_CHARACTERIZATION_PBA": TestModePolicy(
        visible_groups=ALWAYS_VISIBLE_GROUPS | {
            "chemid_conditioning", "chemid_c20_current", "chemid_relax_5h",
            "chemid_manufacturer_cutoff", "chemid_gpcchem_export",
            "temperature_monitoring", "charge_current", "taper",
            # CF-34 (governance-v2 review-kör 1, DFAM-026
            # declared_policy_without_consumer): a chemid_controller.py
            # explicit RECHARGE_REQUIRED terminális állapottal zárul —
            # l. main_window.py::_on_finished() DONE-ági fogyasztóját.
            "recharge_required_warning",
        },
        required_groups=frozenset({"chemid_c20_current", "chemid_manufacturer_cutoff"}),
    ),
    # SUP02-UI-006. Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/
    # Állapot P2 lelet #2: "taper"/"charge_current" mostantól LÁTHATÓ —
    # l. a CHEMID_CHARACTERIZATION_PBA policy fenti kommentje ugyanerről
    # a mintáról (a PRECHARGE beágyazott ChargeController itt is a közös
    # shared_charge_cfg-et kapja).
    "OCV_SOC_CHARACTERIZATION": TestModePolicy(
        visible_groups=ALWAYS_VISIBLE_GROUPS | {
            "capacity_basis", "soc_grid_mode", "uniform_step",
            "partial_discharge_rate", "relax_policy", "pulse_current",
            "pulse_duration", "target_evaluation_times", "acquisition_mode",
            # R1 B8c, F-24 (P2, DFAM-026): a szerkeszthető "terminate_voltage"
            # KIKERÜLT -- az OcvSocController a profil cutoffját használja
            # (ocv_soc_controller.py `profile.terminate_voltage_pack_V`), a
            # cfg-szintű értéknek itt nincs fogyasztója (csak a
            # DischargeController olvassa). Helyette csak-olvasható
            # megjelenítés (config_panel.py, a ChemID cutoff-sor mintájára).
            "ocv_soc_profile_cutoff",
            "temperature_monitoring", "estimated_duration",
            "output_table_summary", "charge_current", "taper",
            # CF-34 (governance-v2 review-kör 1, DFAM-026
            # declared_policy_without_consumer): a lépcsőzetes SOC-grid
            # kisütés az akkut lemerített állapotban hagyja a teszt végén —
            # l. main_window.py::_on_finished() DONE-ági fogyasztóját.
            "recharge_required_warning",
        },
        required_groups=frozenset({"capacity_basis", "pulse_current"}),
    ),
    # WP-AK-16: BQ34Z110 traditional learning cycle (WP-AK-10) — a PC a workflow
    # master, a fizikai ciklust a TraditionalLearningController vezérli; a GUI
    # felől kizárólag a Pico BQ soros port szükséges (a többi paraméter a
    # BatteryProfile-ból/LearningConfig defaultjaiból jön). 2026-08-23
    # (felhasználói review, 2. kör — traditional_learning OV incidens): a
    # "charge_current" mostantól LÁTHATÓ (de nem kötelező — 0 = automatikus
    # profil-alapú származtatás marad az alapértelmezett) — a controller
    # valódi, többórás fizikai töltést végez a session töltőáramával
    # (l. main_window.py shared_charge_cfg), ezt korábban a kezelő nem
    # látta/állíthatta.
    # Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/Állapot P2 lelet
    # #2: "taper" is mostantól LÁTHATÓ, ugyanazon okból — a shared_charge_
    # cfg taper_hold_s-e a TAPER_HOLD->CHARGE_DONE átmenetet vezérli a
    # beágyazott ChargeController-ben, korábban rejtve, egy másik módból
    # visszamaradt értékkel futhatott (l. CHEMID_CHARACTERIZATION_PBA
    # policy kommentje fent ugyanerről a mintáról).
    "TRADITIONAL_LEARNING": TestModePolicy(
        visible_groups=ALWAYS_VISIBLE_GROUPS | {
            "bq_port", "charge_current", "taper",
            # Review-kör 2 (2026-09-09), friss GUI/UX F3 (DFAM-001, a CF-34
            # sibling-keresése egy fix 4-elemű módlistára szűkült, nem a
            # tényleges állapotgép-végállapotra). Q-38 szerint a sorrend vége
            # DISCHARGE_2->REBOUND_CHECK_2->RELAX_2->VERIFY_UPDATE_STATUS_06->
            # FINAL_SNAPSHOT->FINAL_RECHARGE->DONE. A figyelmeztetés az
            # igazolatlan vagy megszakadt záró újratöltésre továbbra is kell;
            # a GUI csak igazolt töltés és OFF után jelzi a befejezést.
            "recharge_required_warning",
        },
        required_groups=frozenset({"bq_port"}),
    ),
    # WP-AK-16: Golden GG / system-rate mérés (WP-AK-11) — a rendszerre jellemző
    # kisütőáramot SUP03-GG-007 szerint explicit meg KELL adni, nem
    # származtatható automatikusan C/10-/C/20-ból. 2026-08-23 kiegészítés —
    # l. TRADITIONAL_LEARNING policy kommentje fent ugyanerről a
    # töltőáram-láthatósági résről (és a 8. kör "taper" kiegészítéséről).
    "GOLDEN_GG": TestModePolicy(
        visible_groups=ALWAYS_VISIBLE_GROUPS | {
            "bq_port", "golden_gg_system_rate_current", "charge_current", "taper",
            # Review-kör 2 (2026-09-09), friss GUI/UX F3 — l. a
            # TRADITIONAL_LEARNING policy fenti kommentje ugyanerről a
            # mintáról: a fázissorrend vége SYSTEM_RATE_DISCHARGE->RELAX_5H->
            # EXPORT_PACKAGE->DONE, nincs záró töltés.
            "recharge_required_warning",
        },
        required_groups=frozenset({"bq_port", "golden_gg_system_rate_current"}),
    ),
    # Governance-v2 kör 2: IEC 60896-21 §6.3 kétpontos DC belső ellenállás
    # mérés. A forrás minden időzítési paramétert (Ia=4×I10@20s, Ib=20×I10@5s,
    # 5 perc pihenő) rögzít -- nincs GUI-szabad numerikus paraméter, mint az
    # OCV_SOC/CHARACTERIZATION módoknál. Az egyetlen mód-specifikus, csak-
    # olvasható elem az "i10_source_display" (a profilból jövő I10 érték
    # megjelenítése, hogy a kezelő indítás előtt lássa, melyik értékből
    # számolódik Ia/Ib -- l. config_panel.py). "charge_current"/"taper"
    # látható a PRECHARGE fázis miatt (ugyanaz a minta, mint CHEMID/OCV_SOC/
    # TRADITIONAL_LEARNING/GOLDEN_GG-nél).
    "IEC_DC_INTERNAL_RESISTANCE": TestModePolicy(
        visible_groups=ALWAYS_VISIBLE_GROUPS | {
            "charge_current", "taper", "temperature_monitoring",
            "i10_source_display",
        },
    ),
}


def is_group_visible(test_type: str, group: str) -> bool:
    """SUP02-VALID-001: csak az aktív módhoz releváns mezők/csoportok kötelezőek/láthatók."""
    policy = TEST_MODE_POLICIES.get(test_type)
    if policy is None:
        return True
    return group in policy.visible_groups


def is_group_required(test_type: str, group: str) -> bool:
    policy = TEST_MODE_POLICIES.get(test_type)
    if policy is None:
        return False
    return group in policy.required_groups


def visible_groups_for(test_type: str) -> frozenset[str]:
    policy = TEST_MODE_POLICIES.get(test_type)
    if policy is None:
        return frozenset()
    return policy.visible_groups
