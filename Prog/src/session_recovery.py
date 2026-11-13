"""
session_recovery.py — pure session-recovery döntési réteg.

REC-001..004 / GAP-OLD-003 (WP-AK-02): power-loss / process-crash recovery.
Nincs PySide6-/hardver-függőség — csak adatstruktúra-összehasonlítás és
fájlrendszer-vizsgálat, PySide6-mentesen, önállóan unit tesztelhető
(l. CLAUDE.md §9 "GUI-002 réteghatárok" elve: driver/business logika nem
importál Qt-t).

Alapelv (CLAUDE.md §14 / a normatív dokumentum REC-001): "Process restart után
aktív CC/CV vagy kisütési állapotba vakon visszaugrani tilos. Resume csak safe
boundary-ról." — ez a modul a döntést hozza meg, a tényleges resume-végrehajtás
(új TestRunner/Logger felépítése, start_step_index átadása) a GUI rétegé.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class ResumeDecision(Enum):
    RESUME_ALLOWED = "RESUME_ALLOWED"
    BLOCKED_UNSAFE_BOUNDARY = "BLOCKED_UNSAFE_BOUNDARY"
    BLOCKED_ALREADY_TERMINAL = "BLOCKED_ALREADY_TERMINAL"
    BLOCKED_MISSING_CHECKPOINT_DATA = "BLOCKED_MISSING_CHECKPOINT_DATA"
    BLOCKED_INSTRUMENT_MISMATCH = "BLOCKED_INSTRUMENT_MISMATCH"
    BLOCKED_CONFIG_MISMATCH = "BLOCKED_CONFIG_MISMATCH"
    BLOCKED_PROFILE_MISMATCH = "BLOCKED_PROFILE_MISMATCH"
    BLOCKED_SAMPLE_MISMATCH = "BLOCKED_SAMPLE_MISMATCH"
    # Governance-v2 review-kör (MAX verifier O-3/F5, DFAM-001): a
    # `next_safe_step_index` korábban SEMMILYEN validáción nem ment át itt --
    # l. evaluate_resume() azonos hivatkozású kommentje.
    BLOCKED_INVALID_STEP_INDEX = "BLOCKED_INVALID_STEP_INDEX"


@dataclass(frozen=True)
class ResumeEvaluation:
    decision: ResumeDecision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is ResumeDecision.RESUME_ALLOWED


# Kanonikus mezőhalmaz a hash-hez — CSAK a biztonság/mérés-azonosítás szempontjából
# releváns mezők, NEM a SessionConfig/BatteryProfile összes mezője (pl. runner_tick_s
# vagy taper_hold_s finomhangolása nem indokolja egy egyébként azonos session
# folytatásának blokkolását).
CONFIG_HASH_FIELDS: tuple[str, ...] = (
    "battery_profile_name", "battery_model", "nominal_capacity_ah", "sample_id",
    "psu_mode", "test_type", "temperature_compensation_mode",
    "charge_current_A_override", "discharge_current_A", "discharge_terminate_voltage_V",
    "discharge_rate_origin",  # GOV-029: belső, effektív ráta-/kapacitásbázis-eredet
    # Review-kör 5 (2026-09-09), Architektúra A-2 (P2, DFAM-003
    # partial_validation_reused_as_full_gate): a mérőlánc fizikai
    # konfigurációja (pl. sérült PT100 helyett NTC-re váltás, l.
    # config_panel.py) korábban NEM volt része a hash-nek, holott a
    # temperature_compensation_mode (OFF/MONITOR_ONLY/ENABLED) igen -- egy
    # resume-nál a szenzor-mód csendben megváltozhatott (a *IDN? azonos,
    # mert ugyanaz a fizikai 34465A DMM szolgál ki mindkét szenzortípust),
    # a hash mégis egyezett volna, és a két fizikailag különböző
    # mérőláncból származó adat jelölés nélkül keveredett volna egyetlen
    # session összegben.
    "temperature_sensor_mode",
    # Teljes kódbázis review 2. kör (2026-09-15, R2-04, P1, DFAM-003 3.
    # előfordulása): 3 további, fizikai setpontot meghatározó SessionConfig
    # mező hiányzott a hash-ből -- egy resume-nál csendben megváltozhattak
    # volna, a hash mégis egyezett volna. discharge_rate_divisor a kisütő
    # áram alap-számításának osztója (nominal_capacity_ah/divisor), tehát
    # közvetlenül fizikai setpont; golden_gg_system_rate_current_A a
    # GOLDEN_GG rate-áram (SUP03-GG-007, nincs biztonságos alapérték);
    # ocv_soc_step_percent az OCV/SOC rácsfelbontást (mérési terv
    # sűrűségét) határozza meg, egy resume-nál eltérő rács csendben
    # keverne össze két különböző mérési tervből származó pontokat.
    "discharge_rate_divisor", "golden_gg_system_rate_current_A", "ocv_soc_step_percent",
    # Teljes kódbázis review 2. kör (2026-09-16, R2-04-F2, P2, DFAM-003
    # sibling): a resume KIZÁRÓLAG RELAX-határról indulhat (test_runner.py
    # ::_run_step(), safe_boundary), és CHARGE_ONLY módban éppen ez a mező
    # adja a RELAX időtartamát (main_window.py::_make_relax_ctrl() ->
    # RelaxConfig.min_relax_s, ha step.relax_purpose nem "post_discharge").
    # A kezelő a crash és a resume KÖZÖTT szabadon átállíthatta a spinboxot
    # (config_panel.py, 60-7200 s), és a folytatott session a checkpoint-
    # időbeli helyett az ÚJ értékkel relaxált volna, azonos config_hash
    # mellett. Az így mért OCV egyensúlyi állapota nem hasonlítható össze
    # a session korábbi pontjaival (CLAUDE.md §7 "gyorsított labor OCV ≠
    # equilibrium OCV"; rules/20-metrology.md). Ez metrológiai/
    # visszakövethetőségi kapu, NEM safety-limit. A testvér `runner_tick_s`/
    # `taper_hold_s` SZÁNDÉKOSAN kimarad (l. a fenti fejkomment): azok
    # finomhangolás, nem egy mérés érvényességi feltétele.
    "relax_after_charge_s",
)

PROFILE_HASH_FIELDS: tuple[str, ...] = (
    "battery_name", "manufacturer", "model", "nominal_voltage_V", "cell_count",
    "nominal_capacity_Ah", "chemistry", "charge_voltage_per_cell_V",
    "terminate_voltage_per_cell_V", "float_voltage_per_cell_V",
    # Teljes kódbázis review 2. kör (2026-09-15, R2-04, P1, DFAM-003 3.
    # előfordulása): 7 további BatteryProfile mező hiányzott a hash-ből,
    # mindegyik közvetlenül fizikai setpontot/safety-limitet határoz meg
    # -- egy resume-nál egy MÁSIK battery_profile.py-beli profil-definíció
    # (pl. C10_capacity_Ah/i10_discharge_current_A frissítés egy korrigált
    # gyártói értékre) csendben megváltozhatott volna, a hash mégis
    # egyezett volna, mert ezek a mezők a whitelist HIÁNYA miatt egyáltalán
    # nem kerültek hashelésre (nem "None -> None", hanem "nincs a
    # whitelistben" -- ez a whitelist-hiányossági defekt, elkülönül a
    # _stable_hash() lentebbi fail-closed defektjétől). i10_discharge_
    # current_A az IEC_DC_INTERNAL_RESISTANCE Ia/Ib terhelési pontok
    # KIZÁRÓLAGOS forrása; C10_capacity_Ah szerepe ennél SZÉLESEBB --
    # battery_profile.py::_c10_base_Ah-n át `design_capacity_mAh` (BQ Data
    # Flash-be írt érték), `effective_max_charge_A` (0,25×) és
    # `effective_taper_A` (0,03×) alapja, tehát önmagában is safety-
    # releváns setpont-forrás; max_charge_current_A/taper_current_A a
    # töltési setpontok felső korlátja; temp_comp_mV_per_cell_per_degC a
    # hőkompenzált töltési feszültség-setpontot módosítja;
    # test_temperature_min_C/max_C az elfogadható mérési hőmérséklet-
    # tartományt határozza meg.
    "C10_capacity_Ah", "i10_discharge_current_A", "max_charge_current_A",
    "taper_current_A", "temp_comp_mV_per_cell_per_degC",
    "test_temperature_min_C", "test_temperature_max_C",
)

# Teljes kódbázis review 2. kör (2026-09-16, R2-04-F7, DFAM-003
# STRUCTURAL_PREVENTION_REVIEW, structural_prevention_status:
# NOT_REVIEWED -> REVIEWED_TEST_ONLY_ADOPTED): a családnak MINDHÁROM
# előfordulása UGYANAZ a mechanizmus volt -- egy létező mező SOSEM
# került be a whitelistbe, és a hiány csendben "nem hashelt"-et
# jelentett. A két MEGLÉVŐ meta-teszt (TestConfigHashFieldsMatchReal
# Dataclass/TestProfileHashFieldsMatchRealDataclass) csak az EGYIK
# irányt védi (whitelist ⊆ dataclass, elgépelés ellen). Ez a két tuple
# a MÁSIK irányt teszi ellenőrizhetővé: egy ÚJ SessionConfig/
# BatteryProfile mező mostantól nem tud csendben a "nem hashelt"
# defaultra esni -- vagy a hash-whitelistbe, vagy ide, INDOKLÁSSAL kell
# kerülnie, különben a meta-teszt bukik. SZÁNDÉKOSAN teszt-only
# prevenció: NINCS refaktor, nincs új kódút, nincs új konfigurációs
# szabadság, a futásidejű viselkedés VÁLTOZATLAN (CLAUDE.md §5/§8/§10
# -- a család `prevention` mezőjében felvetett közös helper/absztrakció
# NEM indokolt, mert az növelné a viselkedési teret, ez viszont csak
# manuális szinkront szüntet meg).
_CONFIG_FIELDS_INTENTIONALLY_NOT_HASHED: tuple[str, ...] = (
    # Külön, ERŐSEBB kapu védi: instrument_identities (*IDN?) egyezés.
    "psu_resource", "load_resource", "dmm_voltage_resource",
    "dmm_temperature_resource",
    # Indítás előtti operátori nyugta, nem mérési/setpont-paraméter.
    "hardware_wiring_confirmed",
    # Finomhangolás, nem egy mérés érvényességi feltétele (l. a
    # CONFIG_HASH_FIELDS fejkommentjét).
    "runner_tick_s", "taper_hold_s",
    # A BQ-transzport PORTJA; a chip azonosságát/állapotát nem ez dönti el.
    "bq_port",
)

_PROFILE_FIELDS_INTENTIONALLY_NOT_HASHED: tuple[str, ...] = (
    # Provenance-metaadat (honnan jött a profil), nem setpont/limit.
    "profile_source",
)

_REQUIRED_CHECKPOINT_KEYS: tuple[str, ...] = (
    "config_hash", "profile_hash", "instrument_identities", "sample_id", "safe_boundary",
    # Governance-v2 review-kör (MAX verifier O-3/F5): a folytatási PONT maga
    # ugyanolyan kötelező része egy resume-képes checkpointnak, mint a
    # biztonságos határ vagy a konfiguráció-egyezés -- enélkül a GUI
    # (main_window.py) egy hallgatólagos 0-ra esett vissza, azaz a MÁR
    # lefutott lépéseket ismételte volna meg a checkpointból átvett
    # (nem-nulla) Ah/Wh összegek mellett. `_checkpoint_traceability_fields()`
    # minden write_checkpoint()-hívásba beleírja, tehát minden WP-AK-02 óta
    # készült valódi checkpoint tartalmazza.
    "next_safe_step_index",
)


_MISSING = object()


def _stable_hash(obj: Any, fields: tuple[str, ...]) -> str:
    """Teljes kódbázis review 2. kör (2026-09-15, R2-04, P1, DFAM-003 3.
    előfordulása): korábban `getattr(obj, f, None)` egy törölt/átnevezett
    mezőnél CSENDBEN None-t hashelt -- egy jövőbeli refaktor, ami pl.
    `discharge_terminate_voltage_V`-t átnevezi, a hash-t ÉSZREVÉTLENÜL
    "sikeresen" kiszámolta volna a régi (most már nem létező) mezőre
    HELYETTESÍTETT None-nal, ahelyett hogy hibát jelezne -- ez épp azt a
    védelmet ásta volna alá, amit a hash-gate biztosítani hivatott. Most
    fail-closed: egy hiányzó mező explicit AttributeError-t dob, nem
    hashelhető csendben None-ként."""
    payload = {}
    for f in fields:
        value = getattr(obj, f, _MISSING)
        if value is _MISSING:
            raise AttributeError(
                f"_stable_hash(): {type(obj).__name__!r} objektumnak nincs "
                f"{f!r} mezője, holott a hash-whitelist tartalmazza -- "
                f"törölt/átnevezett mező csendes None-kénti hashelése "
                f"tilos (DFAM-004 silent_catchall_dispatch elve: ismeretlen/"
                f"hiányzó eset explicit típusú hibát dob, nem csendes "
                f"alapértékre esik vissza)."
            )
        payload[f] = value
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def compute_config_hash(cfg: Any) -> str:
    """GOV-029: a régi kisütési checkpoint nem folytatható új policyvel."""
    from Prog.src.battery_profile import (
        NORMAL_DISCHARGE_POLICY_ID,
        discharge_rate_origin_for_mode,
    )

    base = _stable_hash(cfg, tuple(f for f in CONFIG_HASH_FIELDS if f != "discharge_rate_origin"))
    if cfg.test_type == "TRADITIONAL_LEARNING":
        from dataclasses import asdict

        from Prog.src.traditional_learning_controller import (
            LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL,
            LEARNING_HARD_FLOOR_V_PER_CELL,
            LearningConfig,
        )

        # B8c D-B8-3: a régi TL-checkpoint fail-safe blokkolt, migráció nincs.
        payload = json.dumps({
            "config": base, "policy": "GOV-029/Q-34/Q-35:R16-B16c;Q-9/Q-22/Q-23:R16-B7;Q-42:R16-B23b-1;Q-38:R16-B23b-2",
            "learning_config": asdict(LearningConfig()),
            "cutoffs_V_per_cell": LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL,
            "hard_floor_V_per_cell": LEARNING_HARD_FLOOR_V_PER_CELL,
        }, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    if cfg.test_type not in {
        "DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL",
        "CHEMID_CHARACTERIZATION_PBA", "OCV_SOC_CHARACTERIZATION", "GOLDEN_GG",
    }:
        return base
    # A rejtett mező draftja nem az adott controller effektív rátája.
    origin = discharge_rate_origin_for_mode(
        cfg.test_type, cfg.discharge_rate_origin, cfg.nominal_capacity_ah,
        cfg.discharge_rate_divisor,
    )
    payload = json.dumps(
        {
            "config": base, "policy": NORMAL_DISCHARGE_POLICY_ID, "rate_origin": origin,
            # B8c: a régi GG-checkpoint sem kerülheti meg az új BQ-kaput.
            **({"bq_policy": "Q-9/Q-22/Q-23:R16-B7"} if cfg.test_type == "GOLDEN_GG" else {}),
        },
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def compute_profile_hash(profile: Any) -> str:
    """Stabil, rövidített SHA-256 a biztonság-releváns BatteryProfile mezőkből."""
    return _stable_hash(profile, PROFILE_HASH_FIELDS)


def evaluate_resume(
    checkpoint: dict,
    current_config_hash: str,
    current_profile_hash: str,
    current_instrument_identities: dict[str, str],
    current_sample_id: str,
    total_step_count: int,
) -> ResumeEvaluation:
    """
    A tényleges resume-döntés — REC-001 ("csak biztonságos határról") és a
    Konszolidált terv WP-AK-02 Kapu fault-injection mátrixa szerint.

    Ellenőrzési sorrend (a legkritikusabbtól a legkevésbé kritikusig):
    1. safe_boundary — SOHA nem bírálható felül semmilyen egyezéssel.
    2. hiányos/régi-formátumú checkpoint — fail-safe blokkolás, nem feltételezés.
    3. folytatási pont (next_safe_step_index) érvényessége.
    4. műszerazonosság — más fizikai felállás más session.
    5. konfiguráció/profil/minta egyezés.

    `total_step_count`: a JELENLEG felépített terv lépésszáma (a hívó adja át,
    l. main_window.py::_build_runner()). KÖTELEZŐ (nem opcionális) — független
    MAX verifier lelet (2026-09-12): egy `None`-ra visszaeső, elfelejtett
    hívó CSENDBEN kikapcsolta volna pont a fő védendő esetet (tartományon
    kívüli index), miközben a többi kapu (típus/alsó korlát) továbbra is
    futott volna, hamis biztonságérzetet adva. Mivel a modulnak ma egyetlen
    produkciós hívója van (main_window.py), és az mindig ismeri a felépített
    terv lépésszámát, a kötelezővé tétel nem szűkíti a hívhatóságot, csak
    kizárja a csendes kikapcsolás lehetőségét.
    """
    if not checkpoint.get("safe_boundary", False):
        return ResumeEvaluation(
            ResumeDecision.BLOCKED_UNSAFE_BOUNDARY,
            "A checkpoint nem biztonságos határon (pl. aktív CC/CV/DISCHARGE/PULSE "
            "állapotban) készült — folytatás tiltott (REC-001).",
        )

    # Teljes kódbázis review 20. kör (2026-09-06), Session/Workflow P1
    # lelet: a `checkpoint_is_terminal` mezőt korábban _run_manual_
    # checkpoint() kiszámolta, de sosem perzisztálta a checkpoint.json-ba
    # — egy sikeresen, a terv VÉGÉig eljutott MANUAL_CHECKPOINT (pl. a
    # BQ_LEARNING_PHYSICAL terv egyetlen, mindig utolsó checkpointja)
    # `safe_boundary=True`-val van írva (helyesen, hiszen PSU/Load valóban
    # OFF), tehát minden más feltétel teljesülése esetén RESUME_ALLOWED-ot
    # kapott volna — CLAUDE.md §14 "terminális checkpointnál a GUI ne
    # kínáljon hamis folytatást" megsértése, mert nincs érvényes
    # next_safe_step_index, amitől folytatni lehetne. Ez a kapu ugyanolyan
    # felülbírálhatatlan súlyú, mint a safe_boundary ellenőrzés fent.
    if checkpoint.get("checkpoint_is_terminal", False):
        return ResumeEvaluation(
            ResumeDecision.BLOCKED_ALREADY_TERMINAL,
            "A checkpoint a terv VÉGÉN készült (nincs következő lépés) — "
            "ez a session sikeresen befejeződött, nincs mit folytatni.",
        )

    # A kulcs jelenléte (nem az érték "igazság-értéke") dönt a hiányos/régi
    # formátum felismerésénél — a `sample_id` LEGITIM lehet üres string
    # (az operátor nem mindig ad meg minta-azonosítót), ezért CSAK a kulcs
    # meglétét várjuk el rá, nem a nem-üres tartalmát. A hash-eket és a
    # műszerazonosítást viszont sosem szabad üresnek elfogadni — azok mindig
    # ténylegesen kiszámolt/lekérdezett értékek egy valódi session-ben.
    missing_keys = [k for k in _REQUIRED_CHECKPOINT_KEYS if k not in checkpoint]
    empty_required = [
        k for k in ("config_hash", "profile_hash", "instrument_identities")
        if k in checkpoint and not checkpoint.get(k)
    ]
    if missing_keys or empty_required:
        return ResumeEvaluation(
            ResumeDecision.BLOCKED_MISSING_CHECKPOINT_DATA,
            "A checkpoint hiányos vagy régi formátumú (hiányzó mező: "
            f"{missing_keys + empty_required}) — biztonságos egyezés-ellenőrzés "
            "nem végezhető, folytatás tiltott.",
        )

    # Governance-v2 review-kör (MAX verifier O-3 + F5, DFAM-001): a
    # `next_safe_step_index` eddig SEMMILYEN validáción nem ment át ezen a
    # döntési rétegen, és a GUI (main_window.py) egy `.get(..., 0)`
    # alapértékkel adta tovább a TestRunner.run()-nak. A test_runner.py::run()
    # korai kapuja (CF-44/F9) a tartományon kívüli indexet ma már
    # biztonságosan (safe-off + logger-close + FAULT) kezeli, DE ez egy
    # GARANTÁLTAN elbukó sessiont jelent: a program a műszereket
    # csatlakoztatja, új session-mappát nyit, a SZÜLŐ session checkpointját
    # `RESUMED_INTO_NEW_SESSION`-ként megjelöli (main_window.py, a resume
    # elfogadása után), majd az első run()-ban azonnal FAULT-ol -- az
    # operátor haladása így egy soha el nem indult "folytatás" miatt
    # véglegesen elveszik. Elérhető út: a `config_hash` CSAK a biztonság-
    # releváns mezőket fedi (l. CONFIG_HASH_FIELDS), a terv LÉPÉSSZÁMÁT nem
    # -- a `test_type` ugyan része a hash-nek, de egy jövőbeli
    # terv-módosítás (egy lépés törlése egy MEGLÉVŐ test_type-ból) azonos
    # hash mellett rövidítheti a tervet, és a régi checkpoint indexe
    # túlindexel. A kapu itt (nem csak a GUI-ban) van, mert ez a modul a
    # resume-döntés kanonikus, Qt-mentesen tesztelhető rétege.
    next_safe_step_index = checkpoint.get("next_safe_step_index")
    if (
        not isinstance(next_safe_step_index, int)
        or isinstance(next_safe_step_index, bool)
        or next_safe_step_index < 0
        or next_safe_step_index >= total_step_count
    ):
        return ResumeEvaluation(
            ResumeDecision.BLOCKED_INVALID_STEP_INDEX,
            "A checkpoint folytatási pontja (next_safe_step_index="
            f"{next_safe_step_index!r}) érvénytelen a jelenlegi tervhez "
            f"({total_step_count} lépés) — folytatás tiltott, mert a session "
            "azonnal FAULT-ra futna.",
        )

    if checkpoint.get("instrument_identities") != current_instrument_identities:
        return ResumeEvaluation(
            ResumeDecision.BLOCKED_INSTRUMENT_MISMATCH,
            "A csatlakoztatott műszerek azonosítása eltér a checkpointban rögzítettől "
            "— más fizikai felállás, folytatás tiltott.",
        )

    if checkpoint.get("config_hash") != current_config_hash:
        return ResumeEvaluation(
            ResumeDecision.BLOCKED_CONFIG_MISMATCH,
            "A jelenlegi session-konfiguráció eltér a megszakadt sessionétől — "
            "folytatás tiltott.",
        )

    if checkpoint.get("profile_hash") != current_profile_hash:
        return ResumeEvaluation(
            ResumeDecision.BLOCKED_PROFILE_MISMATCH,
            "A jelenlegi akkumulátorprofil eltér a megszakadt sessionétől — "
            "folytatás tiltott.",
        )

    if checkpoint.get("sample_id") != current_sample_id:
        return ResumeEvaluation(
            ResumeDecision.BLOCKED_SAMPLE_MISMATCH,
            "A jelenlegi minta-azonosító eltér a megszakadt sessionétől — "
            "folytatás tiltott (más fizikai akkumulátor lehet a padon).",
        )

    return ResumeEvaluation(
        ResumeDecision.RESUME_ALLOWED,
        "Minden ellenőrzés (biztonságos határ, műszer, konfiguráció, profil, minta) "
        "megfelelt — folytatás engedélyezett.",
    )


# R1 F-15: a session-mappa neve `session_<battery_model>_<YYYYmmdd_HHMMSS>`
# (main_window.py::_build_runner()); a resume-jelölt időrendje a név végi
# időbélyegből jön, nem a teljes névből (a modellprefix eltorzítaná).
_SESSION_DIR_STAMP_RE = re.compile(r"_(\d{8}_\d{6})$")


def _session_dir_sort_key(session_dir: Path) -> tuple[str, str]:
    """(időbélyeg, név) rendezési kulcs; időbélyeg nélküli név = "" ->
    a legrégebbinek számít (dokumentált kompatibilitási szabály)."""
    match = _SESSION_DIR_STAMP_RE.search(session_dir.name)
    return (match.group(1) if match else "", session_dir.name)


def discover_incomplete_sessions(sessions_root: Path) -> list[dict]:
    """
    Megszakadt (feltehetően crash/kill/áramkimaradás miatt véget ért) sessionök
    felderítése egy induláskori scan-hez.

    WP-AK-01 (DATA-003) óta minden VALÓDI terminális állapot (DONE/FAULT/STOPPED/
    terminális CHECKPOINT_STOPPED) `report.json`-t ír — ez teszi megbízhatóvá az
    egyszerű szabályt: ha van `checkpoint.json`, de nincs `report.json`, a session
    nem futott le rendesen a végéig.
    """
    results: list[dict] = []
    if not sessions_root.exists() or not sessions_root.is_dir():
        return results

    # Review-kör 5 (2026-09-09), Session/Workflow S5-02 (P2, DFAM-001): a
    # mappanevek (session_<név>_<YYYYmmdd_HHMMSS>) lexikografikus sorrendje
    # kronologikusan NÖVEKVŐ -- `find_resumable_session()` (lent) az itt
    # visszaadott listából az ELSŐ engedélyezettet választja, tehát a
    # korábbi (növekvő) sorrend a LEGRÉGEBBI megszakadt sessiont ajánlotta
    # fel egy azonos konfigurációjú/mintájú akkumulátorhoz, ha több is
    # létezett -- fizikailag védhetetlen, mert a padon lévő akku a
    # LEGÚJABB megszakadás állapotában van. `reverse=True`: a legújabb jön
    # elsőként.
    #
    # R1 F-15 pontosítás (DFAM-043): a teljes név szerinti sorrend CSAK
    # azonos modellprefixen belül kronologikus. Ez a lista a startup-
    # dialógust táplálja (sorrendje változatlan); a resume-jelölt
    # kiválasztása (find_resumable_session()) R1 F-15 óta NEM ebből a
    # listából, hanem saját, időbélyeg-alapú bejárással történik.
    for session_dir in sorted((p for p in sessions_root.iterdir() if p.is_dir()), reverse=True):
        checkpoint_path = session_dir / "checkpoint.json"
        report_path = session_dir / "report.json"
        if not checkpoint_path.exists() or report_path.exists():
            continue
        checkpoint, unreadable = _read_checkpoint(checkpoint_path)
        results.append({
            "session_dir": session_dir,
            "checkpoint": checkpoint,
            "unreadable": unreadable,
        })

    return results


def _read_checkpoint(checkpoint_path: Path) -> tuple[dict, bool]:
    """checkpoint.json beolvasása -> (checkpoint, unreadable). R1 F-15: a
    discover_incomplete_sessions() R17-06 parse-logikája 1:1 kiemelve, hogy
    a find_resumable_session() saját bejárása ugyanazt használja."""
    # R17-06 (Kor 17, DFAM-018 + DFAM-004): egy csonka/olvashatatlan
    # checkpoint.json KORABBAN ugyanazt a `{}`-t adta, mint egy
    # maradvany NELKULI, ep checkpoint -- a "nem olvashato" eset
    # CSENDBEN "nincs mire figyelmeztetni"-ve omlott ossze, elnyelve a
    # BQ-maradvany (Cell BL / UNSEALED chip) figyelmeztetest is. A
    # `checkpoint` SZANDEKOSAN `{}` marad (nem `None`): a
    # find_resumable_session() -> evaluate_resume() ut igy
    # valtozatlanul BLOCKED_UNSAFE_BOUNDARY-vel fail-closed (a
    # `safe_boundary` kulcs hianyzik `{}`-bol). Az UJ, KULON
    # `unreadable` kulcs CSAK a startup-dialogus operatori
    # figyelmeztetesehez kell. (R1 F-15 ota a find_resumable_session()
    # az olvashatatlan jeloltnel mar az evaluate_resume() ELOTT None-t ad.)
    unreadable = False
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError lefedi a json.JSONDecodeError-t (leszarmazottja)
        # ES a UnicodeDecodeError-t is (aramkimaradasnal egy tobb-
        # bajtos UTF-8 karakter KOZEPEN csonkolt fajl -- l. logger.py::
        # write_checkpoint() ensure_ascii=False -- read_text(encoding=
        # "utf-8") ilyenkor UnicodeDecodeError-t dob, NEM json.
        # JSONDecodeError-t, tehat a szukebb except ezt korabban
        # athengedte volna, MainWindow.__init__()-et elszalasztva
        # figyelmeztetes nelkul, mert a hivo (_review_incomplete_
        # sessions_on_startup()) nincs try/except-ben).
        checkpoint = {}
        unreadable = True
    if not isinstance(checkpoint, dict) or any(
        checkpoint.get(field) is not None and not isinstance(checkpoint[field], dict)
        for field in (
            "pending_bq_df_restore", "pending_bq_unsealed", "last_safe_off_result",
            "instrument_identities", "warning_code_counts", "error_queue_deferred_s",
        )
    ):
        # K8: a startup/resume dict-almezoit is ellenorizzuk, meg az olvasok
        # elott. Serult adatbol nincs folytatas vagy regebbi session-fallback.
        # A hianyzo/None es az ervenyes dict ertekek viselkedese valtozatlan.
        checkpoint = {}
        unreadable = True
    return checkpoint, unreadable


def _sample_identity_known(sample_id: object) -> bool:
    """R2-B7 (R2-SES-02): azonosított-e a minta. Az üres/whitespace vagy
    nem-string `sample_id` AZONOSÍTATLAN minta -- nem pozitív mintaazonosság,
    és nem is bizonyított mintakülönbség."""
    return isinstance(sample_id, str) and sample_id.strip() != ""


def find_resumable_session(
    sessions_root: Path,
    current_config_hash: str,
    current_profile_hash: str,
    current_instrument_identities: dict[str, str],
    current_sample_id: str,
    total_step_count: int,
) -> Optional[dict]:
    """
    Az egyetlen lehetséges resume-jelölt: a checkpoint.json-nal rendelkező
    session-mappák közül az IDŐBEN LEGÚJABB (mappanév-végi időbélyeg, l.
    `_session_dir_sort_key()`), amelynek `sample_id`-je a jelenlegivel egyezik.
    Ha ez a jelölt lefutott (`report.json`), olvashatatlan, `sample_id` kulcs
    nélküli (régi formátum: a minta ismeretlen), vagy `evaluate_resume()` nem
    RESUME_ALLOWED-ot ad, az eredmény `None` -- régebbi sessionre NINCS
    visszaesés. `None` esetén a hívónak új sessiont kell indítania; a
    megszakadt session(ök) csak az induláskori review-listában látszanak.

    R1 F-15 (DFAM-003 2. előfordulás, SPR): korábban a `discover_incomplete_
    sessions()` listáján lépkedett -- (1) egy blokkolt újabb jelöltről
    továbblépett egy RÉGEBBI engedélyezettre; (2) a lefutott újabb session ki
    sem került a listába; (3) a teljes név szerinti rendezés csak azonos
    modellprefixen belül kronologikus. A padon lévő akku a LEGÚJABB futás
    állapotában van, ezért egy régebbi checkpoint felajánlása elavult
    Ah/lépés-seedet adna.

    Szűkített hatókör (NEM a teljes session-halmaz): checkpoint.json NÉLKÜLI
    mappát nem lehet mintához rendelni, ezért kihagyjuk. Nyitott maradék: egy
    újabb, az első checkpoint-írás ELŐTT megszakadt session (az ablak NEM
    garantáltan egytickes -- inicializálás és ismételt checkpoint-íráshiba is
    meghosszabbíthatja) nem blokkolja a régebbi jelöltet. Időbélyeg nélküli
    mappanév a legrégebbinek számít (dokumentált kompatibilitási szabály).

    R2-B7 (R2-SES-02, DFAM-003 3. előfordulás, SPR): más mintájú újabb
    session CSAK akkor léphető át, ha MINDKÉT minta azonosított (nem üres,
    nem csak whitespace, string -- l. `_sample_identity_known()`); különben
    az eredmény `None`. Mindkét irány: (1) azonosítatlan jelenlegi minta +
    azonosított újabb session; (2) szimmetrikus D/H ág: azonosított jelenlegi
    minta + azonosítatlan (üres/whitespace/`None`/nem-string `sample_id`-jű)
    újabb session. Azonosítatlan mintánál nem bizonyítható, hogy az újabb
    futás MÁS fizikai akkun ment, ezért az átlépés ugyanaz a régebbi
    sessionre való visszaesés volna, amit az F-15 megszüntetett. Az üres--üres
    egyezés továbbra is jelölt (a GUI-hívó ilyenkor külön figyelmeztet); az
    üres mintás resume teljes tiltása policy-kérdés, nem része ennek a fixnek.

    `total_step_count`: továbbadva az evaluate_resume()-nak, KÖTELEZŐ (l. ott
    a BLOCKED_INVALID_STEP_INDEX kapu indoklását és a kötelezővé tétel okát).
    """
    if not sessions_root.exists() or not sessions_root.is_dir():
        return None
    session_dirs = sorted(
        (p for p in sessions_root.iterdir() if p.is_dir()),
        key=_session_dir_sort_key,
        reverse=True,
    )
    for session_dir in session_dirs:
        checkpoint_path = session_dir / "checkpoint.json"
        if not checkpoint_path.exists():
            continue
        checkpoint, unreadable = _read_checkpoint(checkpoint_path)
        if unreadable or "sample_id" not in checkpoint:
            return None
        if checkpoint["sample_id"] != current_sample_id:
            # R2-B7 (R2-SES-02, DFAM-003 3. előfordulás, SPR): más mintájú újabb
            # session átlépése CSAK akkor biztonságos, ha MINDKÉT minta azonosított.
            if not (
                _sample_identity_known(current_sample_id)
                and _sample_identity_known(checkpoint["sample_id"])
            ):
                return None
            continue
        if (session_dir / "report.json").exists():
            return None
        evaluation = evaluate_resume(
            checkpoint,
            current_config_hash,
            current_profile_hash,
            current_instrument_identities,
            current_sample_id,
            total_step_count,
        )
        if not evaluation.allowed:
            return None
        return {
            "session_dir": session_dir,
            "checkpoint": checkpoint,
            "unreadable": False,
            "evaluation": evaluation,
        }
    return None
