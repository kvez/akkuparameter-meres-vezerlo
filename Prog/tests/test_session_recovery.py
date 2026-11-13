"""
session_recovery unit tesztek — WP-AK-02 (GAP-OLD-003, REC-001..004).
A Konszolidált terv WP-AK-02 Kapu fault-injection mátrixának mind az 5 esete
lefedve a tiszta döntési rétegen (evaluate_resume).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from Prog.src.session_recovery import (
    _PROFILE_FIELDS_INTENTIONALLY_NOT_HASHED,
    PROFILE_HASH_FIELDS,
    ResumeDecision,
    compute_config_hash,
    compute_profile_hash,
    discover_incomplete_sessions,
    evaluate_resume,
    find_resumable_session,
)


@dataclass
class _FakeConfig:
    battery_profile_name: str = "FIAMM_12V"
    battery_model: str = "TEST7AH"
    nominal_capacity_ah: float = 7.2
    sample_id: str = "S001"
    psu_mode: str = "INDEPENDENT"
    test_type: str = "CHARACTERIZATION"
    temperature_compensation_mode: str = "MONITOR_ONLY"
    charge_current_A_override: float = 0.0
    discharge_current_A: float = 0.0
    discharge_terminate_voltage_V: float = 0.0
    temperature_sensor_mode: str = "RTD_4W_PT100"
    # R2-04 (2026-09-15): CONFIG_HASH_FIELDS-be felvett mezők.
    discharge_rate_divisor: int = 5
    golden_gg_system_rate_current_A: float = 0.0
    ocv_soc_step_percent: float = 5.0
    # R2-04-F2 (2026-09-16): CONFIG_HASH_FIELDS-be felvett mező.
    relax_after_charge_s: float = 600.0
    discharge_rate_origin: object = None
    unrelated_field: str = "irrelevant"


@dataclass
class _FakeProfile:
    battery_name: str = "Test"
    manufacturer: str = "FIAMM"
    model: str = "FG"
    nominal_voltage_V: float = 12.0
    cell_count: int = 6
    nominal_capacity_Ah: float = 7.2
    chemistry: str = "AGM_VRLA"
    charge_voltage_per_cell_V: float = 2.40
    terminate_voltage_per_cell_V: float = 1.80
    float_voltage_per_cell_V: float = 2.27
    # R2-04 (2026-09-15): PROFILE_HASH_FIELDS-be felvett mezők.
    C10_capacity_Ah: float | None = None
    i10_discharge_current_A: float | None = None
    max_charge_current_A: float | None = None
    taper_current_A: float | None = None
    temp_comp_mV_per_cell_per_degC: float = -2.5
    test_temperature_min_C: float = 15.0
    test_temperature_max_C: float = 30.0


_IDENTITIES = {
    "PSU": "KEITHLEY INSTRUMENTS,MODEL 2220-30-1,MOCK,1.0",
    "Load": "KEITHLEY INSTRUMENTS,MODEL 2380-120-60,MOCK,1.0",
    "DMM_V": "KEYSIGHT TECHNOLOGIES,34465A,MOCK,A.02.00",
    "DMM_T": "KEYSIGHT TECHNOLOGIES,34465A,MOCK,A.02.00",
}


def _valid_checkpoint(**overrides) -> dict:
    base = {
        "safe_boundary": True,
        "config_hash": compute_config_hash(_FakeConfig()),
        "profile_hash": compute_profile_hash(_FakeProfile()),
        "instrument_identities": dict(_IDENTITIES),
        "sample_id": "S001",
        # Governance-v2 review-kör (MAX verifier O-3/F5): a folytatási pont
        # MINDEN valódi checkpointban jelen van (test_runner.py::
        # _checkpoint_traceability_fields() feltétel nélkül beleírja) -- a
        # fixture ezt a TÉNYLEGES checkpoint-formátumot tükrözi, nem egy
        # minimalizált feltevést.
        "next_safe_step_index": 1,
    }
    base.update(overrides)
    return base


class TestComputeHashes:
    def test_config_hash_stable_for_same_values(self):
        assert compute_config_hash(_FakeConfig()) == compute_config_hash(_FakeConfig())

    def test_config_hash_changes_on_relevant_field(self):
        h1 = compute_config_hash(_FakeConfig())
        h2 = compute_config_hash(_FakeConfig(sample_id="S002"))
        assert h1 != h2

    def test_config_hash_changes_on_temperature_sensor_mode(self):
        """[regresszió, review-kör 5, Architektúra A-2 (P2, DFAM-003)] a
        hőmérséklet-mérőlánc fizikai konfigurációja (pl. sérült PT100
        helyett NTC-re váltás) korábban NEM volt része a hash-nek --
        holott a *IDN? azonos marad (ugyanaz a fizikai 34465A DMM szolgál
        ki mindkét szenzortípust), tehát az egyetlen műszer-azonosság-
        alapú kapu sem kapta volna el a cserét."""
        h1 = compute_config_hash(_FakeConfig(temperature_sensor_mode="RTD_4W_PT100"))
        h2 = compute_config_hash(
            _FakeConfig(temperature_sensor_mode="THERMISTOR_2W_NTC_10K_B3950")
        )
        assert h1 != h2

    def test_config_hash_ignores_irrelevant_field(self):
        """A hash-nek NEM szabad megváltoznia egy biztonság szempontjából
        irreleváns mező miatt (pl. egy jövőbeli, itt nem szereplő mező) —
        itt azt igazoljuk, hogy a whitelist-alapú hash stabil marad, ha egy
        nem-whitelistelt mező változik."""
        h1 = compute_config_hash(_FakeConfig())
        h2 = compute_config_hash(_FakeConfig(unrelated_field="changed"))
        assert h1 == h2

    def test_profile_hash_stable_for_same_values(self):
        assert compute_profile_hash(_FakeProfile()) == compute_profile_hash(_FakeProfile())

    def test_profile_hash_changes_on_cell_count(self):
        h1 = compute_profile_hash(_FakeProfile())
        h2 = compute_profile_hash(_FakeProfile(cell_count=12, nominal_voltage_V=24.0))
        assert h1 != h2

    def test_config_hash_changes_on_discharge_rate_divisor(self):
        """R2-04 (2026-09-15, DFAM-003 3. előfordulása): a kisütőáram
        alap-számításának osztója korábban NEM volt része a hash-nek."""
        h1 = compute_config_hash(_FakeConfig(discharge_rate_divisor=5))
        h2 = compute_config_hash(_FakeConfig(discharge_rate_divisor=10))
        assert h1 != h2

    def test_config_hash_changes_on_golden_gg_system_rate_current(self):
        h1 = compute_config_hash(_FakeConfig(golden_gg_system_rate_current_A=0.0))
        h2 = compute_config_hash(_FakeConfig(golden_gg_system_rate_current_A=1.5))
        assert h1 != h2

    def test_config_hash_changes_on_ocv_soc_step_percent(self):
        h1 = compute_config_hash(_FakeConfig(ocv_soc_step_percent=5.0))
        h2 = compute_config_hash(_FakeConfig(ocv_soc_step_percent=10.0))
        assert h1 != h2

    def test_config_hash_changes_on_relax_after_charge_s(self):
        """R2-04-F2 (2026-09-16, DFAM-003 sibling): a resume KIZÁRÓLAG
        RELAX-határról indulhat, és CHARGE_ONLY módban éppen ez a mező
        adja a RELAX időtartamát -- a kezelő a crash és a resume KÖZÖTT
        szabadon átállíthatta a spinboxot, és a folytatott session a
        checkpoint-időbeli helyett az ÚJ értékkel relaxált volna, azonos
        config_hash mellett. Mutáció, amin bukik: a mező eltávolítása a
        CONFIG_HASH_FIELDS tuple-ből."""
        h1 = compute_config_hash(_FakeConfig(relax_after_charge_s=600.0))
        h2 = compute_config_hash(_FakeConfig(relax_after_charge_s=1800.0))
        assert h1 != h2

    def test_profile_hash_changes_on_c10_capacity(self):
        """R2-04 (2026-09-15, DFAM-003 3. előfordulása): a C10/I10-alapú
        IEC DC belső ellenállás terhelési pontok forrásmezői korábban
        NEM voltak része a hash-nek."""
        h1 = compute_profile_hash(_FakeProfile(C10_capacity_Ah=6.0))
        h2 = compute_profile_hash(_FakeProfile(C10_capacity_Ah=7.2))
        assert h1 != h2

    def test_profile_hash_changes_on_i10_discharge_current(self):
        h1 = compute_profile_hash(_FakeProfile(i10_discharge_current_A=0.62))
        h2 = compute_profile_hash(_FakeProfile(i10_discharge_current_A=0.67))
        assert h1 != h2

    def test_profile_hash_changes_on_max_charge_current(self):
        h1 = compute_profile_hash(_FakeProfile(max_charge_current_A=None))
        h2 = compute_profile_hash(_FakeProfile(max_charge_current_A=2.0))
        assert h1 != h2

    def test_profile_hash_changes_on_taper_current(self):
        h1 = compute_profile_hash(_FakeProfile(taper_current_A=None))
        h2 = compute_profile_hash(_FakeProfile(taper_current_A=0.1))
        assert h1 != h2

    def test_profile_hash_changes_on_temp_comp_mv_per_cell(self):
        h1 = compute_profile_hash(_FakeProfile(temp_comp_mV_per_cell_per_degC=-2.5))
        h2 = compute_profile_hash(_FakeProfile(temp_comp_mV_per_cell_per_degC=-3.0))
        assert h1 != h2

    def test_profile_hash_changes_on_test_temperature_min(self):
        """MAX verifier F1 (2026-09-15): az eredeti teszt mindkét korlátot
        egyszerre változtatta, ezért egyik korlát ÖNÁLLÓ elvesztését sem
        tudta volna elkapni (mutációval igazolva: a min/max mező egyenkénti
        eldobása mellett a régi kombinált teszt teljesen zöld maradt volna)."""
        h1 = compute_profile_hash(_FakeProfile(test_temperature_min_C=15.0))
        h2 = compute_profile_hash(_FakeProfile(test_temperature_min_C=10.0))
        assert h1 != h2

    def test_profile_hash_changes_on_test_temperature_max(self):
        h1 = compute_profile_hash(_FakeProfile(test_temperature_max_C=30.0))
        h2 = compute_profile_hash(_FakeProfile(test_temperature_max_C=35.0))
        assert h1 != h2


class TestProfileHashFieldsMatchRealDataclass:
    """R2-04 (2026-09-15, P1): meta-teszt `dataclasses.fields()`
    introspekcióval -- megelőzi, hogy a PROFILE_HASH_FIELDS whitelist egy
    elgépelt/átnevezett mezőnevet tartalmazzon, amit a fail-closed
    `_stable_hash()` egy VALÓDI `BatteryProfile`-lal azonnal AttributeError-
    ra váltana. `battery_profile.py` PySide6-mentes, ezért ez a meta-teszt
    itt, a nem-GUI suite-ban futhat (l. `test_session_config.py` a
    `SessionConfig`/CONFIG_HASH_FIELDS GUI-oldali analóg meta-tesztjéért)."""

    def test_every_profile_hash_field_exists_on_battery_profile(self):
        import dataclasses

        from Prog.src.battery_profile import BatteryProfile

        real_field_names = {f.name for f in dataclasses.fields(BatteryProfile)}
        missing = set(PROFILE_HASH_FIELDS) - real_field_names
        assert not missing, f"PROFILE_HASH_FIELDS elgepelt/nemletezo mezot tartalmaz: {missing}"

    def test_every_battery_profile_field_is_hashed_or_explicitly_excluded(self):
        """R2-04-F7 (2026-09-16, DFAM-003 STRUCTURAL_PREVENTION_REVIEW):
        a MÁSIK irány -- egy ÚJ BatteryProfile mező mostantól nem tud
        csendben a "nem hashelt" defaultra esni. Mutáció, amin bukik: egy
        új mező felvétele a dataclass-ba anélkül, hogy a PROFILE_HASH_
        FIELDS-be vagy a _PROFILE_FIELDS_INTENTIONALLY_NOT_HASHED-be
        kerülne."""
        import dataclasses

        from Prog.src.battery_profile import BatteryProfile

        real_field_names = {f.name for f in dataclasses.fields(BatteryProfile)}
        classified = set(PROFILE_HASH_FIELDS) | set(_PROFILE_FIELDS_INTENTIONALLY_NOT_HASHED)
        unclassified = real_field_names - classified
        assert not unclassified, (
            f"BatteryProfile uj mezo(k) nincs(nek) osztalyozva: {unclassified} -- "
            "vagy PROFILE_HASH_FIELDS-be, vagy _PROFILE_FIELDS_INTENTIONALLY_"
            "NOT_HASHED-be kell kerulniuk, indoklassal"
        )


class TestStableHashFailClosed:
    """R2-04 (2026-09-15, P1, DFAM-003 3. előfordulása): `_stable_hash()`
    korábban `getattr(obj, f, None)`-nel csendben None-t hashelt egy
    hiányzó/törölt/átnevezett mezőre -- egy jövőbeli refaktor emiatt
    ÉSZREVÉTLENÜL aláásta volna a hash-gate saját védelmét."""

    def test_missing_config_field_raises_instead_of_silently_hashing_none(self):
        @dataclass
        class _IncompleteConfig:
            battery_profile_name: str = "FIAMM_12V"
            # A tobbi CONFIG_HASH_FIELDS mezo SZANDEKOSAN hianyzik.

        with pytest.raises(AttributeError):
            compute_config_hash(_IncompleteConfig())

    def test_missing_profile_field_raises_instead_of_silently_hashing_none(self):
        @dataclass
        class _IncompleteProfile:
            battery_name: str = "Test"
            # A tobbi PROFILE_HASH_FIELDS mezo SZANDEKOSAN hianyzik.

        with pytest.raises(AttributeError):
            compute_profile_hash(_IncompleteProfile())

    def test_present_but_none_valued_field_does_not_raise(self):
        """A fail-closed kizarolag a HIANYZO attributumot tiltja -- egy
        LEGITIM None ertek (pl. C10_capacity_Ah meg nincs megadva egy
        profilnal) tovabbra is szabalyosan hashelheto, nem hiba."""
        compute_profile_hash(_FakeProfile(C10_capacity_Ah=None))


class TestEvaluateResumeGateScenarios:
    """A Konszolidált terv WP-AK-02 Kapu fault-injection mátrixának 5 esete."""

    def test_crash_mid_charge_unsafe_boundary_blocks(self):
        """'crash charge közben → resume blocked'"""
        checkpoint = _valid_checkpoint(safe_boundary=False)
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_UNSAFE_BOUNDARY
        assert not result.allowed

    def test_crash_at_relax_boundary_allows_controlled_resume(self):
        """'crash relax boundary-n → controlled resume'"""
        checkpoint = _valid_checkpoint(safe_boundary=True)
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.RESUME_ALLOWED
        assert result.allowed

    def test_resume_allowed_for_checkpoint_without_last_safe_off_result(self):
        """[regresszió, Kör 17 MAX-effort review, R17-03+V4-1] a
        `last_safe_off_result` mező SZÁNDÉKOSAN NEM tagja a
        `_REQUIRED_CHECKPOINT_KEYS`-nek -- egy régi (a mező bevezetése
        ELŐTT írt) checkpoint.json enélkül is resume-olhato kell
        maradjon. `_valid_checkpoint()` maga sem tartalmazza ezt a
        kulcsot -- ez a teszt éppen ezt a hiányt vizsgálja explicit."""
        checkpoint = _valid_checkpoint(safe_boundary=True)
        assert "last_safe_off_result" not in checkpoint
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.RESUME_ALLOWED
        assert result.allowed

    def test_config_changed_blocks(self):
        """'config changed → blocked'"""
        checkpoint = _valid_checkpoint(safe_boundary=True)
        different_config_hash = compute_config_hash(_FakeConfig(nominal_capacity_ah=10.0))
        result = evaluate_resume(
            checkpoint, different_config_hash,
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_CONFIG_MISMATCH

    def test_resume_blocked_when_temperature_sensor_mode_differs(self):
        """[regresszió, review-kör 5, Architektúra A-2 (P2, DFAM-003)] l.
        TestComputeHashes.test_config_hash_changes_on_temperature_sensor_
        mode -- itt a teljes evaluate_resume() kapun át igazolva: egy
        PT100->NTC szenzorváltás önmagában, minden más mező azonossága
        mellett is BLOCKED_CONFIG_MISMATCH-et kell adjon."""
        checkpoint = _valid_checkpoint(
            safe_boundary=True,
            config_hash=compute_config_hash(
                _FakeConfig(temperature_sensor_mode="RTD_4W_PT100")
            ),
        )
        current_config_hash = compute_config_hash(
            _FakeConfig(temperature_sensor_mode="THERMISTOR_2W_NTC_10K_B3950")
        )
        result = evaluate_resume(
            checkpoint, current_config_hash,
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_CONFIG_MISMATCH

    def test_wrong_battery_sample_blocks(self):
        """'wrong battery/sample → blocked'"""
        checkpoint = _valid_checkpoint(safe_boundary=True, sample_id="S001")
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S999_DIFFERENT_BATTERY",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_SAMPLE_MISMATCH

    def test_instrument_changed_blocks(self):
        """'instrument changed → blocked'"""
        checkpoint = _valid_checkpoint(safe_boundary=True)
        different_identities = dict(_IDENTITIES, PSU="KEITHLEY INSTRUMENTS,MODEL 2220-30-1,DIFFERENT_SERIAL,1.0")
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), different_identities, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_INSTRUMENT_MISMATCH


class TestEvaluateResumeAdditionalCases:
    def test_profile_changed_blocks(self):
        checkpoint = _valid_checkpoint(safe_boundary=True)
        different_profile_hash = compute_profile_hash(_FakeProfile(cell_count=12, nominal_voltage_V=24.0))
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            different_profile_hash, _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_PROFILE_MISMATCH

    def test_missing_safe_boundary_key_defaults_to_unsafe(self):
        """Fail-safe: ha a checkpoint-ban egyáltalán nincs safe_boundary kulcs
        (pl. egy régi, WP-AK-02 ELŐTTI formátumú checkpoint), az NEM tekinthető
        biztonságosnak — sosem szabad hallgatólagosan True-nak feltételezni."""
        checkpoint = {"config_hash": "x", "profile_hash": "y"}
        result = evaluate_resume(checkpoint, "x", "y", _IDENTITIES, "S001", total_step_count=99)
        assert result.decision == ResumeDecision.BLOCKED_UNSAFE_BOUNDARY

    def test_old_format_checkpoint_without_required_fields_blocks(self):
        """Egy WP-AK-01-előtti (vagy annál régebbi) checkpoint, ami safe_boundary=True-t
        tartalmaz, de a többi új mező hiányzik — fail-safe blokkolás, nem feltételezés."""
        checkpoint = {"safe_boundary": True, "status": "RUNNING", "step": "relax"}
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_MISSING_CHECKPOINT_DATA

    def test_empty_sample_id_key_present_is_legitimate_not_missing(self):
        """A sample_id LEGITIM lehet üres string (operátor nem adott meg
        minta-azonosítót) — ez NEM tekinthető hiányos/régi checkpointnak,
        amíg a kulcs ténylegesen jelen van."""
        checkpoint = _valid_checkpoint(safe_boundary=True, sample_id="")
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "",
        total_step_count=99,
        )
        assert result.allowed

    def test_empty_instrument_identities_dict_blocks_as_missing(self):
        """Üres instrument_identities dict SOSEM legitim — mindig ténylegesen
        lekérdezett *IDN? eredményeket kell tartalmazzon egy valós session-ben."""
        checkpoint = _valid_checkpoint(safe_boundary=True, instrument_identities={})
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), {}, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_MISSING_CHECKPOINT_DATA

    def test_everything_matches_allows(self):
        checkpoint = _valid_checkpoint(safe_boundary=True)
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.allowed


class TestEvaluateResumeTerminalCheckpointBlocks:
    """[regresszió, Teljes kódbázis review 20. kör (2026-09-06), Session/
    Workflow P1 lelet] egy checkpoint_is_terminal=True checkpoint (a terv
    VÉGÉn készült — pl. a BQ_LEARNING_PHYSICAL terv egyetlen, mindig
    utolsó MANUAL_CHECKPOINT lépése) sosem ajánlható folytatásra, MÉG
    AKKOR SEM, ha minden más feltétel (safe_boundary, hash-ek, műszer,
    minta) egyezik — CLAUDE.md §14 "terminális checkpointnál a GUI ne
    kínáljon hamis folytatást"."""

    def test_terminal_checkpoint_blocks_even_with_everything_else_matching(self):
        checkpoint = _valid_checkpoint(safe_boundary=True, checkpoint_is_terminal=True)
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_ALREADY_TERMINAL
        assert not result.allowed

    def test_non_terminal_checkpoint_still_allows(self):
        """Visszafelé kompatibilitás: egy régi-formátumú (a mező hiányzik,
        getattr fallback False-ra) vagy explicit nem-terminális checkpoint
        változatlanul folytatható marad."""
        checkpoint = _valid_checkpoint(safe_boundary=True, checkpoint_is_terminal=False)
        result = evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result.allowed


class TestEvaluateResumeInvalidStepIndexBlocks:
    """[regresszió, governance-v2 review-kör, MAX verifier O-3 + F5 (P2,
    DFAM-001)] a `next_safe_step_index` korábban SEMMILYEN validáción nem
    ment át ezen a döntési rétegen: a GUI egy `.get(..., 0)` alapértékkel
    adta tovább a TestRunner.run()-nak, ami tartományon kívüli indexnél
    (CF-44/F9 fix óta biztonságosan, de) AZONNAL FAULT-ol -- miközben a
    program már csatlakoztatta a műszereket, új session-mappát nyitott, és
    a SZÜLŐ session checkpointját véglegesen `RESUMED_INTO_NEW_SESSION`-ként
    megjelölte, azaz az operátor haladása egy soha el nem indult
    "folytatás" miatt véglegesen elveszett."""

    def _evaluate(self, checkpoint, total_step_count=99):
        return evaluate_resume(
            checkpoint, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
            total_step_count,
        )

    def test_index_beyond_current_plan_length_blocks(self):
        """A defektet ténylegesen elérhetővé tevő eset: a checkpoint egy 9
        lépéses tervhez készült, a MOST felépített terv 4 lépéses (a
        config_hash a terv lépésszámát NEM fedi le)."""
        checkpoint = _valid_checkpoint(next_safe_step_index=8)
        result = self._evaluate(checkpoint, total_step_count=4)
        assert result.decision == ResumeDecision.BLOCKED_INVALID_STEP_INDEX
        assert not result.allowed

    def test_index_equal_to_plan_length_blocks(self):
        """Határeset: len(steps) MÁR tartományon kívüli (0-alapú index) --
        a test_runner.py::run() kapuja is `>= len(test_plan.steps)`-re bukik."""
        checkpoint = _valid_checkpoint(next_safe_step_index=4)
        result = self._evaluate(checkpoint, total_step_count=4)
        assert result.decision == ResumeDecision.BLOCKED_INVALID_STEP_INDEX

    def test_last_valid_index_still_allows(self):
        """A kapu NEM lehet túl szigorú: az utolsó lépésre mutató index
        (len-1) legitim folytatási pont."""
        checkpoint = _valid_checkpoint(next_safe_step_index=3)
        result = self._evaluate(checkpoint, total_step_count=4)
        assert result.allowed

    def test_negative_index_blocks_even_without_plan_length(self):
        """A -1 a test_runner.py NEM-safe-boundary checkpointjainak jelzője
        (l. _checkpoint_traceability_fields hívások) -- ha egy ilyen érték
        safe_boundary=True mellé kerülne, sosem szabad folytatni."""
        checkpoint = _valid_checkpoint(next_safe_step_index=-1)
        result = self._evaluate(checkpoint)
        assert result.decision == ResumeDecision.BLOCKED_INVALID_STEP_INDEX

    def test_non_integer_index_blocks(self):
        checkpoint = _valid_checkpoint(next_safe_step_index="3")
        result = self._evaluate(checkpoint)
        assert result.decision == ResumeDecision.BLOCKED_INVALID_STEP_INDEX

    def test_missing_index_blocks_as_missing_checkpoint_data(self):
        """Hiányzó folytatási pont: fail-safe blokkolás, nem hallgatólagos
        0 (ami a MÁR lefutott lépéseket ismételné meg a checkpointból
        átvett, nem-nulla Ah/Wh összegek mellett)."""
        checkpoint = _valid_checkpoint()
        del checkpoint["next_safe_step_index"]
        result = self._evaluate(checkpoint, total_step_count=4)
        assert result.decision == ResumeDecision.BLOCKED_MISSING_CHECKPOINT_DATA

    def test_find_resumable_session_skips_invalid_step_index(self, tmp_path):
        """A teljes felderítő úton: egy "mérgezett" checkpoint (tartományon
        kívüli index) egyáltalán NE kerüljön felajánlásra -- így a szülő
        session sem jelölődik meg resumed-ként, és nem indul el egy
        garantáltan FAULT-ra futó session."""
        session_dir = tmp_path / "session_POISONED"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text(
            json.dumps(_valid_checkpoint(next_safe_step_index=8)), encoding="utf-8"
        )
        result = find_resumable_session(
            tmp_path, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
            total_step_count=4,
        )
        assert result is None


class TestDiscoverIncompleteSessions:
    def test_empty_root_returns_empty_list(self, tmp_path):
        assert discover_incomplete_sessions(tmp_path / "nonexistent") == []

    def test_session_with_checkpoint_and_no_report_is_incomplete(self, tmp_path):
        session_dir = tmp_path / "session_A"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text(
            json.dumps({"safe_boundary": True}), encoding="utf-8"
        )
        results = discover_incomplete_sessions(tmp_path)
        assert len(results) == 1
        assert results[0]["session_dir"] == session_dir
        assert results[0]["checkpoint"]["safe_boundary"] is True

    def test_session_with_checkpoint_and_report_is_not_incomplete(self, tmp_path):
        """DATA-003 (WP-AK-01) óta minden terminális állapot report.json-t ír —
        ha van report.json, a session NEM tekinthető megszakadtnak."""
        session_dir = tmp_path / "session_B"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text("{}", encoding="utf-8")
        (session_dir / "report.json").write_text("{}", encoding="utf-8")
        assert discover_incomplete_sessions(tmp_path) == []

    def test_session_without_checkpoint_is_not_incomplete(self, tmp_path):
        session_dir = tmp_path / "session_C"
        session_dir.mkdir()
        assert discover_incomplete_sessions(tmp_path) == []

    def test_corrupt_checkpoint_json_does_not_crash(self, tmp_path):
        session_dir = tmp_path / "session_D"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text("{not valid json", encoding="utf-8")
        results = discover_incomplete_sessions(tmp_path)
        assert len(results) == 1
        assert results[0]["checkpoint"] == {}

    def test_multiple_incomplete_sessions_all_found(self, tmp_path):
        for name in ("session_A", "session_B", "session_C"):
            d = tmp_path / name
            d.mkdir()
            (d / "checkpoint.json").write_text("{}", encoding="utf-8")
        results = discover_incomplete_sessions(tmp_path)
        assert len(results) == 3


class TestDiscoverIncompleteSessionsUnreadableFlag:
    """Kor 18, Batch C7 R17-06 (DFAM-018+DFAM-004): egy csonka/olvashatatlan
    checkpoint.json korabban ugyanazt a `{}`-t adta, mint egy maradvany
    NELKULI, ep checkpoint -- a "nem olvashato" eset csendben "nincs mire
    figyelmeztetni"-ve omlott ossze, elrejtve a BQ-maradvany/safe-off
    figyelmeztetest is. Uj "unreadable" kulcs KULON csatorna."""

    def test_truncated_checkpoint_is_flagged_unreadable(self, tmp_path):
        session_dir = tmp_path / "session_E"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text(
            '{"status": "RUN', encoding="utf-8"
        )
        results = discover_incomplete_sessions(tmp_path)
        assert len(results) == 1
        assert results[0]["unreadable"] is True
        assert results[0]["checkpoint"] == {}

    def test_valid_checkpoint_without_residual_is_not_flagged(self, tmp_path):
        """A discriminator: egy TELJESEN ep, maradvany-mentes checkpoint
        NEM kaphat unreadable=True jelzest."""
        session_dir = tmp_path / "session_F"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text(
            json.dumps({"safe_boundary": True, "step": "DONE"}), encoding="utf-8"
        )
        results = discover_incomplete_sessions(tmp_path)
        assert len(results) == 1
        assert results[0]["unreadable"] is False

    def test_non_object_json_is_flagged_unreadable(self, tmp_path):
        session_dir = tmp_path / "session_G"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text("[1, 2, 3]", encoding="utf-8")
        results = discover_incomplete_sessions(tmp_path)
        assert len(results) == 1
        assert results[0]["unreadable"] is True
        assert results[0]["checkpoint"] == {}

    def test_checkpoint_truncated_mid_multibyte_utf8_char_is_flagged_unreadable(
        self, tmp_path
    ):
        """Fuggetlen verifier lelete (Kor 18, Batch C7 SB4, P2-1): egy
        aramkimaradas TETSZOLEGES bajt-hataron csonkolhatja a fajlt --
        ha ez pont egy tobb-bajtos UTF-8 karakter (pl. magyar ekezet,
        logger.py::write_checkpoint() ensure_ascii=False-szal ir)
        KOZEPEN esik, read_text(encoding="utf-8") UnicodeDecodeError-t
        dob, NEM json.JSONDecodeError-t -- a korabbi szukebb except ezt
        athengedte volna, MainWindow.__init__()-et figyelmeztetes
        nelkul elszalasztva."""
        session_dir = tmp_path / "session_H"
        session_dir.mkdir()
        # "é" UTF-8-ban 2 bajt (\xc3\xa9) -- csak az elso bajtot irjuk ki.
        (session_dir / "checkpoint.json").write_bytes(
            b'{"step": "RELAX k\xc3'
        )
        results = discover_incomplete_sessions(tmp_path)
        assert len(results) == 1
        assert results[0]["unreadable"] is True
        assert results[0]["checkpoint"] == {}


class TestFindResumableSession:
    def test_no_incomplete_sessions_returns_none(self, tmp_path):
        result = find_resumable_session(
            tmp_path, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result is None

    def test_matching_incomplete_session_is_found(self, tmp_path):
        session_dir = tmp_path / "session_A"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text(
            json.dumps(_valid_checkpoint(safe_boundary=True)), encoding="utf-8"
        )
        result = find_resumable_session(
            tmp_path, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result is not None
        assert result["session_dir"] == session_dir
        assert result["evaluation"].allowed

    def test_unsafe_boundary_incomplete_session_not_resumable(self, tmp_path):
        session_dir = tmp_path / "session_A"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text(
            json.dumps(_valid_checkpoint(safe_boundary=False)), encoding="utf-8"
        )
        result = find_resumable_session(
            tmp_path, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result is None

    def test_mismatched_incomplete_session_not_resumable(self, tmp_path):
        session_dir = tmp_path / "session_A"
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text(
            json.dumps(_valid_checkpoint(safe_boundary=True, sample_id="OTHER_SAMPLE")),
            encoding="utf-8",
        )
        result = find_resumable_session(
            tmp_path, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result is None

    def test_prefers_newest_matching_checkpoint_over_oldest(self, tmp_path):
        """[regresszió, review-kör 5, Session/Workflow S5-02 (P2, DFAM-001)]
        ha ugyanahhoz az akkumulátorhoz/konfigurációhoz TÖBB, egymástól
        független megszakadt session tartozik (pl. az operátor egy korábbi
        resume-felajánlást elutasított, majd egy KÉSŐBBI session is
        megszakadt), a padon lévő akku fizikailag a LEGÚJABB megszakadás
        állapotában van -- a korábbi (mappanév szerint növekvő, tehát
        legrégebbi-először) sorrend egy órákkal/napokkal korábbi, elavult
        checkpointot ajánlott volna fel, csendben eldobva a közbenső
        mérési adatot."""
        older_dir = tmp_path / "session_S001_20260901_080000"
        older_dir.mkdir()
        (older_dir / "checkpoint.json").write_text(
            json.dumps(_valid_checkpoint(safe_boundary=True, charge_ah=41.2)),
            encoding="utf-8",
        )
        newer_dir = tmp_path / "session_S001_20260903_140000"
        newer_dir.mkdir()
        (newer_dir / "checkpoint.json").write_text(
            json.dumps(_valid_checkpoint(safe_boundary=True, charge_ah=44.9)),
            encoding="utf-8",
        )
        result = find_resumable_session(
            tmp_path, compute_config_hash(_FakeConfig()),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
        )
        assert result is not None
        assert result["session_dir"] == newer_dir
        assert result["checkpoint"]["charge_ah"] == 44.9


_R1F15_OLD = "session_TEST7AH_20260901_080000"
_R1F15_NEW = "session_TEST7AH_20260903_140000"
_R1F15_ABSENT = object()


def _r1f15_session(root, name, checkpoint=_R1F15_ABSENT, *, raw=None, report=False):
    """Teszt-segéd: session-mappa checkpoint.json-nal (dict -> JSON, `raw`
    -> nyers szöveg, egyik sem -> nincs checkpoint.json) és opcionális
    report.json-nal."""
    d = root / name
    d.mkdir(parents=True)
    if raw is not None:
        (d / "checkpoint.json").write_text(raw, encoding="utf-8")
    elif checkpoint is not _R1F15_ABSENT:
        (d / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    if report:
        (d / "report.json").write_text(json.dumps({"status": "DONE"}), encoding="utf-8")
    return d


def _r1f15_find(root, sample_id="S001"):
    return find_resumable_session(
        root, compute_config_hash(_FakeConfig()),
        compute_profile_hash(_FakeProfile()), _IDENTITIES, sample_id,
        total_step_count=99,
    )


class TestR1F15NewestSameSampleOnly:
    """[regresszió, R1 F-15, DFAM-003 2. előfordulás -> SPR] a
    find_resumable_session() korábban a discover_incomplete_sessions()
    listáján lépkedett: (1) egy BLOKKOLT újabb jelöltről továbblépett egy
    RÉGEBBI, engedélyezett sessionre; (2) a lefutott (report.json-os)
    újabb session ki sem került a listába, így egy régebbi, elavult
    checkpoint lett felajánlva, miközben a padon lévő akku a legújabb
    futás állapotában van; (3) a teljes mappanév szerinti rendezés csak
    azonos modellprefixen belül kronologikus. Új invariáns: CSAK a
    checkpointtal rendelkező sessionök közül az időben legújabb, azonos
    sample_id-jű lehet jelölt; ha az lefutott / nem folytatható /
    olvashatatlan / sample_id nélküli -> None, régebbire nincs
    visszaesés."""

    def test_1_newest_unsafe_boundary_blocks_older_allowed(self, tmp_path):
        _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        _r1f15_session(tmp_path, _R1F15_NEW, _valid_checkpoint(safe_boundary=False))
        assert _r1f15_find(tmp_path) is None

    def test_2_newest_completed_blocks_older_allowed(self, tmp_path):
        _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        _r1f15_session(
            tmp_path, _R1F15_NEW,
            _valid_checkpoint(status="DONE", checkpoint_is_terminal=True), report=True,
        )
        assert _r1f15_find(tmp_path) is None

    def test_3_newest_config_mismatch_blocks_older_allowed(self, tmp_path):
        _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        _r1f15_session(tmp_path, _R1F15_NEW, _valid_checkpoint(config_hash="OTHER_CONFIG"))
        assert _r1f15_find(tmp_path) is None

    def test_4_newest_completed_with_smaller_model_prefix_blocks(self, tmp_path):
        """A rendezés a mappanév végi időbélyegen alapul, nem a teljes
        néven: az 'AAA' modellprefixű, de IDŐBEN újabb lefutott session a
        névszerinti fordított sorrendben a 'ZZZ' mögé kerülne."""
        _r1f15_session(tmp_path, "session_ZZZ_20260901_080000", _valid_checkpoint())
        _r1f15_session(
            tmp_path, "session_AAA_20260903_140000",
            _valid_checkpoint(checkpoint_is_terminal=True), report=True,
        )
        assert _r1f15_find(tmp_path) is None

    def test_5_newest_unreadable_checkpoint_blocks(self, tmp_path):
        _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        _r1f15_session(tmp_path, _R1F15_NEW, raw="{truncated")
        assert _r1f15_find(tmp_path) is None

    def test_6_newest_allowed_is_returned(self, tmp_path):
        _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint(charge_ah=1.0))
        newer = _r1f15_session(tmp_path, _R1F15_NEW, _valid_checkpoint(charge_ah=2.0))
        result = _r1f15_find(tmp_path)
        assert result is not None
        assert result["session_dir"] == newer
        assert result["checkpoint"]["charge_ah"] == 2.0
        assert result["evaluation"].allowed
        assert result["unreadable"] is False

    def test_7_newest_other_sample_is_skipped(self, tmp_path):
        older = _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        _r1f15_session(
            tmp_path, _R1F15_NEW,
            _valid_checkpoint(sample_id="OTHER_SAMPLE", checkpoint_is_terminal=True),
            report=True,
        )
        result = _r1f15_find(tmp_path)
        assert result is not None
        assert result["session_dir"] == older

    def test_8_newest_without_checkpoint_is_skipped(self, tmp_path):
        """Dokumentált maradék: egy checkpoint.json NÉLKÜLI újabb mappa
        (pl. a session az első periodikus checkpoint előtt szakadt meg --
        ez az ablak NEM garantáltan egytickes: inicializálás és ismételt
        checkpoint-íráshiba is meghosszabbíthatja) nem blokkol, mert nincs
        miből megállapítani a mintát."""
        older = _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        _r1f15_session(tmp_path, _R1F15_NEW)
        result = _r1f15_find(tmp_path)
        assert result is not None
        assert result["session_dir"] == older

    def test_9_empty_sample_id_newest_completed_blocks(self, tmp_path):
        _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint(sample_id=""))
        _r1f15_session(
            tmp_path, _R1F15_NEW,
            _valid_checkpoint(sample_id="", checkpoint_is_terminal=True), report=True,
        )
        assert _r1f15_find(tmp_path, sample_id="") is None

    def test_10_single_allowed_session_is_returned(self, tmp_path):
        older = _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        result = _r1f15_find(tmp_path)
        assert result is not None
        assert result["session_dir"] == older

    def test_newest_without_sample_id_key_blocks(self, tmp_path):
        """[FINAL B7/1] a sample_id kulcs nélküli (régi formátumú)
        legújabb checkpoint mintája ismeretlen -> blokkol, nem lép tovább
        a régebbi, engedélyezett sessionre."""
        _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        legacy = _valid_checkpoint()
        del legacy["sample_id"]
        _r1f15_session(tmp_path, _R1F15_NEW, legacy)
        assert _r1f15_find(tmp_path) is None

    def test_missing_sessions_root_returns_none(self, tmp_path):
        assert _r1f15_find(tmp_path / "nonexistent") is None

    def test_sessions_root_that_is_a_file_returns_none(self, tmp_path):
        root = tmp_path / "Sessions"
        root.write_text("not a directory", encoding="utf-8")
        assert _r1f15_find(root) is None

    def test_stampless_dir_sorts_as_oldest(self, tmp_path):
        """Kompatibilitási szabály: időbélyeg nélküli mappanév a
        legrégebbinek számít -- egy névszerint NAGYOBB, de időbélyeg
        nélküli lefutott session nem előzi meg az időbélyeges jelöltet."""
        older = _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        _r1f15_session(
            tmp_path, "session_ZZZ_legacy",
            _valid_checkpoint(checkpoint_is_terminal=True), report=True,
        )
        result = _r1f15_find(tmp_path)
        assert result is not None
        assert result["session_dir"] == older

    def test_stampless_allowed_dir_loses_to_stamped_allowed_dir(self, tmp_path):
        stamped = _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint(charge_ah=3.0))
        _r1f15_session(tmp_path, "session_ZZZ_legacy", _valid_checkpoint(charge_ah=9.0))
        result = _r1f15_find(tmp_path)
        assert result is not None
        assert result["session_dir"] == stamped

    def test_discovery_output_unchanged_for_same_tree(self, tmp_path):
        """A discover_incomplete_sessions() kimenete (startup-dialógus)
        változatlan: lefutott session nincs benne, a sorrend névszerint
        fordított, az olvashatatlan jelölés megmarad."""
        _r1f15_session(tmp_path, _R1F15_OLD, _valid_checkpoint())
        _r1f15_session(tmp_path, _R1F15_NEW, raw="{truncated")
        _r1f15_session(tmp_path, "session_TEST7AH_20260904_000000",
                       _valid_checkpoint(), report=True)
        results = discover_incomplete_sessions(tmp_path)
        assert [r["session_dir"].name for r in results] == [_R1F15_NEW, _R1F15_OLD]
        assert [r["unreadable"] for r in results] == [True, False]
        assert results[0]["checkpoint"] == {}


def _r2b7_find(root, sample_id):
    return find_resumable_session(
        root, compute_config_hash(_FakeConfig(sample_id=sample_id)),
        compute_profile_hash(_FakeProfile()), _IDENTITIES, sample_id,
        total_step_count=99,
    )


def _r2b7_ckpt(sample_id, **overrides):
    return _valid_checkpoint(
        sample_id=sample_id,
        config_hash=compute_config_hash(_FakeConfig(sample_id=sample_id)),
        **overrides,
    )


class TestR2B7UnidentifiedSampleNeverSkipsNewer:
    """[regresszió, R2-B7 / R2-SES-02, DFAM-003 3. előfordulás -> SPR] a
    find_resumable_session() a más mintájú újabb sessiont akkor is átlépte,
    ha valamelyik minta AZONOSÍTATLAN volt (üres/whitespace/nem-string
    sample_id) -- így egy régebbi, elavult checkpointot ajánlott fel, pedig
    az újabb futás ugyanazon az akkun mehetett. Új invariáns: az átlépés
    CSAK két azonosított minta esetén megengedett, különben None."""

    @pytest.mark.parametrize("newer_report", [True, False])
    def test_empty_current_newer_identified_blocks_older_empty(self, tmp_path, newer_report):
        _r1f15_session(tmp_path, _R1F15_OLD, _r2b7_ckpt(""))
        _r1f15_session(
            tmp_path, _R1F15_NEW,
            _r2b7_ckpt("B", checkpoint_is_terminal=newer_report), report=newer_report,
        )
        assert _r2b7_find(tmp_path, "") is None

    def test_identified_current_newer_empty_blocks_older_same(self, tmp_path):
        _r1f15_session(tmp_path, _R1F15_OLD, _r2b7_ckpt("S001"))
        _r1f15_session(
            tmp_path, _R1F15_NEW,
            _r2b7_ckpt("", checkpoint_is_terminal=True), report=True,
        )
        assert _r2b7_find(tmp_path, "S001") is None

    @pytest.mark.parametrize("bad", [None, "   ", 0, 123])
    def test_identified_current_newer_non_identity_blocks(self, tmp_path, bad):
        _r1f15_session(tmp_path, _R1F15_OLD, _r2b7_ckpt("S001"))
        _r1f15_session(tmp_path, _R1F15_NEW, _valid_checkpoint(sample_id=bad))
        assert _r2b7_find(tmp_path, "S001") is None

    def test_control_identified_both_sides_still_skips(self, tmp_path):
        older = _r1f15_session(tmp_path, _R1F15_OLD, _r2b7_ckpt("S001"))
        _r1f15_session(
            tmp_path, _R1F15_NEW,
            _r2b7_ckpt("B", checkpoint_is_terminal=True), report=True,
        )
        result = _r2b7_find(tmp_path, "S001")
        assert result is not None and result["session_dir"] == older

    def test_control_empty_current_newest_empty_allowed_is_offered(self, tmp_path):
        _r1f15_session(tmp_path, _R1F15_OLD, _r2b7_ckpt("B"))
        newer = _r1f15_session(tmp_path, _R1F15_NEW, _r2b7_ckpt(""))
        result = _r2b7_find(tmp_path, "")
        assert result is not None and result["session_dir"] == newer
        assert result["evaluation"].allowed


class TestR16B16aPolicyHash:
    @pytest.mark.parametrize("mode", [
        "DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL",
        "CHEMID_CHARACTERIZATION_PBA", "OCV_SOC_CHARACTERIZATION", "GOLDEN_GG",
    ])
    def test_old_checkpoint_is_blocked(self, mode):
        from Prog.src.session_recovery import CONFIG_HASH_FIELDS, _stable_hash

        cfg = _FakeConfig(test_type=mode)
        legacy = _stable_hash(cfg, tuple(f for f in CONFIG_HASH_FIELDS if f != "discharge_rate_origin"))
        result = evaluate_resume(
            _valid_checkpoint(config_hash=legacy), compute_config_hash(cfg),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001", total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_CONFIG_MISMATCH

    def test_same_current_with_different_origin_changes_hash(self):
        from dataclasses import replace

        from Prog.src.battery_profile import DischargeRateOrigin

        cfg = _FakeConfig(discharge_current_A=0.36, discharge_rate_origin=DischargeRateOrigin(
            "C/20", "NOMINAL_C10_CAPACITY", 7.2,
        ))
        assert compute_config_hash(cfg) != compute_config_hash(replace(
            cfg, discharge_rate_origin=DischargeRateOrigin("CUSTOM", "NOMINAL_C10_CAPACITY", 7.2),
        ))

    def test_charge_only_hash_is_unchanged(self):
        from Prog.src.session_recovery import CONFIG_HASH_FIELDS, _stable_hash

        cfg = _FakeConfig(test_type="CHARGE_ONLY")
        assert compute_config_hash(cfg) == _stable_hash(
            cfg, tuple(f for f in CONFIG_HASH_FIELDS if f != "discharge_rate_origin"),
        )

    def test_policy_identifier_is_hashed(self, monkeypatch):
        from Prog.src import battery_profile

        cfg = _FakeConfig()
        before = compute_config_hash(cfg)
        monkeypatch.setattr(battery_profile, "NORMAL_DISCHARGE_POLICY_ID", "GOV-029/Q-34:next")
        assert compute_config_hash(cfg) != before


class TestR16B16cLearningPolicyHash:
    def test_old_tlc_checkpoint_is_blocked(self):
        from Prog.src.session_recovery import CONFIG_HASH_FIELDS, _stable_hash

        cfg = _FakeConfig(test_type="TRADITIONAL_LEARNING")
        legacy = _stable_hash(cfg, tuple(f for f in CONFIG_HASH_FIELDS if f != "discharge_rate_origin"))
        result = evaluate_resume(
            _valid_checkpoint(config_hash=legacy), compute_config_hash(cfg),
            compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001", total_step_count=99,
        )
        assert result.decision == ResumeDecision.BLOCKED_CONFIG_MISMATCH

    def test_tlc_steps_are_in_hash(self, monkeypatch):
        from Prog.src import traditional_learning_controller as tl

        cfg = _FakeConfig(test_type="TRADITIONAL_LEARNING")
        before = compute_config_hash(cfg)
        monkeypatch.setattr(tl, "LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL", (1.75, 1.74, 1.71, 1.70, 1.70))
        assert compute_config_hash(cfg) != before


class TestR16B7ResumePolicy:
    @pytest.mark.parametrize("mode", ["TRADITIONAL_LEARNING", "GOLDEN_GG"])
    def test_pre_b7_config_hash_cannot_resume(self, mode):
        import hashlib
        from dataclasses import asdict

        import Prog.src.session_recovery as recovery
        from Prog.src.battery_profile import (
            NORMAL_DISCHARGE_POLICY_ID,
            discharge_rate_origin_for_mode,
        )
        from Prog.src.traditional_learning_controller import (
            LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL,
            LEARNING_HARD_FLOOR_V_PER_CELL,
            LearningConfig,
        )

        cfg = _FakeConfig(test_type=mode)
        base = recovery._stable_hash(
            cfg, tuple(f for f in recovery.CONFIG_HASH_FIELDS if f != "discharge_rate_origin"),
        )
        if mode == "TRADITIONAL_LEARNING":
            # A HEAD 8a94185 tényleges payloadja; a régi checkpoint nem migrálható.
            payload = {
                "config": base, "policy": "GOV-029/Q-34/Q-35:R16-B16c",
                "learning_config": asdict(LearningConfig()),
                "cutoffs_V_per_cell": LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL,
                "hard_floor_V_per_cell": LEARNING_HARD_FLOOR_V_PER_CELL,
            }
        else:
            payload = {
                "config": base, "policy": NORMAL_DISCHARGE_POLICY_ID,
                "rate_origin": discharge_rate_origin_for_mode(
                    mode, cfg.discharge_rate_origin, cfg.nominal_capacity_ah,
                    cfg.discharge_rate_divisor,
                ),
            }
        old_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
        ).hexdigest()[:16]
        assert compute_config_hash(cfg) != old_hash
        profile_hash = compute_profile_hash(_FakeProfile())
        result = evaluate_resume(
            _valid_checkpoint(config_hash=old_hash, profile_hash=profile_hash),
            compute_config_hash(cfg), profile_hash, _IDENTITIES,
            current_sample_id="S001", total_step_count=5,
        )
        assert result.decision is ResumeDecision.BLOCKED_CONFIG_MISMATCH
        assert not result.allowed


def test_b23b_q42_old_tlc_hash_is_blocked():
    import hashlib
    from dataclasses import asdict

    from Prog.src.session_recovery import CONFIG_HASH_FIELDS, _stable_hash
    from Prog.src.traditional_learning_controller import (
        LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL,
        LEARNING_HARD_FLOOR_V_PER_CELL,
        LearningConfig,
    )
    cfg = _FakeConfig(test_type="TRADITIONAL_LEARNING")
    old_payload = json.dumps({
        "config": _stable_hash(cfg, tuple(f for f in CONFIG_HASH_FIELDS if f != "discharge_rate_origin")),
        "policy": "GOV-029/Q-34/Q-35:R16-B16c;Q-9/Q-22/Q-23:R16-B7",
        "learning_config": asdict(LearningConfig()),
        "cutoffs_V_per_cell": LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL,
        "hard_floor_V_per_cell": LEARNING_HARD_FLOOR_V_PER_CELL,
    }, sort_keys=True, default=str)
    old_hash = hashlib.sha256(old_payload.encode("utf-8")).hexdigest()[:16]
    current_hash = compute_config_hash(cfg)
    assert old_hash != current_hash
    cp = _valid_checkpoint(config_hash=old_hash)
    verdict = evaluate_resume(
        cp, current_hash, compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
    )
    assert verdict.decision == ResumeDecision.BLOCKED_CONFIG_MISMATCH


def test_b23b_q38_old_tlc_hash_is_blocked():
    import hashlib
    from dataclasses import asdict

    from Prog.src.session_recovery import CONFIG_HASH_FIELDS, _stable_hash
    from Prog.src.traditional_learning_controller import (
        LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL,
        LEARNING_HARD_FLOOR_V_PER_CELL,
        LearningConfig,
    )
    cfg = _FakeConfig(test_type="TRADITIONAL_LEARNING")
    old_payload = json.dumps({
        "config": _stable_hash(cfg, tuple(f for f in CONFIG_HASH_FIELDS if f != "discharge_rate_origin")),
        "policy": "GOV-029/Q-34/Q-35:R16-B16c;Q-9/Q-22/Q-23:R16-B7;Q-42:R16-B23b-1",
        "learning_config": asdict(LearningConfig()),
        "cutoffs_V_per_cell": LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL,
        "hard_floor_V_per_cell": LEARNING_HARD_FLOOR_V_PER_CELL,
    }, sort_keys=True, default=str)
    old_hash = hashlib.sha256(old_payload.encode("utf-8")).hexdigest()[:16]
    current_hash = compute_config_hash(cfg)
    assert old_hash != current_hash
    cp = _valid_checkpoint(config_hash=old_hash)
    verdict = evaluate_resume(
        cp, current_hash, compute_profile_hash(_FakeProfile()), _IDENTITIES, "S001",
        total_step_count=99,
    )
    assert verdict.decision == ResumeDecision.BLOCKED_CONFIG_MISMATCH
