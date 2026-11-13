"""
TraditionalLearningController tesztek — WP-AK-10 (SUP03-LEARN-001..010).

A BQ34Z110 oldalt `PicoBQTransport` + `FakePicoSerial`-lel szimuláljuk
(ugyanaz a minta, mint a WP-AK-09 `test_bq_gauge_calibration.py`-ban) — ez
FEDI mind az olvasó (chem_id/control_status/flags/data_flash_block), mind
az író (unseal/it_enable/seal) hívásokat egyetlen, állapottartó fake
eszközön keresztül. A fizikai oldalt (PSU/Load/DMM) a projekt meglévő
MockPSU/MockLoad/MockDMM fixture-ei szimulálják — a lassú, DMM-taper-alapú
ChargeController-t a `ChemIdCharacterizationController` teszt-mintája
szerint közvetlen állapot-befecskendezéssel (`ctrl._charge_ctrl._state =
ChargeState.CHARGE_DONE`) siettetjük át, nem valós taper-szimulációval.
"""
from __future__ import annotations

import pytest

from Prog.drivers.device_bq34z110 import DEGENERATE_SEALED_DF_BLOCK
from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.battery_profile import BatteryProfile
from Prog.src.bq_consistency_gate import build_expected_df_values
from Prog.src.bq_data_flash_service import FIELD_REGISTRY, decode_df_field, encode_df_field
from Prog.src.charge_controller import ChargeConfig, ChargeState
from Prog.src.relax_controller import RelaxConfig
from Prog.src.safety import PsuMode, SafetyManager, TempCompMode
from Prog.src.traditional_learning_controller import (
    LEARNING_RESULT_AWAITING,
    LEARNING_RESULT_FAILED,
    LEARNING_RESULT_VERIFIED,
    LearningConfig,
    LearningState,
    TraditionalLearningController,
)
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial
from Prog.tests.mock_drivers.mock_dmm import MockDMM
from Prog.tests.mock_drivers.mock_load import MockLoad
from Prog.tests.mock_drivers.mock_psu import MockPSU


class _FakeBqState:
    """SUP03-LEARN-004 forgatókönyv-vezérléséhez elegendő, minimál BQ34Z110
    állapot-modell — nem próbálja szimulálni a valós Impedance Track
    algoritmust, csak a KONTROLLER által olvasott mezőket teszi
    programozhatóvá/megfigyelhetővé."""

    def __init__(self) -> None:
        self.chem_id = 0x0800
        self.qmax = 100
        self.update_status = 0x00
        self.flags_word = 0x0000
        # [SCF-011 újratervezés, 2026-09-11] SEALED alapállapot (SS bit,
        # 0x2000, bq_diagnostics.py::ControlStatusBits.ss) -- realisztikus
        # gyári/session-előtti alapállapot, ugyanaz az elv, mint a MockPSU/
        # MockLoad sessionön-átívelő állapot-modellezése (.claude/rules/
        # 40-instruments.md). unseal()/full_access()/seal() ezt MOST
        # ténylegesen módosítja (l. lent set_sealed()), nem csak
        # call-számlálóként regisztrálódik -- ez teszi tesztelhetővé a
        # _ensure_bq_sealed_or_flag() session-végi fallback-zárását.
        self.control_status_word = 0x2000
        self.ra_table = [10.0 + i for i in range(15)]
        self.cycle_count = 0
        self.dod0 = 0
        self.calls: list[str] = []
        # Block 2 (2026-08-25) — a PROFILE<->BQ Data Flash konzisztencia-gate
        # (bq_consistency_gate.py) mezőnkénti olvasásait szolgálja ki. Üresen
        # hagyva a make_controller() a ténylegesen konstruált profilhoz
        # illeszkedő értékekkel tölti fel (l. _default_df_fields()) — egy
        # teszt, ami szándékos eltérést akar szimulálni, explicit felülírja
        # ELŐBB (make_controller(fake_bq=...) hívás ELŐTT).
        self.df_fields: dict[str, int] = {}
        # Block 3 (2026-08-25) — a Cell BL temp-write/restore ténylegesen
        # WRITE-ol egy DF blokkot; a ténylegesen írt nyers tartalom itt él
        # (a df_fields csak a KEZDETI, még nem írt tartalom szeedeléséhez
        # kell), hogy egy write UTÁNI olvasás a valóban írt értéket adja
        # vissza, ne a kezdetit.
        self._written_blocks: dict[tuple[int, int], bytearray] = {}

    def state_block(self) -> bytes:
        block = bytearray(32)
        block[0:2] = self.qmax.to_bytes(2, "big")
        block[4] = self.update_status
        return bytes(block)

    def ra_block(self) -> bytes:
        block = bytearray([0xFF, 0x55])
        for v in self.ra_table:
            block += int(v).to_bytes(2, "big", signed=True)
        return bytes(block)

    def df_block(self, subclass: int, block: int) -> bytes:
        key = (subclass, block)
        if key in self._written_blocks:
            return bytes(self._written_blocks[key])
        buf = bytearray(32)
        for name, value in self.df_fields.items():
            spec = FIELD_REGISTRY[name]
            if spec.subclass == subclass and spec.block == block:
                encoded = encode_df_field(spec, value)
                buf[spec.offset_in_block:spec.offset_in_block + spec.byte_length] = encoded
        return bytes(buf)

    def write_df_block(self, subclass: int, block: int, data32: bytes) -> None:
        self._written_blocks[(subclass, block)] = bytearray(data32)

    def set_ocvtaken(self, value: bool) -> None:
        self.flags_word = (self.flags_word | 0x0080) if value else (self.flags_word & ~0x0080)

    def set_vok(self, value: bool) -> None:
        self.control_status_word = (
            (self.control_status_word | 0x0002) if value else (self.control_status_word & ~0x0002)
        )

    def set_qen(self, value: bool) -> None:
        self.control_status_word = (
            (self.control_status_word | 0x0001) if value else (self.control_status_word & ~0x0001)
        )

    def set_sealed(self, value: bool) -> None:
        """[SCF-011 újratervezés, 2026-09-11] a `unseal`/`full_access`/
        `seal` RPC-hívások MOST ténylegesen módosítják az SS bitet (0x2000),
        nem csak `self.calls`-ba regisztrálódnak -- ez teszi lehetővé, hogy
        a `_ensure_bq_sealed_or_flag()` session-végi fallback-zárás
        `read_control_status()`-alapú visszaellenőrzése ténylegesen
        érdemi (nem mindig ugyanazt a fix értéket visszaadó) tesztadatot
        kapjon."""
        self.control_status_word = (
            (self.control_status_word | 0x2000) if value else (self.control_status_word & ~0x2000)
        )


def _data_flash_handler(fake: _FakeBqState):
    def _handler(params):
        subclass, block = params["subclass"], params["block"]
        if subclass == 82 and block == 0:
            return fake.state_block().hex()
        if subclass == 88 and block == 0:
            return fake.ra_block().hex()
        return fake.df_block(subclass, block).hex()
    return _handler


def _default_df_fields(profile: BatteryProfile) -> dict[str, int]:
    """Block 2 gate-konzisztens alapértelmezett DF-mezők a `profile`-hoz
    illesztve (a SUT és a fake UGYANAZT a `build_expected_df_values()`-t
    hívja, ezért nem drifthet szét) + Block 3 Cell BL/BH TF1-precedens
    alapértékei (a gate ezeket nem vizsgálja, csak a Cell BL write/restore
    tesztekhez kell egy induló érték)."""
    values = build_expected_df_values(profile)
    values["qmax_cell0_initial_mAh"] = values["design_capacity_mAh"]
    values["cell_bl_set_volt_mV"] = 1950
    values["cell_bh_set_volt_mV"] = 2500
    return values


def _write_recorder(fake: _FakeBqState, name: str):
    def _handler(params):
        fake.calls.append(name)
        return {"status": "ok"}
    return _handler


def _seal_state_recorder(fake: _FakeBqState, name: str, sealed_after: bool):
    """[SCF-011 újratervezés, 2026-09-11] `_write_recorder()`-hez hasonló,
    DE a hívás UTÁN ténylegesen módosítja is a fake SS bitjét
    (`unseal`/`full_access` -> unsealed, `seal` -> sealed) -- l.
    `_FakeBqState.set_sealed()` docstringje."""
    def _handler(params):
        fake.calls.append(name)
        fake.set_sealed(sealed_after)
        return {"status": "ok"}
    return _handler


def _write_data_flash_block_handler(fake: _FakeBqState):
    """Block 3 (2026-08-25) — a Bq34z110DataFlashService.write_fields()
    (Cell BL temp-write/restore) mögötti write_single_df_block_controlled()
    pipeline WRITE lépését szolgálja ki: ténylegesen elmenti az írt bájtokat,
    hogy a pipeline saját READBACK/COMPARE lépése a valóban írt tartalmat
    lássa vissza (nem a kezdeti df_fields-et)."""
    def _handler(params):
        fake.calls.append("write_data_flash_block")
        fake.write_df_block(params["subclass"], params["block"], bytes.fromhex(params["data_hex"]))
        return {"status": "ok"}
    return _handler


def _make_transport(fake: _FakeBqState) -> PicoBQTransport:
    results = {
        "chem_id": lambda params: fake.chem_id,
        "control_status": lambda params: fake.control_status_word,
        "flags": lambda params: fake.flags_word,
        "cycle_count": lambda params: fake.cycle_count,
        "dod0": lambda params: fake.dod0,
        "device_type": lambda params: 0x0541,
        "firmware_version": lambda params: 0x0311,
        "hardware_version": lambda params: 0x0003,
        "voltage_mV": lambda params: 12500,
        "average_current_mA": lambda params: 0,
        "temperature_01K": lambda params: 2951,
        "soc_percent": lambda params: 50,
        "remaining_capacity_mAh": lambda params: 3500,
        "full_charge_capacity_mAh": lambda params: 7000,
        "data_flash_block": _data_flash_handler(fake),
        "unseal": _seal_state_recorder(fake, "unseal", sealed_after=False),
        "full_access": _seal_state_recorder(fake, "full_access", sealed_after=False),
        "seal": _seal_state_recorder(fake, "seal", sealed_after=True),
        "it_enable": _write_recorder(fake, "it_enable"),
        "write_data_flash_block": _write_data_flash_block_handler(fake),
    }
    return PicoBQTransport(serial=FakePicoSerial(results=results))


def _make_profile(capacity_Ah: float = 7.0) -> BatteryProfile:
    return BatteryProfile(
        battery_name="TEST", manufacturer="TEST", model="TST7AH",
        nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=capacity_Ah,
        C10_capacity_Ah=capacity_Ah,  # Szintetikus tesztadat, nem gyártói állítás.
        terminate_voltage_per_cell_V=1.80,
    )


def make_controller(
    dmm_voltage_V: float = 13.0,
    capacity_Ah: float = 7.0,
    config: LearningConfig | None = None,
    fake_bq: _FakeBqState | None = None,
    expected_chem_id: int | None = None,
    charge_config=None,
):
    psu = MockPSU()
    load = MockLoad(voltage_V=dmm_voltage_V)
    dmm_v = MockDMM(voltage_V=dmm_voltage_V)
    dmm_t = MockDMM(temperature_C=22.0)
    profile = _make_profile(capacity_Ah)
    safety = SafetyManager(
        profile=profile, psu_mode=PsuMode.INDEPENDENT, temp_comp_mode=TempCompMode.MONITOR_ONLY,
    )
    fake_bq = fake_bq or _FakeBqState()
    if not fake_bq.df_fields:
        fake_bq.df_fields = _default_df_fields(profile)
    transport = _make_transport(fake_bq)
    cfg = config or LearningConfig(
        relax_after_charge_s=10.0, relax_after_discharge_s=10.0,
        max_relax_extra_wait_s=5.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
        sample_interval_s=5.0, rebound_settle_s=1.0,
    )
    ctrl = TraditionalLearningController(
        psu, load, dmm_v, dmm_t, transport, profile, safety, cfg,
        expected_chem_id=expected_chem_id,
        charge_config=charge_config,
    )
    # Q-38: a kozvetlen controller-tesztek checkpointja csak memoriaban el.
    ctrl.persist_tlc_completion = lambda record: None
    return ctrl, psu, load, dmm_v, dmm_t, fake_bq


def _force_charge_done(ctrl) -> None:
    if ctrl._charge_ctrl is None:
        # A ChargeController lusta inicializálású (`_run_charge()` első
        # hívásakor jön létre) - egy 0.0s tick a konstrukciót kiváltja
        # anélkül, hogy bármilyen valódi töltési időt telne.
        ctrl.advance(0.0)
    assert ctrl._charge_ctrl is not None
    ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE


def _accept_relax(ctrl, fake_bq: _FakeBqState, dt_s: float = 0.1) -> None:
    """R16-B4: set BQ flags; the caller must fill the voltage window."""
    fake_bq.set_ocvtaken(True)
    fake_bq.set_vok(False)


def _relax_tick_s() -> float:
    cfg = RelaxConfig()
    return min(cfg.dvdt_max_gap_s, cfg.sample_period_s / (cfg.dvdt_min_samples - 1))


def _complete_relax_window(ctrl) -> None:
    # R16-B4: feed measured-time samples instead of one favorable tick.
    phase = ctrl.state
    cfg = RelaxConfig()
    min_s = max(ctrl._config.relax_after_charge_s, ctrl._config.relax_after_discharge_s)
    dt_s = _relax_tick_s()
    for _ in range(int(max(min_s, cfg.sample_period_s) / dt_s) + 3):
        if ctrl.state != phase:
            break
        ctrl.advance(dt_s)
    assert ctrl.state != phase


class TestCf06VokPhaseDependentAcceptance:
    """[regresszió, governance-v2 review-kör 1, CF-06, VERIFIED_MULTI_SOURCE:
    TI-SLUSB55B p.30 STEP 8 (5./7./9. lépés) + TI-SLUA711 (5./8./10.
    lépés -- Kör 20 E18/SRC-F-8 korrekció, korábban helytelenül 6./8./10.)]
    a VOK flag CSAK a kisütés utáni relax-fázisokban (RELAX_1/
    RELAX_2) törlődik a sikeres OCV-elfogadáshoz -- a RECHARGE utáni
    relaxban (RELAX_PRE_DISCHARGE_2) a forrás szerint "The VOK flag will
    remain set." A korábbi, egységes "ocvtaken and not vok" kritérium
    emiatt RELAX_PRE_DISCHARGE_2-re STRUKTURÁLISAN sosem teljesülhetett."""

    def test_default_criterion_rejects_vok_set(self):
        """RELAX_1/RELAX_2 alapértéke (require_vok_cleared=True) -- a
        VOK=SET állapotot helyesen ELUTASÍTJA."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(True)
        ok, ocvtaken, vok = ctrl._check_relax_bq_acceptance()
        assert ok is False
        assert ocvtaken is True
        assert vok is True

    def test_relaxed_criterion_accepts_vok_set_when_ocvtaken(self):
        """RELAX_PRE_DISCHARGE_2 kritériuma (require_vok_cleared=False) --
        VOK=SET mellett is ELFOGADJA, ha ocvtaken=True."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(True)
        ok, ocvtaken, vok = ctrl._check_relax_bq_acceptance(require_vok_cleared=False)
        assert ok is True
        assert ocvtaken is True
        assert vok is True

    def test_relaxed_criterion_still_requires_ocvtaken(self):
        """require_vok_cleared=False NEM jelenti, hogy bármi elfogadható --
        ocvtaken=False esetén továbbra is elutasít."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        fake_bq.set_ocvtaken(False)
        fake_bq.set_vok(True)
        ok, ocvtaken, _vok = ctrl._check_relax_bq_acceptance(require_vok_cleared=False)
        assert ok is False
        assert ocvtaken is False


class TestCf42VokNotSetAfterItEnable:
    """[regresszió, governance-v2 review-kör 1, CF-42, VERIFIED_PRIMARY:
    TI-SLUSB55B "IT ENABLE: 0x0021" -- "sets bit 2 of UpdateStatus and
    causes the [VOK] and [QEN] flags to be set... [VOK] is cleared if
    the voltages are not suitable for a Qmax update"] a _run_verify_vok_
    qen() korábban CSAK a QEN hiányát jelezte -- egy IT_ENABLE utáni,
    azonnal törölt VOK-ot (megbízható korai előjelzés, hogy a több órás
    fizikai ciklus végén NEM lesz Qmax-frissítés) semmilyen minőségi
    jelzés nem rögzített, csak órákkal később, a VERIFY_UPDATE_STATUS_05
    kiértékelésénél derült volna ki."""

    def _reach_verify_vok_qen(self, fake_bq):
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=1.0, it_enable_settle_s=0.5, verify_settle_s=0.5,
            ),
        )
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # -> PRECHARGE_RELAX
        _accept_relax(ctrl, fake_bq)
        _complete_relax_window(ctrl)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        ctrl.advance(1.0)  # -> IT_ENABLE_STEP
        ctrl.advance(1.0)  # IT_ENABLE trigger + settle -> VERIFY_VOK_QEN
        assert ctrl.state == LearningState.VERIFY_VOK_QEN
        return ctrl

    def test_vok_not_set_after_it_enable_flags_quality_and_emits_event(self):
        fake_bq = _FakeBqState()
        ctrl = self._reach_verify_vok_qen(fake_bq)
        events = []
        ctrl.on_event = events.append
        fake_bq.set_qen(True)
        fake_bq.set_vok(False)  # IT_ENABLE gauge-oldali VOK-törlése
        ctrl.advance(1.0)  # settle -> DISCHARGE_1
        assert ctrl.state == LearningState.DISCHARGE_1
        assert "VOK_NOT_SET_AFTER_IT_ENABLE" in ctrl.quality_flags
        assert any(e.get("event_code") == "LEARNING_VOK_NOT_SET_AFTER_IT_ENABLE" for e in events)

    def test_vok_set_after_it_enable_does_not_flag(self):
        """Regresszió: a helyes, várt VOK=SET esetén NEM jelenik meg a
        VOK_NOT_SET_AFTER_IT_ENABLE flag -- a QEN-ellenőrzés is
        változatlan marad."""
        fake_bq = _FakeBqState()
        ctrl = self._reach_verify_vok_qen(fake_bq)
        fake_bq.set_qen(True)
        fake_bq.set_vok(True)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.DISCHARGE_1
        assert "VOK_NOT_SET_AFTER_IT_ENABLE" not in ctrl.quality_flags
        assert "QEN_NOT_CONFIRMED_AFTER_IT_ENABLE" not in ctrl.quality_flags

    def test_qen_not_confirmed_still_flags_independently_of_vok(self):
        """A meglévő QEN-ellenőrzés NEM sérülhet az új VOK-ellenőrzés
        bevezetésétől -- a két flag EGYMÁSTÓL FÜGGETLENÜL is
        megjelenhet."""
        fake_bq = _FakeBqState()
        ctrl = self._reach_verify_vok_qen(fake_bq)
        fake_bq.set_qen(False)
        fake_bq.set_vok(True)
        ctrl.advance(1.0)
        assert "QEN_NOT_CONFIRMED_AFTER_IT_ENABLE" in ctrl.quality_flags
        assert "VOK_NOT_SET_AFTER_IT_ENABLE" not in ctrl.quality_flags

    def test_both_flags_present_when_both_qen_and_vok_unset(self):
        """MAX verifier P3-1 lelete (2026-09-09): az előző teszt (qen=False,
        vok=True) NEM tudja megkülönböztetni az `if`/`elif` ágazást --
        VALÓDI függetlenség-bizonyíték csak akkor, ha MINDKÉT flag hamis,
        és MINDKETTŐ jelenik meg (egy `elif not cs.vok` regresszió itt
        elbukna, mert csak az egyik flagot állítaná be)."""
        fake_bq = _FakeBqState()
        ctrl = self._reach_verify_vok_qen(fake_bq)
        fake_bq.set_qen(False)
        fake_bq.set_vok(False)
        ctrl.advance(1.0)
        assert "QEN_NOT_CONFIRMED_AFTER_IT_ENABLE" in ctrl.quality_flags
        assert "VOK_NOT_SET_AFTER_IT_ENABLE" in ctrl.quality_flags

    def test_vok_not_set_event_carries_explicit_warning_severity(self):
        """[regresszió, R3-B3 (R3-GUI-1, P3), DFAM-018 RECURRING] a
        LEARNING_VOK_NOT_SET_AFTER_IT_ENABLE kódneve semleges, ezért a
        központi `_infer_severity()` heurisztika (GOV-024) INFO-nak
        minősítette -- egy minőségi flaget is adó, negatív előjelzés
        színtelenül jelent meg az élő eseménynaplóban. A producer minősít:
        explicit WARNING."""
        fake_bq = _FakeBqState()
        ctrl = self._reach_verify_vok_qen(fake_bq)
        events = []
        ctrl.on_event = events.append
        fake_bq.set_qen(True)
        fake_bq.set_vok(False)
        ctrl.advance(1.0)
        vok = [
            e for e in events
            if e.get("event_code") == "LEARNING_VOK_NOT_SET_AFTER_IT_ENABLE"
        ]
        assert len(vok) == 1
        assert vok[0]["severity"] == "WARNING"


class TestPrecondAndCharge:
    def test_starts_in_init_then_precond(self):
        ctrl, *_ = make_controller()
        assert ctrl.state == LearningState.INIT
        ctrl.advance(0.0)
        assert ctrl.state == LearningState.PRECOND

    def test_precond_to_precharge_on_success(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        assert ctrl.state == LearningState.PRECHARGE
        assert ctrl.provisioning_consistency_report is not None
        assert ctrl.provisioning_consistency_report.consistent

    def test_precond_blocks_gate_on_non_instrument_error_during_bq_readback(self):
        """[Kör 20 Batch E13b, F20-S4, DFAM-002] `_run_precond()` korábban
        csak `InstrumentError`-t fogott el az `unseal()`/
        `read_control_status()`/`evaluate_provisioning_consistency()`
        szekvencia körül -- egy MÁS típusú kivétel (itt szimulálva:
        `RuntimeError`) korábban KIVÉTELKÉNT PROPAGÁLT volna, elkerülve a
        szándékolt BLOCKED-gate-utat. FONTOS: a fix NEM a golden_gg_
        controller.py mintáját veszi át (`emergency_stop()`+`return`) -- a
        TLC SAJÁT, `BLOCKED_BQ_DF_READBACK_FAILED` gate-döntése fut, majd a
        `LEARNING_CONFIGURATION_MISMATCH` FAULT-tá alakul a MEGLÉVŐ,
        VÁLTOZATLAN `gate_report.decision != CONSISTENT` ágon."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()

        def _raise_generic(*args, **kwargs):
            raise RuntimeError("simulated non-InstrumentError transport failure")

        ctrl._bq_transport.unseal = _raise_generic

        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> _run_precond() lefut

        assert ctrl.state == LearningState.FAULT
        assert "LEARNING_CONFIGURATION_MISMATCH" in ctrl.fault_reason
        assert "BQ Data Flash olvasás sikertelen" in ctrl.fault_reason

    def test_precond_records_unseal_but_stays_unsealed_for_session(self):
        """[SCF-011 újratervezés, 2026-09-11, felhasználói döntés] a
        PRECOND gate a session-szintű életciklus ELSŐ lépése: egyszeri
        unseal(), amit a chip a session VÉGÉIG (DONE/FAULT/STOP/ABORT)
        UNSEALED állapotban tart, hogy a folyamatos diagnosztika/DF-
        műveletek ne igényeljenek ismételt unseal->read->seal ciklust.
        A korábbi, minden PRECOND-on lezajló seal() ezért megszűnt --
        a session-végi seal()-t a _restore_cell_bl_if_needed()/
        _ensure_bq_sealed_or_flag() végzi a terminális állapotokban."""
        ctrl, *_, fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE
        assert fake_bq.calls.count("unseal") == 1
        assert fake_bq.calls.count("seal") == 0

    def test_precond_faults_when_unseal_does_not_actually_clear_ss_bit(self):
        """[SCF-011 újratervezés, MAX verifier P1-1 lelete, 2026-09-11]
        egy RPC 'ok' válasz önmagában NEM bizonyítja, hogy az SS bit
        ténylegesen törlődött (rossz unseal-kulcs a DF-ben, SCF-012
        retry-duplikáció, egyéb chip-állapot) -- ez a teszt egy olyan
        unseal()-t szimulál, ami sikeresen válaszol, de a chip SS bitjét
        NEM törli (nem a `_seal_state_recorder` helperen megy át), és
        elvárja, hogy a PRECOND ezt explicit FAULT-tal jelezze, NE
        folytassa a sessiont egy ténylegesen SEALED chipen."""
        ctrl, *_, fake_bq = make_controller()
        ctrl._bq_transport.serial.results["unseal"] = lambda params: {"status": "ok"}
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "BQ_UNSEAL_NOT_EFFECTIVE" in ctrl.fault_reason
        assert fake_bq.control_status_word & 0x2000, "a chip valojaban meg SEALED maradt"

    def test_emergency_stop_right_after_precond_reseals_chip(self):
        """[SCF-011 újratervezés, 2026-09-11] ha a FAULT az IT_ENABLE
        ELŐTT, közvetlenül a PRECOND unseal()-je után következik be (itt:
        DF configuration mismatch), a `_restore_cell_bl_if_needed()`
        no-op marad (`_cell_bl_original_mV is None`, az IT_ENABLE sosem
        állította be) -- ezt a rést az `emergency_stop()`-ban hívott
        `_ensure_bq_sealed_or_flag("EMERGENCY_STOP")` fallback fedi le,
        SIKERES esetben ténylegesen visszazárva a chipet (l. a
        `test_emergency_stop_right_after_precond_seal_failure_surfaces_
        in_quality_flags` sibling tesztet a SIKERTELEN seal-ágra)."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        fake_bq.df_fields = _default_df_fields(_make_profile(7.0))
        fake_bq.df_fields["design_capacity_mAh"] = 4000
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND: unseal + DF mismatch -> emergency_stop()
        assert ctrl.state == LearningState.FAULT
        assert "LEARNING_CONFIGURATION_MISMATCH" in ctrl.fault_reason
        assert fake_bq.control_status_word & 0x2000, "a chipnek visszazárva kell lennie a fallback-on"
        assert ctrl.quality_flags == ()

    def test_emergency_stop_right_after_precond_seal_failure_surfaces_in_quality_flags(self):
        """[SCF-011 újratervezés, 2026-09-11, MAX verifier P2-6 lelet
        alapján átnevezve/kiegészítve] a SIKERTELEN fallback-seal ágat
        fedi le -- ugyanaz a defect family (Architektúra A-1, NEW_FAMILY
        unverified_security_state_restore), mint a korábbi (most
        megszűnt) PRECOND-szintű seal-failure teszt, de az új fallback-
        úton keresztül."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        fake_bq.df_fields = _default_df_fields(_make_profile(7.0))
        fake_bq.df_fields["design_capacity_mAh"] = 4000
        ctrl._bq_transport.serial.errors["seal"] = ("METHOD_ERROR", "simulated seal failure")
        events = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND: unseal + DF mismatch -> emergency_stop()
        assert ctrl.state == LearningState.FAULT
        assert "LEARNING_CONFIGURATION_MISMATCH" in ctrl.fault_reason
        assert any(f.startswith("BQ_SEAL_RESTORE_FAILED_AT_EMERGENCY_STOP:") for f in ctrl.quality_flags)
        assert any(e.get("event_code") == "LEARNING_SEAL_RESTORE_FAILED" for e in events)

    def test_emergency_stop_flags_when_seal_accepted_but_ss_bit_still_set(self):
        """[Round-2 MAX verifier F-2 lelet, 2026-09-11] a P1-1 fix az
        UNSEAL oldalra tett szimmetrikus visszaellenőrzést -- ennek a
        SEAL oldali tükörága (`_ensure_bq_sealed_or_flag()`-ban: a seal()
        RPC 'ok'-val tér vissza, DE a post-seal control_status újraolvasás
        még mindig SS=0-t (UNSEALED) ad, azaz a chip TÉNYLEGESEN nem
        záródott vissza) korábban teszteletlen maradt. Ez a teszt pontosan
        azt az ágat
        gyakorolja, ahol a `seal()` maga NEM dob kivételt, csak nem éri el
        a tényleges hatását."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        fake_bq.df_fields = _default_df_fields(_make_profile(7.0))
        fake_bq.df_fields["design_capacity_mAh"] = 4000
        # A seal() RPC sikeresen valaszol, de NEM megy at a normal
        # `_seal_state_recorder` handleren -- a fake SS bitje valtozatlan
        # (meg UNSEALED, hiszen a PRECOND unseal()-je mar lefutott).
        ctrl._bq_transport.serial.results["seal"] = lambda params: {"status": "ok"}
        events = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND: unseal + DF mismatch -> emergency_stop()
        assert ctrl.state == LearningState.FAULT
        assert any(
            f.startswith("BQ_SEAL_RESTORE_FAILED_AT_EMERGENCY_STOP:")
            and "SS bit still clear" in f
            for f in ctrl.quality_flags
        )
        assert any(e.get("event_code") == "LEARNING_SEAL_RESTORE_FAILED" for e in events)

    def test_precond_faults_on_bq_df_configuration_mismatch(self):
        fake_bq = _FakeBqState()
        ctrl, psu, load, *_ = make_controller(fake_bq=fake_bq)
        # A profil (make_controller default) 7.0 Ah -> 7000 mAh Design
        # Capacity-t vár, de a fake chip-en más van beprogramozva.
        fake_bq.df_fields = _default_df_fields(_make_profile(7.0))
        fake_bq.df_fields["design_capacity_mAh"] = 4000
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "LEARNING_CONFIGURATION_MISMATCH" in ctrl.fault_reason
        assert "design_capacity_mAh" in ctrl.fault_reason
        assert psu.called("all_outputs_off")
        assert load.called("input_off")
        assert not ctrl.provisioning_consistency_report.consistent

    def test_precond_faults_on_bq_df_readback_failure(self):
        ctrl, psu, load, *_, fake_bq = make_controller()
        ctrl._bq_transport.serial.errors["data_flash_block"] = ("TIMEOUT", "simulated")
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "LEARNING_CONFIGURATION_MISMATCH" in ctrl.fault_reason
        assert "BQ_DF_READBACK_FAILED" in ctrl.fault_reason or "olvasás sikertelen" in ctrl.fault_reason

    def test_precond_invokes_on_provisioning_consistency_callback(self):
        """Block 6 (2026-08-25): a GUI-nak (BqDiagnosticsPanel) a
        callback-on keresztül kell eljutnia a gate eredményének — l.
        on_gauge_sample analóg mintája."""
        ctrl, *_ = make_controller()
        received = []
        ctrl.on_provisioning_consistency = received.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE
        assert len(received) == 1
        assert received[0] is ctrl.provisioning_consistency_report
        assert received[0].consistent

    def test_precond_invokes_callback_on_mismatch_too(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        fake_bq.df_fields = _default_df_fields(_make_profile(7.0))
        fake_bq.df_fields["design_capacity_mAh"] = 4000
        received = []
        ctrl.on_provisioning_consistency = received.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert len(received) == 1
        assert not received[0].consistent

    def test_precond_faults_on_chem_id_mismatch(self):
        ctrl, psu, load, *_ = make_controller(expected_chem_id=0x1234)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "CHEM_ID_MISMATCH" in ctrl.fault_reason

    def test_precond_faults_on_dmm_lost(self):
        ctrl, *_ = make_controller()
        ctrl._dmm_v.simulate_timeout = True
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason

    def test_dmm_valid_property_reflects_actual_dmm_read_result(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P2 lelet #4] a ChargeController már exponálja a dmm_valid
        property-t, ezt test_runner.py::_build_sample() a dmm_voltage_valid
        mezőhöz olvassa — a TraditionalLearningController hiányzott, emiatt
        getattr(controller, "dmm_valid", None) mindig None-t adott, és a
        fallback (`u_batt is not None`) gyakorlatilag MINDIG True-t írt,
        akkor is, ha a DMM-olvasás ténylegesen hibázott."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND, DMM-olvasás
        assert ctrl.dmm_valid is True

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)  # a következő DMM-olvasás hibázik
        assert ctrl.dmm_valid is False

    def test_precharge_done_transitions_to_precharge_relax(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

    def test_relax_tick_reads_voltage_dmm_only_once(self):
        """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet #2] Korábban egy RELAX-tick 2x olvasta a
        feszültség-DMM-et (top-level _read_dmm() + a RelaxController saját
        olvasása) — a top-level advance() mostantól _read_temperature_only()-t
        hív a RELAX-alállapotokra (CSAK a hőmérséklet-DMM-et olvassa), a
        feszültség-DMM-et KIZÁRÓLAG a RelaxController olvassa (CLAUDE.md §9).
        L. test_relax_u_batt_matches_relax_ctrl_last_voltage a self._dmm_valid
        szinkronizálás igazolásához."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_v.calls.clear()
        ctrl.advance(1.0)
        assert dmm_v.calls.count("read_voltage()") == 1

    def test_relax_u_batt_matches_relax_ctrl_last_voltage(self):
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_v.voltage_V = 13.1
        ctrl.advance(1.0)
        assert ctrl._u_batt == ctrl._relax_ctrl.last_voltage_V

    def test_relax_dmm_valid_reflects_relax_ctrl_read_outcome(self):
        """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet #2] Mivel a top-level advance() a RELAX-
        alállapotokban már nem olvassa a feszültség-DMM-et, self._dmm_valid-
        nak a RelaxController TÉNYLEGES olvasási eredményét kell tükröznie
        (dmm_fault_count-delta alapján) — nem szabad "megfagynia" a relax
        belépése előtti értéken."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl.dmm_valid is True

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)
        assert ctrl.dmm_valid is False

    def test_relax_first_tick_dmm_failure_does_not_overwrite_u_batt_with_fabricated_zero(self):
        """[regresszió, Kör 17 MAX-effort review, R17-11a, DFAM-024 2.
        előfordulás] RelaxController.last_voltage_V az ELSŐ (INIT-ágú)
        sikertelen olvasás ELŐTT egy fabrikált 0.0 V-ot ad vissza (a saját
        init-értékét) -- korábban ezt FELTÉTEL NÉLKÜL írtuk self._u_batt-ba,
        felülírva a töltési fázisból származó, VALÓS mért értéket. A fix
        (self._dmm_valid-guard) csak sikeres olvasásnál frissíti u_batt-ot."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl._relax_ctrl is None  # meg nem konstrualt -- a kovetkezo tick hozza letre
        u_batt_before_relax = ctrl._u_batt
        assert u_batt_before_relax != 0.0

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)  # RelaxController INIT-agu, sikertelen elso olvasasa
        assert ctrl._relax_ctrl is not None
        assert ctrl._relax_ctrl.last_voltage_V == 0.0  # a RelaxController fabrikalt init-erteke
        assert ctrl.dmm_valid is False
        assert ctrl._u_batt == pytest.approx(u_batt_before_relax)

    def test_relax_dmm_fault_surfaces_in_quality_flags(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26; CF-05
        (governance-v2, DFAM-008) frissítés 2026-09-08] A relax alatti
        DMM-kiesés korábban nyomtalanul maradt — most a quality_flags-be
        kerül, amint a relax-fázis TÉNYLEGESEN lezárul (elfogadva vagy
        timeout). [CF-05 frissítés: a relax_controller.py fix óta a
        RelaxController RELAX_DONE után is folytatja a mérést, ezért a
        session-szintű carry-over már nem az ELSŐ RELAX_DONE-tickben,
        hanem a relax-fázis tényleges lezárásakor történik — l.
        _finalize_relax_ctrl_totals() docstringje."""
        ctrl, _psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            config=LearningConfig(relax_after_charge_s=1.0),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_v.simulate_timeout = True
        ctrl.advance(0.5)  # RELAXING tick, DMM-hiba a RelaxControllerben
        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)  # min_relax_s letelik -> RELAX_DONE, extra-wait indul
        assert not any("RELAX_DMM_FAULTS" in f for f in ctrl.quality_flags)

        _accept_relax(ctrl, fake_bq)
        _complete_relax_window(ctrl)

        assert any("RELAX_DMM_FAULTS" in f for f in ctrl.quality_flags)

    def test_relax_dmm_fault_surfaces_in_relax_dmm_fault_total(self):
        """[regresszió, Teljes kódbázis review 12. kör (2026-08-31), Szoftver/
        Architektúra P2 lelet; CF-05 (governance-v2, DFAM-008) frissítés
        2026-09-08, 2. MAX verifier P3-1 frissítés 2026-09-08] A relax
        alatti DMM-kiesésnek a szöveges quality_flags mellett a golden_gg_
        controller.py/chemid_controller.py mintájú, NUMERIKUS relax_dmm_
        fault_total session-összegbe is be kell kerülnie (a main_window.py::
        _write_report() capacity_result_quality gate ezt olvassa, nem a
        quality_flags szöveget) -- a P3-1 fix óta a property az ÉLŐ (még
        nem finalizált) relax_ctrl.dmm_fault_count-ot is hozzáadja, tehát
        a hiba AZONNAL látszik, nem csak a relax-fázis lezárása után."""
        ctrl, _psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            config=LearningConfig(relax_after_charge_s=1.0),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl.relax_dmm_fault_total == 0

        dmm_v.simulate_timeout = True
        ctrl.advance(0.5)  # RELAXING tick, DMM-hiba a RelaxControllerben
        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)  # min_relax_s letelik -> RELAX_DONE, extra-wait indul
        assert ctrl.relax_dmm_fault_total == 1  # élő tag, MÉG NEM finalizált

        _accept_relax(ctrl, fake_bq)
        _complete_relax_window(ctrl)

        assert ctrl.relax_dmm_fault_total == 1

    def test_relax_dmm_fault_reaches_controller_on_event(self):
        """R2-12 (2026-09-15, DFAM-011): a beágyazott RelaxController
        on_event-je korábban SEHOL nem volt bekötve -- a relax alatti
        DMM-kiesés a relax_dmm_fault_total-on kívül csak a tranziens
        live event-feedben lett volna láthatatlan, mert az esemény sosem
        jutott el a külső controller._emit()-jéig."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller(
            config=LearningConfig(relax_after_charge_s=1.0),
        )
        events = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_v.simulate_timeout = True
        ctrl.advance(0.5)
        dmm_v.simulate_timeout = False

        assert ctrl.relax_dmm_fault_total == 1
        assert any(e.get("event_code") == "RELAX_DMM_FAULT" for e in events)

    def test_forwarded_relax_event_carries_relax_substate_context(self):
        """Egy bare 'RELAX_DMM_FAULT' üzenet önmagában megkülönböztethetetlen
        lenne a 4 relax-alállapot (PRECHARGE_RELAX/RELAX_1/RELAX_PRE_
        DISCHARGE_2/RELAX_2) között -- az _emit_relax_event() a jelenlegi
        self._state.value-t fűzi az üzenet elé."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller(
            config=LearningConfig(relax_after_charge_s=1.0),
        )
        events = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_v.simulate_timeout = True
        ctrl.advance(0.5)
        dmm_v.simulate_timeout = False

        relax_events = [e for e in events if e.get("event_code") == "RELAX_DMM_FAULT"]
        assert relax_events, "nem talalhato tovabbitott RELAX_DMM_FAULT esemeny"
        assert "PRECHARGE_RELAX" in relax_events[0]["event_message"]

    def test_relax_dmm_fault_total_not_multiplied_by_extra_wait_ticks(self):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31), Session/
        Workflow P2 lelet; CF-05 (governance-v2, DFAM-008) frissítés
        2026-09-08, 2. MAX verifier P3-1 frissítés 2026-09-08] A RELAX_DONE
        elérése UTÁN, a SUP03-LEARN-006 BQ-elfogadási extra-wait hurokban
        (OCVTAKEN/VOK még nem teljesül, alapértelmezett _FakeBqState()) a
        relax_dmm_fault_total-nak STABILAN kell mutatnia a valós hibaszámot
        a teljes hurok alatt (P3-1 fix: élő tag) -- nem szabad tick-enként
        NÖVEKEDNIE (ami duplikációt jelezne)."""
        ctrl, _psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            config=LearningConfig(relax_after_charge_s=1.0, max_relax_extra_wait_s=2 * RelaxConfig().sample_period_s),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_v.simulate_timeout = True
        ctrl.advance(0.5)  # RELAXING tick, DMM-hiba a RelaxControllerben
        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)  # min_relax_s letelik -> RELAX_DONE, OCVTAKEN hiányzik -> extra-wait

        assert ctrl.state == LearningState.PRECHARGE_RELAX  # a hurokban maradt
        assert ctrl.relax_dmm_fault_total == 1  # élő tag, MÉG NEM finalizált

        # Több további tick a hurokban (OCVTAKEN továbbra sem teljesül,
        # nincs újabb DMM-hiba) — a total STABILAN 1 marad, nem nő.
        for _ in range(4):
            ctrl.advance(1.0)
        assert ctrl.relax_dmm_fault_total == 1

        _accept_relax(ctrl, fake_bq)
        _complete_relax_window(ctrl)

        assert ctrl.relax_dmm_fault_total == 1  # NEM 5 (tick-enkénti duplikáció)
        assert sum(1 for f in ctrl.quality_flags if "RELAX_DMM_FAULTS" in f) == 1

    def test_extreme_dt_s_count_reflects_relax_phase_and_survives_discard(self):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31), Metrológia/
        Adatintegritás P2 lelet] a beágyazott RelaxController SAJÁT extreme_
        dt_s_count/max_observed_dt_s property-je (10. kör, 6eb395b) korábban
        sosem jutott el a session-szintű extreme_dt_s_count/max_observed_dt_s
        property-ig — egy extrém dt_s-tüske a relax alatt (a leghosszabb
        fázisok egyike) sosem jelent meg a 9. kör capacity_result_quality
        kapujában. Igazolja: (1) az érték ÉLŐBEN tükröződik, amíg a
        relax_ctrl aktív; (2) a RELAX_DONE-utáni egyszeri carry-over NEM
        duplikálja (a BQ-elfogadási extra-wait hurokban sem); (3) a carry-
        over túléli a relax_ctrl tényleges eldobását (elfogadás után)."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller(
            config=LearningConfig(relax_after_charge_s=1.0, max_relax_extra_wait_s=2 * RelaxConfig().sample_period_s),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl.extreme_dt_s_count == 0

        ctrl.advance(301.0)  # extrém dt_s (>300s küszöb) relax KÖZBEN
        assert ctrl.extreme_dt_s_count == 1  # élőben tükröződik (relax_ctrl még aktív)
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)

        ctrl.advance(1.0)  # RELAX_DONE elérve, OCVTAKEN hiányzik -> extra-wait hurok
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl._relax_ctrl is not None  # a hurokban a relax_ctrl még nem dobódik el
        assert ctrl.extreme_dt_s_count == 1  # a one-shot carry-over NEM duplikálta

        for _ in range(3):  # több tick a hurokban — továbbra sincs duplikáció
            ctrl.advance(1.0)
        assert ctrl.extreme_dt_s_count == 1
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)

        _accept_relax(ctrl, fake_bq)
        _complete_relax_window(ctrl)
        assert ctrl._relax_ctrl is None
        assert ctrl.extreme_dt_s_count == 1  # a carry-over túlélte az eldobást
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)


class TestTemperatureOutOfRangeCountProperty:
    """[regresszió, Teljes kódbázis review 21. kör (2026-09-07), Session/
    Workflow/Állapotgép P2 lelet] a beágyazott ChargeController (PRECHARGE/
    RECHARGE alatt) temperature_out_of_range_count számlálóját a
    self._charge_ctrl = None átmenet előtt a _charge_total_
    temperature_out_of_range_count carry-over mezőbe kell menteni —
    ugyanaz a minta, mint _charge_total_extreme_dt_s_count-nál."""

    def test_zero_by_default(self):
        ctrl, *_ = make_controller()
        assert ctrl.temperature_out_of_range_count == 0

    def test_embedded_precharge_counter_survives_charge_ctrl_discard(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._temperature_out_of_range_count = 3
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # PRECHARGE_DONE -> PRECHARGE_RELAX

        assert ctrl._charge_ctrl is None
        assert ctrl.temperature_out_of_range_count == 3


# ------------------------------------------------------------------ #
# TestLearningPhaseElapsedS                                            #
# [CF-19, governance-v2 kör 2, DFAM-023] a TraditionalLearningController#
# MÁR rendelkezett _enter()/_transition_to() bookkeeginggel, de a       #
# _phase_elapsed_s += dt_s növelést korábban csak 4/20 handler végezte  #
# el szórtan (IT_ENABLE_STEP/VERIFY_VOK_QEN/REBOUND_CHECK_*/VERIFY_     #
# UPDATE_STATUS_*) -- PRECHARGE (mint a legtöbb más állapot) SOSEM      #
# kapott növelést, holott a _enter() helyesen nullázta. Ez a teszt      #
# pontosan ezt a régen hibás állapotot (PRECHARGE) fedi.                #
# ------------------------------------------------------------------ #

class TestLearningPhaseElapsedS:
    def test_starts_at_zero(self):
        ctrl, *_ = make_controller()
        assert ctrl.phase_elapsed_s == 0.0

    def test_grows_in_precharge_state(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        assert ctrl.state == LearningState.PRECHARGE
        ctrl.advance(5.0)
        ctrl.advance(3.0)
        assert ctrl.state == LearningState.PRECHARGE
        assert ctrl.phase_elapsed_s == pytest.approx(8.0)

    def test_resets_on_state_transition(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        ctrl.advance(100.0)
        assert ctrl.phase_elapsed_s == pytest.approx(100.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # PRECHARGE -> PRECHARGE_RELAX
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl.phase_elapsed_s == pytest.approx(0.0)

    def test_fault_resets_phase_elapsed_s(self):
        """[CF-19 verifier P2 lelet, governance-v2 kör 2] emergency_stop()
        korábban közvetlen self._state = LearningState.FAULT-ot használt,
        ELTÉRVE a másik 3 összetett controllertől (ahol MINDEN átmenet,
        a terminálisak is, _enter()-en megy át) -- a phase_elapsed_s emiatt
        NEM nullázódott FAULT-ba lépéskor, hanem megfagyott az utolsó aktív
        fázis felhalmozott értékén."""
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)   # INIT -> PRECOND
        ctrl.advance(1.0)   # PRECOND -> PRECHARGE
        ctrl.advance(250.0)  # phase_elapsed_s felhalmozódik PRECHARGE alatt
        assert ctrl.phase_elapsed_s == pytest.approx(250.0)

        ctrl.emergency_stop("TEST_FAULT_REASON")

        assert ctrl.state == LearningState.FAULT
        assert ctrl.phase_elapsed_s == pytest.approx(0.0)

    def test_done_resets_phase_elapsed_s(self):
        """L. test_fault_resets_phase_elapsed_s docstringje -- ugyanaz a
        hiányzó _enter() útvonal a DONE terminális átmenetre
        (_run_final_snapshot())."""
        ctrl, *_ = make_controller()
        ctrl._phase_elapsed_s = 555.0

        ctrl._run_final_snapshot()
        _b23b_finish_recharge(ctrl)

        assert ctrl.state == LearningState.DONE
        assert ctrl.phase_elapsed_s == pytest.approx(0.0)


class TestFullSequenceToDone:
    """Végigviszi a TELJES SUP03-LEARN-004 szekvenciát, a lassú fázisokat
    (relax min. idő, ChargeController taper) mock-manipulációval siettetve —
    a cél a HELYES ÁLLAPOTSORREND igazolása, nem a fizikai időzítés."""

    def _run_relax_to_completion(
        self, ctrl, fake_bq, dt_s: float = 100.0, vok_remains_set: bool = False,
    ):
        if vok_remains_set:
            # CF-06 (governance-v2 review-kör 1, VERIFIED_MULTI_SOURCE:
            # TI-SLUSB55B p.30 STEP 8 7. lépés + TI-SLUA711 8. lépés): a
            # RECHARGE utáni relaxban (RELAX_PRE_DISCHARGE_2) a VOK flag a
            # forrás szerint SET MARAD -- a sima _accept_relax() helper
            # (ami mindig vok=False-ot állít) itt egy IRREÁLIS hardver-
            # állapotot szimulálna, ami elfedné a CF-06 hibáját.
            fake_bq.set_ocvtaken(True)
            fake_bq.set_vok(True)
        else:
            _accept_relax(ctrl, fake_bq)
        _complete_relax_window(ctrl)

    def test_full_sequence_reaches_done_with_verified_result(self):
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=RelaxConfig().sample_period_s + _relax_tick_s(),
                relax_after_discharge_s=RelaxConfig().sample_period_s + _relax_tick_s(),
                max_relax_extra_wait_s=2.0, it_enable_settle_s=0.5, verify_settle_s=0.5,
                sample_interval_s=1000.0, rebound_settle_s=1.0,
            ),
        )
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        assert ctrl.state == LearningState.PRECHARGE

        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        self._run_relax_to_completion(ctrl, fake_bq)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT

        ctrl.advance(1.0)  # INITIAL_SNAPSHOT -> IT_ENABLE_STEP
        assert ctrl.state == LearningState.IT_ENABLE_STEP
        assert ctrl.state == LearningState.IT_ENABLE_STEP
        ctrl.advance(1.0)  # IT_ENABLE trigger + settle
        assert "it_enable" in fake_bq.calls
        assert "unseal" in fake_bq.calls
        assert ctrl.state == LearningState.VERIFY_VOK_QEN

        fake_bq.set_qen(True)
        fake_bq.set_vok(True)
        ctrl.advance(1.0)  # settle
        assert ctrl.state == LearningState.DISCHARGE_1
        assert load.input_commanded_on is True

        # DMM feszültséget a terminate alá visszük, hogy a kisütés lezáruljon.
        # A load levétele után a rebound-ellenőrzés (BQ34Z110_learning_
        # rebound_kisutes.txt) egy KÜLÖN fázis — a MockDMM konstans
        # feszültséget ad (nincs szimulált rebound), ezért 9.0V << 10.9V
        # (alapértelmezett cél 6 × 1.80 = 10.8V + 0.1V tolerancia, egyoldalú
        # felső korlát) miatt az ellenőrzés az ELSŐ próbára elfogadja,
        # de a settle-várakozás (rebound_settle_s=1.0) miatt még egy
        # advance() kell, mielőtt RELAX_1-be lép.
        dmm_v.voltage_V = 9.0  # 6 cella * 1.75V/cella = 10.5V alatt
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RELAX_1

        fake_bq.update_status = 0x05
        self._run_relax_to_completion(ctrl, fake_bq)
        assert ctrl.state == LearningState.VERIFY_UPDATE_STATUS_05
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RECHARGE

        dmm_v.voltage_V = 13.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RELAX_PRE_DISCHARGE_2
        self._run_relax_to_completion(ctrl, fake_bq, vok_remains_set=True)
        assert ctrl.state == LearningState.DISCHARGE_2

        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_2
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RELAX_2

        fake_bq.update_status = 0x06
        fake_bq.qmax = 150  # Qmax frissült az elsőhöz képest
        fake_bq.ra_table = [x + 1.0 for x in fake_bq.ra_table]  # Ra frissült
        self._run_relax_to_completion(ctrl, fake_bq)
        assert ctrl.state == LearningState.VERIFY_UPDATE_STATUS_06
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FINAL_SNAPSHOT

        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FINAL_RECHARGE
        _b23b_finish_recharge(ctrl)
        assert ctrl.state == LearningState.DONE
        summary = ctrl.result_summary
        assert summary is not None
        assert summary.result == LEARNING_RESULT_VERIFIED
        assert summary.qmax_before == 100
        assert summary.qmax_final == 150
        assert summary.update_status_final == 0x06
        # Block 3 (2026-08-25): a Cell BL temp-write/restore a teljes
        # sikeres futás alatt láthatatlanul lezajlott (VERIFIED csak üres
        # quality_flags-szel érhető el, tehát restore-hiba itt SEM történt).
        assert summary.cell_bl_original_mV == 1950
        assert summary.cell_bl_restore_confirmed is True
        assert fake_bq.calls.count("write_data_flash_block") == 2
        # MAX verifier P2-6 (2026-09-11): a testvér golden_gg suite-nak MÁR
        # volt pozitív SEALED-at-DONE assert-je, ennek itt sem szabad
        # hiányoznia -- a session-szintű UNSEALED életciklus VÉGE.
        assert fake_bq.control_status_word & 0x2000, "a chipnek visszazárva kell lennie DONE-nál"


class TestSup03Learn007NoManualSuccess:
    """SUP03-LEARN-007: ha az UpdateStatus sosem éri el a 0x06-ot, a
    klasszifikáció NEM lehet BQ_LEARNING_VERIFIED."""

    def test_missing_update_status_06_yields_awaiting_not_verified(self):
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0, rebound_settle_s=1.0,
            ),
        )
        # UpdateStatus SOSEM éri el a 0x06-ot (fake_bq.update_status marad 0x00)
        self._drive_to_done(ctrl, dmm_v, load, fake_bq)
        assert ctrl.state == LearningState.DONE
        summary = ctrl.result_summary
        assert summary is not None
        assert summary.result in (LEARNING_RESULT_AWAITING, LEARNING_RESULT_FAILED)
        assert summary.result != LEARNING_RESULT_VERIFIED

    @staticmethod
    def _drive_to_done(ctrl, dmm_v, load, fake_bq) -> None:
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            fake_bq.set_ocvtaken(True)
            fake_bq.set_vok(False)
            ctrl.advance(100.0)
        ctrl.advance(1.0)  # snapshot
        ctrl.advance(1.0)  # it_enable trigger
        ctrl.advance(1.0)  # it_enable settle
        ctrl.advance(1.0)  # verify vok/qen -> discharge setup
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)  # discharge -> rebound_check_1
        ctrl.advance(1.0)  # rebound_check_1 -> relax_1
        for _ in range(5):
            if ctrl.state == LearningState.VERIFY_UPDATE_STATUS_05:
                break
            fake_bq.set_ocvtaken(True)
            fake_bq.set_vok(False)
            ctrl.advance(100.0)
        ctrl.advance(1.0)  # -> RECHARGE
        dmm_v.voltage_V = 13.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # -> RELAX_PRE_DISCHARGE_2
        for _ in range(5):
            if ctrl.state == LearningState.DISCHARGE_2:
                break
            fake_bq.set_ocvtaken(True)
            fake_bq.set_vok(False)
            ctrl.advance(100.0)
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)  # -> REBOUND_CHECK_2
        ctrl.advance(1.0)  # -> RELAX_2
        for _ in range(5):
            if ctrl.state == LearningState.VERIFY_UPDATE_STATUS_06:
                break
            fake_bq.set_ocvtaken(True)
            fake_bq.set_vok(False)
            ctrl.advance(100.0)
        ctrl.advance(1.0)  # -> FINAL_SNAPSHOT
        ctrl.advance(1.0)  # -> FINAL_RECHARGE (Q-38)
        _b23b_finish_recharge(ctrl)


class TestR3B3LearningDoneSeverity:
    """[regresszió, R3-B3 (R3-GUI-1, P3), DFAM-018 RECURRING] a LEARNING_DONE
    kódneve semleges, a negatív jelentés (AWAITING/FAILED) az eredményben
    van -- a központi `_infer_severity()` heurisztika (GOV-024) ezt nem
    látja, így minden eredmény INFO-ként (színtelenül) jelent meg az élő
    eseménynaplóban. A producer minősít: nem VERIFIED -> explicit WARNING;
    VERIFIED -> nincs severity-kulcs (INFO marad). Az üzenet változatlan."""

    @staticmethod
    def _done_event(events):
        done = [e for e in events if e.get("event_code") == "LEARNING_DONE"]
        assert len(done) == 1
        return done[0]

    def test_real_non_verified_result_emits_warning(self):
        ctrl, *_ = make_controller()
        events = []
        ctrl.on_event = events.append
        ctrl._run_final_snapshot()
        _b23b_finish_recharge(ctrl)
        assert ctrl.result_summary.result != LEARNING_RESULT_VERIFIED
        ev = self._done_event(events)
        assert ev["severity"] == "WARNING"
        assert ev["event_message"] == f"eredmény: {ctrl.result_summary.result}"

    @pytest.mark.parametrize(
        "result, expected",
        [
            (LEARNING_RESULT_AWAITING, "WARNING"),
            (LEARNING_RESULT_FAILED, "WARNING"),
            (LEARNING_RESULT_VERIFIED, None),
        ],
        ids=["awaiting", "failed", "verified"],
    )
    def test_severity_follows_classified_result(self, monkeypatch, result, expected):
        import dataclasses

        ctrl, *_ = make_controller()
        real_classify = ctrl._classify_result
        monkeypatch.setattr(
            ctrl, "_classify_result",
            lambda: dataclasses.replace(real_classify(), result=result),
        )
        events = []
        ctrl.on_event = events.append
        ctrl._run_final_snapshot()
        _b23b_finish_recharge(ctrl)
        assert ctrl.result_summary.result == result
        ev = self._done_event(events)
        assert ev["event_message"] == f"eredmény: {result}"
        if expected is None:
            # VERIFIED: NINCS explicit severity -- a heurisztika INFO-ja marad.
            assert "severity" not in ev
        else:
            assert ev["severity"] == expected


class TestRelaxAcceptance:
    def test_relax_waits_for_ocvtaken_even_after_min_time(self):
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                max_relax_extra_wait_s=100.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        # min_relax_s (1.0s) letelik, de OCVTAKEN=0 marad -> NEM lép tovább
        ctrl.advance(2.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

    def test_relax_proceeds_after_max_extra_wait_with_quality_flag(self):
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                max_relax_extra_wait_s=2.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        ctrl.advance(1.5)   # min idő letelt, de nincs OCVTAKEN
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        ctrl.advance(3.0)   # max_relax_extra_wait_s is letelt
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        assert any("RELAX_ACCEPTANCE_NOT_CONFIRMED" in f for f in ctrl.quality_flags)

    def test_relax_dmm_fault_total_recorded_on_extra_wait_timeout(self):
        """CF-05 2. MAX verifier P3-2 lelete (2026-09-08): a `_finalize_
        relax_ctrl_totals()` két hívási helye (BQ+dV/dt elfogadva / extra-
        wait timeout) közül a régi tesztkészlet csak az ELFOGADÁS ágat
        fedte le mutációval diszkriminálóan — a TIMEOUT ág finalize hívása
        eltávolítható volt anélkül, hogy bármelyik teszt elbukott volna.
        Ez a teszt kifejezetten a timeout ágat célozza: egy DMM-hiba a
        relax alatt, majd a hurok max_relax_extra_wait_s-ig fut (OCVTAKEN
        sosem teljesül) -- a session-szintű relax_dmm_fault_total-nak a
        timeout UTÁN is tükröznie kell a hibát."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                max_relax_extra_wait_s=2.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_v.simulate_timeout = True
        ctrl.advance(0.5)  # RELAXING tick, DMM-hiba a RelaxControllerben
        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)  # min idő letelt, de nincs OCVTAKEN -> extra-wait
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl.relax_dmm_fault_total == 1  # élő tag (P3-1 fix)

        ctrl.advance(3.0)  # max_relax_extra_wait_s is letelt -> timeout ág
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT

        assert ctrl.relax_dmm_fault_total == 1
        assert sum(1 for f in ctrl.quality_flags if "RELAX_DMM_FAULTS" in f) == 1

    def test_dvdt_gate_uses_fresh_measurement_during_extra_wait(self):
        """CF-05 (governance-v2 review-kör 1, DFAM-008, ceiling P1 a BQ-
        tanulási úton): korábban a dV/dt kapu a RELAX_DONE-t ELSŐKÉNT
        kiváltó EGYETLEN tick mintájára fagyott (l. relax_controller.py::
        advance() korábbi "if RELAX_DONE: return" korai kilépése), és
        SOSEM frissült a BQ-elfogadási extra-wait hurok további (akár
        max_relax_extra_wait_s-ig tartó) ideje alatt. Ez azt jelentette,
        hogy egy NAGY, egyszeri feszültségugrás pontosan a min_relax_s-t
        kiváltó ticken VÉGLEGESEN letiltotta volna az elfogadást, akkor
        is, ha a feszültség rögtön utána stabilizálódott -- a fix nélkül
        ez a teszt az extra-wait timeoutig futna, NEM a dV/dt-alapú
        elfogadásig."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                max_relax_extra_wait_s=2 * RelaxConfig().sample_period_s, it_enable_settle_s=1.0, verify_settle_s=1.0,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        # A RelaxController lusta konstrukciója + INIT-tickje: rögzíti a
        # kiindulási feszültséget (dV/dt-hez), MÉG NEM éri el min_relax_s-t.
        # (make_controller alapértelmezett dmm_voltage_V=13.0 -- explicit
        # 12.5V-re állítjuk itt, hogy a lenti ugrás TÉNYLEGES változás
        # legyen, ne a már meglévő értékkel egyező no-op.)
        dmm_v.voltage_V = 12.5
        ctrl.advance(0.0)

        # BQ-oldali feltétel MÁR teljesül -- az elfogadás kizárólag a
        # dV/dt kapun múlik.
        _accept_relax(ctrl, fake_bq)

        # Nagy feszültségugrás PONTOSAN a min_relax_s-t kiváltó ticken.
        dmm_v.voltage_V = 13.0
        ctrl.advance(1.0)  # min_relax_s letelik -> RELAX_DONE, dV/dt nagy -> extra-wait
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        # R16-B4: the step must leave a fully measured stable window.
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        _complete_relax_window(ctrl)

        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        assert not any("RELAX_ACCEPTANCE_NOT_CONFIRMED" in f for f in ctrl.quality_flags)


class TestSafety:
    def test_relax_dmm_fault_total_survives_emergency_stop_during_extra_wait(self):
        """CF-05 2. MAX verifier P3-1 lelete (2026-09-08): `_finalize_relax_
        ctrl_totals()` csak a KÉT normál kilépési ágon (BQ+dV/dt elfogadva
        / extra-wait timeout) fut le -- egy emergency_stop()-pal megszakadó
        session a relax-fázis alatt korábban ELVESZTETTE volna a relax
        közben már bekövetkezett DMM-hiba bizonyítékát, mert a `relax_dmm_
        fault_total` property csak a carry-overölt (finalizált) összeget
        adta vissza, a self._relax_ctrl élő, még nem finalizált dmm_fault_
        count-ját nem. Javítva: a property most az élő relax_ctrl értékét
        is hozzáadja (l. extreme_dt_s_count/max_observed_dt_s minta)."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                max_relax_extra_wait_s=100.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_v.simulate_timeout = True
        ctrl.advance(0.5)  # RELAXING tick, DMM-hiba a RelaxControllerben
        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)  # min idő letelt, nincs OCVTAKEN -> extra-wait
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        ctrl.emergency_stop("TEST_ABORT_DURING_RELAX_EXTRA_WAIT")

        assert ctrl.relax_dmm_fault_total == 1

    def test_discharge_dmm_lost_faults(self):
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(False)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            ctrl.advance(1.0)
        ctrl.advance(1.0)  # snapshot
        ctrl.advance(1.0)  # it_enable
        ctrl.advance(1.0)  # settle
        ctrl.advance(1.0)  # verify vok/qen -> discharge setup
        assert ctrl.state == LearningState.DISCHARGE_1

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert load._input_is_on is False

    def test_emergency_stop_turns_off_load_and_psu(self):
        ctrl, psu, load, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.emergency_stop("MANUAL_TEST")
        assert ctrl.state == LearningState.FAULT
        assert load._input_is_on is False
        assert psu._output_is_on is False

    def test_advance_after_terminal_state_is_noop(self):
        ctrl, *_ = make_controller()
        ctrl.emergency_stop("X")
        state_before = ctrl.state
        ctrl.advance(100.0)
        assert ctrl.state == state_before

    def test_discharge_load_off_failure_escalates_to_fault_not_silently_ignored(self):
        """[review-lelet] load.input_off() hibáját a kisütés vége NEM
        nyelheti el csendben - relax 0A feltételezését érvénytelenítené
        (ugyanaz az elv, mint ChemIdCharacterizationController._run_c20_discharge())."""
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(False)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            ctrl.advance(1.0)
        ctrl.advance(1.0)  # snapshot
        ctrl.advance(1.0)  # it_enable
        ctrl.advance(1.0)  # settle
        ctrl.advance(1.0)  # verify vok/qen -> discharge setup
        assert ctrl.state == LearningState.DISCHARGE_1

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        dmm_v.voltage_V = 9.0  # terminate_voltage_pack_V alá
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "DISCHARGE_LOAD_OFF_FAILED" in ctrl.fault_reason

    def test_discharge_1_current_far_below_setpoint_gives_warning_not_fault(self):
        """[regresszió, Hardver/Műszervezérlés HW4-02 (review-kör 4, P1,
        DFAM-001)] l. discharge_controller.py testvér tesztjének
        docstringje ugyanerről a hiányzó setpoint-vs-mért kapuról."""
        fake_bq = _FakeBqState()
        ctrl, _psu, load, dmm_v, _dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(False)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            ctrl.advance(1.0)
        ctrl.advance(1.0)  # snapshot
        ctrl.advance(1.0)  # it_enable
        ctrl.advance(1.0)  # settle
        ctrl.advance(1.0)  # verify vok/qen -> discharge setup
        assert ctrl.state == LearningState.DISCHARGE_1

        load.current_A = ctrl._discharge_current_A * 0.1  # a setpoint töredéke -- "ragadt"
        ctrl.advance(1.0)
        assert ctrl.state != LearningState.FAULT
        assert ctrl.last_warning_code == "LOAD_CURRENT_SETPOINT_NOT_REACHED"

    def test_discharge_1_setpoint_warning_tick_counter_survives_higher_priority_warning(self):
        """[regresszió, MAX verifier (review-kör 4, HW4-02, 329c1a3) N1
        lelete] l. discharge_controller.py testvér tesztjének docstringje
        ugyanerről a hiányzó, a _last_warning_code slot-tól független
        tick-számlálóról."""
        fake_bq = _FakeBqState()
        ctrl, _psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(False)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            ctrl.advance(1.0)
        ctrl.advance(1.0)  # snapshot
        ctrl.advance(1.0)  # it_enable
        ctrl.advance(1.0)  # settle
        ctrl.advance(1.0)  # verify vok/qen -> discharge setup
        assert ctrl.state == LearningState.DISCHARGE_1

        dmm_t.temperature_C = 40.0  # profil névleges tartományán (15-30C) kívül -- MINDEN tickben aktív
        load.current_A = ctrl._discharge_current_A * 0.1  # a setpoint töredéke -- "ragadt"
        assert ctrl.load_current_setpoint_warning_ticks == 0
        ctrl.advance(1.0)
        assert ctrl.last_warning_code == "TEMPERATURE_OUT_OF_RANGE"
        assert ctrl.load_current_setpoint_warning_ticks == 1

    def test_reverse_polarity_during_discharge_1_faults_not_silently(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Aktív
        DISCHARGE_1 alatt egy fizikailag ésszerűtlen (<=0V) DMM-olvasás
        korábban csendben a normál kisütési-vég útvonalat futtatta volna
        (rebound-ellenőrzésbe lépve), NEM FAULT-ot (CLAUDE.md §10.6/SAFE-009)."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(False)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            ctrl.advance(1.0)
        ctrl.advance(1.0)  # snapshot
        ctrl.advance(1.0)  # it_enable
        ctrl.advance(1.0)  # settle
        ctrl.advance(1.0)  # verify vok/qen -> discharge setup
        assert ctrl.state == LearningState.DISCHARGE_1

        dmm_v.voltage_V = 0.0
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == "REVERSE_POLARITY_OR_WIRING_FAULT"

    def test_concurrent_psu_during_discharge_1_faults(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Aktív
        DISCHARGE_1 alatt egy PSU ON hardveres konfliktust korábban a
        beágyazott ág sosem ellenőrizte (CLAUDE.md §10.2 defense-in-depth
        hiány)."""
        fake_bq = _FakeBqState()
        ctrl, psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(False)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            ctrl.advance(1.0)
        ctrl.advance(1.0)  # snapshot
        ctrl.advance(1.0)  # it_enable
        ctrl.advance(1.0)  # settle
        ctrl.advance(1.0)  # verify vok/qen -> discharge setup
        assert ctrl.state == LearningState.DISCHARGE_1

        psu.output_commanded_on = True
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "CONCURRENT" in ctrl.fault_reason

    def test_it_enable_failure_attempts_reseal_before_fault(self):
        """[review-lelet] egy sikertelen IT_ENABLE után best-effort SEAL,
        hogy a BQ ne maradjon UNSEALED állapotban a hiba-ágon."""
        fake_bq = _FakeBqState()

        def _raise_it_enable(params):
            raise RuntimeError("simulated it_enable failure")

        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(fake_bq=fake_bq)
        # A transportot manuálisan cseréljük egy olyan verzióra, ahol
        # it_enable hibázik, de seal-t figyeljük.
        results = {
            "chem_id": lambda params: fake_bq.chem_id,
            "control_status": lambda params: fake_bq.control_status_word,
            "flags": lambda params: fake_bq.flags_word,
            "cycle_count": lambda params: fake_bq.cycle_count,
            "dod0": lambda params: fake_bq.dod0,
            "device_type": lambda params: 0x0541,
            "firmware_version": lambda params: 0x0311,
            "hardware_version": lambda params: 0x0003,
            "voltage_mV": lambda params: 12500,
            "average_current_mA": lambda params: 0,
            "temperature_01K": lambda params: 2951,
            "soc_percent": lambda params: 50,
            "remaining_capacity_mAh": lambda params: 3500,
            "full_charge_capacity_mAh": lambda params: 7000,
            "data_flash_block": _data_flash_handler(fake_bq),
            "unseal": _seal_state_recorder(fake_bq, "unseal", sealed_after=False),
            "seal": _seal_state_recorder(fake_bq, "seal", sealed_after=True),
            "write_data_flash_block": _write_data_flash_block_handler(fake_bq),
            "it_enable": _raise_it_enable,
        }
        ctrl._bq_transport = PicoBQTransport(serial=FakePicoSerial(results=results))
        ctrl._bq_driver = ctrl._bq_driver.__class__(transport=ctrl._bq_transport)

        ctrl._state = LearningState.IT_ENABLE_STEP
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "IT_ENABLE_FAILED" in ctrl.fault_reason
        assert "seal" in fake_bq.calls
        # Block 3 (2026-08-25, GAP #5): a Cell BL temp-write MÁR megtörtént
        # (it_enable csak UTÁNA hibázik), ezért emergency_stop()-nak a
        # restore-t is meg kell próbálnia — ez egy MÁSODIK write_data_flash_
        # block hívást eredményez (1. a temp-write, 2. a restore).
        assert fake_bq.calls.count("write_data_flash_block") == 2
        assert ctrl.result_summary.cell_bl_restore_confirmed is True

    def test_it_enable_failure_seal_cleanup_error_surfaces_in_quality_flags(self):
        """Review-kör 5, Architektúra A-1 (P1) -- l. test_it_enable_failure_
        attempts_reseal_before_fault() azonos felépítése, de itt maga a
        best-effort SEAL is elhasal: korábban ez nyomtalanul eltűnt volna
        (`except Exception: pass`), most a quality_flags-ben attesztálva
        van, ÉS a FAULT-ba lépés (emergency_stop) továbbra is lefut."""
        fake_bq = _FakeBqState()

        def _raise_it_enable(params):
            raise RuntimeError("simulated it_enable failure")

        def _raise_seal(params):
            raise RuntimeError("simulated seal failure")

        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(fake_bq=fake_bq)
        results = {
            "chem_id": lambda params: fake_bq.chem_id,
            "control_status": lambda params: fake_bq.control_status_word,
            "flags": lambda params: fake_bq.flags_word,
            "cycle_count": lambda params: fake_bq.cycle_count,
            "dod0": lambda params: fake_bq.dod0,
            "device_type": lambda params: 0x0541,
            "firmware_version": lambda params: 0x0311,
            "hardware_version": lambda params: 0x0003,
            "voltage_mV": lambda params: 12500,
            "average_current_mA": lambda params: 0,
            "temperature_01K": lambda params: 2951,
            "soc_percent": lambda params: 50,
            "remaining_capacity_mAh": lambda params: 3500,
            "full_charge_capacity_mAh": lambda params: 7000,
            "data_flash_block": _data_flash_handler(fake_bq),
            "unseal": _seal_state_recorder(fake_bq, "unseal", sealed_after=False),
            "seal": _raise_seal,
            "write_data_flash_block": _write_data_flash_block_handler(fake_bq),
            "it_enable": _raise_it_enable,
        }
        ctrl._bq_transport = PicoBQTransport(serial=FakePicoSerial(results=results))
        ctrl._bq_driver = ctrl._bq_driver.__class__(transport=ctrl._bq_transport)

        ctrl._state = LearningState.IT_ENABLE_STEP
        ctrl.advance(1.0)

        assert ctrl.state == LearningState.FAULT
        assert "IT_ENABLE_FAILED" in ctrl.fault_reason
        assert any(
            f.startswith("BQ_SEAL_RESTORE_FAILED_AT_IT_ENABLE:") for f in ctrl.quality_flags
        )

    def test_it_enable_refuses_temp_write_if_original_cell_bl_unrestorable(self):
        """[regresszió, Review-kör 2 (2026-09-08), Metrológia F-03 lelet
        (CF-40 P2-3 gyökér-megelőzése)] ha a chipen olvasott EREDETI Cell
        BL Set Volt Threshold érték (itt: egy törölt/soha-nem-írt blokk
        0xFFFF-ként, azaz -1-ként dekódolva) nem esne át az encode_df_
        field() Min/Max ellenőrzésén (Min=0), a temp-write EL SEM
        INDULHAT -- különben a restore a ciklus VÉGÉN garantáltan
        ValueError-ral hiúsulna, és a chip VÉGLEGESEN az ideiglenes
        learning-küszöbön ragadna. A kapunak MÉG A TEMP-WRITE ELŐTT kell
        elkapnia ezt, nem csak a restore-kísérletkor."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller(fake_bq=fake_bq)
        # Cell BL Set Volt Threshold (SC49/offset9, I2) törölt/soha-nem-írt
        # blokkot szimulál -- 0xFFFF -> -1, ami a Min=0 korlátot sérti.
        block = bytearray(32)
        block[9] = 0xFF
        block[10] = 0xFF
        fake_bq._written_blocks[(49, 0)] = block

        ctrl._state = LearningState.IT_ENABLE_STEP
        ctrl.advance(1.0)

        assert ctrl.state == LearningState.FAULT
        assert "CELL_BL_ORIGINAL_VALUE_UNRESTORABLE" in ctrl.fault_reason
        # A fizikai IT_ENABLE parancs SOSEM ment ki.
        assert "it_enable" not in fake_bq.calls
        # A temp-write SOSEM történt meg -- csak a best-effort re-seal
        # futott, semmilyen write_data_flash_block hívás nem történt
        # (sem temp-write, sem -- mivel _cell_bl_original_mV sosem
        # íródott be -- egy hiábavaló restore-kísérlet).
        assert "write_data_flash_block" not in fake_bq.calls
        assert "seal" in fake_bq.calls

    def test_it_enable_refuses_temp_write_on_degenerate_sealed_block(self):
        """[regresszió, Kör 21 F18, HW21-01, SCF-011 (a) opció] A fenti
        UNRESTORABLE-kapu (0xFFFF -> -1) NEM fogta el a HIL-igazolt SEALED-módú
        degenerált blokkot: abban a `cell_bl_set_volt_mV` bájtjai (offset 9-10)
        ÉPPEN 0x00 0x00, tehát `0`-ra dekódolódtak, ami ÁTMEGY a CF-40 Min=0
        ellenőrzésen. A védelem nélkül tehát `_cell_bl_original_mV = 0` lett
        volna, a temp-write elindult volna, és a session végén a
        `_restore_cell_bl_if_needed()` a FIZIKAI chipbe írta volna vissza a
        `0`-t `passed=True`-val -- a readback-verify csak azt igazolja, hogy a
        `0` HELYESEN íródott ki, nem azt, hogy a `0` HELYES érték volt.

        A szolgáltatás-szintű kapu (bq_data_flash_service.py) most MÁR a
        `read_field()`-nél dob, így a vezérlő fail-closed módon FAULT-ba megy:
        se IT_ENABLE, se temp-write, se restore-írás."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller(fake_bq=fake_bq)
        fake_bq._written_blocks[(49, 0)] = bytearray(DEGENERATE_SEALED_DF_BLOCK)

        # Maga a finding: védelem nélkül ez a blokk 0-t adott volna.
        assert decode_df_field(FIELD_REGISTRY["cell_bl_set_volt_mV"], DEGENERATE_SEALED_DF_BLOCK) == 0

        ctrl._state = LearningState.IT_ENABLE_STEP
        ctrl.advance(1.0)

        assert ctrl.state == LearningState.FAULT
        assert "IT_ENABLE_FAILED" in ctrl.fault_reason
        assert "BQ-DF-SEALED-001," in ctrl.fault_reason
        assert "it_enable" not in fake_bq.calls
        # SEMMILYEN DF-írás nem történt -- sem temp-write, sem restore.
        assert "write_data_flash_block" not in fake_bq.calls
        # A chip visszazárása megtörtént (nem marad UNSEALED egy FAULT után).
        assert "seal" in fake_bq.calls
        # `_cell_bl_original_mV` sosem íródott be -> a restore garantáltan no-op,
        # tehát a 0 SOHA nem juthat vissza a chipbe (az archívum saját
        # regressziós követelménye).
        assert ctrl._cell_bl_original_mV is None
        ctrl._restore_cell_bl_if_needed()
        assert "write_data_flash_block" not in fake_bq.calls


class TestReleaseAudit20260823AhBookkeeping:
    """Release-audit 2026-08-23 (2.7): accumulated_charge_Ah/
    accumulated_discharge_Ah property — a TestRunner._run_step() ezt a
    generikus getattr(controller, "accumulated_charge_Ah", 0.0) mintán
    keresztül olvassa a session-szintű Ah-összesítéshez (CHEMID-AH-GAP-01
    mintázatának kiterjesztése erre a controllerre). Enélkül a mező hiánya
    miatt egy teljes, valós fizikai ciklust futtató session mindig
    0.0000 Ah-t mutatott volna a report.json-ban/GUI-ban."""

    def test_accumulated_charge_ah_reflects_live_charge_ctrl(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + PRECHARGE
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._integrator.add_sample(1.5, 13.0, 3600.0)  # 1.5A*1h

        assert ctrl.accumulated_charge_Ah == pytest.approx(1.5)

    def test_accumulated_charge_ah_preserved_across_precharge_to_recharge(self):
        """[a CRITICAL lelet lényege] PRECHARGE Ah-ja NEM veszhet el, amikor
        a RECHARGE lépés egy ÚJ ChargeController-t hoz létre."""
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + PRECHARGE
        ctrl._charge_ctrl._integrator.add_sample(1.5, 13.0, 3600.0)  # PRECHARGE: 1.5 Ah
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # CHARGE_DONE feldolgozása -> PRECHARGE_RELAX

        assert ctrl._charge_ctrl is None
        assert ctrl.accumulated_charge_Ah == pytest.approx(1.5)

        ctrl._state = LearningState.RECHARGE
        ctrl.advance(0.0)  # új ChargeController jön létre (lusta konstrukció)
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._integrator.add_sample(2.0, 13.0, 3600.0)  # RECHARGE: 2.0 Ah

        assert ctrl.accumulated_charge_Ah == pytest.approx(3.5)  # 1.5 + 2.0

    def test_accumulated_charge_wh_preserved_across_precharge_to_recharge(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P2] az
        accumulated_charge_Ah Wh-párja — ugyanaz a PRECHARGE->RECHARGE
        carry-over minta, mint fent."""
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        ctrl._charge_ctrl._integrator.add_sample(1.5, 13.0, 3600.0)  # PRECHARGE: 19.5 Wh
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # CHARGE_DONE feldolgozása -> PRECHARGE_RELAX

        assert ctrl.accumulated_charge_Wh == pytest.approx(1.5 * 13.0)

        ctrl._state = LearningState.RECHARGE
        ctrl.advance(0.0)
        ctrl._charge_ctrl._integrator.add_sample(2.0, 13.0, 3600.0)  # RECHARGE: 26.0 Wh

        assert ctrl.accumulated_charge_Wh == pytest.approx(1.5 * 13.0 + 2.0 * 13.0)

    def test_extreme_dt_s_count_preserved_across_precharge_to_recharge(self):
        """[regresszió, Teljes kódbázis review 11. kör (2026-08-31),
        Metrológia/Adatintegritás P3 lelet] ugyanaz a PRECHARGE->RECHARGE
        carry-over minta, mint accumulated_charge_Ah/Wh-nál fent — egy
        extrém dt_s-tüske a PRECHARGE fázis alatt (ami sikeresen lezárul)
        korábban elveszett, ha a RECHARGE-fázis saját (tiszta) extreme_
        dt_s_count-ja fedte el a végső TestResult-ban (a 9. kör
        capacity_result_quality kapuja óta ez session-szintű minőségi
        jelző, nem csak diagnosztika)."""
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + PRECHARGE
        # Kiugró dt_s (>300s küszöb) a PRECHARGE fázis alatt.
        ctrl._charge_ctrl._integrator.add_sample(1.5, 13.0, 301.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # CHARGE_DONE feldolgozása -> PRECHARGE_RELAX

        assert ctrl._charge_ctrl is None
        assert ctrl.extreme_dt_s_count == 1
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)

        ctrl._state = LearningState.RECHARGE
        ctrl.advance(0.0)  # új ChargeController jön létre (lusta konstrukció)
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._integrator.add_sample(2.0, 13.0, 30.0)  # RECHARGE: tiszta, normál dt_s

        assert ctrl.extreme_dt_s_count == 1  # a PRECHARGE-beli tüske NEM veszett el
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)


class TestUBattVProperty:
    """2026-08-23 traditional_learning OV incidens elemzés: u_batt_V
    property — a TestRunner._build_sample() ezt olvassa a generikus
    getattr(controller, "u_batt_V", None) mintán keresztül, hogy a
    naplózott battery_voltage_V a controller SAJÁT, ugyanabban a tick-ben
    a safety-check-hez ténylegesen használt olvasása legyen, ne egy
    második, deszinkron InstrumentManager.read_all() DMM-lekérdezés
    eredménye (CLAUDE.md §9). Enélkül a mező hiánya miatt getattr mindig
    None-t adott volna vissza (CHEMID-AH-GAP-01 mintázatának
    kiterjesztése, l. accumulated_charge_Ah fent)."""

    def test_u_batt_v_reflects_precharge_reading(self):
        ctrl, *_ = make_controller(dmm_voltage_V=13.37)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE (ChargeController konstrukció + advance)
        assert ctrl.u_batt_V == pytest.approx(13.37)


class TestBatteryTemperatureCProperty:
    """Ugyanaz az elv, mint TestUBattVProperty-nél, de a hőmérséklet-DMM-re
    (instrument_manager.py skip_battery_temperature bekötése, l.
    battery_temperature_C docstringje)."""

    def test_battery_temperature_c_reflects_precharge_reading(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        assert ctrl.battery_temperature_C == pytest.approx(22.0)  # MockDMM(temperature_C=22.0)

    def test_battery_temperature_c_defaults_before_any_charge_tick(self):
        ctrl, *_ = make_controller()
        assert ctrl.battery_temperature_C == pytest.approx(20.0)


class TestChargeConfigPropagation:
    """2026-08-23 (felhasználói review, 2. kör — traditional_learning OV
    incidens): a session/GUI töltési konfigurációja (charge_current_A_
    override, taper_hold_s) mostantól eljut a beágyazott ChargeController-
    hez — korábban a controller mindig egy bare ChargeConfig()-ot hozott
    létre, figyelmen kívül hagyva a hívó explicit beállításait."""

    def test_charge_current_override_reaches_embedded_charge_ctrl(self):
        charge_cfg = ChargeConfig(charge_current_A_override=0.8)
        ctrl, *_ = make_controller(dmm_voltage_V=13.0, charge_config=charge_cfg)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + PRECHARGE
        assert ctrl._charge_ctrl is not None
        # A beágyazott ChargeController saját INIT->PRECHECK->PSU_PRESET->
        # CHARGE_CC állapotgépét futtatjuk, amíg a preset ténylegesen lefut.
        for _ in range(5):
            if ctrl._charge_ctrl.state == ChargeState.CHARGE_CC:
                break
            ctrl._charge_ctrl.advance(1.0)
        assert ctrl._charge_ctrl._charge_current_set_A == pytest.approx(0.8)

    def test_no_explicit_charge_config_falls_back_to_default(self):
        """Ha a hívó nem ad charge_config-ot, a régi, bare ChargeConfig()
        marad az alapértelmezés — visszafelé kompatibilis."""
        ctrl, *_ = make_controller(dmm_voltage_V=13.0)
        assert ctrl._charge_config.charge_current_A_override == 0.0


class TestUPsuSetVAndWarningProxyProperties:
    """2026-08-23 (felhasználói jelzés: "A PSU SET V még mindíg nem mutat
    értéket"): u_psu_set_V/last_warning_code proxy property-k — ugyanaz a
    getattr-mintázat, mint u_batt_V-nél, l. TestUBattVProperty."""

    def test_u_psu_set_v_reflects_embedded_charge_ctrl(self):
        ctrl, *_ = make_controller(dmm_voltage_V=13.37)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE (ChargeController konstrukció)
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._u_psu_set = 15.25
        assert ctrl.u_psu_set_V == pytest.approx(15.25)

    def test_last_integration_source_is_not_a_live_proxy_of_embedded_charge_ctrl(self):
        """[regresszió, Teljes kódbázis review 11. kör (2026-08-31),
        Szoftver/Architektúra P3 lelet] l. u_psu_set_V teszt fenti
        docstringje ugyanerről a hiányzó proxy-mintáról. R6-B3 (R6-
        metrology-01, DFAM-043 pontositas): a last_integration_source
        azota NEM proxy, hanem a kompozit sajat, tick-scoped erteke --
        ez a teszt a nem-proxy szerzodest rogziti; a 11. kori proxy-
        hivatkozas torteneti."""
        ctrl, *_ = make_controller(dmm_voltage_V=13.37)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl._charge_ctrl is not None
        # R6-B3 (R6-metrology-01, DFAM-001): nem elo proxy -- a gyermek
        # forrasa csak a kovetkezo integralo advance()-ban rogzul.
        ctrl._charge_ctrl._last_integration_source = "PSU_READBACK"
        assert ctrl.last_integration_source == "ZERO"

    def test_last_integration_source_zero_when_no_charge_ctrl(self):
        ctrl, *_ = make_controller()
        assert ctrl.last_integration_source == "ZERO"

    def test_taper_timer_s_reflects_embedded_charge_ctrl(self):
        """[regresszió, Teljes kódbázis review 12. kör (2026-08-31),
        Szoftver/Architektúra P3 lelet] l. u_psu_set_V teszt fenti
        docstringje ugyanerről a hiányzó proxy-mintáról."""
        ctrl, *_ = make_controller(dmm_voltage_V=13.37)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._taper_timer_s = 42.0
        assert ctrl.taper_timer_s == pytest.approx(42.0)

    def test_taper_timer_s_zero_when_no_charge_ctrl(self):
        ctrl, *_ = make_controller()
        assert ctrl.taper_timer_s == 0.0

    def test_u_psu_set_v_zero_when_no_charge_ctrl(self):
        ctrl, *_ = make_controller()
        assert ctrl.u_psu_set_V == 0.0

    def test_last_warning_code_reflects_embedded_charge_ctrl(self):
        ctrl, *_ = make_controller(dmm_voltage_V=13.37)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._last_warning_code = "DIODE_POWER_TOO_HIGH"
        assert ctrl.last_warning_code == "DIODE_POWER_TOO_HIGH"

    def test_accumulated_discharge_ah_reflects_live_discharge_integrator(self):
        ctrl, *_ = make_controller()
        ctrl._discharge_integrator.add_sample(-2.0, 12.0, 3600.0)  # 2.0 Ah kisütve

        assert ctrl.accumulated_discharge_Ah == pytest.approx(2.0)

    def test_accumulated_discharge_ah_preserved_across_discharge_1_to_2(self):
        """DISCHARGE_1 Ah-ja NEM veszhet el, amikor a _begin_discharge()
        DISCHARGE_2-re nullázza az integrátort."""
        ctrl, *_ = make_controller()
        ctrl._discharge_integrator.add_sample(-2.0, 12.0, 3600.0)  # DISCHARGE_1: 2.0 Ah
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_discharge(LearningState.DISCHARGE_2)  # snapshot + reset

        assert ctrl.accumulated_discharge_Ah == pytest.approx(2.0)  # a friss integrátor 0-ról indul

        ctrl._discharge_integrator.add_sample(-1.2, 12.0, 3600.0)  # DISCHARGE_2: 1.2 Ah

        assert ctrl.accumulated_discharge_Ah == pytest.approx(3.2)  # 2.0 + 1.2

    def test_accumulated_discharge_wh_preserved_across_discharge_1_to_2(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P2] az
        accumulated_discharge_Ah Wh-párja — ugyanaz a DISCHARGE_1->2
        carry-over minta, mint fent."""
        ctrl, *_ = make_controller()
        ctrl._discharge_integrator.add_sample(-2.0, 12.0, 3600.0)  # DISCHARGE_1: 24.0 Wh
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_discharge(LearningState.DISCHARGE_2)

        assert ctrl.accumulated_discharge_Wh == pytest.approx(2.0 * 12.0)

        ctrl._discharge_integrator.add_sample(-1.2, 12.0, 3600.0)  # DISCHARGE_2: 14.4 Wh

    def test_extreme_dt_s_count_preserved_across_discharge_1_to_2(self):
        """[regresszió, Teljes kódbázis review 12. kör (2026-08-31),
        Metrológia/Adatintegritás P2 lelet] ugyanaz a DISCHARGE_1->2
        carry-over minta, mint accumulated_discharge_Ah/Wh-nál fent — egy
        extrém dt_s-tüske a DISCHARGE_1 fázis alatt korábban elveszett, ha
        a DISCHARGE_2-fázis saját (tiszta) extreme_dt_s_count-ja fedte el
        a végső TestResult-ban (a fb4e031/11. kör PRECHARGE->RECHARGE
        fixjével azonos hibaosztály, itt a kisütési oldalon)."""
        ctrl, *_ = make_controller()
        # Kiugró dt_s (>300s küszöb) a DISCHARGE_1 fázis alatt.
        ctrl._discharge_integrator.add_sample(-2.0, 12.0, 301.0)
        assert ctrl.extreme_dt_s_count == 1
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)

        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_discharge(LearningState.DISCHARGE_2)  # snapshot + reset

        assert ctrl.extreme_dt_s_count == 1  # a DISCHARGE_1-beli tüske NEM veszett el
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)

        ctrl._discharge_integrator.add_sample(-1.2, 12.0, 30.0)  # DISCHARGE_2: tiszta, normál dt_s

        assert ctrl.extreme_dt_s_count == 1
        assert ctrl.max_observed_dt_s == pytest.approx(301.0)

    def test_classify_result_measured_capacity_sums_both_discharge_cycles(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P2 lelet #2] _classify_result() korábban
        `self._discharge_integrator.accumulated_discharge_Ah`-t (az ÉLŐ,
        csak az UTOLSÓ ciklus integrátorát) olvasta measured_capacity_Ah-hoz
        — ha DISCHARGE_2 elindult, ez KIZÁRÓLAG a 2. ciklus kapacitását
        adta, a discharge_1_capacity_Ah-t figyelmen kívül hagyva. A helyes,
        MINDKÉT ciklust összegző accumulated_discharge_Ah property már
        létezett (l. fenti carry-over tesztek), de _classify_result() nem
        ezt olvasta. report.json-ba kerül, felhasználó-látható."""
        ctrl, *_ = make_controller()
        ctrl._discharge_integrator.add_sample(-2.0, 12.0, 3600.0)  # DISCHARGE_1: 2.0 Ah
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_discharge(LearningState.DISCHARGE_2)
        ctrl._discharge_integrator.add_sample(-1.2, 12.0, 3600.0)  # DISCHARGE_2: 1.2 Ah

        summary = ctrl._classify_result()

        assert summary.measured_capacity_Ah == pytest.approx(3.2)  # 2.0 + 1.2, NEM csak 1.2

        assert ctrl.accumulated_discharge_Wh == pytest.approx(2.0 * 12.0 + 1.2 * 12.0)


class TestReleaseAudit20260823TempDmmGap01Extension:
    """Release-audit 2026-08-23 (2.12, TEMP-DMM-GAP-01 kiterjesztése): a
    hőmérséklet-DMM kiesését korábban a relax/kisütés fázisokban
    _read_dmm() csendben elnyelte, FAULT-ot sosem váltott ki — a PRECHARGE/
    RECHARGE fázist a beágyazott ChargeController saját védelme már fedte,
    a relax/kisütés fázisokat nem."""

    def test_temp_dmm_failure_during_precharge_relax_faults(self):
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_t.simulate_temp_failure = True
        ctrl.advance(70.0)  # MONITOR_ONLY timeout=60s, egy tick alatt túllépve

        assert ctrl.state == LearningState.FAULT
        assert "TEMPERATURE" in ctrl.fault_reason

    def test_temp_dmm_failure_recovers_when_reading_succeeds_again(self):
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)  # 10s < 60s timeout, még nem faultol
        assert ctrl.state == LearningState.PRECHARGE_RELAX

    def test_temp_dmm_warning_during_precharge_relax_sets_last_warning_code(self):
        """[regresszió, Teljes kódbázis review 9. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet] last_warning_code korábban KIZÁRÓLAG a
        beágyazott ChargeController-t proxyzta (PRECHARGE/RECHARGE alatt) —
        a PRECHARGE_RELAX fázisban a saját szintű hőmérséklet-DMM
        MONITOR_ONLY figyelmeztetés kiszámolódott, de sosem jutott el a
        property-ig."""
        ctrl, _psu, _load, _dmm_v, dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX

        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)  # 10s < 60s timeout, WARNING, nem FAULT

        assert ctrl.state != LearningState.FAULT
        assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"

    def test_temp_dmm_fault_ticks_counts_failed_reads_not_seconds(self):
        """[regresszió, review-kör 3 (2026-09-09), friss Metrológia M3-02 /
        Szoftver/Architektúra F2 (P2)] A TraditionalLearningController
        korábban hiányzott a temp_dmm_fault_ticks property/számlálóból,
        amely a chemid/golden_gg testvéreken már megvolt — a
        main_window.py temp_dmm_fault_ticks_total összege és a
        capacity_result_quality DEGRADED-gate emiatt sosem látta a
        TRADITIONAL_LEARNING session alatti hőmérséklet-DMM-kiesést.
        Tick-alapú (nem másodperc-alapú), a chemid_controller.py azonos
        tesztjének mintáját követve."""
        ctrl, _psu, _load, _dmm_v, dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl.temp_dmm_fault_ticks == 0

        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)  # 1 sikertelen tick, WARNING-küszöb alatt
        assert ctrl.temp_dmm_fault_ticks == 1

        ctrl.advance(5.0)  # 2. sikertelen tick, MÉG mindig WARNING alatt
        assert ctrl.temp_dmm_fault_ticks == 2

        dmm_t.simulate_temp_failure = False
        ctrl.advance(1.0)  # sikeres olvasás -- a tick-számláló NEM nullázódik
        assert ctrl.temp_dmm_fault_ticks == 2

    def test_temp_dmm_valid_true_after_successful_read(self):
        """[regresszió, MAX verifier (review-kör 3, a9cb161469c468fc8) P2-1]
        a temp_dmm_valid property (Metrológia M3-01, P1) egyik
        controlleren sem kapott önálló, controller-szintű regressziós
        tesztet -- ez a hiány engedte át a chemid_controller.py-beli P1-1
        blokkoló hibát 1792 zöld teszt mellett."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_after_failed_read(self):
        ctrl, _psu, _load, _dmm_v, dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_valid_true_delegated_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a3f0e6dcf93588360) P2-1]
        a self._dmm_valid (feszültség) delegálás a beágyazott
        ChargeControllerből mindig megtörtént PRECHARGE alatt, de a
        self._temp_dmm_valid nem -- egy ott ténylegesen hibázó
        hőmérséklet-DMM a szülő szintjén hamisan True-t jelentett volna,
        mert a saját _read_dmm() ebben az állapotban ki van kapcsolva."""
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        assert ctrl.state == LearningState.PRECHARGE

        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_delegated_from_embedded_charge_controller(self):
        ctrl, _psu, _load, _dmm_v, dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_fault_ticks_live_add_and_carry_over_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a306e7e764e3a717b) P2-1,
        DFAM-001 további előfordulása] l. test_chemid_controller.py azonos
        tesztjének docstringje ugyanerről a hiányzó live-add + carry-over
        mintáról. A TraditionalLearningController-nek 3-utas összegzése
        van (saját + élő gyerek + korábbi PRECHARGE/RECHARGE-fázisok
        carry-over összege), a temperature_out_of_range_count property
        mintáját követve."""
        ctrl, _psu, _load, _dmm_v, dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_fault_ticks == 2

        dmm_t.simulate_temp_failure = False
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # PRECHARGE -> következő állapot
        assert ctrl._charge_ctrl is None
        assert ctrl.temp_dmm_fault_ticks == 2


class TestReadDmmIndependentVoltageTemperatureChannels:
    """[regresszió, Teljes kódbázis review 9. kör (2026-08-30), Szoftver/
    Architektúra P2 lelet #2] _read_dmm() korábban a feszültség-DMM
    kivétele esetén korai return-nel megakadályozta a hőmérséklet-DMM
    olvasását is — két teljesen független fizikai műszerről van szó
    (Keysight 34465A #1/#2), egyik hibája nem indokolja a másik
    kihagyását. A testvér controllerek (discharge_controller.py,
    chemid_controller.py, ocv_soc_controller.py, golden_gg_controller.py)
    mindig két önálló try/except blokkot futtatnak."""

    def test_temperature_still_read_when_voltage_dmm_fails(self):
        ctrl, _psu, _load, dmm_v, dmm_t, _fake_bq = make_controller()
        dmm_v.set_dmm_invalid()
        dmm_t.temperature_C = 31.5

        result = ctrl._read_dmm(1.0)

        assert result is False
        assert ctrl._battery_temperature_C == 31.5
        assert ctrl._safety.temperature_dmm_fault_s == 0.0

    def test_voltage_still_read_when_temperature_dmm_fails(self):
        ctrl, _psu, _load, dmm_v, dmm_t, _fake_bq = make_controller()
        dmm_v.voltage_V = 13.37
        dmm_t.simulate_temp_failure = True

        result = ctrl._read_dmm(1.0)

        assert result is True
        assert ctrl._u_batt == 13.37
        assert ctrl._safety.temperature_dmm_fault_s == 1.0


class TestReboundCheck:
    """2026-08-24 — Folyamatok/tervek/BQ34Z110_learning_rebound_kisutes.txt
    (forrás: TI SLUSB55B "STEP 8: Run an Optimization Cycle"): a kisütés
    lezárása NEM egyszerű "U<=terminate -> LOAD OFF -> RELAX", hanem
    terhelés-levétel utáni rebound-mérés, és ha a visszapattant feszültség
    a learning rebound-cél + rebound_tolerance_V egyoldalú felső korlát
    fölött van, további C/10 kisütés alacsonyabb cutoff-küszöbbel,
    amíg a rebound erre a felső korlátra vagy alá nem esik (vagy el nem
    fogy a max_rebound_retries).

    Block 3 (2026-08-25, felhasználói döntés): a cutoff/rebound-cél
    forrása 2026-08-24 óta megváltozott — a profil normál FIAMM
    `terminate_voltage_pack_V` helyett a `LearningConfig.
    learning_discharge_floor_V_per_cell`/`learning_rebound_target_V_
    per_cell`-ből származik, l. _floor_V()/_target_V()."""

    @staticmethod
    def _target_V(ctrl) -> float:
        return ctrl._config.learning_rebound_target_V_per_cell * ctrl._profile.cell_count

    @staticmethod
    def _floor_V(ctrl) -> float:
        return ctrl._config.learning_discharge_floor_V_per_cell * ctrl._profile.cell_count

    def _drive_to_discharge_1(self, dmm_voltage_V: float = 13.0, config=None):
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            dmm_voltage_V=dmm_voltage_V, fake_bq=fake_bq,
            config=config or LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0, rebound_settle_s=2.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(False)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            ctrl.advance(1.0)
        ctrl.advance(1.0)  # snapshot
        ctrl.advance(1.0)  # it_enable
        ctrl.advance(1.0)  # settle
        ctrl.advance(1.0)  # verify vok/qen -> discharge setup
        assert ctrl.state == LearningState.DISCHARGE_1
        return ctrl, psu, load, dmm_v, dmm_t, fake_bq

    def test_rebound_accepted_on_first_attempt_no_retry(self):
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = self._drive_to_discharge_1()
        cutoff = ctrl._discharge_cutoff_V
        assert cutoff == pytest.approx(self._floor_V(ctrl))

        dmm_v.voltage_V = cutoff - 0.5  # cutoff alá visszük
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1
        assert load._input_is_on is False  # load levéve a rebound-méréshez

        # A MockDMM konstans feszültséget ad (nincs valós rebound
        # szimulálva) — a cél+tolerancia alatt marad, tehát a settle
        # letelte után elsőre elfogadott.
        ctrl.advance(2.0)  # rebound_settle_s=2.0 -> elfogadva
        assert ctrl.state == LearningState.RELAX_1
        assert ctrl._rebound_retry_count == 0
        assert not any("REBOUND" in f for f in ctrl.quality_flags)

    def test_ti_worked_example_rebound_accepted_at_cell_termination_voltage(self):
        """MAX verifier (2026-09-15, F-5, CLAIM_LEDGER.yaml BQ-REBOUND-001):
        diszkriminaló regresszió, amely a TI SLUSB55B STEP 8 CAUTION saját,
        nyomtatott (30. oldal) pélaszámait kódolja: egy 10.0V-os (6-cellás)
        terhelés-alatti cutoff után a rebound ~10.8V-ra (a Cell Termination
        Voltage-ra) pattan, és ezt EL KELL FOGADNI elsőre, retry nélkül. Ez
        a teszt a RÉGI, hibásan 1.75 V/cella-ra állított target alatt
        BUKOTT volna (10.8V > 1.75×6+0.1=10.6V -> feleslegesen retry-t
        váltott volna ki a TI forrás SAJÁT sikeres esetén is) -- ez
        pontosan az a defektus, amit a 2026-09-15-i 1.75->1.80 javítás
        megszüntetett."""
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = self._drive_to_discharge_1()
        cutoff = ctrl._discharge_cutoff_V
        dmm_v.voltage_V = cutoff - 0.5
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1
        assert load._input_is_on is False

        # A TI példa saját száma: a Cell Termination Voltage (1.800 V/cella),
        # a controller profiljának cell_count-jára skálázva -- ide pattan
        # vissza a feszültség a terhelés levétele után a TI sikeres esetében.
        ti_rebound_V = 1.800 * ctrl._profile.cell_count
        dmm_v.voltage_V = ti_rebound_V
        ctrl.advance(2.0)  # rebound_settle_s=2.0 -> kiértékelés

        assert ctrl.state == LearningState.RELAX_1, (
            f"A TI SLUSB55B STEP 8 saját sikeres esete ({ti_rebound_V}V) "
            f"elutasítva -- ez pontosan az eredeti, 1.75 V/cella targetre "
            f"állított hiba tünete lenne."
        )
        assert ctrl._rebound_retry_count == 0
        assert not any("REBOUND" in f for f in ctrl.quality_flags)

    def test_rebound_check_faults_on_reverse_polarity_voltage(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P1] a
        REBOUND_CHECK fázis korábban nem hívott check_battery_voltage()-t
        (SAFE-009) — egy negatív/ésszerűtlen feszültség hamis
        REBOUND_ACCEPTED-et adott volna a rendszernek egy megbízhatatlan
        mérésre alapozva, FAULT helyett."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1()
        cutoff = ctrl._discharge_cutoff_V

        dmm_v.voltage_V = cutoff - 0.5  # cutoff alá visszük -> REBOUND_CHECK_1
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1

        dmm_v.voltage_V = -1.0  # csendben félreolvasott (nem kivételt dobó) hibás bekötés
        ctrl.advance(2.0)  # rebound_settle_s=2.0

        assert ctrl.state == LearningState.FAULT
        assert "REVERSE_POLARITY" in ctrl.fault_reason

    def test_rebound_check_faults_on_concurrent_psu_load(self):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] a
        REBOUND_CHECK fázis korábban nem hívott check_concurrent_psu_load()-t
        (CLAUDE.md §10.2) — a testvér _run_discharge() már megkapta 1.
        körben. Ha a PSU eközben (pl. hibás relé) ON állapotba kerülne, ez
        korábban a settle-várakozás teljes ideje alatt észrevétlen maradt
        volna."""
        ctrl, psu, load, dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1()
        cutoff = ctrl._discharge_cutoff_V

        dmm_v.voltage_V = cutoff - 0.5  # cutoff alá visszük -> REBOUND_CHECK_1
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1
        assert load.input_commanded_on is False  # a Load levéve a rebound-méréshez

        # Szimulált hibás relé/konkurrens PSU aktivitás — a Load flag-jét is
        # ON-ra állítjuk, mert a valódi hardware-conflict-nak mindkét oldal
        # egyidejű aktivitása kell (l. check_concurrent_psu_load()).
        psu.output_commanded_on = True
        load.input_commanded_on = True
        ctrl.advance(2.0)  # rebound_settle_s=2.0

        assert ctrl.state == LearningState.FAULT
        assert "CONCURRENT_PSU_LOAD" in ctrl.fault_reason

    def test_begin_discharge_faults_on_concurrent_psu_load_before_energizing(self):
        """[Kör 20 Batch E13a, F20-S3, DFAM-001] `_begin_discharge()`
        korábban a `try:` blokk BELSEJÉBEN, feltétel nélkül futtatta
        `psu.all_outputs_off()`-t, majd `load.input_on()`-t -- egy stale
        `psu.output_commanded_on=True` flaget az `all_outputs_off()`
        CSENDBEN letörölt volna, MIELŐTT bármi észlelhetné a konfliktust.
        Ez a teszt a fix ELŐTTI kódon bukna: `ctrl.state` NEM lenne FAULT,
        és `load.input_commanded_on` True lenne."""
        ctrl, psu, load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        psu.output_commanded_on = True

        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_discharge(LearningState.DISCHARGE_1)

        assert ctrl.state == LearningState.FAULT
        assert "CONCURRENT_PSU_LOAD" in ctrl.fault_reason
        assert load.input_commanded_on is False

    def test_discharge_faults_on_load_power_limit_exceeded(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P1 lelet] A standalone DischargeController._run_cc() minden
        ticken ellenőrzi a Keithley 2380 250W hardver-teljesítménykorlátot —
        a beágyazott _run_discharge() korábban nem, ezért egy nagy pack +
        magas áram kombináció csendben, kontrollált FAULT nélkül futhatott
        volna a hardver saját belső védelméig."""
        ctrl, _psu, load, _dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1(dmm_voltage_V=13.0)
        assert ctrl.state == LearningState.DISCHARGE_1

        load.current_A = 25.0  # 13V * 25A = 325W > 250W max
        ctrl.advance(1.0)

        assert ctrl.state == LearningState.FAULT
        assert "LOAD_POWER_LIMIT" in ctrl.fault_reason

    def test_fault_during_discharge_1_reports_capacity_in_discharge_1_field_not_2(self):
        """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), Session/
        Workflow P2 lelet] discharge_2_capacity_Ah korábban feltétel
        nélkül az ÉLŐ self._discharge_integrator.accumulated_discharge_Ah-
        ból töltődött — egy FAULT DISCHARGE_1 közben (mielőtt DISCHARGE_2
        ténylegesen elindulna és _begin_discharge() resetelné az
        integrátort) azt eredményezte, hogy discharge_1_capacity_Ah=None,
        DE discharge_2_capacity_Ah TÉVESEN az 1. ciklus Ah-ját mutatta —
        felcserélt mezőcímkézés a report.json-ban."""
        ctrl, _psu, load, _dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1(dmm_voltage_V=13.0)
        assert ctrl.state == LearningState.DISCHARGE_1
        assert ctrl._discharge_1_capacity_Ah is None  # DISCHARGE_2 még nem indult

        load.current_A = 25.0  # 13V * 25A = 325W > 250W max -> FAULT
        ctrl.advance(1.0)

        assert ctrl.state == LearningState.FAULT
        summary = ctrl.result_summary
        assert summary is not None
        assert summary.discharge_1_capacity_Ah is not None
        assert summary.discharge_1_capacity_Ah > 0.0
        assert summary.discharge_2_capacity_Ah is None

    def test_discharge_faults_on_max_discharge_time_reached(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P2 lelet] a beágyazott DISCHARGE_1/DISCHARGE_2 fázis eddig
        KIZÁRÓLAG a feszültség-alapú leállási feltételre támaszkodott —
        egy sosem-csökkenő (pl. hibás/rögzült) DMM-olvasás mellett a
        kisütés időben korlátlanul futhatott volna."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1(dmm_voltage_V=13.0)
        assert ctrl.state == LearningState.DISCHARGE_1

        ctrl.advance(86400.1)

        assert ctrl.state == LearningState.FAULT
        assert "MAX_DISCHARGE_TIME_REACHED" in ctrl.fault_reason

    def test_discharge_faults_on_max_discharge_ah_reached(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P2 lelet] ugyanaz a hiányzó backstop az Ah-oldalon."""
        ctrl, _psu, load, _dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1(dmm_voltage_V=13.0)
        assert ctrl.state == LearningState.DISCHARGE_1

        load.current_A = 10.0  # nagy áram -> gyorsan túllépi a névleges kapacitás 110%-át
        ctrl.advance(3600.0)

        assert ctrl.state == LearningState.FAULT
        assert "MAX_DISCHARGE_AH_REACHED" in ctrl.fault_reason

    def test_discharge_faults_on_max_discharge_ah_reached_uses_c10_not_c20_base(self):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31), Szoftver/
        Architektúra P2 lelet] a fenti teszt csak azt igazolja, hogy a
        backstop EGYÁLTALÁN kivált — ez a teszt azt igazolja, hogy a
        kanonikus C10-bázisból (_c10_base_Ah), NEM a nyers (FG-sorozatnál
        C20-alapú) nominal_capacity_Ah-ból számol."""
        ctrl, _psu, load, _dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1(dmm_voltage_V=13.0)
        assert ctrl.state == LearningState.DISCHARGE_1
        # FG-sorozat szimulációja: nominal_capacity_Ah=C20=20.0, C10=10.0 —
        # az alap max_discharge_Ah_factor=1.10 mellett a C10-plafon 11.0 Ah,
        # a C20-plafon 22.0 Ah — a lenti 15.0 Ah a kettő KÖZÖTT van, tehát
        # csak a helyes (C10) bázisból számolva vált ki FAULT-ot.
        ctrl._profile = BatteryProfile(
            battery_name="TEST_FG", manufacturer="TEST", model="FG20AH",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=20.0,
            C10_capacity_Ah=10.0, terminate_voltage_per_cell_V=1.80,
        )

        load.current_A = 15.0
        ctrl.advance(3600.0)  # 15.0 Ah kisütve

        assert ctrl.state == LearningState.FAULT
        assert "MAX_DISCHARGE_AH_REACHED" in ctrl.fault_reason

    def test_discharge_max_time_respects_safety_manager_override(self):
        """[regresszió, Teljes kódbázis review 8. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet] max_discharge_time_s korábban egy privát,
        hardkódolt modul-konstansként élt itt — egy SafetyManager-en
        beállított egyedi érték NEM ért volna el ide. Mostantól
        self._safety.max_discharge_time_s a kanonikus forrás, a standalone
        DischargeController mintáját követve (5-8. kör konszolidáció)."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1(dmm_voltage_V=13.0)
        assert ctrl.state == LearningState.DISCHARGE_1
        ctrl._safety.max_discharge_time_s = 5.0  # << a 86400s alapértéktől

        ctrl.advance(10.0)  # a szűkített limit fölött, de az alapérték alatt

        assert ctrl.state == LearningState.FAULT
        assert "MAX_DISCHARGE_TIME_REACHED" in ctrl.fault_reason

    def test_rebound_too_high_resumes_discharge_with_lower_cutoff(self):
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = self._drive_to_discharge_1()
        cutoff_1 = ctrl._discharge_cutoff_V

        dmm_v.voltage_V = cutoff_1 - 0.1
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1
        assert load._input_is_on is False

        # Rebound TÚL magasra pattan vissza (a cél + rebound_tolerance_V
        # felső korlát fölé).
        dmm_v.voltage_V = self._target_V(ctrl) + 1.0
        ctrl.advance(2.0)  # settle letelik -> túl magas -> LOAD ON, cutoff csökken

        assert ctrl.state == LearningState.DISCHARGE_1
        assert load._input_is_on is True
        assert ctrl._rebound_retry_count == 1
        assert ctrl._discharge_cutoff_V < cutoff_1

        # Második próbálkozás: most már célon belüli rebound.
        dmm_v.voltage_V = ctrl._discharge_cutoff_V - 0.1
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1
        dmm_v.voltage_V = self._target_V(ctrl)
        ctrl.advance(2.0)
        assert ctrl.state == LearningState.RELAX_1
        assert ctrl._rebound_retries_1 == 1
        assert any("REBOUND_ACCEPTED_AFTER_RETRY" in f for f in ctrl.quality_flags)

    def test_rebound_retry_stale_psu_flag_blocks_load_input_on(self):
        """Teljes kódbázis review 15. kör (2026-09-04), Hardver/
        Műszervezérlés P1 lelet: a REBOUND_CHECK fázis ELEJÉN futó
        kölcsönös kizárás ellenőrzés a JELENLEGI load_commanded_on-t
        nézi, ami a retry-ág input_on() hívása ELŐTT (a Load a cutoff-
        detekció óta ki van kapcsolva) garantáltan False -- egy stale
        PSU-flag emiatt korábban minden ellenőrzés nélkül átment volna
        egészen a retry input_on()-ig. A 14. kör ChargeController.
        _run_psu_preset() fixének mintáját követő, explicit előrejelző
        check zárja ezt le közvetlenül input_on() elé."""
        ctrl, psu, load, dmm_v, *_ = self._drive_to_discharge_1()
        cutoff_1 = ctrl._discharge_cutoff_V

        dmm_v.voltage_V = cutoff_1 - 0.1
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1
        assert load._input_is_on is False

        # Stale PSU-flag PONT a retry input_on() előtti ticken.
        psu.output_commanded_on = True
        dmm_v.voltage_V = self._target_V(ctrl) + 1.0  # túl magas rebound -> retry
        ctrl.advance(2.0)  # settle letelik -> retry-ág -> itt hívódna input_on()

        assert ctrl.state == LearningState.FAULT
        assert "CONCURRENT" in ctrl.fault_reason
        assert load._input_is_on is False

    def test_rebound_max_retries_exceeded_proceeds_with_degraded_flag(self):
        cfg = LearningConfig(
            relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
            max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
            sample_interval_s=1000.0, rebound_settle_s=1.0, max_rebound_retries=2,
        )
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = self._drive_to_discharge_1(config=cfg)

        # A rebound MINDEN próbálkozásra a célsáv fölött marad.
        for _ in range(cfg.max_rebound_retries + 1):
            dmm_v.voltage_V = ctrl._discharge_cutoff_V - 0.1
            ctrl.advance(1.0)
            assert ctrl.state == LearningState.REBOUND_CHECK_1
            dmm_v.voltage_V = self._target_V(ctrl) + 1.0
            ctrl.advance(1.0)

        assert ctrl.state == LearningState.RELAX_1
        assert ctrl._rebound_retries_1 == cfg.max_rebound_retries
        assert any("REBOUND_MAX_RETRIES_EXCEEDED" in f for f in ctrl.quality_flags)

    def test_rebound_accepted_event_message_states_one_sided_upper_bound(self):
        """R1 B11c (F-33 (b)): az elfogadás egyoldalú felső korlát
        (rebound_V <= target_V + tol_V), nem ± célsáv. A cél ALATTI
        rebound is elfogadott -- az eseményszöveg ezt nem írhatja le
        "célsávban" ± sávként."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1()
        events = []
        ctrl.on_event = events.append

        dmm_v.voltage_V = ctrl._discharge_cutoff_V - 0.1
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1

        target = self._target_V(ctrl)
        tol = ctrl._config.rebound_tolerance_V
        dmm_v.voltage_V = target - 0.5  # a cél ALATT -- nem "± célsávban"
        ctrl.advance(2.0)
        assert ctrl.state == LearningState.RELAX_1

        accepted = [e for e in events if e.get("event_code") == "LEARNING_REBOUND_ACCEPTED"]
        assert len(accepted) == 1
        msg = accepted[0]["event_message"]
        assert "±" not in msg
        assert f"<= cél {target:.3f}V+{tol:.3f}V felső korlát" in msg

    def test_rebound_max_retries_event_message_states_one_sided_upper_bound(self):
        """R1 B11c (F-33 (b)): a max-retries esemény a felső korlát
        (target_V + tol_V) túllépését nevezi meg, nem ± célsávot."""
        cfg = LearningConfig(
            relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
            max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
            sample_interval_s=1000.0, rebound_settle_s=1.0, max_rebound_retries=2,
        )
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = self._drive_to_discharge_1(config=cfg)
        events = []
        ctrl.on_event = events.append

        target = self._target_V(ctrl)
        tol = ctrl._config.rebound_tolerance_V
        for _ in range(cfg.max_rebound_retries + 1):
            dmm_v.voltage_V = ctrl._discharge_cutoff_V - 0.1
            ctrl.advance(1.0)
            assert ctrl.state == LearningState.REBOUND_CHECK_1
            dmm_v.voltage_V = target + 1.0
            ctrl.advance(1.0)

        assert ctrl.state == LearningState.RELAX_1
        exceeded = [
            e for e in events
            if e.get("event_code") == "LEARNING_REBOUND_MAX_RETRIES_EXCEEDED"
        ]
        assert len(exceeded) == 1
        msg = exceeded[0]["event_message"]
        assert "±" not in msg
        assert f"> cél {target:.3f}V+{tol:.3f}V felső korlát" in msg

    def test_i_load_set_a_reflects_c10_current_only_during_active_discharge(self):
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = self._drive_to_discharge_1()
        assert ctrl.i_load_set_A == pytest.approx(ctrl._discharge_current_A)
        assert ctrl.i_load_set_A > 0

        dmm_v.voltage_V = ctrl._discharge_cutoff_V - 0.5
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1
        assert ctrl.i_load_set_A == 0.0

        dmm_t.simulate_temp_failure = False
        ctrl.advance(1.0)  # sikeres olvasás -> _temp_dmm_fault_s nullázódik
        assert ctrl._safety.temperature_dmm_fault_s == 0.0


class TestR4B1ReboundAcceptedSeverity:
    """[regresszió, R4-B1 (P3), DFAM-018 RECURRING] a LEARNING_REBOUND_
    ACCEPTED kódneve semleges; retry > 0 esetén viszont a VALÓDI elfogadási
    ág REBOUND_ACCEPTED_AFTER_RETRY quality flaget ad (az eredmény emiatt
    nem lehet VERIFIED), az élő esemény mégis severity nélkül, INFO-ként
    jelent meg. A producer minősít: retry > 0 -> explicit WARNING; retry == 0
    -> nincs severity-kulcs (INFO marad). Kód, üzenet, flag, számlálók és
    állapotátmenet változatlan. A döntési ág NINCS kimockolva: a rebound a
    MockDMM-en át, valódi advance()-tickekkel jut el az elfogadásig."""

    @staticmethod
    def _drive_to_discharge(discharge_state):
        """DISCHARGE_1-ig a TestReboundCheck meglévő segédjével; DISCHARGE_2-
        ig a TestFullSequenceToDone állapotsorrendjével (D1 rebound az ELSŐ
        próbára elfogadva -> retries_1 == 0)."""
        rc = TestReboundCheck()
        ctrl, _psu, _load, dmm_v, _dmm_t, fake_bq = rc._drive_to_discharge_1()
        if discharge_state == LearningState.DISCHARGE_1:
            return ctrl, dmm_v, fake_bq
        dmm_v.voltage_V = ctrl._discharge_cutoff_V - 0.1
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.REBOUND_CHECK_1
        dmm_v.voltage_V = rc._target_V(ctrl)
        ctrl.advance(2.0)
        assert ctrl.state == LearningState.RELAX_1
        assert ctrl._rebound_retries_1 == 0
        seq = TestFullSequenceToDone()
        fake_bq.update_status = 0x05
        seq._run_relax_to_completion(ctrl, fake_bq)
        assert ctrl.state == LearningState.VERIFY_UPDATE_STATUS_05
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RECHARGE
        dmm_v.voltage_V = 13.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RELAX_PRE_DISCHARGE_2
        seq._run_relax_to_completion(ctrl, fake_bq, vok_remains_set=True)
        assert ctrl.state == LearningState.DISCHARGE_2
        assert ctrl._rebound_retry_count == 0
        return ctrl, dmm_v, fake_bq

    @pytest.mark.parametrize(
        "discharge_state, rebound_state, relax_state",
        [
            (LearningState.DISCHARGE_1, LearningState.REBOUND_CHECK_1, LearningState.RELAX_1),
            (LearningState.DISCHARGE_2, LearningState.REBOUND_CHECK_2, LearningState.RELAX_2),
        ],
        ids=["discharge_1", "discharge_2"],
    )
    @pytest.mark.parametrize("retries", [0, 1, 2], ids=["retry0", "retry1", "retry2"])
    def test_accepted_event_severity_follows_retry_count(
        self, retries, discharge_state, rebound_state, relax_state,
    ):
        ctrl, dmm_v, _fake_bq = self._drive_to_discharge(discharge_state)
        assert retries <= ctrl._config.max_rebound_retries
        target = TestReboundCheck._target_V(ctrl)
        events = []
        ctrl.on_event = events.append

        # retries db túl magas rebound (valódi retry-ág: LOAD ON, cutoff le).
        for i in range(retries):
            dmm_v.voltage_V = ctrl._discharge_cutoff_V - 0.1
            ctrl.advance(1.0)
            assert ctrl.state == rebound_state
            dmm_v.voltage_V = target + 1.0
            ctrl.advance(2.0)
            assert ctrl.state == discharge_state
            assert ctrl._rebound_retry_count == i + 1
        # Célon belüli rebound -> valódi elfogadási ág.
        dmm_v.voltage_V = ctrl._discharge_cutoff_V - 0.1
        ctrl.advance(1.0)
        assert ctrl.state == rebound_state
        dmm_v.voltage_V = target
        ctrl.advance(2.0)

        assert ctrl.state == relax_state
        too_high = [e for e in events if e.get("event_code") == "LEARNING_REBOUND_TOO_HIGH"]
        assert len(too_high) == retries
        accepted = [e for e in events if e.get("event_code") == "LEARNING_REBOUND_ACCEPTED"]
        assert len(accepted) == 1
        ev = accepted[0]
        # Az üzenet változatlan (a próbaszám benne marad).
        assert f"({retries} próba után) — RELAX következik." in ev["event_message"]
        assert ev["event_message"].startswith(f"{discharge_state.value}: rebound ")
        flag_prefix = f"REBOUND_ACCEPTED_AFTER_RETRY_AT_{discharge_state.value}("
        flags = [f for f in ctrl.quality_flags if f.startswith(flag_prefix)]
        if retries > 0:
            assert ev["severity"] == "WARNING"
            assert len(flags) == 1
            assert flags[0].endswith(f"retries={retries})")
        else:
            # retry == 0: NOMINÁLIS elfogadás -- nincs explicit severity, a
            # heurisztika INFO-ja marad; quality flag sincs.
            assert "severity" not in ev
            assert flags == []
            assert not any("REBOUND_ACCEPTED_AFTER_RETRY" in f for f in ctrl.quality_flags)
        if discharge_state == LearningState.DISCHARGE_1:
            assert ctrl._rebound_retries_1 == retries
            assert ctrl._rebound_retries_2 == 0
        else:
            assert ctrl._rebound_retries_1 == 0
            assert ctrl._rebound_retries_2 == retries


class TestCellBlThresholdHandling:
    """Block 3 (2026-08-25, felhasználói döntés) — Cell BL Set Volt
    Threshold tranzakciós kezelése (TI SLUSB55B STEP 8, 2./10. lépés)."""

    def _drive_to_verify_vok_qen(self, config=None, profile_cell_count: int | None = None):
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=config or LearningConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
                sample_interval_s=1000.0, rebound_settle_s=1.0,
            ),
        )
        if profile_cell_count is not None:
            # A cell_count befolyásolja a discharge floor/rebound target
            # pack-szintű skálázását (l. test_discharge_floor_and_rebound_
            # target_scale_with_cell_count) — a _make_profile() fixen
            # 6 cellás, ezért itt manuálisan cseréljük ki a profilt, ÉS a
            # Block 2 konzisztencia-gate ne bukjon el emiatt: a fake DF-
            # mezőket is újra kell szeedelni az ÚJ profilhoz, MIELŐTT
            # PRECOND lefut.
            ctrl._profile = BatteryProfile(
                battery_name="TEST12", manufacturer="TEST", model="TST24V",
                nominal_voltage_V=24.0, cell_count=profile_cell_count,
                nominal_capacity_Ah=7.0, C10_capacity_Ah=7.0, terminate_voltage_per_cell_V=1.80,
            )
            fake_bq.df_fields = _default_df_fields(ctrl._profile)
            fake_bq._written_blocks.clear()
            # A 12-cellás setup is a saját pakkszintű kapuja fölül induljon.
            dmm_v.voltage_V = 2.10 * profile_cell_count
            ctrl._u_batt = dmm_v.voltage_V
            ctrl._safety.profile = ctrl._profile
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(False)
        for _ in range(5):
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                break
            ctrl.advance(1.0)
        ctrl.advance(1.0)  # snapshot -> IT_ENABLE_STEP
        ctrl.advance(1.0)  # IT_ENABLE trigger + settle -> VERIFY_VOK_QEN
        assert ctrl.state == LearningState.VERIFY_VOK_QEN
        return ctrl, psu, load, dmm_v, dmm_t, fake_bq

    def test_cell_bl_temp_write_happens_exactly_once_with_configured_value(self):
        ctrl, *_, fake_bq = self._drive_to_verify_vok_qen()
        assert fake_bq.calls.count("write_data_flash_block") == 1
        assert ctrl._cell_bl_original_mV == 1950  # _default_df_fields() seed
        written = ctrl._bq_transport.read_data_flash_block(49, 0)
        written_value = decode_df_field(FIELD_REGISTRY["cell_bl_set_volt_mV"], written)
        assert written_value == round(1.65 * 1000)

    def test_cell_bl_temp_value_independent_of_cell_count(self):
        """A DF-mező eleve per-cella (a BQ chip belsőleg skáláz), tehát a
        beírt mV-érték 12 cellás packnál is UGYANAZ marad — csak a
        _discharge_cutoff_V/rebound target pack-szintű mennyiségek
        skálázódnak (l. lenti teszt)."""
        ctrl, *_, fake_bq = self._drive_to_verify_vok_qen(profile_cell_count=12)
        written = ctrl._bq_transport.read_data_flash_block(49, 0)
        written_value = decode_df_field(FIELD_REGISTRY["cell_bl_set_volt_mV"], written)
        assert written_value == round(1.65 * 1000)

    def test_discharge_floor_and_rebound_target_double_for_12_cell_pack(self):
        cfg = LearningConfig(
            relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
            max_relax_extra_wait_s=1.0, it_enable_settle_s=0.2, verify_settle_s=0.2,
            sample_interval_s=1000.0, rebound_settle_s=1.0,
        )
        ctrl_6, *_ = self._drive_to_verify_vok_qen(config=cfg)
        ctrl_12, *_ = self._drive_to_verify_vok_qen(config=cfg, profile_cell_count=12)

        for ctrl in (ctrl_6, ctrl_12):
            ctrl.advance(0.2)  # settle -> discharge setup (_begin_discharge)
            assert ctrl.state == LearningState.DISCHARGE_1

        assert ctrl_6._discharge_cutoff_V == pytest.approx(10.50)
        assert ctrl_12._discharge_cutoff_V == pytest.approx(21.00)
        assert ctrl_12._discharge_cutoff_V == pytest.approx(2 * ctrl_6._discharge_cutoff_V)

        target_6 = TestReboundCheck._target_V(ctrl_6)
        target_12 = TestReboundCheck._target_V(ctrl_12)
        # D10 (2026-09-15): learning_rebound_target_V_per_cell 1.75 -> 1.80
        # (TI SLUSB55B STEP 8 saját 6-cellás példájának TÉNYLEGES targetje,
        # a Cell Termination Voltage -- l. traditional_learning_controller.py
        # LearningConfig docstring-je a korábbi 1.75 hibás eredetéről).
        assert target_6 == pytest.approx(10.8)
        assert target_12 == pytest.approx(21.6)
        assert target_12 == pytest.approx(2 * target_6)

    def test_restore_is_noop_when_it_enable_never_reached(self):
        ctrl, *_, fake_bq = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND -> PRECHARGE (no IT_ENABLE yet)
        assert ctrl._cell_bl_original_mV is None
        calls_before = len(fake_bq.calls)
        ctrl._restore_cell_bl_if_needed()
        assert len(fake_bq.calls) == calls_before
        assert ctrl._cell_bl_restore_confirmed is None

    def test_restore_is_idempotent(self):
        ctrl, *_, fake_bq = self._drive_to_verify_vok_qen()
        writes_after_it_enable = fake_bq.calls.count("write_data_flash_block")
        ctrl._restore_cell_bl_if_needed()
        writes_after_first_restore = fake_bq.calls.count("write_data_flash_block")
        assert writes_after_first_restore == writes_after_it_enable + 1
        ctrl._restore_cell_bl_if_needed()  # második hívás -> no-op
        assert fake_bq.calls.count("write_data_flash_block") == writes_after_first_restore

    def test_restore_failure_appends_quality_flag_and_prevents_verified(self):
        ctrl, *_, fake_bq = self._drive_to_verify_vok_qen()
        assert ctrl._cell_bl_original_mV is not None

        # A temp-write MÁR megtörtént (_drive_to_verify_vok_qen() alatt, a
        # régi handlerrel) — a handler CSERÉJE UTÁNI, egyetlen hívás lesz a
        # restore-write, azt kell megbuktatni.
        def _fail_write(params):
            raise RuntimeError("simulated restore write failure")

        ctrl._bq_transport.serial.results["write_data_flash_block"] = _fail_write

        ctrl.emergency_stop("MANUAL_TEST_FOR_RESTORE_FAILURE")
        assert any("BL_THRESHOLD_RESTORE_FAILED" in f for f in ctrl.quality_flags)
        assert ctrl.result_summary.cell_bl_restore_confirmed is False
        assert ctrl.result_summary.result != LEARNING_RESULT_VERIFIED

    def test_restore_seal_failure_surfaces_in_quality_flags_even_when_write_succeeds(self):
        """Review-kör 5, Architektúra A-1 (P1): a fenti teszttől eltérően
        itt a DF-visszaírás SIKERES, csak az UTÁNA következő best-effort
        SEAL hasal el -- korábban ez nyomtalanul eltűnt volna (`except
        Exception: pass`), most a quality_flags-ben külön attesztálva van."""
        ctrl, *_, fake_bq = self._drive_to_verify_vok_qen()
        assert ctrl._cell_bl_original_mV is not None
        ctrl._bq_transport.serial.errors["seal"] = ("METHOD_ERROR", "simulated seal failure")
        events = []
        ctrl.on_event = events.append

        ctrl._restore_cell_bl_if_needed()

        assert ctrl._cell_bl_restore_confirmed is True, (
            "a DF-iras sikeres volt, csak a KOVETKEZO seal-lepes hibazott"
        )
        assert any(
            f.startswith("BQ_SEAL_RESTORE_FAILED_AT_CELL_BL_RESTORE:") for f in ctrl.quality_flags
        )
        assert any(e.get("event_code") == "LEARNING_SEAL_RESTORE_FAILED" for e in events)

    def test_verify_vok_qen_stays_unsealed_for_session(self):
        """[SCF-011 újratervezés, 2026-09-11, felhasználói döntés] a
        VERIFY_VOK_QEN fázis korábbi záró best-effort SEAL-je megszűnt --
        a session-szintű életciklusban a chip a PRECOND unseal()-je óta
        UNSEALED marad a teljes fizikai ciklus alatt (folyamatos
        diagnosztika/DF-hozzáférés), a session-végi seal()-t a
        `_restore_cell_bl_if_needed()`/`_ensure_bq_sealed_or_flag()`
        végzi a terminális állapotokban. Sibling: [[test_precond_records_
        unseal_but_stays_unsealed_for_session]] ugyanezt PRECOND-ra."""
        ctrl, *_, fake_bq = self._drive_to_verify_vok_qen()
        seal_calls_before = fake_bq.calls.count("seal")

        ctrl.advance(1.0)  # verify_settle_s (0.2) letelt -> _run_verify_vok_qen lefut

        assert ctrl.state == LearningState.DISCHARGE_1
        assert fake_bq.calls.count("seal") == seal_calls_before


class TestPendingBqDfRestore:
    """[teljes kódbázis review kör 7 (2026-09-15), C-12, DFAM-016 rokona]
    pending_bq_df_restore -- l. a property docstringje a teljes
    indoklásért. Ugyanazt a `_drive_to_verify_vok_qen()` fixture-t
    használja, mint TestCellBlThresholdHandling."""

    def test_none_before_temp_write(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND -> PRECHARGE (no IT_ENABLE yet)
        assert ctrl._cell_bl_original_mV is None
        assert ctrl.pending_bq_df_restore is None

    def test_set_after_temp_write(self):
        """A temp-write MÁR megtörtént (VERIFY_VOK_QEN-ig eljutva), a
        restore MÉG NEM futott -- a property ekkor a maradványt kell,
        hogy jelezze."""
        ctrl, *_ = self._drive_to_verify_vok_qen()
        pending = ctrl.pending_bq_df_restore
        assert pending is not None
        assert pending["field"] == "cell_bl_set_volt_mV"
        assert pending["original_mV"] == 1950
        assert pending["restore_confirmed"] is None
        assert pending["bq_unsealed"] is True

    def test_cleared_after_confirmed_restore(self):
        ctrl, *_ = self._drive_to_verify_vok_qen()
        assert ctrl.pending_bq_df_restore is not None

        ctrl._restore_cell_bl_if_needed()

        assert ctrl._cell_bl_restore_confirmed is True
        assert ctrl.pending_bq_df_restore is None

    def test_still_set_when_restore_failed(self):
        """[a kritikus eset] egy SIKERTELEN restore UTÁN a maradványnak
        NEM szabad eltűnnie -- pont ekkor a legfontosabb, hogy a
        checkpoint/dialógus jelezze a fizikai chip állapotát."""
        ctrl, *_ = self._drive_to_verify_vok_qen()

        def _fail_write(params):
            raise RuntimeError("simulated restore write failure")

        ctrl._bq_transport.serial.results["write_data_flash_block"] = _fail_write
        ctrl._restore_cell_bl_if_needed()

        assert ctrl._cell_bl_restore_confirmed is False
        pending = ctrl.pending_bq_df_restore
        assert pending is not None
        assert pending["restore_confirmed"] is False
        # MAX verifier F3 lelete (2026-09-15): a DF-visszaírás sikertelen
        # volt, DE a `finally`-ágban futó `seal(confirm=True)` (nincs
        # konfigurálva hibázni) TÉNYLEGESEN sikeres -- a chip ténylegesen
        # SEALED, tehát bq_unsealed-nek False-nak KELL lennie, nem a
        # korábbi, feltétel nélküli True-nak.
        assert pending["bq_unsealed"] is False

    def test_bq_unsealed_true_when_restore_and_reseal_both_failed(self):
        """[MAX verifier F3 lelete, 2026-09-15] ha a DF-visszaírás ÉS a
        session-végi reseal is meghiúsul, a chip ténylegesen unsealed
        marad -- bq_unsealed-nek True-nak kell maradnia."""
        ctrl, *_ = self._drive_to_verify_vok_qen()

        def _fail_write(params):
            raise RuntimeError("simulated restore write failure")

        ctrl._bq_transport.serial.results["write_data_flash_block"] = _fail_write
        ctrl._bq_transport.serial.errors["seal"] = ("METHOD_ERROR", "simulated seal failure")
        ctrl._restore_cell_bl_if_needed()

        assert ctrl._cell_bl_restore_confirmed is False
        # R17-05 (Kor 17, DFAM-032): a bare seal() RPC hibaja termeszetesen
        # False-t hagy, de a valodi diszkriminacio (l. lejjebb) az, hogy a
        # RPC SIKERE eseten is False marad, ha az SS bit nem all be.
        assert ctrl._cell_bl_reseal_confirmed is False
        pending = ctrl.pending_bq_df_restore
        assert pending is not None
        assert pending["bq_unsealed"] is True

    def test_reseal_not_confirmed_when_seal_rpc_succeeds_but_ss_bit_stays_clear(self):
        """[regresszió, Kör 17 MAX-effort review, R17-05, DFAM-032 5.
        előfordulás -- a DISZKRIMINÁLÓ teszt] a `seal(confirm=True)` RPC
        SIKERES visszatérése önmagában NEM elég -- ha a chip SS bitje
        (ControlStatusBits.ss) ezután is clear marad, a reseal NEM
        tekinthető megerősítettnek. Ez a teszt a JAVÍTÁS ELŐTTI kódon
        buknia -- ott a bare seal() RPC sikere önmagában True-ra
        állította a flaget, függetlenül az SS bittől."""
        ctrl, *_, fake_bq = self._drive_to_verify_vok_qen()

        # A seal() RPC "sikeres" (nem dob kivételt), DE a fake NEM allitja
        # be a sealed SS bitet -- pontosan a firmware-oldali "elfogadta a
        # parancsot, de nem zart" forgatokonyv.
        ctrl._bq_transport.serial.results["seal"] = _seal_state_recorder(
            fake_bq, "seal", sealed_after=False
        )
        ctrl._restore_cell_bl_if_needed()

        assert ctrl._cell_bl_reseal_confirmed is False
        assert any(
            f.startswith("BQ_SEAL_RESTORE_FAILED_AT_CELL_BL_RESTORE:")
            and "SS bit still clear" in f
            for f in ctrl._quality_flags
        )
        # A DF-visszaírás maga SIKERES volt ebben a forgatókönyvben (csak a
        # reseal SS-bit-je maradt clear) -- a pending_bq_df_restore property
        # KIZÁRÓLAG a DF-ÉRTÉK maradványát követi (l. docstringje), nem a
        # seal-állapotot; a seal-oldali maradvány-csatorna a KÜLÖN R17-08
        # tétel (pending_bq_unsealed hatóköre), itt még nincs bekötve.
        assert ctrl.pending_bq_df_restore is None

    def test_ensure_bq_sealed_or_flag_sets_reseal_confirmed_on_already_sealed_noop(self):
        """[MAX verifier P3 lelete, 2026-09-16] a HELPER "már sealed?"
        gyors korai-return ága (amikor cs.ss MÁR True a hívás elején,
        seal() meg sem hívva) is beállítja _cell_bl_reseal_confirmed-et --
        ez a MÁSODIK, kevésbé nyilvánvaló siker-ág, amit egyetlen korábbi
        teszt sem fedett (mindegyik a seal()-t TÉNYLEGESEN hívó ágon
        futott át)."""
        ctrl, *_ = self._drive_to_verify_vok_qen()
        # Az első _ensure_bq_sealed_or_flag()-hívás (a finally-ből) a
        # PRECOND-unseal óta unsealed chipet zárja -- ez a "normál" ág,
        # SS bit False -> seal()-t hív -> True-ra allitja.
        assert ctrl._ensure_bq_sealed_or_flag("FIRST_CALL") is True
        n_seal_before = sum(
            1 for r in ctrl._bq_transport.serial.written_requests if r.get("method") == "seal"
        )

        # A MÁSODIK hívás a chipet MÁR SEALED állapotban találja (az első
        # hívás seal()-je óta) -- a korai-return ágnak KELL lefutnia,
        # seal() ISMÉTELT hívása nélkül. A flaget explicit False-ra
        # állítjuk vissza az első hívás UTÁN, hogy a lenti assert
        # KIZÁRÓLAG a MÁSODIK hívás korai-return ágának tulajdonítható
        # legyen, ne az első hívás maradék állapota legyen (mutáció-
        # diszkriminátor).
        ctrl._cell_bl_reseal_confirmed = False
        assert ctrl._ensure_bq_sealed_or_flag("SECOND_CALL") is True
        n_seal_after = sum(
            1 for r in ctrl._bq_transport.serial.written_requests if r.get("method") == "seal"
        )
        assert n_seal_after == n_seal_before, (
            "a mar sealed korai-return agnak NEM szabad ujra seal()-t hivnia"
        )
        assert ctrl._cell_bl_reseal_confirmed is True

    def test_reseal_confirmed_when_ss_bit_actually_set(self):
        """Boldog út / over-correction guard: ha a seal() RPC sikeres ÉS
        az SS bit ténylegesen beáll, a reseal megerősítettnek számít
        (ugyanaz, mint a meglévő test_cleared_after_confirmed_restore,
        de explicit a flag-re, nem csak a property-re nézve)."""
        ctrl, *_ = self._drive_to_verify_vok_qen()
        ctrl._restore_cell_bl_if_needed()

        assert ctrl._cell_bl_reseal_confirmed is True
        assert ctrl.pending_bq_df_restore is None

    def test_reseal_not_confirmed_when_post_seal_verify_read_fails(self):
        """Az újonnan elérhető hibamód: az ELSŐ ("már sealed?" gyors
        előzetes) control_status-olvasás SIKERES (cs.ss=False, tehát a
        végrehajtás folytatódik a seal() felé), a seal() RPC IS SIKERES,
        DE az AZT KÖVETŐ, MÁSODIK SS-bit-visszaolvasás dob -- a reseal
        NEM tekinthető megerősítettnek, és a BQ_SEAL_STATE_VERIFY_FAILED_
        AT_ jelző jelenik meg (nem a BQ_SEAL_RESTORE_FAILED_AT_).

        [MAX verifier P3 lelete, 2026-09-16] a korábbi verzió a
        control_status-t GLOBÁLISAN, minden hívásra hibáztatta -- ez
        valójában az ELŐZETES (pre-check) olvasást buktatta el, NEM a
        seal() UTÁNI verify-t (a két ág külön kódutat jár, külön
        tesztelendő). Ez a verzió egy hívásszámlálóval PONTOSAN a
        MÁSODIK control_status-hívást hibáztatja."""
        ctrl, *_ = self._drive_to_verify_vok_qen()
        original_control_status = ctrl._bq_transport.serial.results["control_status"]
        state = {"calls": 0}

        def _fail_from_second_call_onward(params):
            state["calls"] += 1
            if state["calls"] >= 2:
                # >=, nem ==2: a transport retry-logikaja (max_retries=3)
                # ujraprobalna egy egyetlen sikertelen kiserletet, es az
                # UJRAPROBALKOZAS sikere elfedne a hibat -- minden, a
                # pre-check UTANI kiserletnek buknia kell.
                raise RuntimeError("simulated control_status read failure (post-seal verify)")
            return original_control_status(params)

        ctrl._bq_transport.serial.results["control_status"] = _fail_from_second_call_onward
        ctrl._restore_cell_bl_if_needed()

        assert state["calls"] >= 2, "a tesztnek legalabb 2 control_status-hivast kell latnia"
        assert any(
            req.get("method") == "seal" for req in ctrl._bq_transport.serial.written_requests
        ), "a seal() RPC-t is tenylegesen meg kellett kiserelni a 2 olvasas kozott"
        assert ctrl._cell_bl_reseal_confirmed is False
        assert any(
            f.startswith("BQ_SEAL_STATE_VERIFY_FAILED_AT_CELL_BL_RESTORE:")
            for f in ctrl._quality_flags
        )
        # A hiba PONTOSAN a pre-check UTÁNI control_status-olvasásokra
        # szűkített, a DF-visszaírás pipeline saját (korábbi) readback-
        # lépését nem érinti -- a restore_confirmed True marad, a
        # pending_bq_df_restore ezért helyesen None (a property KIZÁRÓLAG
        # a DF-ÉRTÉK maradványát követi, nem a seal-állapotot -- l. a
        # test_reseal_not_confirmed_when_seal_rpc_succeeds_but_ss_bit_
        # stays_clear teszt azonos hivatkozású megjegyzését).
        assert ctrl._cell_bl_restore_confirmed is True
        assert ctrl.pending_bq_df_restore is None

    def _drive_to_verify_vok_qen(self, config=None, profile_cell_count: int | None = None):
        return TestCellBlThresholdHandling()._drive_to_verify_vok_qen(
            config=config, profile_cell_count=profile_cell_count
        )


class TestR1708PendingBqUnsealed:
    """[regresszió, Kör 17 MAX-effort review, R17-08, DFAM-016+DFAM-018]
    pending_bq_unsealed -- l. a property docstringje a teljes indoklásért.
    A GoldenGgController.pending_bq_unsealed (R2-11b) TLC-oldali testvére,
    a PRECOND-unseal -> IT_ENABLE-temp-write ablakra szűkítve, IDŐBEN
    DISZJUNKT módon a pending_bq_df_restore-tól."""

    def test_none_before_any_unseal_attempt(self):
        ctrl, *_ = make_controller()
        assert ctrl._bq_unseal_attempted is False
        assert ctrl.pending_bq_unsealed is None

    def test_flag_set_before_rpc_survives_unseal_rpc_raising(self):
        """[MAX verifier F-2 lelete, 2026-09-16] a `_bq_unseal_attempted`
        SZÁNDÉKOSAN az unseal() RPC ELŐTT vált True-ra (l. a property
        docstringje és a GoldenGgController azonos mintája) -- ez akkor
        számít, ha a chip fizikailag MÁR unsealed allapotba kerul (az SS
        bit tenylegesen torlodik), DE az RPC maga meg kivetelt dob (pl.
        egy tranziens transport-hiba a valasz visszaolvasasakor). Ha a
        flag a try-on BELUL/UTAN valtana True-ra, ez a forgatokonyv
        NULLAZOTT maradvany-jelzest adna, holott a chip fizikailag
        tenylegesen unsealed."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
        # A chip fizikailag MAR unsealed (SS bit torolve) -- realisztikus
        # forgatokonyv: egy korabbi, felbeszakadt session vagy egy
        # tranziens allapot mar torolte a bitet --, DE az unseal() RPC
        # MAGA kivetelt dob (pl. tranziens transport-hiba a valasz
        # visszaolvasasakor).
        fake_bq.set_sealed(False)
        ctrl._bq_transport.serial.errors["unseal"] = (
            "METHOD_ERROR", "simulated transient unseal RPC failure"
        )
        # A PRECOND-hiba emergency_stop()-ot valt ki, ami sajat fallback-
        # reseal-kiserletet tesz (_ensure_bq_sealed_or_flag) -- ha AZ
        # sikeres lenne, elfedne a tesztelt ordering-kerdest (a flag mar
        # True volt-e a BUKOTT unseal ELOTT). Ezert a fallback seal()-t
        # is hibaztatjuk, hogy _cell_bl_reseal_confirmed biztosan False
        # maradjon, es a pending_bq_unsealed KIZAROLAG az (a) es (b)
        # feltetelektol fuggjon, ne (c)-tol.
        ctrl._bq_transport.serial.errors["seal"] = (
            "METHOD_ERROR", "simulated fallback seal failure"
        )
        ctrl.advance(0.0)  # INIT -> PRECOND (belepes, meg nem futtatva)
        ctrl.advance(0.0)  # PRECOND teste lefut -- emergency_stop-ra fut

        assert ctrl.pending_bq_unsealed is not None, (
            "a chip fizikailag unsealed maradt (SS bit torolve), a "
            "maradvany-jelzesnek MEG KELL jelennie, akkor is, ha az "
            "unseal() RPC maga kivetelt dobott"
        )

    def test_set_after_precond_unseal_before_temp_write(self):
        """[a DISZKRIMINÁLÓ teszt] a PRECOND unseal() már megtörtént
        (PRECHARGE-ig eljutva), de a Cell BL temp-write MÉG NEM -- ez
        pontosan az a korábban lefedetlen ablak, amit R17-08 zár le."""
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND (belepes, meg nem futtatva)
        ctrl.advance(0.0)  # PRECOND teste lefut -> PRECHARGE (no IT_ENABLE yet)
        assert ctrl._bq_unseal_attempted is True
        assert ctrl._cell_bl_original_mV is None

        pending = ctrl.pending_bq_unsealed
        assert pending is not None
        assert pending["controller"] == "TRADITIONAL_LEARNING"
        assert pending["bq_unsealed"] is True
        # I1 (DFAM-018): a ket maradvany-csatorna SOSEM lehet egyszerre
        # nem-None.
        assert ctrl.pending_bq_df_restore is None

    def test_none_after_confirmed_fallback_reseal(self):
        """Egy korai PRECOND-utani FAULT az EMERGENCY_STOP kontextusban
        futtatja az _ensure_bq_sealed_or_flag()-et (R17-05 ota SS-bit-tel
        igazolva) -- ez a Cell BL temp-write NELKUL is lezarja a chipet,
        a pending_bq_unsealed-nek EKKOR None-ra kell valtania."""
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND (belepes, meg nem futtatva)
        ctrl.advance(0.0)  # PRECOND teste lefut, unseal megtortent
        assert ctrl.pending_bq_unsealed is not None

        ctrl.emergency_stop("SIMULATED_FAULT_BEFORE_TEMP_WRITE")

        assert ctrl._cell_bl_reseal_confirmed is True
        assert ctrl.pending_bq_unsealed is None

    def test_still_set_when_fallback_reseal_seal_rpc_failed(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND (belepes, meg nem futtatva)
        ctrl.advance(0.0)  # PRECOND teste lefut, unseal megtortent
        ctrl._bq_transport.serial.errors["seal"] = (
            "METHOD_ERROR", "simulated seal failure"
        )

        ctrl.emergency_stop("SIMULATED_FAULT_BEFORE_TEMP_WRITE")

        assert ctrl._cell_bl_reseal_confirmed is False
        assert ctrl.pending_bq_unsealed is not None

    def test_still_set_when_fallback_seal_accepted_but_ss_bit_stays_clear(self):
        """[R17-05 hibaosztálya, a R17-08 csatornán át nézve] a seal() RPC
        sikeres, DE az SS bit clear marad -- ugyanaz a fizikai
        bizonytalanság, ami a pending_bq_df_restore-nál is bq_unsealed=
        True-t adna."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND (belepes, meg nem futtatva)
        ctrl.advance(0.0)  # PRECOND teste lefut, unseal megtortent
        ctrl._bq_transport.serial.results["seal"] = _seal_state_recorder(
            fake_bq, "seal", sealed_after=False
        )

        ctrl.emergency_stop("SIMULATED_FAULT_BEFORE_TEMP_WRITE")

        assert ctrl._cell_bl_reseal_confirmed is False
        assert ctrl.pending_bq_unsealed is not None

    def test_pending_bq_unsealed_is_none_once_temp_write_happened(self):
        """[I1/DFAM-018 + hamis-dialógus-szöveg őr] a temp-write UTÁN, amíg a
        Data Flash-maradvány (pending_bq_df_restore) nyitott, a
        pending_bq_unsealed-nek None-nak KELL lennie, akkor is, ha a
        restore még nem történt meg vagy nem sikeres -- a két csatorna
        diszjunkt (l. `MainWindow._review_incomplete_sessions_on_startup()`
        "BQ SEAL MARADVÁNY" sorának "Nyitott (vissza nem állított) Data
        Flash-érték-maradvány ezen az úton nincs" szövegét). R1 F-17 óta a
        tiltás a NYITOTT DF-maradványhoz kötött, nem a temp-write tényéhez:
        a megerősített restore UTÁN a seal-maradvány a reseal-megerősítéstől
        függ -- ezért az alábbi None-elvárás előfeltétele a megerősített
        reseal."""
        ctrl, *_ = self._drive_to_verify_vok_qen()
        assert ctrl._cell_bl_original_mV is not None
        assert ctrl.pending_bq_df_restore is not None
        assert ctrl.pending_bq_unsealed is None, (
            "a temp-write UTAN a pending_bq_df_restore az egyetlen "
            "helyes csatorna -- a ketto SOSEM lehet egyszerre nem-None"
        )

        # Es a restore MEGERŐSÍTETT sikere UTÁN is None marad (a
        # pending_bq_df_restore is None ekkor).
        ctrl._restore_cell_bl_if_needed()
        assert ctrl._cell_bl_restore_confirmed is True
        assert ctrl.pending_bq_df_restore is None
        assert ctrl._cell_bl_reseal_confirmed is True
        assert ctrl.pending_bq_unsealed is None

    def _drive_to_verify_vok_qen(self, config=None, profile_cell_count: int | None = None):
        return TestCellBlThresholdHandling()._drive_to_verify_vok_qen(
            config=config, profile_cell_count=profile_cell_count
        )


class TestR1F17PendingBqUnsealedAfterRestoredCellBl:
    """[regresszió, R1 F-17, DFAM-018+DFAM-016] a pending_bq_unsealed
    korábban a temp-write TÉNYÉRE (`_cell_bl_original_mV is not None`)
    tiltott -- egy MEGERŐSÍTETT Cell BL restore után, sikertelen /
    SS-bittel nem igazolt reseal mellett ezért MINDKÉT maradvány-property
    None volt, holott a chip UNSEALED maradhatott: a checkpoint és a
    startup-dialógus semmit nem jelzett. A fix a tiltást a NYITOTT
    Data Flash-maradványhoz (`pending_bq_df_restore is not None`) köti."""

    def _drive_to_verify_vok_qen(self, config=None, profile_cell_count: int | None = None):
        return TestCellBlThresholdHandling()._drive_to_verify_vok_qen(
            config=config, profile_cell_count=profile_cell_count
        )

    def test_restored_cell_bl_with_failed_reseal_rpc_flags_unsealed(self):
        ctrl, *_ = self._drive_to_verify_vok_qen()
        ctrl._bq_transport.serial.errors["seal"] = ("METHOD_ERROR", "simulated seal failure")

        ctrl._restore_cell_bl_if_needed()

        assert ctrl._cell_bl_restore_confirmed is True
        assert ctrl._cell_bl_reseal_confirmed is False
        assert ctrl.pending_bq_df_restore is None
        pending = ctrl.pending_bq_unsealed
        assert pending is not None
        assert pending["controller"] == "TRADITIONAL_LEARNING"
        assert pending["reason"] == "CELL_BL_RESTORED_RESEAL_UNCONFIRMED"
        assert pending["bq_unsealed"] is True

    def test_restored_cell_bl_with_ss_bit_still_clear_flags_unsealed(self):
        ctrl, *_, fake_bq = self._drive_to_verify_vok_qen()
        ctrl._bq_transport.serial.results["seal"] = _seal_state_recorder(
            fake_bq, "seal", sealed_after=False
        )

        ctrl._restore_cell_bl_if_needed()

        assert ctrl._cell_bl_restore_confirmed is True
        assert ctrl._cell_bl_reseal_confirmed is False
        assert ctrl.pending_bq_df_restore is None
        pending = ctrl.pending_bq_unsealed
        assert pending is not None
        assert pending["reason"] == "CELL_BL_RESTORED_RESEAL_UNCONFIRMED"

    @pytest.mark.parametrize("unseal_attempted", [False, True])
    @pytest.mark.parametrize("temp_write", [False, True])
    @pytest.mark.parametrize("restore", [None, False, True])
    @pytest.mark.parametrize("reseal", [False, True])
    def test_disjointness_matrix(self, unseal_attempted, temp_write, restore, reseal):
        """Diszjunktsági invariáns a teljes jelző-mátrixon:
        pending_bq_df_restore nem-None <=> temp-write ÉS restore nincs
        igazolva; pending_bq_unsealed nem-None <=> unseal megkísérelve ÉS
        a DF-csatorna nem aktív ÉS reseal nincs igazolva; a kettő SOSEM
        egyszerre nem-None."""
        ctrl, *_ = make_controller()
        ctrl._bq_unseal_attempted = unseal_attempted
        ctrl._cell_bl_original_mV = 1950 if temp_write else None
        ctrl._cell_bl_restore_confirmed = restore
        ctrl._cell_bl_reseal_confirmed = reseal

        df_expected = temp_write and restore is not True
        unsealed_expected = unseal_attempted and not df_expected and not reseal

        assert (ctrl.pending_bq_df_restore is not None) is df_expected
        pending_unsealed = ctrl.pending_bq_unsealed
        assert (pending_unsealed is not None) is unsealed_expected
        assert not (ctrl.pending_bq_df_restore is not None and pending_unsealed is not None)
        if pending_unsealed is not None:
            assert pending_unsealed["reason"] == (
                "CELL_BL_RESTORED_RESEAL_UNCONFIRMED" if temp_write
                else "PRECOND_DF_READ_REQUIRES_UNSEALED"
            )


class TestReview2ndRoundEarlyFaultNotClassifiedAsAwaiting:
    """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P1] a
    _classify_result() 3. elif ága (`not (qmax_update_evidenced and
    ra_update_evidenced and vok_qen_consistent)`) korábban FÜGGETLEN volt
    `no_safety_fault`-tól — egy korai (evidence gyűjtése ELŐTTI) safety FAULT
    után qmax/ra/vok_qen mind hiányzik ("nem evidenced"), tehát ez az ág
    futott le, és a MEG SEM KEZDETT fizikai ciklus tévesen
    LEARNING_RESULT_AWAITING lett, holott CLAUDE.md §2 szerint az
    "AWAITING_BQ_VERIFICATION" KIZÁRÓLAG a BEFEJEZETT fizikai ciklusra
    vonatkozó, elfogadható automatikus eredmény."""

    def test_fault_before_any_evidence_gathered_yields_failed_not_awaiting(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        # Egyetlen advance() sem futott — semmilyen Qmax/Ra/VOK/QEN
        # bizonyíték nem gyűlt össze, pontosan az az eset, ami korábban a
        # 3. elif ágba futott (qmax_update_evidenced/ra_update_evidenced/
        # vok_qen_consistent mind False, no_safety_fault-tól függetlenül).
        ctrl.emergency_stop("SIMULATED_EARLY_FAULT_BEFORE_EVIDENCE")

        assert ctrl.state == LearningState.FAULT
        assert ctrl.result_summary is not None
        assert ctrl.result_summary.result == LEARNING_RESULT_FAILED
        assert ctrl.result_summary.result != LEARNING_RESULT_AWAITING

    def test_operator_stop_is_not_classified_as_bq_learning_failed(self):
        """[regresszió, review-kör 5, Session/Workflow S5-01 (P2)] a runner
        egy operátori Stop-ot emergency_stop("USER_STOP_REQUESTED")-en át
        zár le -- ez NEM bizonyított BQ-tanulási kudarc, csak egy egyszerű
        megszakítás egy fizikailag még be nem fejezett cikluson.

        KORREKCIÓ (2026-09-15, C-09/DFAM-025): a klasszifikáció mostantól
        `runner_abort_origin`-t néz, nem a fault_reason string-prefixét --
        ezt a mezőt a valós integrációban test_runner.py::
        _abort_active_controller() állítja be az emergency_stop() hívása
        ELŐTT (l. ott), a teszt ezt szimulálja közvetlenül."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.runner_abort_origin = "USER_STOP_REQUESTED"
        ctrl.emergency_stop("USER_STOP_REQUESTED")

        assert ctrl.state == LearningState.FAULT
        assert ctrl.result_summary is not None
        assert ctrl.result_summary.result != LEARNING_RESULT_FAILED
        assert ctrl.result_summary.result == LEARNING_RESULT_AWAITING
        assert "ABORTED_BY_RUNNER(USER_STOP_REQUESTED)" in ctrl.result_summary.quality_flags

    def test_runner_unhandled_exception_is_not_classified_as_bq_learning_failed(self):
        """[regresszió, review-kör 5, Session/Workflow S5-01 (P2)] l. az
        előző teszt docstringje -- itt egy a BQ-tanulással összefüggéstelen
        runner-szintű kivétel (pl. Logger I/O-hiba) esetét igazolja."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.runner_abort_origin = "UNHANDLED_EXCEPTION: OSError: [Errno 28] disk full"
        ctrl.emergency_stop("UNHANDLED_EXCEPTION: OSError: [Errno 28] disk full")

        assert ctrl.state == LearningState.FAULT
        assert ctrl.result_summary is not None
        assert ctrl.result_summary.result != LEARNING_RESULT_FAILED
        assert ctrl.result_summary.result == LEARNING_RESULT_AWAITING
        assert any(
            f.startswith("ABORTED_BY_RUNNER(UNHANDLED_EXCEPTION")
            for f in ctrl.result_summary.quality_flags
        )

    def test_user_emergency_stop_is_not_classified_as_bq_learning_failed(self):
        """[regresszió, 2026-09-15, C-09/DFAM-025 2. előfordulás] a régi
        string-prefix whitelist csak USER_STOP_REQUESTED-et és
        UNHANDLED_EXCEPTION:-et ismerte -- a GUI vészleállítás gombja
        viszont "USER_EMERGENCY_STOP"-ot ad (test_runner.py default
        emergency_stop_reason), ami korábban HAMISAN "BQ_LEARNING_FAILED"-et
        eredményezett egy egyébként hibátlan, csak megszakított cikluson."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.runner_abort_origin = "USER_EMERGENCY_STOP"
        ctrl.emergency_stop("USER_EMERGENCY_STOP")

        assert ctrl.result_summary.result == LEARNING_RESULT_AWAITING
        assert "ABORTED_BY_RUNNER(USER_EMERGENCY_STOP)" in ctrl.result_summary.quality_flags

    def test_scpi_critical_error_is_not_classified_as_bq_learning_failed(self):
        """[regresszió, 2026-09-15, C-09/DFAM-025 2. előfordulás] egy PSU/
        Load/DMM error-queue-ból eredő kritikus SCPI-hiba (test_runner.py
        SCPI_CRITICAL_ERROR[...] reasonje) egy műszer-kommunikációs esemény,
        nem BQ-tanulási bizonyíték -- korábban ez is HAMISAN FAILED-et adott."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.runner_abort_origin = "SCPI_CRITICAL_ERROR[PSU]: -113,\"Undefined header\""
        ctrl.emergency_stop("SCPI_CRITICAL_ERROR[PSU]: -113,\"Undefined header\"")

        assert ctrl.result_summary.result == LEARNING_RESULT_AWAITING
        assert any(
            f.startswith("ABORTED_BY_RUNNER(SCPI_CRITICAL_ERROR")
            for f in ctrl.result_summary.quality_flags
        )

    def test_controller_own_safety_fault_without_runner_abort_origin_still_fails(self):
        """[regresszió, 2026-09-15, C-09 ellen-mutáció] ha a controller SAJÁT
        safety-/mérési logikája hívja ki az emergency_stop()-ot (a runner
        NEM avatkozott közbe, runner_abort_origin marad None), a klasszi-
        fikáció TOVÁBBRA IS LEARNING_RESULT_FAILED -- ez a hamis-AWAITING
        irányú regresszió elleni discriminator."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        assert ctrl.runner_abort_origin is None
        ctrl.emergency_stop("BATTERY_OVERVOLTAGE")

        assert ctrl.result_summary.result == LEARNING_RESULT_FAILED
        assert not any(
            f.startswith("ABORTED_BY_RUNNER") for f in ctrl.result_summary.quality_flags
        )


class TestKor20BatchE13bQmaxReadFailureAt0x05CheckpointRecordsQualityFlag:
    """[Kör 20 Batch E13b, SW20-4, DFAM-001+DFAM-018] `_run_verify_update_
    status()` a 0x05-checkpont `read_qmax()`-ját korábban CSENDBEN nyelte
    el (`except Exception: pass`), miközben a 11 sorral feljebb lévő
    testvér (`read_update_status()`) UGYANEZT a checkpontot egy explicit
    `quality_flag`-gel jelzi. Ez a teszt a fix ELŐTTI kódon bukna: a
    `quality_flags` NEM tartalmazna semmilyen Qmax-hoz kötődő bejegyzést."""

    def test_qmax_read_failure_records_quality_flag_not_silently_swallowed(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl._phase_elapsed_s = 999999.0  # túllépi a verify_settle_s-t

        def _raise_generic():
            raise RuntimeError("simulated qmax read timeout")

        ctrl._bq_driver.read_qmax = _raise_generic

        ctrl._run_verify_update_status(0.0, is_final=False, next_state=LearningState.RECHARGE)

        assert any(
            f.startswith("QMAX_READ_FAILED_AT_0X05_CHECKPOINT")
            for f in ctrl._quality_flags
        )
        # A safety-allapot NEM valt FAULT-ba -- egy olvasasi hiba a BQ
        # tanulasi mero-jelzo mellett nem safety-kerdes.
        assert ctrl.state != LearningState.FAULT

    def test_qmax_read_failure_downgrades_verified_to_awaiting_not_failed(self):
        """FIX-INDUCED RISK (az archívum saját kikötése): igazold, hogy az
        új flag legrosszabb esetben VERIFIED->AWAITING átmenetet okoz, NEM
        FAILED-et -- ugyanaz a "bizonytalan != bizonyítottan sikertelen"
        elv, amit a sibling UPDATE_STATUS_READ_FAILED_AT_* flag már követ
        (l. TestSession9FinalUpdateStatusReadFailureNotClassifiedAsFailed).

        [NORMAL verifier N-3 lelete, javítva a commit ELŐTT] a korábbi
        verzió egy FRISS controlleren hívta `_classify_result()`-t, ahol
        `update_status_ok` már eleve False -- a teszt emiatt az `elif
        update_status_ok is False: AWAITING` ágra futott, NEM arra az
        `else:` ágra, amit a docstring állít (ahol MINDEN hard BQ-bizonyíték
        pozitív, és a kizárólagos ok a nem-üres `quality_flags`). Most
        MINDEN hard evidence-et explicit pozitívra állítunk, hogy a teszt
        valóban a docstring által állított ágat fedje le."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl._update_status_final = 0x06
        ctrl._qmax_before, ctrl._qmax_final = 6800, 6950
        ctrl._ra_before, ctrl._ra_final = (10.0, 12.0), (11.0, 13.0)
        ctrl._vok_qen_observed = {"vok": False, "qen": True}
        ctrl._quality_flags.append(
            "QMAX_READ_FAILED_AT_0X05_CHECKPOINT:simulated qmax read timeout"
        )

        summary = ctrl._classify_result()

        assert summary.result == LEARNING_RESULT_AWAITING
        assert summary.result != LEARNING_RESULT_FAILED


class TestSession9FinalUpdateStatusReadFailureNotClassifiedAsFailed:
    """[regresszió, Teljes kódbázis review 9. kör (2026-08-30), Session/
    Workflow P2 lelet] _classify_result() korábban `self._update_status_
    final is None`-t (a VÉGSŐ UpdateStatus OLVASÁSA hiúsult meg, l.
    _run_verify_update_status() kivétel-ága) LEARNING_RESULT_FAILED-ként
    klasszifikálta — összekeverve egy múló BQ soros-port kommunikációs
    hibát egy valódi, sikeresen leolvasott, 0x06 alatti UpdateStatus-
    értékkel. Egy read-timeout NEM bizonyítja a BQ-tanulás kudarcát
    (CLAUDE.md §2/§7) — egy fizikailag hibátlanul lezajlott, több órás
    session tévesen "BQ_LEARNING_FAILED"-ként jelent volna meg a
    report.json-ban, egy múló hiba miatt az utolsó néhány másodpercben."""

    def test_final_update_status_read_failure_yields_awaiting_not_failed(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        # _run_verify_update_status(is_final=True) kivétel-ágának eredménye:
        # status=None -> self._update_status_final = None, quality_flag
        # hozzáfűzve — a state NEM FAULT (safety-fault nélkül, csak egy
        # olvasási hiba az utolsó ellenőrzésnél).
        ctrl._update_status_final = None
        ctrl._quality_flags.append(
            "UPDATE_STATUS_READ_FAILED_AT_FINAL:simulated serial timeout"
        )

        summary = ctrl._classify_result()

        assert summary.result == LEARNING_RESULT_AWAITING
        assert summary.result != LEARNING_RESULT_FAILED

    def test_final_update_status_successfully_read_below_threshold_still_awaiting(self):
        """Kontroll-eset: egy VALÓDI, sikeresen leolvasott, 0x06 alatti
        UpdateStatus-érték is AWAITING marad (nem csak a None-eset) — a
        fix nem szabad, hogy megváltoztassa ezt a már helyes ágat."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl._update_status_final = 0x03

        summary = ctrl._classify_result()

        assert summary.result == LEARNING_RESULT_AWAITING
        assert summary.result != LEARNING_RESULT_FAILED


class TestSession11AllHardEvidencePositiveWithQualityFlagNotClassifiedAsFailed:
    """[regresszió, Teljes kódbázis review 11. kör (2026-08-31), Session/
    Workflow P1 lelet] _classify_result() végső `else` ága KIZÁRÓLAG akkor
    érhető el, ha update_status_ok ÉS qmax_update_evidenced ÉS
    ra_update_evidenced ÉS vok_qen_consistent MIND igaz — minden hard BQ/
    fizikai bizonyíték pozitív —, és az egyetlen ok, hogy nem VERIFIED, az,
    hogy self._quality_flags NEM üres. A quality_flags viszont jóindulatú
    (nem-kudarc) bejegyzésekkel is megtelhet (pl. REBOUND_ACCEPTED_AFTER_
    RETRY_AT_* — a controller saját dokumentációja szerint NEM safety-
    fault). Korábban ez FAILED-et adott — egy ténylegesen sikeres, csak
    degradált minőségi jelzőt tartalmazó fizikai+BQ ciklust tévesen
    bizonyított kudarcnak minősítve."""

    def test_all_hard_evidence_positive_with_benign_quality_flag_yields_awaiting(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        # Minden hard evidence pozitív:
        ctrl._update_status_final = 0x06
        ctrl._qmax_before, ctrl._qmax_final = 100, 105
        ctrl._ra_before, ctrl._ra_final = [10], [11]
        ctrl._vok_qen_observed = {"vok": True, "qen": True}
        # ...de van egy jóindulatú, nem-kudarc quality_flag (pl. elfogadott
        # rebound-retry, l. _run_rebound_check()):
        ctrl._quality_flags.append("REBOUND_ACCEPTED_AFTER_RETRY_AT_DISCHARGE_1")

        summary = ctrl._classify_result()

        assert summary.result == LEARNING_RESULT_AWAITING
        assert summary.result != LEARNING_RESULT_FAILED

    def test_control_all_hard_evidence_positive_no_quality_flags_yields_verified(self):
        """Kontroll-eset: ugyanez a hard evidence, DE üres quality_flags ->
        VERIFIED marad (a fix nem szabad, hogy megváltoztassa a már helyes,
        tiszta sikeres ágat)."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl._update_status_final = 0x06
        ctrl._qmax_before, ctrl._qmax_final = 100, 105
        ctrl._ra_before, ctrl._ra_final = [10], [11]
        ctrl._vok_qen_observed = {"vok": True, "qen": True}

        summary = ctrl._classify_result()

        assert summary.result == LEARNING_RESULT_VERIFIED

    def test_bq_transport_escalation_quality_flag_downgrades_verified_to_awaiting(self):
        """MAX verifier 2. kör (2026-09-14, F-2 megjegyzés,
        CLAIM_LEDGER.yaml BQ-TRANSPORT-ESCALATION-001): ugyanaz a hard
        evidence, mint a fenti kontroll-esetben (mind pozitív), DE a
        session közben a BQ-transport tartósan elérhetetlenné vált
        (BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE quality_flag) -- ez
        SZÁNDÉKOSAN lefokozza a klasszifikációt AWAITING-re, mert a
        tartósan megfigyelhetetlen BQ-diagnosztika mellett a hard
        evidence forrása maga is kétséges (CLAUDE.md §7: BQ-sikerhez
        BQ-oldali bizonyíték kell, nem csak a session VÉGÉN véletlenül
        helyreállt olvasás)."""
        fake_bq = _FakeBqState()
        cfg = LearningConfig(
            relax_after_charge_s=10.0, relax_after_discharge_s=10.0,
            max_relax_extra_wait_s=5.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
            sample_interval_s=1.0, rebound_settle_s=1.0,
            bq_transport_unreachable_after_n_polls=1,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        ctrl._update_status_final = 0x06
        ctrl._qmax_before, ctrl._qmax_final = 100, 105
        ctrl._ra_before, ctrl._ra_final = [10], [11]
        ctrl._vok_qen_observed = {"vok": True, "qen": True}

        ctrl._bq_transport.serial.simulate_no_response = True
        ctrl._record_gauge_sample(1.0)
        assert any(
            f.startswith("BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE_AT_") for f in ctrl.quality_flags
        )

        summary = ctrl._classify_result()

        assert summary.result == LEARNING_RESULT_AWAITING


class TestReview3rdRoundDesignCapacityUsesC10NotNominal:
    """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P1] a
    _classify_result() korábban `self._profile.nominal_capacity_Ah * 1000.0`-t
    (FG-sorozatnál C20 alapú) számított újra a design_capacity_mAh-hoz,
    holott a BatteryProfile MÁR exponál egy helyes, C10-alapú
    `design_capacity_mAh` property-t (amit bq_consistency_gate.py Block 2
    gate-je is használ). CLAUDE.md §11.1: "A C10 és C20 nem cserélhető fel"."""

    def test_design_capacity_mah_uses_c10_not_nominal_for_fg_series_profile(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        # FG-sorozat szimulációja: nominal_capacity_Ah=C20=20.0, a C10 (kisebb)
        # explicit megadva — a BatteryProfile validációja ezt engedi
        # (C10_capacity_Ah <= nominal_capacity_Ah).
        fg_profile = BatteryProfile(
            battery_name="TEST_FG", manufacturer="TEST", model="FG20AH",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=20.0,
            C10_capacity_Ah=14.0, terminate_voltage_per_cell_V=1.80,
        )
        ctrl._profile = fg_profile

        summary = ctrl._classify_result()

        assert summary.design_capacity_mAh == pytest.approx(14000.0)
        assert summary.design_capacity_mAh != pytest.approx(20000.0)
        assert summary.design_capacity_mAh == pytest.approx(fg_profile.design_capacity_mAh)


class TestEmergencyStopOffFailureReported:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Hardver/
    Műszervezérlés P2 lelet] l. test_charge_controller.py::
    TestEmergencyStopOffFailureReported ugyanerről a csendben elnyelt
    OFF-kísérlet-hiba mintáról. Itt az emittált LEARNING_FAULT esemény
    event_message-ének is a kiegészített fault_reason-t kell kapnia."""

    def test_load_off_failure_surfaces_in_fault_reason_and_event(self):
        ctrl, _psu, load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        events = []
        ctrl.on_event = events.append

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.emergency_stop("DMM_FEEDBACK_LOST")

        assert ctrl.state == LearningState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert "LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "simulated load off failure" in ctrl.fault_reason
        fault_events = [e for e in events if e["event_code"] == "LEARNING_FAULT"]
        assert len(fault_events) == 1
        assert "LOAD_OFF_FAILED" in fault_events[0]["event_message"]

    def test_off_success_leaves_fault_reason_unchanged(self):
        ctrl, *_ = make_controller()
        ctrl.emergency_stop("DMM_FEEDBACK_LOST")
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"


class TestGaugePollThrottling:
    """CF-10a (governance-v2 review-kör 1, DFAM-027, ceiling P1): a
    _record_gauge_sample() -> poll_bq_diagnostics() 19 mezőt olvas, minden
    mezőt a transport saját max_retries-ével (alapból 3) -- teljes
    Pico-kiesésnél ez akár 19*3*1.0s ~= 57s szinkron blokkolást okozott
    egyetlen tickben. A _poll_bq_diagnostics_throttled() ideiglenesen
    1-re csökkenti a max_retries-t a diagnosztikai poll idejére."""

    def test_throttled_poll_uses_single_attempt_per_field_on_total_disconnect(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(
            fake_bq=fake_bq, config=LearningConfig(sample_interval_s=1.0),
        )
        serial = ctrl._bq_transport.serial
        serial.simulate_no_response = True
        writes_before = len(serial.written_requests)

        ctrl._record_gauge_sample(1.0)

        writes_after = len(serial.written_requests)
        # 19 mező x 1 kísérlet (max_retries=1 a throttled poll alatt) --
        # a fix nélkül 19 x 3 = 57 írás történne ugyanerre az egy pollra.
        assert writes_after - writes_before == 19

    def test_throttled_poll_restores_original_max_retries_afterwards(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(
            fake_bq=fake_bq, config=LearningConfig(sample_interval_s=1.0),
        )
        original = ctrl._bq_transport.max_retries
        assert original == 3  # PicoBQTransport alapértéke

        ctrl._record_gauge_sample(1.0)

        assert ctrl._bq_transport.max_retries == original

    def test_throttled_poll_restores_max_retries_even_after_total_disconnect(self):
        """A visszaállításnak akkor is meg kell történnie, ha a poll alatt
        MINDEN mező sikertelen volt (nem csak a sikeres esetben)."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(
            fake_bq=fake_bq, config=LearningConfig(sample_interval_s=1.0),
        )
        ctrl._bq_transport.serial.simulate_no_response = True

        ctrl._record_gauge_sample(1.0)

        assert ctrl._bq_transport.max_retries == 3

    def test_one_shot_initial_snapshot_poll_not_throttled(self):
        """A _run_initial_snapshot()/_run_final_snapshot() egyszeri,
        terminális pollja a Qmax/Ra learning-eredmény TÉNYLEGES adatát
        rögzíti -- ez NEM veszítheti el a retry-robusztusságot egy
        tranziens, elhárítható hiba miatt, szemben a tisztán diagnosztikai
        _record_gauge_sample()-mel."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        serial = ctrl._bq_transport.serial
        serial.simulate_no_response = True
        writes_before = len(serial.written_requests)

        ctrl._run_initial_snapshot()

        writes_after = len(serial.written_requests)
        # 19 mező x 3 kísérlet (a max_retries VÁLTOZATLAN marad ezen az
        # úton) -- ha ez is throttle-ölődne, itt is 19 írás lenne 57 helyett.
        # + 2 mező (control_status ELŐZETES ellenőrzés + seal) x 3 kísérlet:
        # [SCF-011 újratervezés, 2026-09-11] a qmax/ra hiba miatt itt
        # FAULT-ol -> emergency_stop() -> _ensure_bq_sealed_or_flag
        # ("EMERGENCY_STOP") fallback-zárási kísérlete ugyanazon (throttle
        # nélküli) transporton először egy control_status-t olvas (gyors
        # "már sealed?" ellenőrzés). R17-05 (Kör 17, MAX verifier P2 lelete,
        # 2026-09-16): korábban ha EZ az első olvasás hibázott, a metódus
        # rögtön False-t adott, SOHA nem kísérelte meg a seal()-t -- egy
        # tranziens olvasási hiba emiatt a chipet UNSEALED állapotban
        # hagyta volna, holott a seal() RPC ténylegesen sikeres lett volna.
        # Javítva: a hiba rögzítve marad, DE a végrehajtás folytatódik a
        # seal() megkísérlésére -- ezért itt teljes kommunikáció-kiesésnél
        # MOST már KÉT mező (control_status ELŐZETES + seal) éri el a
        # max_retries=3-at, nem csak egy.
        assert writes_after - writes_before == 19 * 3 + 2 * 3
        assert ctrl._bq_transport.max_retries == 3

    def test_initial_snapshot_qmax_or_ra_read_failure_faults_early(self):
        """[regresszió, Review-kör 2 (2026-09-08), Session/Workflow/
        Állapotgép S-4 lelet] a Qmax/Ra tábla olvasási hibáját MÁR
        _run_initial_snapshot()-ban (a fizikai ciklus LEGELEJÉN) kell
        észlelni -- korábban semmilyen kapu nem volt, a hiba csak
        _run_final_snapshot()/_classify_result()-ban, a ~30 órás fizikai
        ciklus VÉGÉN derült volna ki (l. sibling: golden_gg_controller.py
        ::_run_precond() GG_SOURCE_SNAPSHOT_INCOMPLETE kapuja, ami ezt már
        korábban megoldotta ugyanerre a hibaosztályra)."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller(fake_bq=fake_bq)
        ctrl._bq_transport.serial.simulate_no_response = True

        ctrl._run_initial_snapshot()

        assert ctrl.state == LearningState.FAULT
        assert "LEARNING_SOURCE_SNAPSHOT_INCOMPLETE" in ctrl.fault_reason
        # A fizikai IT_ENABLE lépés SOSEM indult el.
        assert "it_enable" not in fake_bq.calls
        assert ctrl._bq_transport.max_retries == 3


class TestDecisiveSnapshotsReachGaugeSample:
    """Kor 18, Batch C7 R17-16 (DFAM-021+DFAM-001): a HAROM donto BQ
    snapshot (GoldenGgController PRECOND-forras + TraditionalLearning
    Controller INITIAL_SNAPSHOT/FINAL_SNAPSHOT) kozul a masodik ketto
    korabban KIZAROLAG a controller belso mezojebe (_initial_snapshot/
    _final_snapshot) kerult -- sem az elo BQ-diagnosztika nezetbe, sem
    a bq_diagnostics.csv-be nem jutott el, tehat a learning-verdikt
    bizonyitéka nem volt rekonstrualhato a session-mappabol."""

    def test_initial_snapshot_emits_on_gauge_sample(self):
        ctrl, *_ = make_controller()
        captured = []
        ctrl.on_gauge_sample = captured.append

        ctrl._run_initial_snapshot()

        assert len(captured) == 1
        assert captured[0] is ctrl._initial_snapshot

    def test_final_snapshot_emits_on_gauge_sample(self):
        ctrl, *_ = make_controller()
        captured = []
        ctrl.on_gauge_sample = captured.append

        ctrl._run_final_snapshot()

        assert len(captured) == 1
        assert captured[0] is ctrl._final_snapshot

    def test_final_snapshot_emit_exception_does_not_block_cell_bl_restore(self):
        """A legfontosabb regresszio: egy on_gauge_sample-kivetel NEM
        akadalyozhatja a _restore_cell_bl_if_needed()-et/a zaro seal()-t
        -- ez a DFAM-033 kockazat, amit a best-effort guard celoz
        kizarni (l. _emit_gauge_sample_best_effort() docstringje)."""
        ctrl, *_ = make_controller()

        def _raise(_snapshot):
            raise RuntimeError("simulated GUI callback failure")
        ctrl.on_gauge_sample = _raise

        ctrl._run_final_snapshot()
        _b23b_finish_recharge(ctrl)

        assert ctrl.state == LearningState.DONE
        assert ctrl._final_snapshot is not None

    def test_no_subscriber_is_safe(self):
        ctrl, *_ = make_controller()
        assert ctrl.on_gauge_sample is None
        ctrl._run_initial_snapshot()
        assert ctrl.state == LearningState.IT_ENABLE_STEP

    def test_gauge_log_is_not_extended_by_decisive_snapshots(self):
        """A _gauge_log-nak MA nincs fogyasztoja (DFAM-026), ezert a
        decisive snapshotok bovitese indokolatlanul szeleseitene a diffet
        -- csak a periodikus _record_gauge_sample() irhat bele (l.
        _emit_gauge_sample_best_effort() docstringje, KEEP_AS_IS)."""
        ctrl, *_ = make_controller()
        captured = []
        ctrl.on_gauge_sample = captured.append

        before = len(ctrl.gauge_log)
        ctrl._run_initial_snapshot()
        ctrl._run_final_snapshot()

        assert len(captured) == 2
        assert len(ctrl.gauge_log) == before


class TestGaugePollThrottleSelfReinforcingLoopFix:
    """[regresszió, 2026-09-15, C-01/DFAM-027 P0, MAX verifier V-2 lelete]
    A Round 2 ELSŐ regressziós tesztjei (TestBqTransportUnreachableEscalation,
    dt_s=100.0 minden hívásra) NEM diszkrimináltak: 100.0 >= sample_
    interval_s mellett MINDEN hívás pollozna HEAD-kódon (a kizárás nélküli
    régin) IS, tehát a teszt a régi és az új kódon egyaránt zölden futott
    volna át. Ez a teszt a TestRunner tick-hurok VALÓS dt_s-visszacsatolását
    szimulálja (l. test_runner.py `_last_tick_t` kommentje): minden
    szimulált tick `dt_s`-e a NOMINÁLIS tickidő + az ELŐZŐ `_record_gauge_
    sample()`-hívás SAJÁT (itt: egy lassú, fake órával szimulált poll)
    ideje -- pontosan úgy, ahogy `test_runner.py` a `controller.advance()`
    hívás saját I/O-idejét a KÖVETKEZŐ `actual_dt_s`-be engedi (M5-01
    minta). A HEAD-kódon (dt_s-kizárás nélkül) az ELSŐ lassú poll UTÁN
    MINDEN további tick maga is >= sample_interval_s, tehát minden ticken
    újra pollozna (önerősítő hurok, D10 HIL: ~54s/tick). A fix után a poll
    a NÉVLEGES kadenciára áll vissza."""

    def test_repeated_slow_polls_settle_to_nominal_cadence_not_every_tick(self, monkeypatch):
        import Prog.src.traditional_learning_controller as tlc_module

        fake_bq = _FakeBqState()
        cfg = LearningConfig(
            relax_after_charge_s=10.0, relax_after_discharge_s=10.0,
            max_relax_extra_wait_s=5.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
            sample_interval_s=30.0, rebound_settle_s=1.0,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)

        fake_now = [0.0]
        monkeypatch.setattr(tlc_module.time, "perf_counter", lambda: fake_now[0])

        poll_count = [0]
        original_poll = ctrl._poll_bq_diagnostics_throttled
        # MAX verifier 2. kör W-2 lelete: 35.0 NEM D10-en mért érték (a
        # dokumentált worst-case ~19s, egy D10 session-ben a poll saját
        # ideje ~2s volt mérve) -- SZÁNDÉKOSAN a sample_interval_s=30.0-nál
        # nagyobbra választva, hogy a degenerált (önerősítő) esetet
        # kényszerítse ki a tesztben.
        POLL_DURATION_S = 35.0

        def _slow_poll():
            poll_count[0] += 1
            fake_now[0] += POLL_DURATION_S
            return original_poll()

        ctrl._poll_bq_diagnostics_throttled = _slow_poll  # type: ignore[method-assign]

        NOMINAL_TICK_S = 2.0
        TOTAL_TICKS = 40
        last_tick_t = 0.0
        for _ in range(TOTAL_TICKS):
            tick_start_t = fake_now[0]
            dt_s = tick_start_t - last_tick_t
            ctrl._record_gauge_sample(dt_s)
            last_tick_t = tick_start_t
            fake_now[0] += NOMINAL_TICK_S  # a külső hurok nominális tick-kadenciája

        # Névleges kadencia mellett ~40*2/(30+35) ≈ 1.2 poll várható -- a
        # HEAD-kódon (kizárás nélkül) az ELSŐ lassú poll után MINDEN
        # további tick újra pollozna (≈ 40-15=25 további poll), tehát a
        # helyes/hibás viselkedés között nagyságrendi a különbség.
        assert poll_count[0] <= 3


class TestBqTransportUnreachableEscalation:
    """HIL-D5 (2026-09-14, CF-10a/DFAM-027, CLAIM_LEDGER.yaml PICO-
    RECONNECT-001): egy valós Pico-kiesés HIL-mérése kimutatta, hogy a
    kiesés SOHA nem áll helyre magától a puszta ismételt próbálkozástól --
    nincs "elég hosszú backoff", ami segítene. A helyes minta egy
    escalation-küszöb: k egymást követő TELJESEN érvénytelen poll után
    explicit, operátor felé látható esemény, a fizikai ciklus leállítása
    nélkül."""

    def test_no_event_before_threshold(self):
        fake_bq = _FakeBqState()
        cfg = LearningConfig(
            relax_after_charge_s=10.0, relax_after_discharge_s=10.0,
            max_relax_extra_wait_s=5.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
            sample_interval_s=1.0, rebound_settle_s=1.0,
            bq_transport_unreachable_after_n_polls=3,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl._bq_transport.serial.simulate_no_response = True

        # Review 2026-09-15 (C-01/DFAM-027): dt_s=100.0 (>> sample_interval_s),
        # hogy minden hívás önmagában, bőséges ráhagyással lépje át a
        # küszöböt -- a C-01 fix a poll SAJÁT (bár itt mock alatt is
        # mérhető, nem-nulla) wall-clock idejét kizárja a KÖVETKEZŐ hívás
        # akkumulációjából, ami dt_s==sample_interval_s pontos egyezésnél
        # minden további hívást egy hajszálnyival a küszöb ALÁ vetett volna.
        ctrl._record_gauge_sample(100.0)
        ctrl._record_gauge_sample(100.0)

        assert ctrl._consecutive_fully_invalid_bq_polls == 2
        assert not any(e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE" for e in events)

    def test_event_fires_exactly_at_threshold(self):
        fake_bq = _FakeBqState()
        cfg = LearningConfig(
            relax_after_charge_s=10.0, relax_after_discharge_s=10.0,
            max_relax_extra_wait_s=5.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
            sample_interval_s=1.0, rebound_settle_s=1.0,
            bq_transport_unreachable_after_n_polls=3,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl._bq_transport.serial.simulate_no_response = True

        # Review 2026-09-15 (C-01/DFAM-027) -- l. test_no_event_before_
        # threshold() kommentje a dt_s=100.0 margó indoklásáért.
        ctrl._record_gauge_sample(100.0)
        ctrl._record_gauge_sample(100.0)
        ctrl._record_gauge_sample(100.0)

        matching = [e for e in events if e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE"]
        assert len(matching) == 1
        assert matching[0]["severity"] == "CRITICAL"
        assert any(
            f.startswith("BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE_AT_") for f in ctrl.quality_flags
        )

    def test_event_does_not_repeat_while_still_unreachable(self):
        """Miután egyszer jelezve lett, a KÖVETKEZŐ pollok (amíg a
        transport ugyanúgy elérhetetlen marad) NE spam-eljék az esemény-
        naplót minden sample_interval_s-nél."""
        fake_bq = _FakeBqState()
        cfg = LearningConfig(
            relax_after_charge_s=10.0, relax_after_discharge_s=10.0,
            max_relax_extra_wait_s=5.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
            sample_interval_s=1.0, rebound_settle_s=1.0,
            bq_transport_unreachable_after_n_polls=2,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl._bq_transport.serial.simulate_no_response = True

        for _ in range(5):
            ctrl._record_gauge_sample(1.0)

        matching = [e for e in events if e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE"]
        assert len(matching) == 1

    def test_counter_resets_and_can_refire_after_recovery(self):
        """Ha a transport helyreáll (sikeres poll), majd ÚJRA kiesik, az
        escalation ÚJRA jelezhet -- a "már jelezve" flag NEM permanens."""
        fake_bq = _FakeBqState()
        cfg = LearningConfig(
            relax_after_charge_s=10.0, relax_after_discharge_s=10.0,
            max_relax_extra_wait_s=5.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
            sample_interval_s=1.0, rebound_settle_s=1.0,
            bq_transport_unreachable_after_n_polls=2,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl._bq_transport.serial.simulate_no_response = True

        # Review 2026-09-15 (C-01/DFAM-027) -- l. test_no_event_before_
        # threshold() kommentje a dt_s=100.0 margó indoklásáért.
        ctrl._record_gauge_sample(100.0)
        ctrl._record_gauge_sample(100.0)
        assert ctrl._consecutive_fully_invalid_bq_polls == 2

        # "Helyreáll" -- a következő poll sikeres.
        ctrl._bq_transport.serial.simulate_no_response = False
        ctrl._record_gauge_sample(100.0)
        assert ctrl._consecutive_fully_invalid_bq_polls == 0
        assert ctrl._bq_transport_unreachable_flagged is False

        # Újra kiesik -- az escalation ÚJRA jelezhet.
        ctrl._bq_transport.serial.simulate_no_response = True
        ctrl._record_gauge_sample(100.0)
        ctrl._record_gauge_sample(100.0)

        matching = [e for e in events if e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE"]
        assert len(matching) == 2

    def test_successful_poll_never_triggers_escalation(self):
        fake_bq = _FakeBqState()
        cfg = LearningConfig(
            relax_after_charge_s=10.0, relax_after_discharge_s=10.0,
            max_relax_extra_wait_s=5.0, it_enable_settle_s=1.0, verify_settle_s=1.0,
            sample_interval_s=1.0, rebound_settle_s=1.0,
            bq_transport_unreachable_after_n_polls=1,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append

        ctrl._record_gauge_sample(1.0)

        assert ctrl._consecutive_fully_invalid_bq_polls == 0
        assert not any(e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE" for e in events)


class TestReviewRound5S505UnhandledStateRaises:
    """[regresszió, review-kör 5, Session/Workflow S5-05 (P3, DFAM-004
    sibling) -- l. test_charge_controller.py azonos hivatkozású tesztje
    ugyanerről a hibaosztályról] advance() korábban hallgatólagosan
    (a dict.get()-None ágon keresztül) no-op-olt egy le nem kezelt
    LearningState-re."""

    def test_advance_raises_on_unhandled_state(self):
        ctrl, *_ = make_controller()
        ctrl._state = "BOGUS_FUTURE_STATE"
        with pytest.raises(ValueError, match="BOGUS_FUTURE_STATE"):
            ctrl.advance(1.0)


def test_c08_psu_readback_proxies_delegate_to_charge_ctrl():
    """[MAX verifier P3-4, 2026-09-15, review-kör 6, C-08a/C-08b] l.
    test_ocv_soc_controller.py azonos nevű tesztje a teljes indoklásért."""
    ctrl, *_ = make_controller()
    assert ctrl._charge_ctrl is None
    assert ctrl.i_psu_measured_A is None
    assert ctrl.psu_current_readback_valid is None
    assert ctrl.u_psu_measured_V is None
    assert ctrl.psu_voltage_readback_valid is None

    class _FakeChargeCtrl:
        i_psu_measured_A = 1.23
        psu_current_readback_valid = True
        u_psu_measured_V = 14.56
        psu_voltage_readback_valid = False

    ctrl._charge_ctrl = _FakeChargeCtrl()
    assert ctrl.i_psu_measured_A == 1.23
    assert ctrl.psu_current_readback_valid is True
    assert ctrl.u_psu_measured_V == 14.56
    assert ctrl.psu_voltage_readback_valid is False


class TestLoadCurrentReadbackResetOrder:
    """MAX verifier V-1 (2026-09-15, R2-06, 12. writer-kör): l.
    test_golden_gg_controller.py azonos nevű teszt-osztálya a teljes
    indoklásért -- a C-08a F-1 hibaosztály-minta replikálása
    traditional_learning_controller.py-ra (DISCHARGE_1 fázis). A
    controller direktbe DISCHARGE_1 állapotba kényszerítve (a teljes
    BQ VOK/QEN-szekvencia megkerülésével -- ez a teszt kizárólag a
    validity-flag reset-sorrendet vizsgálja, nem a BQ-workflow-t)."""

    def test_resets_to_none_on_dmm_feedback_lost_gate(self):
        ctrl, _psu, load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl._state = LearningState.DISCHARGE_1
        load._input_is_on = True

        load.current_A = 0.7
        ctrl.advance(1.0)
        assert ctrl.load_current_readback_valid is True
        assert ctrl.i_load_measured_A == pytest.approx(0.7)

        dmm_v.simulate_timeout = True  # top-of-tick DMM_FEEDBACK_LOST kapu
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert ctrl.load_current_readback_valid is None
        assert ctrl.i_load_measured_A is None


class TestR1B3DischargeStartDmmGateAndD1Handover:
    """[regresszió, Claude-Codex loop R1 B3: F-41/NS-01 (DFAM-028/017),
    F-02 (DFAM-001), CF-05 P2-1 (DFAM-008)]

    F-41/NS-01: `_begin_discharge(DISCHARGE_2)` korábban a D1 Ah/Wh/
    extrém-dt számlálóit a concurrent-kapu és a setup-`try` ELŐTT másolta
    a `_discharge_1_*`/`_discharge_total_*` mezőkbe, miközben az élő
    integrátor nullázása csak a sikeres `input_on()` UTÁN jött -- korai
    kilépésnél (concurrent-FAULT, setup-kivétel) a D1 adatai kétszer
    számolódtak (2 Ah / 48 Wh / 4 extrém dt 1 Ah / 24 Wh / 2 helyett,
    d2 = d1 hamis második mérésként).

    F-02: a négy kisütésindító (TLC/ChemID/GG/OCV) a per-tick DMM-kaput
    csak a KÖVETKEZŐ ticken futtatta -- egy ismerten hibás DMM-olvasás
    mellett a Load INPUT ON-t kapott.

    CF-05 P2-1: sikertelen DMM-olvasásnál a RelaxController a régi dV/dt-t
    tartja meg, a `_run_relax()` elfogadó ága ezt friss mérésnek vette."""

    @staticmethod
    def _cfg(**overrides):
        base = {
            "relax_after_charge_s": 1.0, "relax_after_discharge_s": 1.0,
            "max_relax_extra_wait_s": 2.0, "it_enable_settle_s": 0.5, "verify_settle_s": 0.5,
            "sample_interval_s": 1000.0, "rebound_settle_s": 1.0,
        }
        base.update(overrides)
        return LearningConfig(**base)

    @staticmethod
    def _controller_with_d1_sample():
        """D1 élő integrátor: 1 Ah / 24 Wh, 2 extrém dt (3600 s és 301 s)."""
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller()
        ctrl._discharge_integrator.add_sample(-1.0, 24.0, 3600.0)
        ctrl._discharge_integrator.add_sample(0.0, 24.0, 301.0)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(1.0)
        assert ctrl.extreme_dt_s_count == 2
        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        return ctrl, psu, load, dmm_v, dmm_t, fake_bq

    @staticmethod
    def _assert_d1_counted_exactly_once(ctrl) -> None:
        assert ctrl.accumulated_discharge_Ah == pytest.approx(1.0)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(24.0)
        assert ctrl.extreme_dt_s_count == 2
        summary = ctrl.result_summary
        assert summary is not None
        assert summary.discharge_1_capacity_Ah == pytest.approx(1.0)
        assert summary.discharge_2_capacity_Ah is None
        assert summary.measured_capacity_Ah == pytest.approx(1.0)

    def _drive_to_precharge_relax(self, config):
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(dmm_voltage_V=13.5, config=config)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> PRECHARGE
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # -> PRECHARGE_RELAX
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        return ctrl, psu, load, dmm_v, dmm_t, fake_bq

    def _drive_to_relax_pre_discharge_2(self, config):
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = self._drive_to_precharge_relax(config)
        _accept_relax(ctrl, fake_bq)
        for _ in range(5):
            if ctrl.state != LearningState.PRECHARGE_RELAX:
                break
            ctrl.advance(100.0)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        ctrl.advance(1.0)  # INITIAL_SNAPSHOT -> IT_ENABLE_STEP
        ctrl.advance(1.0)  # IT_ENABLE + settle -> VERIFY_VOK_QEN
        fake_bq.set_qen(True)
        fake_bq.set_vok(True)
        ctrl.advance(1.0)  # -> DISCHARGE_1
        assert ctrl.state == LearningState.DISCHARGE_1
        ctrl.advance(3600.0)  # D1 Ah felhalmozása
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)  # -> REBOUND_CHECK_1
        ctrl.advance(1.0)  # -> RELAX_1
        assert ctrl.state == LearningState.RELAX_1
        fake_bq.update_status = 0x05
        _accept_relax(ctrl, fake_bq)
        for _ in range(5):
            if ctrl.state != LearningState.RELAX_1:
                break
            ctrl.advance(100.0)
        ctrl.advance(1.0)  # VERIFY_UPDATE_STATUS_05 -> RECHARGE
        assert ctrl.state == LearningState.RECHARGE
        dmm_v.voltage_V = 13.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RELAX_PRE_DISCHARGE_2
        return ctrl, psu, load, dmm_v, dmm_t, fake_bq

    # --- F-41 / NS-01 ------------------------------------------------- #

    def test_d2_concurrent_fault_counts_d1_exactly_once(self):
        ctrl, psu, load, *_ = self._controller_with_d1_sample()
        psu.output_commanded_on = True

        ctrl._begin_discharge(LearningState.DISCHARGE_2)

        assert ctrl.state == LearningState.FAULT
        assert "CONCURRENT_PSU_LOAD" in ctrl.fault_reason
        assert load.input_commanded_on is False
        assert "input_on()" not in load.calls
        self._assert_d1_counted_exactly_once(ctrl)

    def test_d2_setup_exception_counts_d1_exactly_once(self):
        ctrl, psu, load, *_ = self._controller_with_d1_sample()

        def _input_on_fails(clear_first: bool = True, **kwargs) -> None:
            raise TimeoutError("simulated input_on ack lost")

        load.input_on = _input_on_fails

        ctrl._begin_discharge(LearningState.DISCHARGE_2)

        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason.startswith("DISCHARGE_SETUP_FAILED")
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        self._assert_d1_counted_exactly_once(ctrl)

    # --- F-02: DMM-őr a kisütésindító elején --------------------------- #

    def test_d2_dmm_invalid_faults_before_any_energize_and_counts_d1_once(self):
        ctrl, psu, load, *_ = self._controller_with_d1_sample()
        ctrl._dmm_valid = False

        ctrl._begin_discharge(LearningState.DISCHARGE_2)

        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls
        assert not any(c.startswith("set_current") for c in load.calls)
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        self._assert_d1_counted_exactly_once(ctrl)

    def test_d1_dmm_invalid_faults_before_energize(self):
        ctrl, psu, load, *_ = make_controller()
        ctrl._dmm_valid = False
        ctrl._dmm_v.simulate_timeout = True  # Az új kapumérés is hibás.

        ctrl._begin_discharge(LearningState.DISCHARGE_1)

        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls
        assert not any(c.startswith("set_current") for c in load.calls)
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls

    def test_rpd2_timeout_with_invalid_dmm_faults_instead_of_energizing(self):
        ctrl, _psu, load, dmm_v, _dmm_t, fake_bq = self._drive_to_relax_pre_discharge_2(self._cfg())
        d1_Ah = ctrl.accumulated_discharge_Ah
        assert d1_Ah > 0.0
        assert ctrl._discharge_1_capacity_Ah is None

        fake_bq.set_ocvtaken(False)  # BQ-elfogadás nem teljesül -> timeout-ág
        ctrl.advance(0.0)  # RelaxController INIT
        load.reset_calls()
        dmm_v.simulate_timeout = True
        ctrl.advance(10.0)  # min idő + extra wait letelt, DMM EZEN a ticken hibás

        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls
        assert load.input_commanded_on is False
        assert any(
            f.startswith("RELAX_ACCEPTANCE_NOT_CONFIRMED_AT_RELAX_PRE_DISCHARGE_2")
            for f in ctrl.quality_flags
        )
        summary = ctrl.result_summary
        assert summary is not None
        assert summary.result != LEARNING_RESULT_VERIFIED
        assert summary.cell_bl_restore_confirmed is True
        assert fake_bq.control_status_word & 0x2000, "a chipnek visszazárva kell lennie FAULT-nál"
        assert summary.discharge_1_capacity_Ah == pytest.approx(d1_Ah)
        assert summary.discharge_2_capacity_Ah is None

        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "input_on()" not in load.calls

    def test_precharge_relax_timeout_invalid_dmm_blocks_discharge_1_start(self):
        """Az első kisütés friss kapumérése tartós DMM-hibánál tilt,
        a közbenső IT/BQ-lépések után is."""
        ctrl, _psu, load, dmm_v, _dmm_t, fake_bq = self._drive_to_precharge_relax(self._cfg())
        ctrl.advance(0.0)  # RelaxController INIT (OCVTAKEN=0 -> timeout-ág)
        dmm_v.simulate_timeout = True
        ctrl.advance(10.0)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        assert ctrl.dmm_valid is False

        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)  # INITIAL_SNAPSHOT -> IT_ENABLE_STEP
        ctrl.advance(1.0)  # IT_ENABLE + settle -> VERIFY_VOK_QEN
        assert ctrl.state == LearningState.VERIFY_VOK_QEN
        assert ctrl.dmm_valid is False
        fake_bq.set_qen(True)
        fake_bq.set_vok(True)
        load.reset_calls()
        dmm_v.simulate_timeout = True  # A döntést a kapumérés aktuális hibája indokolja.
        ctrl.advance(1.0)  # -> _begin_discharge(DISCHARGE_1)

        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls
        assert load.input_commanded_on is False
        summary = ctrl.result_summary
        assert summary is not None
        assert summary.cell_bl_restore_confirmed is True
        assert fake_bq.control_status_word & 0x2000, "a chipnek visszazárva kell lennie FAULT-nál"

    # --- CF-05 P2-1: dV/dt-elfogadás csak érvényes DMM-ticken ----------- #

    def test_relax_not_accepted_on_failed_dmm_tick_then_accepted_after_refill(self):
        ctrl, _psu, _load, dmm_v, _dmm_t, fake_bq = self._drive_to_precharge_relax(
            self._cfg(max_relax_extra_wait_s=2 * RelaxConfig().sample_period_s),
        )
        ctrl.advance(0.0)  # RelaxController INIT
        _accept_relax(ctrl, fake_bq)  # BQ-feltétel teljesül

        dmm_v.simulate_timeout = True
        ctrl.advance(2.0)  # RELAX_DONE-tick, DE a DMM hibás
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl.dmm_valid is False

        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)  # R16-B4: one fresh tick is insufficient.
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        _complete_relax_window(ctrl)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        assert not any("RELAX_ACCEPTANCE_NOT_CONFIRMED" in f for f in ctrl.quality_flags)

    def test_transient_dmm_failure_at_rpd2_acceptance_does_not_fault(self):
        ctrl, _psu, load, dmm_v, _dmm_t, fake_bq = self._drive_to_relax_pre_discharge_2(
            self._cfg(max_relax_extra_wait_s=2 * RelaxConfig().sample_period_s),
        )
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(True)  # CF-06: RECHARGE utáni relaxban a VOK SET marad
        ctrl.advance(0.0)  # RelaxController INIT
        load.reset_calls()

        dmm_v.simulate_timeout = True
        ctrl.advance(2.0)  # elfogadó tick, DE a DMM hibás
        assert ctrl.state == LearningState.RELAX_PRE_DISCHARGE_2
        assert "input_on()" not in load.calls

        dmm_v.simulate_timeout = False
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RELAX_PRE_DISCHARGE_2
        assert "input_on()" not in load.calls
        _complete_relax_window(ctrl)
        assert ctrl.state == LearningState.DISCHARGE_2
        assert "input_on()" in load.calls


class TestR1B4UpdateStatusAndQmaxRangeGate:
    """[R1 B4: F-10(a), F-10(b)/NS-11, F-11] A Traditional Learning VERIFIED-
    kapuja.

    - F-10(a): a végső UpdateStatus-kapu korábban `>= 0x06` volt, a 0x05-
      checkpoint `< 0x05` -- így 0x07..0xFF (TI SLUSB55B rev. B Table 11 /
      Table 20 szerint nem definiált learning-állapot) VERIFIED-et adott.
      Most pontos egyezés a bq_learning_verification.py meglévő
      konstansaival: végső == 0x06, checkpoint {0x05, 0x06}.
    - F-10(b)/NS-11: a Qmax before/final párt nem ellenőrizte a TI-
      tartományra (Qmax Cell 0, SC82/off0, I2, 0..32767; FIELD_REGISTRY);
      egy -1/-32768 érték "Qmax-frissülésnek" és értelmetlen
      qmax_change_percent-nek számított.
    - F-11: az IT_ENABLE ELŐTTI kiinduló UpdateStatus már a summary része.
      A Q-4 döntés alapján B18 óta a kezdeti 0x06 erre a ciklusra AWAITING;
      a 10. teszt ezt és a BQ-státusz változatlanságát ellenőrzi."""

    @staticmethod
    def _set_positive_hard_evidence(ctrl, qmax_before=100, qmax_final=105):
        ctrl._update_status_final = 0x06
        ctrl._qmax_before, ctrl._qmax_final = qmax_before, qmax_final
        ctrl._ra_before, ctrl._ra_final = [10], [11]
        ctrl._vok_qen_observed = {"vok": True, "qen": True}

    # 1. -----------------------------------------------------------------
    @pytest.mark.parametrize("value", [*range(0x100), None])
    def test_final_update_status_only_exact_0x06_is_verified(self, value):
        ctrl, *_ = make_controller()
        self._set_positive_hard_evidence(ctrl)
        ctrl._update_status_final = value

        summary = ctrl._classify_result()

        if value == 0x06:
            assert summary.result == LEARNING_RESULT_VERIFIED
        else:
            assert summary.result == LEARNING_RESULT_AWAITING
        assert summary.result != LEARNING_RESULT_FAILED

    # 2. -----------------------------------------------------------------
    @pytest.mark.parametrize("value", [0x05, 0x06, 0x07, 0xFF])
    def test_final_check_flags_every_value_except_0x06(self, value):
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
        ctrl._phase_elapsed_s = 999999.0  # túllépi a verify_settle_s-t
        fake_bq.update_status = value

        ctrl._run_verify_update_status(
            0.0, is_final=True, next_state=LearningState.FINAL_SNAPSHOT,
        )

        assert ctrl._update_status_final == value
        expected_flag = f"UPDATE_STATUS_NOT_0X06_AT_FINAL_CHECK(got={value})"
        if value == 0x06:
            assert not any(
                f.startswith("UPDATE_STATUS_NOT_0X06_AT_FINAL_CHECK") for f in ctrl._quality_flags
            )
        else:
            assert expected_flag in ctrl._quality_flags
        assert ctrl.state == LearningState.FINAL_SNAPSHOT

    # 3. -----------------------------------------------------------------
    @pytest.mark.parametrize("value", range(0x100))
    def test_0x05_checkpoint_accepts_only_0x05_and_0x06(self, value):
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
        ctrl._phase_elapsed_s = 999999.0
        fake_bq.update_status = value

        ctrl._run_verify_update_status(
            0.0, is_final=False, next_state=LearningState.RECHARGE,
        )

        assert ctrl._update_status_at_0x05_check == value
        flagged = f"UPDATE_STATUS_NOT_0X05_AT_CHECKPOINT(got={value})" in ctrl._quality_flags
        assert flagged is (value not in (0x05, 0x06))

    # 4. -----------------------------------------------------------------
    @pytest.mark.parametrize(
        "qmax_before, qmax_final",
        [(100, -1), (100, -32768), (-1, 105), (100, 32768), (32768, 100)],
    )
    def test_qmax_outside_ti_range_is_not_update_evidence(self, qmax_before, qmax_final):
        ctrl, *_ = make_controller()
        self._set_positive_hard_evidence(ctrl, qmax_before, qmax_final)

        summary = ctrl._classify_result()

        assert summary.result == LEARNING_RESULT_AWAITING
        assert summary.qmax_change_percent is None
        # A nyers értékek változatlanul a summaryban maradnak (nyers ≠ származtatott).
        assert summary.qmax_before == qmax_before
        assert summary.qmax_final == qmax_final

    @pytest.mark.parametrize(
        "qmax_before, qmax_final, expected_percent",
        [(100, 105, 5.0), (100, 32767, (32767 - 100) / 100 * 100.0)],
    )
    def test_qmax_inside_ti_range_control_stays_verified(
        self, qmax_before, qmax_final, expected_percent,
    ):
        ctrl, *_ = make_controller()
        self._set_positive_hard_evidence(ctrl, qmax_before, qmax_final)

        summary = ctrl._classify_result()

        assert summary.result == LEARNING_RESULT_VERIFIED
        assert summary.qmax_change_percent == pytest.approx(expected_percent)

    # 5. -----------------------------------------------------------------
    def test_qmax_range_bounds_come_from_field_registry(self, monkeypatch):
        import dataclasses

        import Prog.src.traditional_learning_controller as tlc_mod

        within = tlc_mod._qmax_within_ti_range
        spec = FIELD_REGISTRY["qmax_cell0_initial_mAh"]
        assert spec.min_value is not None and spec.max_value is not None
        assert within(spec.min_value - 1) is False
        assert within(spec.min_value) is True
        assert within(spec.max_value) is True
        assert within(spec.max_value + 1) is False
        assert within(None) is False

        ctrl, *_ = make_controller()
        self._set_positive_hard_evidence(ctrl, qmax_before=100, qmax_final=150)
        # Literál-mutáció elleni próba: a tartományforrás a FIELD_REGISTRY,
        # nem a helperbe égetett 0..32767.
        monkeypatch.setitem(
            FIELD_REGISTRY, "qmax_cell0_initial_mAh", dataclasses.replace(spec, max_value=100),
        )
        assert within(150) is False
        assert within(100) is True

        summary = ctrl._classify_result()

        assert summary.result == LEARNING_RESULT_AWAITING
        assert summary.qmax_change_percent is None

    # 6. -----------------------------------------------------------------
    @pytest.mark.parametrize("raw, decoded", [(0x8000, -32768), (0xFFFF, -1)])
    def test_final_snapshot_out_of_range_qmax_flags_and_keeps_raw(self, raw, decoded):
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
        self._set_positive_hard_evidence(ctrl, qmax_before=100, qmax_final=None)
        ctrl._ra_before = [1.0]  # a fake ra_table-tól eltér -> Ra-frissülés igazolt
        fake_bq.update_status = 0x06
        fake_bq.qmax = raw

        ctrl._run_final_snapshot()
        _b23b_finish_recharge(ctrl)

        assert ctrl.state == LearningState.DONE
        summary = ctrl.result_summary
        assert summary is not None
        assert f"QMAX_OUT_OF_TI_RANGE_AT_FINAL_SNAPSHOT(value={decoded})" in summary.quality_flags
        assert summary.qmax_final == decoded
        assert summary.result == LEARNING_RESULT_AWAITING
        assert summary.qmax_change_percent is None

    # 7. -----------------------------------------------------------------
    def test_initial_snapshot_out_of_range_qmax_flags_without_fault(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
        fake_bq.qmax = 0xFFFF

        ctrl._run_initial_snapshot()

        assert ctrl.state == LearningState.IT_ENABLE_STEP
        assert ctrl._qmax_before == -1
        assert "QMAX_OUT_OF_TI_RANGE_AT_INITIAL_SNAPSHOT(value=-1)" in ctrl._quality_flags

    def test_initial_snapshot_in_range_qmax_does_not_flag(self):
        ctrl, *_ = make_controller()

        ctrl._run_initial_snapshot()

        assert ctrl.state == LearningState.IT_ENABLE_STEP
        assert not any(f.startswith("QMAX_OUT_OF_TI_RANGE") for f in ctrl._quality_flags)

    # 8. -----------------------------------------------------------------
    @pytest.mark.parametrize("value", [0x00, 0x05, 0x06])
    def test_summary_preserves_initial_update_status(self, value):
        import dataclasses

        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
        fake_bq.update_status = value

        ctrl._run_initial_snapshot()
        fake_bq.update_status = 0x02  # későbbi változás nem írhatja felül a baseline-t
        summary = ctrl._classify_result()

        assert summary.update_status_initial == value
        assert dataclasses.asdict(summary)["update_status_initial"] == value

    def test_summary_initial_update_status_none_on_read_failure(self):
        ctrl, *_ = make_controller()
        events = []
        ctrl.on_event = events.append

        def _raise():
            raise RuntimeError("simulated update status read timeout")

        ctrl._bq_driver.read_update_status = _raise

        # Q-39 C: az ismeretlen baseline 3 újrapróbálás után FAULT.
        ctrl._enter(LearningState.INITIAL_SNAPSHOT)
        from unittest.mock import patch

        import Prog.src.traditional_learning_controller as module

        clock = [0.0]
        with patch.object(module.time, "monotonic", lambda: clock[0]):
            for _ in range(4):
                clock[0] += 0.07
                ctrl.advance(0.07)
        summary = ctrl._classify_result()

        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == (
            "LEARNING_INITIAL_UPDATE_STATUS_UNREADABLE: "
            "attempts=4; simulated update status read timeout"
        )
        assert summary.result == LEARNING_RESULT_FAILED
        assert summary.update_status_initial is None
        snap_events = [e for e in events if e.get("event_code") == "LEARNING_INITIAL_SNAPSHOT"]
        assert snap_events == []
        assert sum(e["event_code"] == "LEARNING_INITIAL_UPDATE_STATUS_RETRY" for e in events) == 3

    def test_summary_initial_update_status_none_without_snapshot(self):
        ctrl, *_ = make_controller()

        summary = ctrl._classify_result()

        assert summary.update_status_initial is None

    # 9-10. full sequence -------------------------------------------------
    @staticmethod
    def _relax_to_completion(ctrl, fake_bq, vok_remains_set: bool = False) -> None:
        fake_bq.set_ocvtaken(True)
        fake_bq.set_vok(vok_remains_set)
        _complete_relax_window(ctrl)

    def _drive_full_sequence(self, initial_update_status: int, before_final_snapshot=None):
        """A TestFullSequenceToDone::test_full_sequence_reaches_done_with_
        verified_result mintáját követi; a kiinduló UpdateStatus
        paraméterezhető."""
        fake_bq = _FakeBqState()
        fake_bq.update_status = initial_update_status
        ctrl, _psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=LearningConfig(
                relax_after_charge_s=RelaxConfig().sample_period_s + _relax_tick_s(),
                relax_after_discharge_s=RelaxConfig().sample_period_s + _relax_tick_s(),
                max_relax_extra_wait_s=2.0, it_enable_settle_s=0.5, verify_settle_s=0.5,
                sample_interval_s=1000.0, rebound_settle_s=1.0,
            ),
        )
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        self._relax_to_completion(ctrl, fake_bq)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.IT_ENABLE_STEP
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.VERIFY_VOK_QEN
        fake_bq.set_qen(True)
        fake_bq.set_vok(True)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.DISCHARGE_1
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RELAX_1
        fake_bq.update_status = 0x05
        self._relax_to_completion(ctrl, fake_bq)
        assert ctrl.state == LearningState.VERIFY_UPDATE_STATUS_05
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RECHARGE
        dmm_v.voltage_V = 13.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RELAX_PRE_DISCHARGE_2
        self._relax_to_completion(ctrl, fake_bq, vok_remains_set=True)
        assert ctrl.state == LearningState.DISCHARGE_2
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.RELAX_2
        fake_bq.update_status = 0x06
        fake_bq.qmax = 150
        fake_bq.ra_table = [x + 1.0 for x in fake_bq.ra_table]
        self._relax_to_completion(ctrl, fake_bq)
        assert ctrl.state == LearningState.VERIFY_UPDATE_STATUS_06
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FINAL_SNAPSHOT
        if before_final_snapshot is not None:
            before_final_snapshot(ctrl, fake_bq)
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FINAL_RECHARGE
        _b23b_finish_recharge(ctrl)
        assert ctrl.state == LearningState.DONE
        return ctrl, events

    def test_full_sequence_0x05_to_0x06_transition_verified_with_baseline(self):
        ctrl, events = self._drive_full_sequence(initial_update_status=0x05)

        summary = ctrl.result_summary
        assert summary is not None
        assert summary.result == LEARNING_RESULT_VERIFIED
        assert summary.update_status_initial == 0x05
        assert summary.update_status_at_0x05_check == 0x05
        assert summary.update_status_final == 0x06
        snap = [e for e in events if e.get("event_code") == "LEARNING_INITIAL_SNAPSHOT"]
        assert len(snap) == 1
        assert "UpdateStatus=0x05 " in snap[0]["event_message"]
        assert "Q-4" not in snap[0]["event_message"]

    def test_full_sequence_baseline_0x06_is_awaiting_per_q4(self):
        """Q-4: teljes pozitív evidence mellett sem jelenthetünk új tanulást."""
        ctrl, events = self._drive_full_sequence(initial_update_status=0x06)

        summary = ctrl.result_summary
        assert summary is not None
        assert summary.result == LEARNING_RESULT_AWAITING
        assert summary.update_status_initial == 0x06
        assert summary.update_status_final == 0x06
        assert summary.qmax_before == 100 and summary.qmax_final == 150
        assert summary.ra_before and summary.ra_final != summary.ra_before
        assert summary.vok_qen_observed_after_it_enable["qen"] is True
        flag = "UPDATE_STATUS_ALREADY_0X06_AT_INITIAL_SNAPSHOT"
        assert summary.quality_flags == (flag,)
        assert ctrl._classify_result().quality_flags == (flag,)
        self._assert_closed_out_without_fault(ctrl)
        assert ctrl._psu.output_commanded_on is False
        assert ctrl._cell_bl_reseal_confirmed is True
        done, = [e for e in events if e["event_code"] == "LEARNING_DONE"]
        assert done["severity"] == "WARNING"
        assert LEARNING_RESULT_AWAITING in done["event_message"]
        assert LEARNING_RESULT_VERIFIED not in done["event_message"]
        snap = [e for e in events if e.get("event_code") == "LEARNING_INITIAL_SNAPSHOT"]
        assert len(snap) == 1
        assert " 0x06;" in snap[0]["event_message"]
        assert "Q-4 szerint AWAITING" in snap[0]["event_message"]

    # 11. R2-B1 (R2-source-01) --------------------------------------------
    @staticmethod
    def _assert_closed_out_without_fault(ctrl) -> None:
        summary = ctrl.result_summary
        assert summary is not None
        assert ctrl.state == LearningState.DONE
        assert not ctrl.fault_reason
        # A VERIFY-olvasas erteke nem irodik felul.
        assert summary.update_status_final == 0x06
        # A Qmax/Ra a zaro snapshotbol tovabbra is atkerul.
        assert summary.qmax_final == 150
        # Restore + reseal feltetel nelkul lefutott (nincs korai return).
        assert summary.cell_bl_restore_confirmed is True
        assert ctrl._cell_bl_reseal_confirmed is True
        assert ctrl._load.input_commanded_on is False

    @pytest.mark.parametrize("value", [0x00, 0x02, 0x04, 0x05, 0x07, 0xFF])
    def test_full_sequence_final_snapshot_update_status_not_0x06_is_awaiting(self, value):
        def _hook(_ctrl, fake_bq):
            fake_bq.update_status = value

        ctrl, _events = self._drive_full_sequence(0x05, before_final_snapshot=_hook)

        summary = ctrl.result_summary
        assert summary.result == LEARNING_RESULT_AWAITING
        assert f"UPDATE_STATUS_NOT_0X06_AT_FINAL_SNAPSHOT(got={value})" in summary.quality_flags
        assert not any(f.startswith("UPDATE_STATUS_READ_FAILED") for f in summary.quality_flags)
        self._assert_closed_out_without_fault(ctrl)

    def test_full_sequence_final_snapshot_update_status_read_failure_is_awaiting(self):
        def _hook(ctrl, _fake_bq):
            def _raise():
                raise RuntimeError("simulated UpdateStatus read failure")
            ctrl._bq_driver.read_update_status = _raise

        ctrl, _events = self._drive_full_sequence(0x05, before_final_snapshot=_hook)

        summary = ctrl.result_summary
        assert summary.result == LEARNING_RESULT_AWAITING
        assert (
            "UPDATE_STATUS_READ_FAILED_AT_FINAL_SNAPSHOT:simulated UpdateStatus read failure"
            in summary.quality_flags
        )
        assert not any(
            f.startswith("UPDATE_STATUS_NOT_0X06_AT_FINAL_SNAPSHOT") for f in summary.quality_flags
        )
        self._assert_closed_out_without_fault(ctrl)

    def test_full_sequence_final_snapshot_update_status_0x06_control_verified(self):
        def _hook(_ctrl, fake_bq):
            fake_bq.update_status = 0x06

        ctrl, _events = self._drive_full_sequence(0x05, before_final_snapshot=_hook)

        summary = ctrl.result_summary
        assert summary.result == LEARNING_RESULT_VERIFIED
        assert not any("AT_FINAL_SNAPSHOT" in f for f in summary.quality_flags)
        self._assert_closed_out_without_fault(ctrl)

    def test_final_snapshot_0x06_does_not_clear_earlier_final_check_flag(self):
        ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
        self._set_positive_hard_evidence(ctrl, qmax_before=100, qmax_final=None)
        ctrl._ra_before = [1.0]
        earlier = "UPDATE_STATUS_NOT_0X06_AT_FINAL_CHECK(got=5)"
        ctrl._quality_flags.append(earlier)
        fake_bq.update_status = 0x06
        fake_bq.qmax = 150

        ctrl._run_final_snapshot()

        summary = ctrl.result_summary
        assert summary.result == LEARNING_RESULT_AWAITING
        assert earlier in summary.quality_flags
        assert not any("AT_FINAL_SNAPSHOT" in f for f in summary.quality_flags)

    # 12. R1-B4-NONE-STATUS-PARAM-TEST-GAP --------------------------------
    @pytest.mark.parametrize(
        "is_final, next_state, read_flag, gate_flag",
        [
            (True, LearningState.FINAL_SNAPSHOT,
             "UPDATE_STATUS_READ_FAILED_AT_FINAL:", "UPDATE_STATUS_NOT_0X06_AT_FINAL_CHECK(got=None)"),
            (False, LearningState.RECHARGE,
             "UPDATE_STATUS_READ_FAILED_AT_0X05:", "UPDATE_STATUS_NOT_0X05_AT_CHECKPOINT(got=None)"),
        ],
        ids=["final", "checkpoint"],
    )
    def test_update_status_read_failure_flags_read_and_gate(
        self, is_final, next_state, read_flag, gate_flag,
    ):
        ctrl, *_ = make_controller()
        ctrl._phase_elapsed_s = 999999.0

        def _raise():
            raise RuntimeError("simulated")

        ctrl._bq_driver.read_update_status = _raise

        ctrl._run_verify_update_status(0.0, is_final=is_final, next_state=next_state)

        assert read_flag + "simulated" in ctrl._quality_flags
        assert gate_flag in ctrl._quality_flags
        if is_final:
            assert ctrl._update_status_final is None
        else:
            assert ctrl._update_status_at_0x05_check is None
        assert ctrl.state == next_state


class TestR1B3bDischargeStartBatteryVoltageGate:
    """[regresszió, Claude-Codex loop R1 B3b: NS-02 (DFAM-001)]

    `_begin_discharge(DISCHARGE_1/DISCHARGE_2)` a B3 DMM-őr után `check_battery_voltage()` nélkül adott
    Load INPUT ON-t: egy érvényes, de a meglévő safety-limiten
    (`SafetyManager.check_battery_voltage()`: U_batt <= 0 V vagy
    > `batt_absolute_max_V`) kívüli U_batt csak a KÖVETKEZŐ tick per-tick
    kapujában (`_run_discharge()`) okozott FAULT-ot, az energizálás UTÁN. Elvárt
    sorrend: DMM-őr -> feszültségkapu -> concurrent-kapu -> setup (a
    `_run_rebound_check()` és az OCV IMPULSE_PREP meglévő sorrendje).
    A kapu a meglévő limitet alkalmazza, új/szigorúbb limitet nem vezet be
    (határ-karakterizáció)."""

    @staticmethod
    def _bad_voltage(ctrl, case: str) -> tuple[float, str]:
        return {
            "zero": (0.0, "REVERSE_POLARITY_OR_WIRING_FAULT"),
            "negative": (-0.5, "REVERSE_POLARITY_OR_WIRING_FAULT"),
            "above_abs_max": (ctrl._profile.batt_absolute_max_V + 0.01, "BATTERY_OVERVOLTAGE"),
        }[case]

    @staticmethod
    def _assert_faulted_without_energize(ctrl, psu, load, expected_reason: str) -> None:
        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == expected_reason
        assert "input_on()" not in load.calls
        assert not any(c.startswith("set_current") for c in load.calls)
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls

    @staticmethod
    def _make():
        """D1 élő integrátor: 1 Ah / 24 Wh."""
        ctrl, psu, load, *_ = make_controller()
        ctrl._discharge_integrator.add_sample(-1.0, 24.0, 3600.0)
        return ctrl, psu, load

    @staticmethod
    def _assert_d1_kept_and_no_d2(ctrl) -> None:
        assert ctrl.accumulated_discharge_Ah == pytest.approx(1.0)
        summary = ctrl.result_summary
        assert summary is not None
        assert summary.discharge_2_capacity_Ah is None
        assert summary.result != LEARNING_RESULT_VERIFIED

    @pytest.mark.parametrize(
        "state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2], ids=["d1", "d2"]
    )
    @pytest.mark.parametrize("case", ["zero", "negative", "above_abs_max"])
    def test_bad_voltage_faults_before_energizing(self, case, state):
        ctrl, psu, load = self._make()
        u, reason = self._bad_voltage(ctrl, case)
        ctrl._u_batt = u
        ctrl._dmm_v.voltage_V = u  # D1 az indítás előtt újra mér.

        ctrl._begin_discharge(state)

        self._assert_faulted_without_energize(ctrl, psu, load, reason)
        self._assert_d1_kept_and_no_d2(ctrl)
        load.reset_calls()
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert "input_on()" not in load.calls

    @pytest.mark.parametrize(
        "state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2], ids=["d1", "d2"]
    )
    def test_dmm_guard_precedes_voltage_gate(self, state):
        """Sorrend a (karakterizáció): DMM-hiba + 0 V -> a B3 DMM-őr nyer."""
        ctrl, _psu, load = self._make()
        ctrl._dmm_valid = False
        ctrl._u_batt = 0.0
        ctrl._dmm_v.simulate_timeout = True  # A friss D1-mérés is hibát jelez.

        ctrl._begin_discharge(state)

        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls

    @pytest.mark.parametrize(
        "state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2], ids=["d1", "d2"]
    )
    def test_voltage_gate_precedes_concurrent_gate(self, state):
        """Sorrend b: PSU commanded ON + 0 V -> a feszültségkapu nyer, nem a concurrent-kapu."""
        ctrl, psu, load = self._make()
        psu.output_commanded_on = True
        ctrl._u_batt = 0.0
        ctrl._dmm_v.voltage_V = 0.0

        ctrl._begin_discharge(state)

        assert ctrl.fault_reason == "REVERSE_POLARITY_OR_WIRING_FAULT"
        assert "input_on()" not in load.calls

    @pytest.mark.parametrize(
        "state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2], ids=["d1", "d2"]
    )
    def test_exact_abs_max_still_energizes(self, state):
        """Határ (karakterizáció): pontosan batt_absolute_max_V mellett még
        energizál -- nincs új, szigorúbb limit."""
        ctrl, _psu, load = self._make()
        ctrl._u_batt = ctrl._profile.batt_absolute_max_V
        ctrl._dmm_v.voltage_V = ctrl._profile.batt_absolute_max_V

        ctrl._begin_discharge(state)

        assert "input_on()" in load.calls
        assert ctrl.state != LearningState.FAULT

    def test_chain_relax_pre_discharge_2_timeout_zero_volt_faults_before_d2_energize(self):
        """Lánc: RELAX_PRE_DISCHARGE_2 timeout-ág, friss, érvényes 0 V
        DMM-olvasás -> FAULT a D2-energizálás ELŐTT; Cell BL restore
        megerősítve, SEALED, D1 rögzítve, D2 None."""
        b3 = TestR1B3DischargeStartDmmGateAndD1Handover()
        ctrl, psu, load, dmm_v, _dmm_t, fake_bq = b3._drive_to_relax_pre_discharge_2(b3._cfg())
        d1_Ah = ctrl.accumulated_discharge_Ah
        fake_bq.set_ocvtaken(False)
        ctrl.advance(0.0)
        load.reset_calls()
        psu.reset_calls()
        dmm_v.voltage_V = 0.0

        ctrl.advance(10.0)

        self._assert_faulted_without_energize(
            ctrl, psu, load, "REVERSE_POLARITY_OR_WIRING_FAULT"
        )
        summary = ctrl.result_summary
        assert summary is not None
        assert summary.cell_bl_restore_confirmed is True
        assert fake_bq.control_status_word & 0x2000
        assert summary.discharge_1_capacity_Ah == pytest.approx(d1_Ah)
        assert summary.discharge_2_capacity_Ah is None


class TestR8B1LearningStartPowerGate:
    """R8-B1: inditas/retry teljesitmenykapu, a BL-restore es a konyveles megmarad."""

    @staticmethod
    def _make(capacity_Ah, voltage_V):
        # Q-22: a nagy pack már profilkapun elakadna. A teljesítménykapu
        # változatlan 19,9/20/30 A-s hibainjekciója támogatott profillal indul.
        result = make_controller(dmm_voltage_V=voltage_V)
        ctrl, _psu, _load, *_ = result
        ctrl._discharge_current_A = capacity_Ah / 10
        ctrl._u_batt = voltage_V
        ctrl._run_it_enable(0.0)
        assert ctrl._cell_bl_original_mV == 1950
        assert ctrl._cell_bl_restore_confirmed is None
        return result

    @staticmethod
    def _assert_restored(ctrl, fake_bq):
        assert ctrl._cell_bl_restore_confirmed is True
        assert ctrl._cell_bl_reseal_confirmed is True
        restored = decode_df_field(
            FIELD_REGISTRY["cell_bl_set_volt_mV"], fake_bq.df_block(49, 0),
        )
        assert restored == ctrl._cell_bl_original_mV
        assert fake_bq.calls.count("write_data_flash_block") == 2
        assert ctrl.pending_bq_df_restore is None

    @pytest.mark.parametrize("state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2])
    def test_over_power_faults_before_setup_and_restores(self, state):
        ctrl, psu, load, _dv, _dt, fake_bq = self._make(300.0, 12.5)
        ctrl._discharge_integrator.add_sample(-1.0, 12.5, 3600.0)
        events = []
        ctrl.on_event = events.append
        assert ctrl._discharge_current_A == pytest.approx(30.0)

        ctrl._begin_discharge(state)

        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == "LOAD_POWER_LIMIT_EXCEEDED"
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(1.0)
        assert ctrl.result_summary.discharge_2_capacity_Ah is None
        assert any(
            e.get("event_code") == "LEARNING_FAULT"
            and e.get("event_message") == "LOAD_POWER_LIMIT_EXCEEDED" for e in events
        )
        self._assert_restored(ctrl, fake_bq)
        load.reset_calls()
        psu.reset_calls()
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.FAULT
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)
        self._assert_restored(ctrl, fake_bq)

    @pytest.mark.parametrize("state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2])
    @pytest.mark.parametrize("capacity_Ah", [199.0, 200.0])
    def test_at_or_below_limit_starts(self, state, capacity_Ah):
        ctrl, _psu, load, *_ = self._make(capacity_Ah, 12.5)

        ctrl._begin_discharge(state)

        assert ctrl.state == state
        assert load.input_commanded_on is True
        assert f"set_current({capacity_Ah / 10.0})" in load.calls
        assert ctrl._cell_bl_restore_confirmed is None

    @pytest.mark.parametrize(
        "discharge_state,rebound_state,relax_state",
        [
            (LearningState.DISCHARGE_1, LearningState.REBOUND_CHECK_1, LearningState.RELAX_1),
            (LearningState.DISCHARGE_2, LearningState.REBOUND_CHECK_2, LearningState.RELAX_2),
        ],
    )
    @pytest.mark.parametrize("rebound_V", [12.4, 12.5, 12.6])
    def test_retry_power_gate_preserves_bookkeeping_and_restore(
        self, discharge_state, rebound_state, relax_state, rebound_V,
    ):
        ctrl, psu, load, dmm_v, _dt, fake_bq = self._make(200.0, 12.4)
        ctrl._begin_discharge(discharge_state)
        assert ctrl.state == discharge_state
        integrator = ctrl._discharge_integrator
        integrator.add_sample(-1.0, 12.4, 3600.0)
        initial_cutoff = ctrl._discharge_cutoff_V
        events = []
        ctrl.on_event = events.append
        dmm_v.voltage_V = initial_cutoff - 0.01
        ctrl.advance(0.0)
        assert ctrl.state == rebound_state
        assert load.input_commanded_on is False
        dmm_v.voltage_V = rebound_V
        load.reset_calls()
        psu.reset_calls()

        ctrl.advance(ctrl._config.rebound_settle_s)

        assert ctrl._rebound_retry_count == 1
        step_V = 0.02 * ctrl._profile.cell_count  # Q-34: az első két retry lépése.
        assert ctrl._discharge_cutoff_V == pytest.approx(initial_cutoff - step_V)
        assert ctrl._discharge_integrator is integrator
        assert ctrl.accumulated_discharge_Ah == pytest.approx(1.0)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(12.4)
        if rebound_V > 12.5:
            assert ctrl.state == LearningState.FAULT
            assert ctrl.fault_reason == "LOAD_POWER_LIMIT_EXCEEDED"
            assert load.input_commanded_on is False
            assert psu.output_commanded_on is False
            assert "input_off()" in load.calls
            assert "all_outputs_off()" in psu.calls
            assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
            assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)
            assert any(
                e.get("event_code") == "LEARNING_FAULT"
                and e.get("event_message") == "LOAD_POWER_LIMIT_EXCEEDED" for e in events
            )
            self._assert_restored(ctrl, fake_bq)
            load.reset_calls()
            psu.reset_calls()
            ctrl.advance(1.0)
            assert ctrl.state == LearningState.FAULT
            assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
            assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)
            self._assert_restored(ctrl, fake_bq)
        else:
            assert ctrl.state == discharge_state
            assert load.input_commanded_on is True
            assert load.calls == ["input_on()"]
            assert ctrl._cell_bl_restore_confirmed is None
            load.input_off()
            ctrl._enter(rebound_state)
            load.reset_calls()
            ctrl.advance(ctrl._config.rebound_settle_s)
            assert ctrl.state == discharge_state
            assert ctrl._rebound_retry_count == 2
            assert ctrl._discharge_cutoff_V == pytest.approx(initial_cutoff - 2 * step_V)
            assert ctrl._discharge_integrator is integrator
            assert ctrl.accumulated_discharge_Ah == pytest.approx(1.0)
            assert ctrl.accumulated_discharge_Wh == pytest.approx(12.4)
            assert load.calls == ["input_on()"]
            load.input_off()
            ctrl._enter(rebound_state)
            dmm_v.voltage_V = (
                ctrl._config.learning_rebound_target_V_per_cell * ctrl._profile.cell_count
            )
            ctrl.advance(ctrl._config.rebound_settle_s)
            assert ctrl.state == relax_state
            retries = (
                ctrl._rebound_retries_1 if discharge_state == LearningState.DISCHARGE_1
                else ctrl._rebound_retries_2
            )
            assert retries == 2
            assert ctrl._cell_bl_restore_confirmed is None


class TestR16B4RelaxAcceptance:
    # N10 residual excerpt: samples[0:64], end_monotonic_s, full-record OLS removed.
    # Source: 20261107T073752_153915Z_OCV_NPLC10.json; rounded to 0.001 uV.
    # No measurement-file I/O at test time; voltage origin shifted to the test pack.
    _residual_uV = (
        32.377, -5.957, 5.006, -9.926, 12.040, 11.303, 16.171, 10.337,
        0.900, 16.168, -4.566, -3.303, 7.965, 26.731, 20.394, 25.562,
        31.426, 21.093, 10.659, -8.577, 4.990, 10.956, -5.480, 5.387,
        19.653, 9.417, 32.184, 12.450, 11.914, 11.481, 19.147, 0.311,
        18.976, -6.756, -9.890, -24.127, 7.041, 9.607, -23.030, 6.738,
        -0.796, 0.767, 15.135, 11.701, -7.436, -20.068, -1.402, 2.161,
        -21.073, 40.095, 19.460, 12.324, 5.692, 4.457, 16.721, 11.189,
        7.854, -34.980, 1.584, -43.449, -21.083, -21.919, 14.548, 2.214,
    )
    _dt_s = (
        0.0, 2.0, 1.984, 2.016, 2.0, 1.984, 2.016, 2.0,
        1.984, 2.016, 2.0, 1.984, 2.016, 2.0, 1.984, 2.016,
        1.984, 2.016, 2.0, 1.984, 2.016, 2.0, 1.984, 2.016,
        2.0, 1.984, 2.016, 2.0, 1.984, 2.016, 2.0, 1.984,
        2.0, 2.016, 2.0, 1.984, 2.016, 2.0, 1.984, 2.016,
        2.0, 1.984, 2.016, 2.0, 1.984, 2.016, 2.0, 1.984,
        2.0, 2.016, 2.0, 1.984, 2.016, 2.0, 1.984, 2.016,
        2.0, 2.0, 1.984, 2.016, 2.0, 1.984, 2.016, 2.0,
    )
    _phases = (
        (LearningState.PRECHARGE_RELAX, LearningState.INITIAL_SNAPSHOT),
        (LearningState.RELAX_1, LearningState.VERIFY_UPDATE_STATUS_05),
        (LearningState.RELAX_PRE_DISCHARGE_2, LearningState.DISCHARGE_2),
        (LearningState.RELAX_2, LearningState.VERIFY_UPDATE_STATUS_06),
    )

    @staticmethod
    def _new(phase=LearningState.PRECHARGE_RELAX, min_s=0.0, extra_s=None):
        window = RelaxConfig().sample_period_s
        ctrl, psu, load, dmm, _temp, bq = make_controller(config=LearningConfig(
            relax_after_charge_s=min_s, relax_after_discharge_s=min_s,
            max_relax_extra_wait_s=extra_s if extra_s is not None else 4 * window,
            sample_interval_s=10000.0,
        ))
        ctrl._state = phase
        bq.set_ocvtaken(True)
        bq.set_vok(phase == LearningState.RELAX_PRE_DISCHARGE_2)
        return ctrl, psu, load, dmm, bq

    @pytest.mark.parametrize("phase,next_state", _phases)
    @pytest.mark.parametrize("drift_uV_s", [-20.0, -10.0, -5.0, 5.0, 10.0, 20.0])
    def test_n10_noise_cannot_accept_above_threshold_ramp(self, phase, next_state, drift_uV_s):
        ctrl, _psu, _load, dmm, _bq = self._new(phase)
        elapsed = 0.0
        previous = None
        favorable_pair = False
        assert len(self._dt_s) == len(self._residual_uV)
        for dt, residual in zip(self._dt_s, self._residual_uV):
            elapsed += dt
            voltage = 12.5 + (residual + drift_uV_s * elapsed) * 1e-6
            if previous is not None:
                favorable_pair |= abs((voltage - previous) / dt) < 4e-6
            dmm.voltage_V = voltage
            ctrl.advance(dt)
            assert ctrl.state == phase, next_state
            previous = voltage
        assert favorable_pair, "fixture must discriminate the old two-point gate"
        assert ctrl._relax_ctrl.dvdt_valid
        assert not ctrl.quality_flags

    @pytest.mark.parametrize("phase,next_state", _phases)
    def test_stable_n10_residual_accepted_after_full_window(self, phase, next_state):
        ctrl, _psu, _load, dmm, _bq = self._new(phase)
        elapsed = 0.0
        for dt, residual in zip(self._dt_s, self._residual_uV):
            elapsed += dt
            dmm.voltage_V = 12.5 + residual * 1e-6
            ctrl.advance(dt)
            if ctrl.state != phase:
                break
        assert elapsed >= RelaxConfig().sample_period_s
        assert ctrl.state == next_state
        assert not ctrl.quality_flags

    def test_minimum_time_and_bq_gate_still_required(self):
        window = RelaxConfig().sample_period_s
        ctrl, _psu, _load, _dmm, bq = self._new(min_s=2 * window)
        ctrl.advance(0.0)
        dt = _relax_tick_s()
        for _ in range(int(window / dt)):
            ctrl.advance(dt)
        assert ctrl._relax_ctrl.dvdt_valid
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        bq.set_ocvtaken(False)
        for _ in range(int(window / dt) + 1):
            ctrl.advance(dt)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        bq.set_ocvtaken(True)
        bq.set_vok(True)
        ctrl.advance(dt)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        bq.set_vok(False)
        ctrl.advance(dt)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT

    def test_strict_threshold_and_custom_override(self, monkeypatch):
        ctrl, _psu, _load, _dmm, _bq = self._new()
        ctrl.advance(0.0)
        # Isolate comparison semantics from floating point OLS rounding.
        rc = ctrl._relax_ctrl
        monkeypatch.setattr(rc, "_update_dvdt", lambda v, _sample_time: None)
        ctrl._config.dvdt_threshold_V_per_s = 7e-6
        rc._dvdt = 7e-6
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        rc._dvdt = -7e-6
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        rc._dvdt = 6.9e-6
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT

    @pytest.mark.parametrize("phase,next_state", _phases)
    @pytest.mark.parametrize("failure", ["dmm", 4.01, 5.0])
    def test_invalid_window_cannot_pass_and_must_refill(self, phase, next_state, failure):
        from math import isnan

        ctrl, _psu, _load, dmm, bq = self._new(phase)
        bq.set_ocvtaken(False)
        ctrl.advance(0.0)
        rc = ctrl._relax_ctrl
        for _ in range(60):
            ctrl.advance(1.0)
        assert rc.dvdt_valid
        assert len(rc._dvdt_samples) == 61
        bq.set_ocvtaken(True)
        if failure == "dmm":
            dmm.simulate_timeout = True
            ctrl.advance(1.0)
            assert ctrl.state == phase
            assert not ctrl.dmm_valid
            assert not rc.dvdt_valid
            assert isnan(rc.dvdt_V_per_s)
            assert len(rc._dvdt_samples) == 0
            dmm.simulate_timeout = False
            ctrl.advance(1.0)
        else:
            ctrl.advance(failure)
        assert ctrl.state == phase
        assert ctrl.dmm_valid
        assert not rc.dvdt_valid
        assert isnan(rc.dvdt_V_per_s)
        assert len(rc._dvdt_samples) == 1
        for _ in range(59):
            ctrl.advance(1.0)
            assert ctrl.state == phase
            assert not rc.dvdt_valid
            assert isnan(rc.dvdt_V_per_s)
        # 0,01 s ráhagyás a 4,01 s-os rés lebegőpontos időhatárához.
        ctrl.advance(1.01)
        assert rc.dvdt_valid
        assert ctrl.state == next_state
        assert not any("RELAX_ACCEPTANCE_NOT_CONFIRMED" in f for f in ctrl.quality_flags)

    def test_next_relax_phase_starts_with_empty_window(self):
        ctrl, _psu, _load, _dmm, _bq = self._new()
        ctrl.advance(0.0)
        old = ctrl._relax_ctrl
        _complete_relax_window(ctrl)
        assert ctrl._relax_ctrl is None
        ctrl._enter(LearningState.RELAX_1)
        ctrl.advance(0.0)
        assert ctrl._relax_ctrl is not old
        ctrl.advance(_relax_tick_s())
        assert ctrl.state == LearningState.RELAX_1
        assert not ctrl._relax_ctrl.dvdt_valid

    def test_extra_wait_timeout_event_and_carry_over_unchanged(self):
        ctrl, _psu, _load, dmm, _bq = self._new(min_s=1.0, extra_s=3.0)
        events = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl._relax_extra_wait_s == 1.0
        dmm.simulate_timeout = True
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.PRECHARGE_RELAX
        assert ctrl.relax_dmm_fault_total == 1
        dmm.simulate_timeout = False
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        assert ctrl._relax_ctrl is None
        assert ctrl.relax_dmm_fault_total == 1
        flags = ctrl.quality_flags
        assert sum("RELAX_ACCEPTANCE_NOT_CONFIRMED_AT_PRECHARGE_RELAX" in f for f in flags) == 1
        assert any("dvdt_ok=False" in f for f in flags)
        assert sum("RELAX_DMM_FAULTS_AT_PRECHARGE_RELAX(count=1)" in f for f in flags) == 1
        assert sum(e["event_code"] == "LEARNING_RELAX_ACCEPTANCE_TIMEOUT" for e in events) == 1
        ctrl.advance(0.0)
        assert ctrl.relax_dmm_fault_total == 1


class TestR16B4F1RelaxCoverage:
    @pytest.mark.parametrize("phase,next_state", TestR16B4RelaxAcceptance._phases)
    def test_sample_count_without_full_time_coverage_cannot_accept(self, phase, next_state):
        from math import isnan

        ctrl, _psu, _load, _dmm, _bq = TestR16B4RelaxAcceptance._new(phase)
        ctrl.advance(0.0)
        rc = ctrl._relax_ctrl
        for _ in range(30):
            ctrl.advance(1.0)
            assert ctrl.state == phase
        assert len(rc._dvdt_samples) == 31
        assert rc.elapsed_s == 30.0
        assert ctrl.dmm_valid
        assert not rc.dvdt_valid
        assert isnan(rc.dvdt_V_per_s)
        for _ in range(29):
            ctrl.advance(1.0)
            assert ctrl.state == phase
            assert not rc.dvdt_valid
            assert isnan(rc.dvdt_V_per_s)
        ctrl.advance(1.0)
        assert rc.dvdt_valid
        assert ctrl.state == next_state
        assert not ctrl.quality_flags


class TestR16B16cLearningFloor:
    """Q-34/Q-35: pakkszintű TLC-lépcsők és egyszeri lezáró minta."""

    @staticmethod
    def _make(cells=6, retries=3):
        from dataclasses import replace

        result = make_controller(
            dmm_voltage_V=2.10 * cells,
            config=LearningConfig(max_rebound_retries=retries, rebound_settle_s=0.0),
        )
        ctrl, _psu, _load, _dv, _dt, fake = result
        ctrl._profile = replace(ctrl._profile, cell_count=cells, nominal_voltage_V=2.0 * cells)
        ctrl._safety.profile = ctrl._profile
        fake.df_fields = _default_df_fields(ctrl._profile)
        ctrl._u_batt = 2.10 * cells
        ctrl._dmm_valid = True
        ctrl._run_it_enable(0.0)  # Valódi tranzakciós kód, kizárólag FakePicoSerial.
        return result

    @staticmethod
    def _assert_fault(result, events, reason="BATTERY_UNDERVOLTAGE"):
        ctrl, psu, load, _dv, _dt, fake = result
        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == reason
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert "input_off()" in load.calls and "all_outputs_off()" in psu.calls
        assert any(e["event_code"] == "LEARNING_FAULT" for e in events)
        TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
        load.reset_calls()
        psu.reset_calls()
        ctrl.advance(1.0)
        assert not any("input_on" in c for c in load.calls)
        assert not any("output_on" in c for c in psu.calls)

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("retries", [3, 4])
    @pytest.mark.parametrize("state,rebound,relax", [
        (LearningState.DISCHARGE_1, LearningState.REBOUND_CHECK_1, LearningState.RELAX_1),
        (LearningState.DISCHARGE_2, LearningState.REBOUND_CHECK_2, LearningState.RELAX_2),
    ])
    def test_exact_steps_and_retry_budget(self, cells, retries, state, rebound, relax):
        ctrl, _psu, load, dv, *_ = self._make(cells, retries)
        ctrl._begin_discharge(state)
        expected = [1.75, 1.73, 1.71, 1.70, 1.70][:retries + 1]
        for index, per_cell in enumerate(expected):
            assert ctrl.state == state and load.input_commanded_on
            assert ctrl._discharge_cutoff_V == pytest.approx(per_cell * cells)
            assert ctrl._rebound_retry_count == index
            dv.voltage_V = ctrl._discharge_cutoff_V
            ctrl.advance(0.0)
            assert ctrl.state == rebound and not load.input_commanded_on
            dv.voltage_V = 1.95 * cells
            ctrl.advance(0.0)
        assert ctrl.state == relax and not load.input_commanded_on
        assert sum(c == "input_on()" for c in load.calls) == retries + 1
        assert LearningConfig().max_rebound_retries == 3
        assert LearningConfig().learning_rebound_target_V_per_cell == 1.80
        assert LearningConfig().cell_bl_learning_temp_V_per_cell == 1.65

    @pytest.mark.parametrize("value", [1.699999, 1.6667, 1.4267, float("nan"), float("inf"), -float("inf")])
    def test_invalid_cutoff_rejected_without_clamp(self, value):
        with pytest.raises(ValueError):
            LearningConfig(learning_discharge_floor_V_per_cell=value)

    @pytest.mark.parametrize("value", [-1, 5, 3.5, True])
    def test_invalid_retry_count(self, value):
        with pytest.raises(ValueError):
            LearningConfig(max_rebound_retries=value)

    @pytest.mark.parametrize("value", [1.69, 1.4267, float("nan"), float("inf")])
    def test_invalid_retry_candidate_rejected_in_config(self, monkeypatch, value):
        from Prog.src import traditional_learning_controller as tl

        monkeypatch.setattr(tl, "LEARNING_DISCHARGE_CUTOFFS_V_PER_CELL", (1.75, value, 1.71, 1.70, 1.70))
        with pytest.raises(ValueError):
            LearningConfig()

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("state,rebound", [
        (LearningState.DISCHARGE_1, LearningState.REBOUND_CHECK_1),
        (LearningState.DISCHARGE_2, LearningState.REBOUND_CHECK_2),
    ])
    def test_first_overshoot_then_second_loaded_sample_faults(self, cells, state, rebound, caplog):
        result = self._make(cells)
        ctrl, _psu, load, dv, *_ = result
        events = []
        ctrl.on_event = events.append
        ctrl._begin_discharge(state)
        dv.voltage_V = 1.69 * cells
        ctrl.advance(0.0)
        assert ctrl.state == rebound and not load.input_commanded_on
        event, = [e for e in events if e["event_code"] == "LEARNING_DISCHARGE_CUTOFF_OVERSHOOT"]
        assert event["measured_V"] == pytest.approx(1.69 * cells)
        assert event["minimum_V"] == pytest.approx(1.70 * cells)
        assert event["cutoff_V"] == pytest.approx(1.75 * cells)
        assert event["overshoot_V"] == pytest.approx(0.06 * cells)
        assert event["below_hard_floor"] is True
        assert "Q-34/Q-35" in caplog.text
        assert not any(e["event_code"] == "LEARNING_FAULT" for e in events)
        load.input_commanded_on = True  # Beragadt/parancsolt terhelés; nincs új ON parancs.
        ctrl.advance(0.0)
        self._assert_fault(result, events)

    @pytest.mark.parametrize("state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2])
    def test_start_below_floor_faults_and_restores(self, state):
        result = self._make()
        ctrl, _psu, load, dv, *_ = result
        events = []
        ctrl.on_event = events.append
        ctrl._u_batt = dv.voltage_V = 6 * 1.69
        ctrl._begin_discharge(state)
        assert "input_on()" not in load.calls
        self._assert_fault(result, events)

    @pytest.mark.parametrize("retry", [False, True])
    @pytest.mark.parametrize("state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2])
    def test_each_load_on_site_has_setup_gate(self, monkeypatch, retry, state):
        ctrl, _psu, load, _dv, *_ = self._make()
        calls = []
        def deny(cutoff_V, **kwargs):
            calls.append(cutoff_V)
            ctrl.emergency_stop("BATTERY_UNDERVOLTAGE")
            return False
        monkeypatch.setattr(ctrl, "_check_learning_discharge_floor", deny)
        if retry:
            ctrl._discharge_cutoff_V = 10.5
            ctrl._run_rebound_check(0.0, state, LearningState.RELAX_1)
        else:
            ctrl._begin_discharge(state)
        assert calls and ctrl.state == LearningState.FAULT
        assert "input_on()" not in load.calls

    def test_mutated_config_faults_before_on(self):
        result = self._make()
        ctrl, _psu, load, *_ = result
        ctrl._config.learning_discharge_floor_V_per_cell = 1.4267
        events = []
        ctrl.on_event = events.append
        ctrl._begin_discharge(LearningState.DISCHARGE_1)
        assert "input_on()" not in load.calls
        self._assert_fault(result, events, "DISCHARGE_CONFIG_INVALID")

    @pytest.mark.parametrize("failure", ["none", "restore", "seal_verify"])
    def test_throwing_callback_runs_after_all_cleanup_attempts(self, failure):
        result = self._make()
        ctrl, psu, load, _dv, _dt, fake = result
        if failure == "restore":
            ctrl._bq_transport.serial.errors["write_data_flash_block"] = ("METHOD_ERROR", "restore")
        elif failure == "seal_verify":
            ctrl._bq_transport.serial.errors["control_status"] = ("METHOD_ERROR", "verify")
        seen = []
        def raising(event):
            seen.append(event)
            assert ctrl.state == LearningState.FAULT
            assert not load.input_commanded_on and not psu.output_commanded_on
            assert ctrl._cell_bl_restore_done
            assert "seal" in fake.calls
            assert ctrl.result_summary is not None
            raise KeyboardInterrupt("callback")
        ctrl.on_event = raising
        with pytest.raises(KeyboardInterrupt, match="callback"):
            ctrl.emergency_stop("TEST")
        assert seen
        assert ctrl._cell_bl_restore_confirmed is (failure == "none")
        assert ctrl._cell_bl_reseal_confirmed is (failure != "seal_verify")
        if failure == "none":
            TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)

    @pytest.mark.parametrize("state", [LearningState.RELAX_1, LearningState.RELAX_2])
    def test_relax_raw_sample_gate_faults_before_following_good_sample(self, state):
        from Prog.src.relax_controller import RelaxSample

        result = self._make()
        ctrl, _psu, load, *_ = result
        events = []
        ctrl.on_event = events.append
        ctrl._enter(state)
        load.input_commanded_on = True
        ctrl._check_loaded_relax_sample(RelaxSample(1, 1, 0.0, 0.0, 0.0, 10.0))
        self._assert_fault(result, events)

    @pytest.mark.parametrize("state", [LearningState.RELAX_1, LearningState.RELAX_2])
    def test_legacy_relax_sample_has_floor_gate(self, state):
        result = self._make()
        ctrl, _psu, load, dv, *_ = result
        events = []
        ctrl.on_event = events.append
        ctrl._enter(state)
        load.input_commanded_on = True
        dv.voltage_V = 10.0
        ctrl.advance(0.0)
        self._assert_fault(result, events)

    def test_normal_gate_is_not_relaxed_by_tlc(self):
        ctrl, *_ = self._make()
        ctrl._begin_discharge(LearningState.DISCHARGE_1)
        normal = ctrl._safety.check_normal_discharge(10.3, 0.7, 10.8)
        assert normal.fault is not None
        assert normal.fault.name == "BATTERY_UNDERVOLTAGE"


class TestR16B16cTerminalErrorCallbacks:
    def test_precond_mismatch_callback_after_seal(self):
        ctrl, psu, load, _dv, _dt, fake = make_controller()
        fake.df_fields["design_capacity_mAh"] += 100
        ctrl._u_batt = 13.0
        ctrl._dmm_valid = True
        def raising(_report):
            assert ctrl.state == LearningState.FAULT
            assert not psu.output_commanded_on and not load.input_commanded_on
            assert fake.control_status_word & 0x2000
            assert ctrl._cell_bl_reseal_confirmed
            raise KeyboardInterrupt("mismatch")
        ctrl.on_provisioning_consistency = raising
        with pytest.raises(KeyboardInterrupt, match="mismatch"):
            ctrl._run_precond()

    def test_it_enable_seal_failure_callback_after_restore(self):
        ctrl, psu, load, _dv, _dt, fake = make_controller()
        ctrl._bq_transport.serial.errors["it_enable"] = ("METHOD_ERROR", "IT")
        original = ctrl._bq_transport.seal
        attempts = []
        def fail_once(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("seal")
            return original(*args, **kwargs)
        ctrl._bq_transport.seal = fail_once
        def raising(event):
            if event["event_code"] != "LEARNING_SEAL_RESTORE_FAILED":
                return
            assert ctrl.state == LearningState.FAULT
            assert not psu.output_commanded_on and not load.input_commanded_on
            TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
            assert len(attempts) >= 2
            raise KeyboardInterrupt("seal callback")
        ctrl.on_event = raising
        with pytest.raises(KeyboardInterrupt, match="seal callback"):
            ctrl._run_it_enable(0.0)


class TestR16B16cRawRelaxDrain:
    @pytest.mark.parametrize("phase", [LearningState.RELAX_1, LearningState.RELAX_2])
    @pytest.mark.parametrize("drain", ["normal", "final_pending", "final_released"])
    def test_bad_record_before_good_record_faults_without_transition(self, phase, drain):
        from Prog.src.relax_controller import (
            RelaxSample,
            RelaxSampleBatch,
            RelaxSamplingService,
        )

        class Source:
            epoch = 1603
            shutdown_error = None
            released = False
            def __init__(self):
                self.records = []
                self.tail = []
                self._clock = lambda: 103.1
            def drain(self):
                batch = RelaxSampleBatch(tuple(self.records), False, self.released)
                self.records.clear()
                return batch
            def stop(self):
                self.records.extend(self.tail)
                self.tail.clear()
            def snapshot_for(self, record):
                return RelaxSamplingService.snapshot_for(self, record)

        result = TestR16B16cLearningFloor._make()
        ctrl, _psu, load, *_ = result
        ctrl._enter(phase)
        source = Source()
        source.records = [RelaxSample(source.epoch, 1, 99.9, 100.1, 100.0, 12.6)]
        ctrl.prepare_relax_sampling = lambda child: child.attach_sampling_service(source)
        ctrl.advance(0.0)
        events = []
        ctrl.on_event = events.append
        load.input_commanded_on = True
        records = [
            RelaxSample(source.epoch, 2, 101.4, 101.6, 101.5, 10.0),
            RelaxSample(source.epoch, 3, 102.9, 103.1, 103.0, 12.6),
        ]
        if drain == "normal":
            source.records = records
        else:
            ctrl._relax_exit_pending = True
            source.tail = records
            source.released = drain == "final_released"
        ctrl.advance(0.0)
        assert ctrl.u_batt_V == pytest.approx(10.0)
        TestR16B16cLearningFloor._assert_fault(result, events)


class TestR16B16cF1CutoffEvidence:
    """Q-7/Q-32/Q-35: minden lépcső első lezáró mintája visszakövethető."""

    @staticmethod
    def _at_step(cells, state, step):
        result = TestR16B16cLearningFloor._make(cells)
        ctrl, _psu, _load, dv, *_ = result
        ctrl._begin_discharge(state)
        rebound = (
            LearningState.REBOUND_CHECK_1
            if state == LearningState.DISCHARGE_1 else LearningState.REBOUND_CHECK_2
        )
        for _ in range(step):
            dv.voltage_V = ctrl._discharge_cutoff_V
            ctrl.advance(0.0)
            assert ctrl.state == rebound
            dv.voltage_V = 1.95 * cells
            ctrl.advance(0.0)
            assert ctrl.state == state
        assert ctrl._discharge_cutoff_V == pytest.approx([1.75, 1.73, 1.71, 1.70][step] * cells)
        return result, rebound

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2])
    @pytest.mark.parametrize("step", [0, 1, 2, 3])
    @pytest.mark.parametrize("below_floor", [False, True])
    def test_first_closing_sample_logged_once(self, cells, state, step, below_floor, caplog):
        result, rebound = self._at_step(cells, state, step)
        ctrl, psu, load, dv, *_ = result
        events = []
        ctrl.on_event = events.append
        caplog.clear()
        cutoff_V = [1.75, 1.73, 1.71, 1.70][step] * cells
        minimum_V = 1.70 * cells
        # Az 1,70-es lépcsőn nincs cutoff alatti, de kemény határ feletti érték:
        # ott a határon záró (nulla túllövésű) minta a nem-alálövő kontroll.
        measured_V = 1.69 * cells if below_floor else (cutoff_V + minimum_V) / 2
        dv.voltage_V = measured_V
        ctrl.advance(0.0)
        assert ctrl.state == rebound
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert ctrl._learning_cutoff_seen
        assert not any(e["event_code"] == "LEARNING_FAULT" for e in events)
        event, = [e for e in events if e["event_code"] == "LEARNING_DISCHARGE_CUTOFF_OVERSHOOT"]
        assert event["severity"] == "WARNING"
        assert event["measured_V"] == pytest.approx(measured_V)
        assert event["cutoff_V"] == pytest.approx(cutoff_V)
        assert event["minimum_V"] == pytest.approx(minimum_V)
        assert event["overshoot_V"] == pytest.approx(cutoff_V - measured_V)
        assert event["below_hard_floor"] is below_floor
        record, = [r for r in caplog.records if "Q-34/Q-35" in r.getMessage()]
        assert record.getMessage() == event["event_message"]
        for fragment in (
            f"U_batt={measured_V:.6f} V", f"cutoff={cutoff_V:.6f} V",
            f"overshoot={cutoff_V - measured_V:.6f} V", f"below_hard_floor={below_floor}",
        ):
            assert fragment in record.getMessage()

        # Új ON nélkül fennmaradó terhelés: nincs második lezáró napló.
        caplog.clear()
        ctrl._config.rebound_settle_s = 100.0
        load.input_commanded_on = True
        ctrl.advance(0.0)
        assert len([e for e in events if e["event_code"] == "LEARNING_DISCHARGE_CUTOFF_OVERSHOOT"]) == 1
        assert not any("Q-34/Q-35" in r.getMessage() for r in caplog.records)
        if below_floor:
            TestR16B16cLearningFloor._assert_fault(result, events)
        else:
            assert ctrl.state == rebound
            assert not any(e["event_code"] == "LEARNING_FAULT" for e in events)

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("state", [LearningState.DISCHARGE_1, LearningState.DISCHARGE_2])
    @pytest.mark.parametrize("step", [0, 1, 2, 3])
    @pytest.mark.parametrize("below_floor", [False, True])
    @pytest.mark.parametrize("failure", ["logger", "event"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
    def test_evidence_failure_restores_before_fault_callback(
        self, monkeypatch, cells, state, step, below_floor, failure, error_type,
    ):
        from Prog.src import traditional_learning_controller as tl

        result, _rebound = self._at_step(cells, state, step)
        ctrl, psu, load, dv, _dt, fake = result
        cutoff_V = [1.75, 1.73, 1.71, 1.70][step] * cells
        dv.voltage_V = 1.69 * cells if below_floor else (cutoff_V + 1.70 * cells) / 2
        trace = []
        original_load_off = load.input_off
        original_psu_off = psu.all_outputs_off

        def load_off():
            trace.append("load_off")
            original_load_off()

        def psu_off():
            trace.append("psu_off")
            original_psu_off()

        def raising(*args, **kwargs):
            trace.append("evidence_failed")
            raise error_type("F1 evidence")

        def on_event(event):
            if event["event_code"] == "LEARNING_DISCHARGE_CUTOFF_OVERSHOOT":
                assert failure == "event"
                raising()
            if event["event_code"] == "LEARNING_FAULT":
                trace.append("fault_callback")
                assert ctrl.state == LearningState.FAULT
                assert not load.input_commanded_on and not psu.output_commanded_on
                TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
                assert ctrl.result_summary is not None

        monkeypatch.setattr(load, "input_off", load_off)
        monkeypatch.setattr(psu, "all_outputs_off", psu_off)
        ctrl.on_event = on_event
        if failure == "logger":
            monkeypatch.setattr(tl.logging.getLogger(tl.__name__), "warning", raising)
        with pytest.raises(error_type, match="F1 evidence"):
            ctrl.advance(0.0)
        assert trace == ["evidence_failed", "load_off", "psu_off", "fault_callback"]
        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == "DISCHARGE_OVERSHOOT_EVENT_FAILED"
        TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
        load.reset_calls()
        psu.reset_calls()
        ctrl.advance(1.0)
        assert not any("input_on" in call for call in load.calls)
        assert not any("output_on" in call for call in psu.calls)


class TestR16B16cF2NormalSampleGates:
    """A TLC 1,70-es kivétele nem szivároghat töltésbe vagy relaxba."""

    @staticmethod
    def _make(cells, state):
        result = TestR16B16cLearningFloor._make(cells)
        ctrl, psu, load, *_ = result
        ctrl._enter(state)
        ctrl._discharge_cutoff_V = 1.75 * cells
        events = []
        ctrl.on_event = events.append
        load.input_commanded_on = True
        psu.output_commanded_on = False
        load.reset_calls()
        psu.reset_calls()
        return result, events

    @staticmethod
    def _assert_result(result, events, state, fault):
        ctrl, psu, load, *_ = result
        # A mintakapu és az utána következő tick sem adhat új ON parancsot.
        assert not any("input_on" in call for call in load.calls)
        assert not any("output_on" in call for call in psu.calls)
        if fault:
            TestR16B16cLearningFloor._assert_fault(result, events)
        else:
            assert ctrl.state == state
            assert not ctrl.fault_reason
            assert load.input_commanded_on
            assert not any(event["event_code"] == "LEARNING_FAULT" for event in events)

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("state", [LearningState.PRECHARGE, LearningState.RECHARGE])
    @pytest.mark.parametrize("per_cell,fault", [(1.75, True), (1.85, False)])
    def test_charge_child_sample_uses_normal_floor(self, cells, state, per_cell, fault):
        """A fake töltő friss mintájára a szülő normál alsó kapuja érvényes."""
        from types import SimpleNamespace

        result, events = self._make(cells, state)
        ctrl, *_ = result
        ctrl._charge_ctrl = SimpleNamespace(
            handoff_stop_requested=None,
            advance=lambda dt_s: ChargeState.CHARGE_CC,
            last_integration_source="ZERO",
            dmm_valid=True,
            temp_dmm_valid=True,
            u_batt_V=per_cell * cells,
            battery_temperature_C=22.0,
            fault_reason="",
        )
        ctrl.advance(0.0)
        assert ctrl.u_batt_V == pytest.approx(per_cell * cells)
        self._assert_result(result, events, state, fault)

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("per_cell,fault", [(1.75, True), (1.85, False)])
    def test_advance_other_state_uses_normal_floor(self, cells, per_cell, fault):
        """A nem kisütési advance-ág sem örökli a tanulási kivételt."""
        state = LearningState.VERIFY_VOK_QEN
        result, events = self._make(cells, state)
        ctrl, *_ = result
        # Nulla dt: a verify várakozik, így más kapu nem fedheti el a hibát.
        ctrl._u_batt = per_cell * cells
        ctrl.advance(0.0)
        self._assert_result(result, events, state, fault)

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("state", [LearningState.RELAX_1, LearningState.RELAX_2])
    @pytest.mark.parametrize("mode", ["SERVICE", "LEGACY_TICK"])
    @pytest.mark.parametrize("per_cell,fault", [(1.75, True), (1.85, False)])
    def test_relax_sample_uses_normal_floor(self, cells, state, mode, per_cell, fault):
        """SERVICE-ben minden nyers rekord, LEGACY-ben a friss tick kapuzott."""
        from Prog.src.relax_controller import (
            RelaxSample,
            RelaxSampleBatch,
            RelaxSamplingService,
        )

        class Source:
            epoch = 1603
            shutdown_error = None
            released = False

            def __init__(self):
                self.records = []
                self._clock = lambda: 103.1

            def drain(self):
                batch = RelaxSampleBatch(tuple(self.records), False, self.released)
                self.records.clear()
                return batch

            def stop(self):
                self.released = True

            def snapshot_for(self, record):
                return RelaxSamplingService.snapshot_for(self, record)

        result, events = self._make(cells, state)
        ctrl, _psu, _load, dv, *_ = result
        if mode == "SERVICE":
            source = Source()
            source.records = [
                RelaxSample(source.epoch, 1, 99.9, 100.1, 100.0, per_cell * cells),
                # A következő jó rekord nem fedheti el az előző alulfeszültségét.
                RelaxSample(source.epoch, 2, 102.9, 103.1, 103.0, 1.85 * cells),
            ]
            ctrl.prepare_relax_sampling = lambda child: child.attach_sampling_service(source)
        else:
            dv.voltage_V = per_cell * cells
        ctrl.advance(0.0)
        assert ctrl._relax_ctrl.sampling_mode == mode
        expected_per_cell = per_cell if fault or mode == "LEGACY_TICK" else 1.85
        assert ctrl.u_batt_V == pytest.approx(expected_per_cell * cells)
        self._assert_result(result, events, state, fault)


class TestR16B17LearningDiagnostics:
    @pytest.mark.parametrize("entry", ["begin", "finish"])
    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
    def test_not_fresh_callback_follows_cleanup(self, entry, error_type):
        from types import SimpleNamespace

        ctrl, psu, load, dv, _dt, fake = TestR16B16cLearningFloor._make()
        phase = LearningState.VERIFY_VOK_QEN if entry == "begin" else LearningState.RELAX_PRE_DISCHARGE_2
        ctrl._enter(phase)
        dv.dmm_valid = False
        if entry == "finish":
            # A leállított sampler frissességi eredménye; minden BQ-művelet fake.
            ctrl._relax_ctrl = SimpleNamespace(
                poll_sampling_stop=lambda: True, dmm_valid=True,
                last_voltage_V=13.0, sampling_mode="SERVICE",
                read_handoff_voltage=lambda *, emit=None: SimpleNamespace(
                    quality="READ_ERROR", age_s=0.25, read_start=123.0,
                ),
                dmm_fault_count=2, missed_deadline_count=3,
                extreme_dt_s_count=1, max_observed_dt_s=7.0,
            )
            ctrl._relax_exit_pending = True
        seen, trace = [], []
        load_off, psu_off = load.input_off, psu.all_outputs_off

        def off_load():
            trace.append("load")
            load_off()

        def off_psu():
            trace.append("psu")
            psu_off()

        def on_event(event):
            if event["event_code"] != "LEARNING_RELAX_DMM_NOT_FRESH":
                return
            trace.append("event")
            seen.append(event)
            assert ctrl.state == LearningState.FAULT
            assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
            assert not load.input_commanded_on and not psu.output_commanded_on
            TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
            assert ctrl.result_summary is not None
            raise error_type("B17 frissesség")

        load.input_off, psu.all_outputs_off = off_load, off_psu
        ctrl.on_event = on_event
        with pytest.raises(error_type, match="B17 frissesség"):
            if entry == "begin":
                ctrl._begin_discharge(LearningState.DISCHARGE_1)
            else:
                ctrl._finish_relax_exit(LearningState.DISCHARGE_2)
        assert trace == ["load", "psu", "event"]
        event, = seen
        assert event["event_message"].startswith(phase.value + ": READ_ERROR;")
        assert event["dmm_reason"] == "READ_ERROR"
        assert "sample_age_s=" in event["event_message"]
        assert "read_start=" in event["event_message"]
        if entry == "finish":
            assert ctrl._relax_ctrl is None and not ctrl._relax_exit_pending
            assert ctrl._relax_dmm_fault_total == 2
            assert ctrl._relax_missed_deadline_total == 3
            assert ctrl._relax_total_extreme_dt_s_count == 1
            assert ctrl._relax_total_max_observed_dt_s == 7.0
        before = list(load.calls)
        ctrl.advance(1.0)
        assert load.calls == before and "input_on()" not in load.calls

    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("entry", ["begin", "retry", "second"])
    @pytest.mark.parametrize("throwing", [False, True])
    def test_floor_fault_contains_sample_after_cleanup(self, cells, entry, throwing):
        ctrl, psu, load, dv, _dt, fake = TestR16B16cLearningFloor._make(cells)
        if entry == "second":
            ctrl._begin_discharge(LearningState.DISCHARGE_1)
            dv.voltage_V = ctrl._discharge_cutoff_V
            ctrl.advance(0.0)
            assert ctrl.state == LearningState.REBOUND_CHECK_1
            load.input_commanded_on = True
        elif entry == "retry":
            ctrl._enter(LearningState.REBOUND_CHECK_1)
            ctrl._discharge_cutoff_V = 1.75 * cells
            # A retry-ágig jutó alacsony rebound, a célérték szándékos tesztinjekciójával.
            ctrl._config.learning_rebound_target_V_per_cell = 1.60
        cutoff_V = 1.73 * cells if entry == "retry" else 1.75 * cells
        measured_V = 1.69 * cells
        dv.voltage_V = ctrl._u_batt = measured_V
        events = []

        def on_event(event):
            if event["event_code"] != "BATTERY_UNDERVOLTAGE":
                return
            assert ctrl.state == LearningState.FAULT
            assert not load.input_commanded_on and not psu.output_commanded_on
            TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
            events.append(event)
            if throwing:
                raise RuntimeError("B17 floor callback")

        ctrl.on_event = on_event

        def run():
            if entry == "begin":
                ctrl._begin_discharge(LearningState.DISCHARGE_1)
            elif entry == "retry":
                ctrl._run_rebound_check(0.0, LearningState.DISCHARGE_1, LearningState.RELAX_1)
            else:
                ctrl.advance(0.0)

        if throwing:
            with pytest.raises(RuntimeError, match="B17 floor callback"):
                run()
        else:
            run()
        event, = events
        assert event["severity"] == "CRITICAL"
        assert event["measured_V"] == pytest.approx(measured_V)
        assert event["minimum_V"] == pytest.approx(1.70 * cells)
        assert event["cutoff_V"] == pytest.approx(cutoff_V)
        for fragment in (
            f"U_batt={measured_V:.6f} V", f"minimum={1.70 * cells:.6f} V",
            f"cutoff={cutoff_V:.6f} V",
        ):
            assert fragment in event["event_message"]
        assert ctrl.fault_reason == "BATTERY_UNDERVOLTAGE"
        before = (list(load.calls), list(psu.calls))
        ctrl.advance(1.0)
        assert (load.calls, psu.calls) == before


class TestR16B17F1ThermalHandoffPriority:
    @pytest.mark.parametrize(
        ("temperature_C", "expected_fault"),
        [
            (22.0, "DMM_FEEDBACK_LOST"),
            (59.0, "DMM_FEEDBACK_LOST"),
            (60.0, "BATTERY_OVERTEMPERATURE"),
            (61.0, "BATTERY_OVERTEMPERATURE"),
        ],
    )
    @pytest.mark.parametrize("quality", ["READ_ERROR", "STALE"])
    def test_handoff_preserves_fault_priority(
        self, monkeypatch, temperature_C, expected_fault, quality,
    ):
        from types import SimpleNamespace

        ctrl, psu, load, _dv, dt, fake = TestR16B16cLearningFloor._make()
        ctrl._enter(LearningState.RELAX_PRE_DISCHARGE_2)
        ctrl._battery_temperature_C = dt.temperature_C = temperature_C
        ctrl._temp_dmm_valid = True
        ctrl._relax_ctrl = SimpleNamespace(
            poll_sampling_stop=lambda: True, dmm_valid=True,
            last_voltage_V=13.0, sampling_mode="SERVICE",
            read_handoff_voltage=lambda *, emit=None: SimpleNamespace(
                quality=quality, age_s=5.0, read_start=123.0,
            ),
            dmm_fault_count=2, missed_deadline_count=3,
            extreme_dt_s_count=1, max_observed_dt_s=7.0,
        )
        ctrl._relax_exit_pending = True
        trace, events = [], []

        def record_call(target, name, label):
            original = getattr(target, name)

            def recorded(*args, **kwargs):
                result = original(*args, **kwargs)
                trace.append(label)
                return result

            monkeypatch.setattr(target, name, recorded)

        record_call(load, "input_off", "load")
        record_call(psu, "all_outputs_off", "psu")
        record_call(ctrl, "_restore_cell_bl_if_needed", "bl")
        record_call(ctrl, "_ensure_bq_sealed_or_flag", "seal")

        def on_event(event):
            events.append(event)
            if event["event_code"] == "LEARNING_RELAX_DMM_NOT_FRESH":
                assert trace == ["load", "psu", "seal", "bl", "seal"], trace
                assert ctrl.state == LearningState.FAULT
                assert ctrl.result_summary is not None
                TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
                trace.append("event")

        ctrl.on_event = on_event
        load.calls.clear()
        psu.calls.clear()
        ctrl.advance(0.0)

        assert trace == ["load", "psu", "seal", "bl", "seal", "event"]
        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == expected_fault
        fault, = [e for e in events if e["event_code"] == "LEARNING_FAULT"]
        assert fault["event_message"] == expected_fault
        handoff, = [e for e in events if e["event_code"] == "LEARNING_RELAX_DMM_NOT_FRESH"]
        assert handoff["dmm_reason"] == quality
        assert handoff["event_message"].startswith(
            LearningState.RELAX_PRE_DISCHARGE_2.value + f": {quality};",
        )
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert load.calls == ["input_off()"]
        assert psu.calls == ["all_outputs_off()"]
        TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
        assert ctrl._relax_ctrl is None and not ctrl._relax_exit_pending
        assert ctrl._relax_dmm_fault_total == 2
        assert ctrl._relax_missed_deadline_total == 3
        assert ctrl._relax_total_extreme_dt_s_count == 1
        assert ctrl._relax_total_max_observed_dt_s == 7.0
        before = (list(load.calls), list(psu.calls), list(trace), list(events))
        ctrl.advance(1.0)
        assert (load.calls, psu.calls, trace, events) == before
        assert ctrl._relax_dmm_fault_total == 2
        assert ctrl._relax_missed_deadline_total == 3


class TestR16B19HandoffDmmFaultCleanup:
    @pytest.mark.parametrize("callback", [None, "record", RuntimeError, KeyboardInterrupt])
    @pytest.mark.parametrize(
        ("temperature_C", "expected_fault"),
        [(22.0, "DMM_FEEDBACK_LOST"), (59.0, "DMM_FEEDBACK_LOST"),
         (60.0, "BATTERY_OVERTEMPERATURE"), (61.0, "BATTERY_OVERTEMPERATURE")],
    )
    def test_real_handoff_fault_is_reported_after_cleanup(
        self, monkeypatch, callback, temperature_C, expected_fault,
    ):
        from Prog.src.relax_controller import (
            RelaxConfig,
            RelaxController,
            RelaxSamplingService,
        )

        ctrl, psu, load, dv, dt, fake = TestR16B16cLearningFloor._make()
        phase = LearningState.RELAX_PRE_DISCHARGE_2
        ctrl._enter(phase)

        ctrl._battery_temperature_C = dt.temperature_C = temperature_C
        ctrl._temp_dmm_valid = True
        psu.output_commanded_on = load.input_commanded_on = True
        ctrl._relax_dmm_fault_total = 7
        ctrl._relax_missed_deadline_total = 11
        # Valódi gyermek és service, workerindítás és valódi műszer nélkül.
        child = RelaxController(dv, RelaxConfig())
        service = RelaxSamplingService(dv, clock=lambda: 123.0)
        child.attach_sampling_service(service)
        service._publish(122.0, 122.0, 13.0)
        child.collect_sampling_records()
        child._dmm_fault_count = 2
        child._missed_deadline_count = 3
        child.on_event = ctrl._emit_relax_event
        ctrl._relax_ctrl = child
        ctrl._relax_exit_pending = True
        dv.dmm_valid = False
        dv.calls.clear()
        trace, events = [], []

        def record_call(target, name, label):
            original = getattr(target, name)

            def recorded(*args, **kwargs):
                result = original(*args, **kwargs)
                trace.append(label)
                return result

            monkeypatch.setattr(target, name, recorded)

        record_call(load, "input_off", "load")
        record_call(psu, "all_outputs_off", "psu")
        child._integrator.extreme_dt_s_count = 1
        child._integrator.max_observed_dt_s = 7.0
        record_call(ctrl, "_restore_cell_bl_if_needed", "bl")
        record_call(ctrl, "_ensure_bq_sealed_or_flag", "seal")
        cleanup = ["load", "psu", "seal", "bl", "seal"]

        def on_event(event):
            events.append(event)
            if event["event_code"] != "RELAX_DMM_FAULT":
                return
            assert trace == cleanup
            assert ctrl.state == LearningState.FAULT
            assert ctrl.fault_reason == expected_fault
            assert not load.input_commanded_on and not psu.output_commanded_on
            TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
            assert ctrl.result_summary is not None
            assert ctrl._relax_ctrl is None and not ctrl._relax_exit_pending
            assert ctrl._relax_dmm_fault_total == 10
            assert ctrl._relax_missed_deadline_total == 14
            trace.append("event")
            if callback not in (None, "record"):
                raise callback("B19 handoff")

        ctrl.on_event = on_event if callback is not None else None
        load.calls.clear()
        psu.calls.clear()
        if callback in (None, "record"):
            ctrl.advance(0.0)
        else:
            with pytest.raises(callback, match="B19 handoff"):
                ctrl.advance(0.0)

        assert trace == cleanup + ([] if callback is None else ["event"])
        assert dv.calls == ["read_voltage()"]
        assert child.sampling_shutdown_complete and not child.dmm_valid
        assert child.dmm_fault_count == 3
        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == expected_fault
        assert ctrl._relax_ctrl is None and not ctrl._relax_exit_pending
        assert ctrl._relax_dmm_fault_total == 10
        assert ctrl._relax_missed_deadline_total == 14
        assert ctrl._relax_total_extreme_dt_s_count == 1
        assert ctrl._relax_total_max_observed_dt_s == 7.0
        TestR8B1LearningStartPowerGate._assert_restored(ctrl, fake)
        if callback is not None:
            event, = [e for e in events if e["event_code"] == "RELAX_DMM_FAULT"]
            assert event["event_message"] == (
                phase.value + ": Átadási DMM_READ_ERROR: MockDMM: dmm_valid=False"
            )
            fault, = [e for e in events if e["event_code"] == "LEARNING_FAULT"]
            assert fault["event_message"] == expected_fault
        if callback == "record":
            diagnostic, = [
                e for e in events if e["event_code"] == "LEARNING_RELAX_DMM_NOT_FRESH"
            ]
            assert diagnostic["dmm_reason"] == "READ_ERROR"
            assert diagnostic["read_start"] == 123.0 and diagnostic["sample_age_s"] == 0.0
        assert load.calls == ["input_off()"]
        assert psu.calls == ["all_outputs_off()"]
        # A terminális újabb tick sem ismétel totalátvételt vagy ON parancsot.
        before = (list(load.calls), list(psu.calls), list(trace), list(events))
        ctrl.advance(1.0)
        assert (load.calls, psu.calls, trace, events) == before
        assert ctrl._relax_dmm_fault_total == 10
        assert ctrl._relax_missed_deadline_total == 14


class TestR16B7LearningC10Required:
    def test_missing_c10_blocks_constructor_without_hardware_commands(self):
        from unittest.mock import Mock

        from Prog.src.exceptions import ProfileValidationError

        profile = _make_profile()
        profile.C10_capacity_Ah = None
        psu, load, dmm_v, dmm_t, transport, safety = [Mock() for _ in range(6)]
        with pytest.raises(ProfileValidationError, match="SOURCE_REQUIRED"):
            TraditionalLearningController(psu, load, dmm_v, dmm_t, transport, profile, safety, LearningConfig())
        assert psu.mock_calls == []
        assert load.mock_calls == []
        assert transport.mock_calls == []


def test_r16b20f1_precond_logs_existing_b7_scale_readback_once(monkeypatch):
    import Prog.src.traditional_learning_controller as module

    ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
    events = []
    ctrl.on_event = events.append
    original = module.evaluate_provisioning_consistency
    gate_calls = []

    def gate(profile, service):
        assert not fake_bq.control_status_word & 0x2000
        report = original(profile, service)
        gate_calls.append(report)
        return report

    monkeypatch.setattr(module, "evaluate_provisioning_consistency", gate)
    ctrl.advance(0.0)  # INIT -> PRECOND
    ctrl.advance(0.0)  # PRECOND -> charge
    assert len(gate_calls) == 1
    logged = [e for e in events if e["event_code"] == "LEARNING_BQ_DF_CROSS_CHECK_PASSED"]
    assert len(logged) == 1
    values = {c.field_name: c.bq_readback for c in gate_calls[0].comparisons}
    assert values["design_energy_scale"] == 10
    assert values["pack_configuration"] == 0
    assert logged[0]["event_message"].endswith(
        "source=B7 PRECOND UNSEALED; Design Energy Scale=10; X10=0"
    )
    ctrl.advance(0.0)
    assert len(gate_calls) == 1
    assert sum(e["event_code"] == "LEARNING_BQ_DF_CROSS_CHECK_PASSED" for e in events) == 1


class TestR16B23aInitialStatus:
    """Q-39: ismeretlen baseline nem juthat az IT_ENABLE elé."""

    @staticmethod
    def _new(monkeypatch, failure, success_on, value=0x05):
        from dataclasses import replace
        from unittest.mock import patch

        import Prog.src.traditional_learning_controller as module

        ctrl, psu, load, _dmm, _temp, bq = make_controller()
        ctrl._enter(LearningState.INITIAL_SNAPSHOT)
        original_read = ctrl._bq_driver.read_update_status
        original_poll = module.poll_bq_diagnostics
        attempts, polls, events, off_order = [], [], [], []
        clock = [0.0]
        advance = ctrl.advance

        def tick(dt_s):
            clock[0] += dt_s
            return advance(dt_s)

        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(ctrl, "advance", tick)
        original_load_off = load.input_off
        original_psu_off = psu.all_outputs_off

        def read_status():
            attempts.append(len(attempts) + 1)
            assert len(attempts) <= 4, "Q-39 retry-korlát"
            if success_on is not None and len(attempts) >= success_on:
                return value
            if failure == "exception":
                raise RuntimeError("Q39-read-error")
            if failure == "sealed":
                with patch.object(
                    ctrl._bq_transport, "read_data_flash_block",
                    return_value=DEGENERATE_SEALED_DF_BLOCK,
                ):
                    return original_read()
            return None

        def poll(driver):
            polls.append(driver)
            snapshot = original_poll(driver)
            if failure == "valid_false" and (
                success_on is None or len(attempts) < success_on
            ):
                snapshot.update_status = replace(
                    snapshot.update_status, valid=False, value=0x06,
                    error="Q39-invalid-field",
                )
            return snapshot

        def load_off():
            off_order.append("LOAD_OFF")
            original_load_off()

        def psu_off():
            off_order.append("PSU_ALL_OFF")
            original_psu_off()

        monkeypatch.setattr(ctrl._bq_driver, "read_update_status", read_status)
        monkeypatch.setattr(module, "poll_bq_diagnostics", poll)
        monkeypatch.setattr(load, "input_off", load_off)
        monkeypatch.setattr(psu, "all_outputs_off", psu_off)
        ctrl.on_event = events.append
        return ctrl, psu, load, bq, attempts, polls, events, off_order, clock

    @pytest.mark.parametrize(
        "failure", ("valid_false", "value_none", "exception", "sealed"),
    )
    @pytest.mark.parametrize("success_on", (1, 2, 3, 4, None))
    @pytest.mark.parametrize("value", (0x00, 0x05, 0x06))
    def test_bounded_retry_or_fault(
        self, monkeypatch, failure, success_on, value,
    ):
        ctrl, psu, load, bq, attempts, polls, events, off, _clock = self._new(
            monkeypatch, failure, success_on, value,
        )
        ctrl.advance(0.0)
        # A retry nem olvashatja újra a Qmax/Ra baseline-t.
        bq.qmax = 150
        for _ in range(3):
            if ctrl.state != LearningState.INITIAL_SNAPSHOT:
                break
            before = len(attempts)
            ctrl.advance(0.069)
            assert len(attempts) == before, "70 ms előtt nincs új olvasás"
            ctrl.advance(0.002)
        expected_attempts = success_on if success_on is not None else 4
        assert attempts == list(range(1, expected_attempts + 1))
        assert len(polls) == 1
        retries = [
            e for e in events
            if e["event_code"] == "LEARNING_INITIAL_UPDATE_STATUS_RETRY"
        ]
        assert [e["attempts"] for e in retries] == list(
            range(1, expected_attempts),
        )
        assert all(
            e["max_attempts"] == 4 and e["retry_wait_s"] == 0.07
            for e in retries
        )
        snapshots = [
            e for e in events
            if e["event_code"] == "LEARNING_INITIAL_SNAPSHOT"
        ]
        if success_on is None:
            assert ctrl.state == LearningState.FAULT
            assert ctrl.fault_reason.startswith(
                "LEARNING_INITIAL_UPDATE_STATUS_UNREADABLE: attempts=4;",
            )
            assert ctrl.result_summary.result == LEARNING_RESULT_FAILED
            assert snapshots == []
            assert off == ["LOAD_OFF", "PSU_ALL_OFF"]
            frozen = (len(attempts), len(polls), len(bq.calls))
            ctrl.advance(100.0)
            assert (len(attempts), len(polls), len(bq.calls)) == frozen
        else:
            assert ctrl.state == LearningState.IT_ENABLE_STEP
            assert ctrl._qmax_before == 100
            assert ctrl._initial_snapshot.update_status.valid
            assert ctrl._initial_snapshot.update_status.value == value
            assert len(snapshots) == 1
            assert snapshots[0]["initial_update_status_attempts"] == success_on
            assert f"UpdateStatus=0x{value:02X}" in snapshots[0]["event_message"]
            summary = ctrl._classify_result()
            assert summary.update_status_initial == value
            if value == 0x06:
                assert summary.result == LEARNING_RESULT_AWAITING
                assert (
                    "UPDATE_STATUS_ALREADY_0X06_AT_INITIAL_SNAPSHOT"
                    in summary.quality_flags
                )
            assert off == []
        assert not psu.output_commanded_on
        assert not load.input_commanded_on
        assert "input_on()" not in load.calls
        assert "it_enable" not in bq.calls
        assert "write_data_flash_block" not in bq.calls

    def test_published_retry_snapshots_keep_their_original_status(self, monkeypatch):
        ctrl, _psu, _load, _bq, attempts, _polls, _events, _off, _clock = (
            self._new(monkeypatch, "valid_false", 3, 0x05)
        )
        history = []
        ctrl.on_gauge_sample = history.append
        for _ in range(3):
            ctrl.advance(0.071)
        assert attempts == [1, 2, 3]
        assert ctrl.state == LearningState.IT_ENABLE_STEP
        assert len(history) == 3
        assert len({id(s) for s in history}) == 3
        assert [s.update_status.valid for s in history] == [False, False, True]
        assert [s.update_status.value for s in history] == [0x06, None, 0x05]
        assert [s.update_status.error for s in history] == [
            "Q39-invalid-field", "Update Status value=None", None,
        ]
        assert [
            s.update_status.timestamp_monotonic for s in history
        ] == pytest.approx([0.071, 0.142, 0.213])
        assert all(s.qmax.value == 100 for s in history)
        assert ctrl._initial_snapshot is history[-1]

    def test_retry_clock_epoch_is_independent_of_phase_elapsed(self, monkeypatch):
        ctrl, _psu, _load, _bq, attempts, _polls, _events, _off, clock = (
            self._new(monkeypatch, "exception", 2)
        )
        clock[0] = 10000.0
        ctrl.advance(0.0)
        assert attempts == [1]
        ctrl.advance(0.071)
        assert attempts == [1, 2]
        assert ctrl.state == LearningState.IT_ENABLE_STEP

    def test_retry_deadline_starts_after_slow_snapshot(self, monkeypatch):
        import Prog.src.traditional_learning_controller as module

        ctrl, _psu, _load, _bq, attempts, _polls, _events, _off, clock = (
            self._new(monkeypatch, "exception", 2)
        )
        poll = module.poll_bq_diagnostics

        def slow_poll(driver):
            clock[0] += 0.2
            return poll(driver)

        monkeypatch.setattr(module, "poll_bq_diagnostics", slow_poll)
        TraditionalLearningController.advance(ctrl, 0.0)
        assert attempts == [1] and clock[0] == 0.2
        clock[0] += 0.001
        # A következő dt tartalmazza az előző poll idejét: nem retry-idő.
        TraditionalLearningController.advance(ctrl, 0.201)
        assert attempts == [1]
        clock[0] += 0.07
        TraditionalLearningController.advance(ctrl, 0.07)
        assert attempts == [1, 2]
        assert ctrl.state == LearningState.IT_ENABLE_STEP

    def test_stop_before_retry_has_no_new_io(self, monkeypatch):
        ctrl, _psu, _load, bq, attempts, polls, events, _off, _clock = self._new(
            monkeypatch, "exception", None,
        )
        ctrl.advance(0.0)
        assert attempts == [1]
        before = (len(attempts), len(polls), len(bq.calls), len(events))
        ctrl.handoff_stop_requested = lambda: True
        ctrl.advance(1.0)
        assert ctrl.state == LearningState.INITIAL_SNAPSHOT
        assert (len(attempts), len(polls), len(bq.calls), len(events)) == before

    def test_unknown_baseline_cannot_verify_even_with_06_and_changed_qmax_ra(
        self, monkeypatch,
    ):
        ctrl, psu, load, dmm, _temp, bq = make_controller(
            config=LearningConfig(
                relax_after_charge_s=RelaxConfig().sample_period_s + 2.0,
                relax_after_discharge_s=RelaxConfig().sample_period_s + 2.0,
                sample_interval_s=10000.0, rebound_settle_s=1.0,
            ),
        )
        bq.update_status = 0x06
        read = ctrl._bq_driver.read_update_status
        initial_failures = []
        clock = [0.0]
        import Prog.src.traditional_learning_controller as module

        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

        def status():
            if ctrl.state == LearningState.INITIAL_SNAPSHOT:
                initial_failures.append(True)
                raise RuntimeError("B18 initial Update Status unavailable")
            return read()

        monkeypatch.setattr(ctrl._bq_driver, "read_update_status", status)
        for _ in range(1000):
            phase = ctrl.state
            if phase in (LearningState.DONE, LearningState.FAULT):
                break
            if phase in (LearningState.PRECHARGE, LearningState.RECHARGE):
                _force_charge_done(ctrl)
            bq.set_ocvtaken(True)
            bq.set_qen(True)
            bq.set_vok(phase in (
                LearningState.VERIFY_VOK_QEN,
                LearningState.RELAX_PRE_DISCHARGE_2,
            ))
            dmm.voltage_V = 9.0 if phase in (
                LearningState.DISCHARGE_1, LearningState.REBOUND_CHECK_1,
                LearningState.RELAX_1, LearningState.DISCHARGE_2,
                LearningState.REBOUND_CHECK_2, LearningState.RELAX_2,
            ) else 13.0
            if phase == LearningState.FINAL_SNAPSHOT:
                bq.qmax = 150
                bq.ra_table = [x + 1.0 for x in bq.ra_table]
            clock[0] += 2.0
            ctrl.advance(2.0)
        assert initial_failures, "A B18 hibát ténylegesen elő kell idézni"
        assert ctrl.state in (LearningState.DONE, LearningState.FAULT)
        assert ctrl.result_summary.result == LEARNING_RESULT_FAILED, ctrl.result_summary
        assert ctrl.state == LearningState.FAULT
        assert len(initial_failures) == 4
        assert "attempts=4;" in ctrl.fault_reason
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert "it_enable" not in bq.calls
        assert "write_data_flash_block" not in bq.calls


class TestR16B23aRelaxMargin:
    """Q-41: pakk-DMM OLS és a chip visszaigazolása együtt kötelező."""

    @staticmethod
    def _new(phase, ocvtaken):
        ctrl, psu, load, dmm, bq = TestR16B4RelaxAcceptance._new(
            phase, min_s=RelaxConfig().sample_period_s + _relax_tick_s(),
            extra_s=3600.0,
        )
        bq.set_ocvtaken(ocvtaken)
        events = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        return ctrl, psu, load, dmm, bq, events

    @staticmethod
    def _fill(ctrl, dmm, slope):
        dt = _relax_tick_s()
        ticks = int(RelaxConfig().sample_period_s / dt) + 1
        for _ in range(ticks):
            dmm.voltage_V += slope * dt
            ctrl.advance(dt)

    def test_default_is_project_margin_and_time_policies_are_unchanged(self):
        cfg = LearningConfig()
        assert cfg.dvdt_threshold_V_per_s == 3.5e-6
        assert (cfg.relax_after_charge_s, cfg.relax_after_discharge_s) == (
            7200.0, 18000.0,
        )
        assert cfg.max_relax_extra_wait_s == 3600.0

    @pytest.mark.parametrize(
        "phase,next_state", TestR16B4RelaxAcceptance._phases,
    )
    @pytest.mark.parametrize("slope", (3.4e-6, -3.4e-6, 3.6e-6, -3.6e-6))
    def test_pack_ols_margin(self, phase, next_state, slope):
        ctrl, _psu, _load, dmm, _bq, events = self._new(phase, True)
        self._fill(ctrl, dmm, slope)
        accepted = [
            e for e in events if e["event_code"] == "LEARNING_RELAX_ACCEPTED"
        ]
        if abs(slope) < 3.5e-6:
            assert ctrl.state == next_state
            assert len(accepted) == 1
            assert accepted[0]["dvdt_V_per_s"] == pytest.approx(slope, abs=1e-12)
        else:
            assert ctrl.state == phase
            assert ctrl._relax_ctrl.dvdt_valid
            assert accepted == []
        assert not ctrl.quality_flags

    @pytest.mark.parametrize(
        "phase,next_state", TestR16B4RelaxAcceptance._phases,
    )
    @pytest.mark.parametrize(
        "ocvtaken,slope,direction",
        ((False, 3.4e-6, "DMM_ONLY"), (True, 3.8e-6, "BQ_OCVTAKEN_ONLY")),
    )
    def test_mismatch_both_directions_once(
        self, phase, next_state, ocvtaken, slope, direction,
    ):
        ctrl, _psu, _load, dmm, _bq, events = self._new(phase, ocvtaken)
        self._fill(ctrl, dmm, slope)
        for _ in range(5):
            dmm.voltage_V += slope * _relax_tick_s()
            ctrl.advance(_relax_tick_s())
        assert ctrl.state == phase, next_state
        mismatch = [
            e for e in events
            if e["event_code"] == "LEARNING_RELAX_GATE_MISMATCH"
        ]
        assert len(mismatch) == 1
        event = mismatch[0]
        assert event["direction"] == direction
        assert event["phase"] == phase.value
        assert event["ocvtaken"] is ocvtaken
        assert event["vok"] is (phase == LearningState.RELAX_PRE_DISCHARGE_2)
        assert event["dvdt_V_per_s"] == pytest.approx(slope, abs=1e-12)
        assert event["dvdt_threshold_V_per_s"] == 3.5e-6
        assert phase.value in event["event_message"]
        assert direction in event["event_message"]
        assert "dvdt_threshold_V_per_s=3.5e-06" in event["event_message"]
        assert "'dvdt_V_per_s':" in event["event_message"]
        assert "'ocvtaken':" in event["event_message"]
        assert "'vok':" in event["event_message"]

    def test_mismatch_rearms_after_agreement_and_for_next_phase(self):
        phase = LearningState.RELAX_1
        ctrl, _psu, _load, dmm, bq, events = self._new(phase, False)
        self._fill(ctrl, dmm, 3.4e-6)
        bq.set_vok(True)  # VOK zárva tartja a kaput az OCVTAKEN=1 kontrollnál.
        bq.set_ocvtaken(True)
        ctrl.advance(_relax_tick_s())
        assert ctrl.state == phase
        bq.set_ocvtaken(False)
        ctrl.advance(_relax_tick_s())
        ctrl._relax_ctrl = None  # Új, energiamentes LEGACY_TICK relax-fázis.
        ctrl._enter(LearningState.RELAX_2)
        bq.set_vok(False)
        ctrl.advance(0.0)
        self._fill(ctrl, dmm, 3.4e-6)
        mismatch = [
            e for e in events
            if e["event_code"] == "LEARNING_RELAX_GATE_MISMATCH"
        ]
        assert [e["phase"] for e in mismatch] == [
            "RELAX_1", "RELAX_1", "RELAX_2",
        ]
        assert all(e["direction"] == "DMM_ONLY" for e in mismatch)

    @pytest.mark.parametrize(
        "phase,next_state", TestR16B4RelaxAcceptance._phases,
    )
    def test_timeout_still_excludes_verified(self, phase, next_state):
        ctrl, _psu, _load, dmm, _bq, events = self._new(phase, False)
        self._fill(ctrl, dmm, 3.4e-6)
        assert ctrl.state == phase
        ctrl.advance(3601.0)
        assert ctrl.state == next_state
        assert sum(
            f.startswith(f"RELAX_ACCEPTANCE_NOT_CONFIRMED_AT_{phase.value}")
            for f in ctrl.quality_flags
        ) == 1
        assert sum(
            e["event_code"] == "LEARNING_RELAX_ACCEPTANCE_TIMEOUT"
            for e in events
        ) == 1
        ctrl._update_status_final = 0x06
        ctrl._qmax_before, ctrl._qmax_final = 100, 150
        ctrl._ra_before, ctrl._ra_final = (10.0,), (11.0,)
        ctrl._vok_qen_observed = {"qen": True}
        assert ctrl._classify_result().result == LEARNING_RESULT_AWAITING


@pytest.mark.parametrize("exception_kind", ("abort", "keyboard", "system_exit"))
def test_r16b23a_fix1_retry_preserves_control_flow_exception(
    monkeypatch, exception_kind,
):
    from unittest.mock import Mock

    import Prog.src.traditional_learning_controller as module
    from Prog.src.exceptions import TestAbortedByUser

    ctrl, psu, load, _dmm, _temp, bq = make_controller()
    ctrl._enter(LearningState.INITIAL_SNAPSHOT)
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    exception_type = {
        "abort": TestAbortedByUser,
        "keyboard": KeyboardInterrupt,
        "system_exit": SystemExit,
    }[exception_kind]
    interrupted = exception_type("Q39_ABORT")
    read_status = Mock(side_effect=[
        RuntimeError("Q39 initial read failed"), interrupted, 0x05,
    ])
    monkeypatch.setattr(ctrl._bq_driver, "read_update_status", read_status)
    events = []
    ctrl.on_event = events.append
    ctrl.advance(0.0)
    assert read_status.call_count == 1
    assert ctrl.state == LearningState.INITIAL_SNAPSHOT
    initial_snapshot = ctrl._initial_snapshot
    assert not initial_snapshot.update_status.valid

    clock[0] = 0.071
    with pytest.raises(exception_type, match="Q39_ABORT") as raised:
        ctrl.advance(0.071)

    assert raised.value is interrupted
    assert read_status.call_count == 2
    assert ctrl.state == LearningState.INITIAL_SNAPSHOT
    assert ctrl._initial_snapshot is initial_snapshot
    assert not any(e["event_code"] == "LEARNING_INITIAL_SNAPSHOT" for e in events)
    assert not psu.output_commanded_on and not load.input_commanded_on
    assert "input_on()" not in load.calls
    assert not {"unseal", "write_data_flash_block", "it_enable"}.intersection(bq.calls)


class TestR16B25TlcEvidence:
    @pytest.mark.parametrize("source", ["read", "gauge"])
    def test_fourth_invalid_status_fault_does_not_repeat_cleanup(self, monkeypatch, source):
        from unittest.mock import Mock

        ctrl, _psu, _load, _dmm, _temp, _bq = make_controller()
        ctrl._enter(LearningState.INITIAL_SNAPSHOT)
        reads, gauges = [], []
        stop = Mock(wraps=ctrl.emergency_stop)
        monkeypatch.setattr(ctrl, "emergency_stop", stop)

        def read():
            reads.append(True)
            assert len(reads) <= 4, "B25 Update Status-korlát"
            if len(reads) == 4 and source == "read":
                ctrl.emergency_stop("B25_READ_FAULT")
            raise RuntimeError("B25 invalid status")

        def gauge(_snapshot):
            gauges.append(len(reads))
            if len(reads) == 4 and source == "gauge":
                ctrl.emergency_stop("B25_GAUGE_FAULT")

        monkeypatch.setattr(ctrl._bq_driver, "read_update_status", read)
        ctrl.on_gauge_sample = gauge
        for _ in range(4):
            ctrl._initial_update_status_retry_at_s = 0.0
            ctrl._run_initial_snapshot()
        assert len(reads) == 4
        assert gauges == ([1, 2, 3] if source == "read" else [1, 2, 3, 4])
        stop.assert_called_once_with("B25_" + source.upper() + "_FAULT")
        assert ctrl.state == LearningState.FAULT
        assert ctrl.fault_reason == "B25_" + source.upper() + "_FAULT"

    @pytest.mark.parametrize("phase,next_state", TestR16B4RelaxAcceptance._phases)
    def test_unknown_ocvtaken_has_no_mismatch(self, monkeypatch, phase, next_state):
        from unittest.mock import Mock

        ctrl, psu, load, dmm, _bq, events = TestR16B23aRelaxMargin._new(phase, False)
        flags = Mock(side_effect=RuntimeError("B25 Flags unreadable"))
        monkeypatch.setattr(ctrl._bq_driver, "read_flags", flags)
        TestR16B23aRelaxMargin._fill(ctrl, dmm, 3.4e-6)
        assert flags.call_count > 0
        assert ctrl._relax_ctrl.dvdt_valid
        assert ctrl._check_relax_bq_acceptance(True) == (False, None, None)
        assert ctrl.state == phase
        assert not any(e["event_code"] in (
            "LEARNING_RELAX_GATE_MISMATCH", "LEARNING_RELAX_ACCEPTED",
        ) for e in events)
        assert not psu.output_commanded_on and not load.input_commanded_on

    @pytest.mark.parametrize("read_s", [0.0, 4.0, 4.001, 6.0])
    def test_d1_only_fresh_voltage_replaces_parent_value(self, monkeypatch, read_s):
        import Prog.src.traditional_learning_controller as module

        ctrl, psu, load, dmm, _temp, _bq = make_controller()
        ctrl._state = LearningState.VERIFY_VOK_QEN
        ctrl._u_batt = 12.9
        clock, reads, events = [100.0], [], []
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

        def read():
            reads.append(True)
            assert len(reads) == 1
            clock[0] += read_s
            return 12.3

        monkeypatch.setattr(dmm, "read_voltage", read)
        ctrl.on_event = events.append
        ctrl._begin_discharge(LearningState.DISCHARGE_1)
        assert reads == [True]
        assert ctrl.dmm_valid is (read_s <= 4.0)
        assert ctrl.u_batt_V == (12.3 if read_s <= 4.0 else 12.9)
        if read_s > 4.0:
            assert ctrl.state == LearningState.FAULT
            assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
            assert not psu.output_commanded_on and not load.input_commanded_on
            assert "input_off()" in load.calls and "all_outputs_off()" in psu.calls
            assert "input_on()" not in load.calls
            event, = [e for e in events if e["event_code"] == "LEARNING_RELAX_DMM_NOT_FRESH"]
            assert event["dmm_reason"] == "STALE"
        else:
            assert ctrl.state == LearningState.DISCHARGE_1
            assert "input_on()" in load.calls


@pytest.mark.parametrize("path,method", [
    ("precond", "read_chem_id"), ("precond", "read_control_status"),
    ("relax", "read_flags"), ("relax", "read_control_status"),
    ("vok_qen", "read_control_status"), ("update_05", "read_update_status"),
    ("update_06", "read_update_status"), ("update_05", "read_qmax"),
])
def test_r16b25_tlc_bq_read_siblings_preserve_abort(monkeypatch, path, method):
    from Prog.src.exceptions import TestAbortedByUser

    ctrl, psu, load, _dmm, _temp, bq = make_controller()
    ctrl._u_batt = 12.8
    ctrl._phase_elapsed_s = ctrl._config.verify_settle_s
    bq.update_status = 0x05
    original = getattr(ctrl._bq_driver, method)
    aborted, reads = TestAbortedByUser("B25 BQ sibling abort"), []

    def read():
        reads.append(True)
        assert len(reads) == 1, "B25 megszakadt BQ-olvasás nem ismételhető"
        raise aborted

    monkeypatch.setattr(ctrl._bq_driver, method, read)
    try:
        with pytest.raises(TestAbortedByUser) as caught:
            if path == "precond":
                ctrl._run_precond()
            elif path == "relax":
                ctrl._check_relax_bq_acceptance(True)
            elif path == "vok_qen":
                ctrl._run_verify_vok_qen(0.0)
            else:
                ctrl._run_verify_update_status(0.0, path == "update_06", LearningState.RELAX_2)
        assert caught.value is aborted and reads == [True]
        assert "input_on()" not in load.calls
        assert not {"write_data_flash_block", "it_enable"}.intersection(bq.calls)
    finally:
        monkeypatch.setattr(ctrl._bq_driver, method, original)
        ctrl.emergency_stop("B25_TEST_CLEANUP")
    assert not psu.output_commanded_on and not load.input_commanded_on


class TestR16B26aEmbeddedChargeCap:
    @pytest.mark.parametrize("phase", [LearningState.PRECHARGE, LearningState.RECHARGE])
    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("temperature,per_cell", [
        (0.0, 2.400), (5.0, 2.400), (10.0, 2.400), (15.0, 2.400),
        (19.9, 2.400), (20.0, 2.400), (25.0, 2.3875), (35.0, 2.3625),
    ])
    def test_parent_charge_target_and_absolute_gate(self, phase, cells, temperature, per_cell):
        from dataclasses import replace

        ctrl, psu, load, dmm, temp, fake = make_controller(
            dmm_voltage_V=2.2 * cells,
            charge_config=ChargeConfig(charge_current_A_override=0.67, fast_loop_enabled=False),
        )
        ctrl._profile = replace(ctrl._profile, cell_count=cells, nominal_voltage_V=2.0 * cells)
        ctrl._safety.profile = ctrl._profile
        ctrl._safety.psu_mode = PsuMode.SERIES if cells == 12 else PsuMode.INDEPENDENT
        ctrl._safety.temp_comp_mode = TempCompMode.ENABLED
        fake.df_fields = _default_df_fields(ctrl._profile)
        temp.temperature_C = temperature
        events = []

        def event(data):
            if data["event_code"] == "LEARNING_FAULT":
                assert not load.input_commanded_on and not psu.output_commanded_on
            events.append(data)

        ctrl.on_event = event
        ctrl._enter(phase)
        for _ in range(3):
            ctrl.advance(0.1)
        child = ctrl._charge_ctrl
        assert child is not None and child.state == ChargeState.CHARGE_CC
        assert child._effective_charge_target_V() == pytest.approx(per_cell * cells, abs=1e-12)
        assert psu.voltage_V == pytest.approx(per_cell * cells, abs=1e-12)
        assert psu.output_commanded_on
        dmm.voltage_V = per_cell * cells + 0.050
        ctrl.advance(0.1)
        assert ctrl.state == phase and child.state != ChargeState.FAULT
        assert ctrl.fault_reason == ""
        dmm.voltage_V = round(2.425 * cells, 6) + 0.001
        ctrl.advance(0.1)
        assert ctrl.state == LearningState.FAULT
        assert "BATTERY_OVERVOLTAGE" in ctrl.fault_reason
        fault_events = [item for item in events if item["event_code"] == "LEARNING_FAULT"]
        assert len(fault_events) == 1, "Q44 parent-fault event trigger"
        fault_event = fault_events[0]
        assert "BATTERY_OVERVOLTAGE" in fault_event["event_message"]
        assert not load.input_commanded_on and not psu.output_commanded_on


class TestR16B26bLearningConfigCode:
    @pytest.mark.parametrize("kind,value", [
        ("cutoff", float("nan")), ("cutoff", float("inf")),
        ("cutoff", -float("inf")), ("cutoff", 10.199999),
        ("floor", float("nan")), ("floor", 1.69),
        ("retries", -1), ("retries", 5), ("retries", 3.5), ("retries", True),
    ])
    def test_floor_validation_keeps_safe_cleanup(self, monkeypatch, kind, value):
        result = TestR16B16cLearningFloor._make()
        ctrl, psu, load, *_ = result
        cutoff = 10.5
        if kind == "cutoff":
            cutoff = value
        elif kind == "floor":
            ctrl._config.learning_discharge_floor_V_per_cell = value
        else:
            ctrl._config.max_rebound_retries = value
        trace, events = [], []
        load_off, psu_off = load.input_off, psu.all_outputs_off

        def stop_load():
            trace.append("LOAD")
            load_off()

        def stop_psu():
            trace.append("PSU")
            psu_off()

        monkeypatch.setattr(load, "input_off", stop_load)
        monkeypatch.setattr(psu, "all_outputs_off", stop_psu)
        ctrl.on_event = events.append
        assert not ctrl._check_learning_discharge_floor(cutoff)
        assert trace[:2] == ["LOAD", "PSU"]
        TestR16B16cLearningFloor._assert_fault(result, events, "DISCHARGE_CONFIG_INVALID")

    @pytest.mark.parametrize("bad", [-1, 5, 3.5, True, float("nan"), float("inf")])
    @pytest.mark.parametrize("phase,discharge,final", [
        (LearningState.REBOUND_CHECK_1, LearningState.DISCHARGE_1, LearningState.RELAX_1),
        (LearningState.REBOUND_CHECK_2, LearningState.DISCHARGE_2, LearningState.RELAX_2),
    ])
    def test_rebound_validation_keeps_config_reason(self, bad, phase, discharge, final):
        result = TestR16B16cLearningFloor._make()
        ctrl, _psu, _load, *_ = result
        ctrl._config.max_rebound_retries = bad
        ctrl._enter(phase)
        ctrl._u_batt = 12.8
        events = []
        ctrl.on_event = events.append
        ctrl._run_rebound_check(0.0, discharge, final)
        assert ctrl._rebound_retry_count == 0
        TestR16B16cLearningFloor._assert_fault(result, events, "DISCHARGE_CONFIG_INVALID")


@pytest.mark.parametrize("vpc", [1.6999, 1.70, 1.73, 1.75])
def test_b23b_q42_intermediate_recharge_uses_tlc_context(vpc):
    ctrl, psu, load, _, _, _ = make_controller(dmm_voltage_V=6 * vpc)
    ctrl._enter(LearningState.RECHARGE)
    for _ in range(3):
        ctrl.advance(0.1)
        if ctrl.state == LearningState.FAULT:
            break
    assert psu.called("output_on") is (vpc >= 1.70)
    assert not load.input_commanded_on
    if vpc < 1.70:
        assert ctrl.state == LearningState.FAULT
        assert "DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED" in ctrl.fault_reason
        assert psu.called("all_outputs_off")
    assert (ctrl._safety.check_precheck_voltage(6 * vpc).fault is None) is (vpc >= 1.75)


def _b23b_final_ready(*, awaiting=False):
    """Real BQ restore/seal and real charge child; no file or hardware I/O."""
    from dataclasses import replace

    from Prog.src.bq_diagnostics import poll_bq_diagnostics

    ctrl, psu, load, dmm, temp, bq = make_controller(
        charge_config=ChargeConfig(charge_current_A_override=1.2, taper_hold_s=2.0),
    )
    ctrl._safety.temp_comp_mode = TempCompMode.ENABLED
    ctrl._initial_snapshot = poll_bq_diagnostics(ctrl._bq_driver)
    ctrl._qmax_before = bq.qmax
    ctrl._ra_before = tuple(bq.ra_table)
    ctrl._vok_qen_observed = {"vok": True, "qen": True}
    ctrl._update_status_at_0x05_check = 0x05
    ctrl._update_status_final = 0x06
    ctrl._cell_bl_original_mV = 1950
    ctrl._bq_unseal_attempted = True
    bq.set_sealed(False)
    bq.update_status = 0x05 if awaiting else 0x06
    bq.qmax += 50
    bq.ra_table = [v + 1 for v in bq.ra_table]
    ctrl._enter(LearningState.FINAL_SNAPSHOT)
    checkpoints = []
    ctrl.persist_tlc_completion = checkpoints.append
    ctrl._charge_config = replace(ctrl._charge_config, fast_loop_enabled=False)
    return ctrl, psu, load, dmm, temp, bq, checkpoints


def _b23b_finish_recharge(ctrl, max_ticks=20):
    """Bounded CC -> CV -> taper; never replace the child terminal state."""
    assert ctrl.state == LearningState.FINAL_RECHARGE
    # Szintetikus, terheles nelkuli rebound; a normal toltesi kapu lefut.
    ctrl._dmm_v.voltage_V = ctrl._profile.cell_count * 1.80
    seen = []
    for _ in range(max_ticks):
        child = ctrl._charge_ctrl
        if child is not None:
            seen.append(child.state)
            if child.state in (ChargeState.CHARGE_CC, ChargeState.CHARGE_CV_DMM_CONTROL, ChargeState.TAPER_HOLD):
                ctrl._dmm_v.voltage_V = child._effective_charge_target_V()
                ctrl._psu.current_A = 0.1
        dt_s = max(1.0, child._config.taper_hold_s) if child is not None and child.state == ChargeState.TAPER_HOLD else 1.0
        ctrl.advance(dt_s)
        if ctrl.state in (LearningState.DONE, LearningState.FAULT):
            break
    assert ctrl.state == LearningState.DONE, ctrl.fault_reason
    assert ChargeState.CHARGE_CC in seen
    assert ChargeState.CHARGE_CV_DMM_CONTROL in seen
    assert ChargeState.TAPER_HOLD in seen


@pytest.mark.parametrize("awaiting", [False, True])
def test_b23b_final_recharge_preserves_precharge_evidence_and_real_taper(monkeypatch, awaiting):
    from copy import deepcopy
    from datetime import datetime, timedelta

    ctrl, psu, load, _, _, bq, checkpoints = _b23b_final_ready(awaiting=awaiting)
    events, order = [], []
    ctrl.on_event = lambda e: (events.append(e), order.append(e["event_code"]))
    original_on = psu.output_on

    def on(**kwargs):
        assert not load.read_input_state()
        assert bq.control_status_word & 0x2000
        assert ctrl._cell_bl_restore_confirmed is True
        assert checkpoints and checkpoints[-1]["learning_evidence"]
        assert "LEARNING_EVIDENCE_CAPTURED" in order
        order.append("PSU_ON")
        bq.update_status = 0
        bq.qmax = 1
        bq.ra_table = [1] * 15
        original_on(**kwargs)

    monkeypatch.setattr(psu, "output_on", on)
    ctrl.advance(0.1)
    assert ctrl.state == LearningState.FINAL_RECHARGE
    evidence = deepcopy(ctrl.tlc_completion["learning_evidence"])
    expected = LEARNING_RESULT_AWAITING if awaiting else LEARNING_RESULT_VERIFIED
    assert evidence["result_summary"]["result"] == expected
    assert "LEARNING_DONE" not in order
    _b23b_finish_recharge(ctrl)
    record = ctrl.tlc_completion
    assert record["learning_evidence"] == evidence
    assert ctrl.result_summary.result == expected
    assert record["final_recharge"]["status"] == "COMPLETED"
    final = record["final_recharge"]
    assert final["off_readback"] == {"Load": False, "PSU": False}
    assert final["taper_timer_s"] >= 2.0
    assert final["psu_current_A"] == pytest.approx(0.1)
    end = datetime.fromisoformat(final["completed_at_utc"])
    assert datetime.fromisoformat(final["next_campaign_not_before_utc"]) - end == timedelta(hours=48)
    assert checkpoints[-1] == record
    assert record["effective_charge_config"]["charge_current_A_override"] == 0.67
    assert ctrl._charge_config.charge_current_A_override == 1.2
    assert psu.calls.count("set_output_current(0.67)") == 1
    assert not load.input_commanded_on and not psu.output_commanded_on
    assert order.index("LEARNING_EVIDENCE_CAPTURED") < order.index("PSU_ON")
    assert order.index("LEARNING_FINAL_RECHARGE_COMPLETED") < order.index("LEARNING_DONE")
    assert bq.calls.count("unseal") == 1, "No DF access or unseal during final recharge"
    copied = ctrl.tlc_completion
    copied["learning_evidence"]["result_summary"]["qmax_final"] = -1
    assert ctrl.tlc_completion["learning_evidence"] == evidence


@pytest.mark.parametrize("failure", ["checkpoint", "missing_checkpoint", "restore", "seal", "late_seal_verify", "off"])
def test_b23b_final_recharge_gate_blocks_on(monkeypatch, failure):
    ctrl, psu, load, _, _, _, _ = _b23b_final_ready()
    if failure == "checkpoint":
        def fail(_record):
            raise OSError("B23b checkpoint failure")
        ctrl.persist_tlc_completion = fail
    elif failure == "missing_checkpoint":
        ctrl.persist_tlc_completion = None
    elif failure == "restore":
        def fail_restore(**kwargs):
            ctrl._cell_bl_restore_confirmed = False
        monkeypatch.setattr(ctrl, "_restore_cell_bl_if_needed", fail_restore)
    elif failure == "seal":
        monkeypatch.setattr(ctrl._bq_transport, "seal", lambda **kwargs: None)
    elif failure == "late_seal_verify":
        original = ctrl._ensure_bq_sealed_or_flag
        def latest(context, **kwargs):
            return False if context == "FINAL_SNAPSHOT" else original(context, **kwargs)
        monkeypatch.setattr(ctrl, "_ensure_bq_sealed_or_flag", latest)
    else:
        monkeypatch.setattr(load, "read_input_state", lambda: True)
    ctrl.advance(0.1)
    assert ctrl.state == LearningState.FAULT
    assert not psu.called("output_on")
    assert load.called("input_off") and psu.called("all_outputs_off")
    assert ctrl.tlc_completion["final_recharge"]["status"] == "INCOMPLETE"
    assert ctrl.tlc_completion["final_recharge"]["completed_at_utc"] is None


@pytest.mark.parametrize("fault", ["stop", "dmm", "temperature", "overtemperature", "psu", "time", "ah", "off_readback"])
def test_b23b_final_charge_fault_keeps_learning_result(monkeypatch, fault):
    from copy import deepcopy

    ctrl, psu, load, _, temp, _, _ = _b23b_final_ready()
    ctrl.advance(0.1)
    for _ in range(3):
        ctrl.advance(0.1)
    assert psu.called("output_on"), "Final charge must actually be active before injection"
    evidence = deepcopy(ctrl.tlc_completion["learning_evidence"])
    if fault == "stop":
        ctrl.runner_abort_origin = "USER_STOP_REQUESTED"
        ctrl.emergency_stop("USER_STOP_REQUESTED")
    elif fault == "dmm":
        ctrl._dmm_v.set_dmm_invalid()
    elif fault == "temperature":
        temp.simulate_temp_failure = True
    elif fault == "overtemperature":
        temp.temperature_C = 60.0
    elif fault == "psu":
        psu.simulate_current_readback_timeout = True
    elif fault == "time":
        ctrl._charge_ctrl._config.max_charge_time_s = 0.0
    elif fault == "ah":
        ctrl._charge_ctrl._config.max_charge_Ah_factor = 0.0
    else:
        monkeypatch.setattr(psu, "read_output_state", lambda: True)
    for _ in range(20):
        if ctrl.state == LearningState.FAULT:
            break
        child = ctrl._charge_ctrl
        if child is not None and fault == "off_readback":
            ctrl._dmm_v.voltage_V = child._effective_charge_target_V()
            psu.current_A = 0.1
        ctrl.advance(1.0)
    assert ctrl.state == LearningState.FAULT
    assert ctrl.tlc_completion["learning_evidence"] == evidence
    assert ctrl.result_summary.result == LEARNING_RESULT_VERIFIED
    final = ctrl.tlc_completion["final_recharge"]
    assert final["status"] == "INCOMPLETE"
    assert final["completed_at_utc"] is None
    assert final["next_campaign_not_before_utc"] is None
    assert not load.input_commanded_on and not psu.output_commanded_on
    on_count = psu.calls.count("output_on()")
    ctrl.advance(1.0)
    assert psu.calls.count("output_on()") == on_count == 1


@pytest.mark.parametrize("mode", [TempCompMode.OFF, TempCompMode.MONITOR_ONLY, TempCompMode.ENABLED])
@pytest.mark.parametrize("temperature", [5.0, 20.0, 35.0, 50.0])
def test_b23b_q49_session_mode_accepts_precond_and_completes_final_recharge(mode, temperature):
    precond, _, _, _, temp, _ = make_controller()
    assert precond._safety.temp_comp_mode == TempCompMode.MONITOR_ONLY
    precond._safety.temp_comp_mode = mode
    temp.temperature_C = temperature
    precond.advance(0.0)
    assert precond.state == LearningState.PRECOND
    precond.advance(0.1)
    assert precond.state == LearningState.PRECHARGE, precond.fault_reason
    assert precond._temp_dmm_valid and precond.battery_temperature_C == temperature

    ctrl, psu, load, _, temp, _, checkpoints = _b23b_final_ready()
    ctrl._safety.temp_comp_mode = mode
    temp.temperature_C = temperature
    ctrl.advance(0.1)
    assert ctrl.state == LearningState.FINAL_RECHARGE
    for _ in range(3):
        ctrl.advance(0.1)
    assert psu.called("output_on"), ctrl.fault_reason
    assert ctrl._charge_ctrl.state == ChargeState.CHARGE_CC
    assert ctrl._charge_ctrl._safety.temp_comp_mode == mode
    target = ctrl._charge_ctrl._effective_charge_target_V()
    expected = 6 * min(2.4, 2.4 - 0.0025 * (temperature - 20.0))
    assert target == pytest.approx(expected)
    assert target <= expected + 1e-9
    assert ctrl._charge_ctrl.i_psu_set_A == pytest.approx(0.67)
    _b23b_finish_recharge(ctrl)
    assert ctrl._safety.temp_comp_mode == mode
    assert ctrl.tlc_completion["final_recharge"]["status"] == "COMPLETED"
    assert checkpoints[-1] == ctrl.tlc_completion
    assert not load.input_commanded_on and not psu.output_commanded_on


@pytest.mark.parametrize("mode", [TempCompMode.OFF, TempCompMode.MONITOR_ONLY, TempCompMode.ENABLED])
@pytest.mark.parametrize("phase", [LearningState.PRECOND, LearningState.FINAL_RECHARGE])
@pytest.mark.parametrize("failure", [
    "invalid_T", "nan_T", "inf_T", "negative_inf_T",
    "profile", "nan_profile", "inf_profile", "negative_inf_profile",
    "psu", "unknown_psu", "checkpoint",
])
def test_b23b_q49_final_charge_prerequisites_still_block_on(monkeypatch, mode, phase, failure):
    from Prog.src import charge_controller as charge_module

    if phase == LearningState.PRECOND:
        ctrl, psu, load, _, temp, _ = make_controller()
        ctrl.advance(0.0)
    else:
        ctrl, psu, load, _, temp, _, _ = _b23b_final_ready()
        ctrl.advance(0.1)
    assert ctrl.state == phase
    ctrl._safety.temp_comp_mode = mode
    if failure == "invalid_T":
        temp.simulate_temp_failure = True
        ctrl._temp_dmm_valid = False
    elif failure.endswith("_T"):
        value = {"nan_T": float("nan"), "inf_T": float("inf"), "negative_inf_T": -float("inf")}[failure]
        temp.temperature_C = ctrl._battery_temperature_C = value
    elif failure.endswith("profile"):
        ctrl._profile.max_charge_current_A = {
            "profile": 0.6699, "nan_profile": float("nan"),
            "inf_profile": float("inf"), "negative_inf_profile": -float("inf"),
        }[failure]
    elif failure == "psu":
        monkeypatch.setitem(charge_module._PSU_HW_MAX_CHARGE_A, PsuMode.INDEPENDENT, 0.6699)
    elif failure == "unknown_psu":
        monkeypatch.setattr(ctrl._safety, "psu_mode", "UNKNOWN")
    else:
        ctrl.persist_tlc_completion = None
    trace, events = [], []
    load_off, psu_off = load.input_off, psu.all_outputs_off

    def off_load():
        trace.append("LOAD_OFF")
        load_off()

    def off_psu():
        trace.append("PSU_ALL_OFF")
        psu_off()

    monkeypatch.setattr(load, "input_off", off_load)
    monkeypatch.setattr(psu, "all_outputs_off", off_psu)
    ctrl.on_event = events.append
    ctrl.advance(0.1)
    expected = (
        "TEMPERATURE_MONITOR_LOST_CRITICAL"
        if failure == "invalid_T" and phase == LearningState.PRECOND and mode == TempCompMode.ENABLED
        else "LEARNING_FINAL_RECHARGE_CONFIG_INVALID"
    )
    assert ctrl.state == LearningState.FAULT
    assert expected in ctrl.fault_reason
    assert trace[:2] == ["LOAD_OFF", "PSU_ALL_OFF"]
    assert not load.called("input_on") and not psu.called("output_on")
    assert not load.input_commanded_on and not psu.output_commanded_on
    assert any(e["event_code"] == "LEARNING_FAULT" and expected in e["event_message"] for e in events)
    before = (list(load.calls), list(psu.calls))
    ctrl.advance(1.0)
    assert (load.calls, psu.calls) == before


@pytest.mark.parametrize("temperature", [5.0, 20.0, 35.0, 50.0])
def test_b23b_final_child_reuses_b26a_target_and_absolute_fault(temperature):
    ctrl, psu, _, _, temp, _, _ = _b23b_final_ready()
    temp.temperature_C = temperature
    ctrl.advance(0.1)
    for _ in range(3):
        ctrl.advance(0.1)
    assert psu.called("output_on")
    target = ctrl._charge_ctrl._effective_charge_target_V()
    assert target == pytest.approx(6 * min(2.4, 2.4 - 0.0025 * (temperature - 20.0)))
    ctrl._dmm_v.voltage_V = 6 * 2.425 + 0.001
    ctrl.advance(0.1)
    assert ctrl.state == LearningState.FAULT
    assert "BATTERY_OVERVOLTAGE" in ctrl.fault_reason


def test_b23b_keeps_q48_legacy_cold_jeita_blocked():
    ctrl, psu, load, _, _, bq = make_controller()
    bq.df_fields["cell_charge_voltage_t1t2_mV"] = 2425
    ctrl.advance(0.0)
    ctrl.advance(0.1)
    assert ctrl.state == LearningState.FAULT
    assert "LEARNING_CONFIGURATION_MISMATCH" in ctrl.fault_reason
    assert not psu.called("output_on") and not load.called("input_on")


@pytest.mark.parametrize("vpc", [1.6999, 1.70, 1.73])
def test_b23b_q42_precond_to_precharge_uses_tlc_context(monkeypatch, vpc):
    ctrl, psu, load, _, temp, _ = make_controller(dmm_voltage_V=6 * vpc)
    trace = []
    load_off, psu_off = load.input_off, psu.all_outputs_off

    def off_load():
        trace.append("LOAD_OFF")
        load_off()

    def off_psu():
        trace.append("PSU_ALL_OFF")
        psu_off()

    monkeypatch.setattr(load, "input_off", off_load)
    monkeypatch.setattr(psu, "all_outputs_off", off_psu)
    ctrl.advance(0.0)
    assert ctrl.state == LearningState.PRECOND
    ctrl.advance(0.1)
    if vpc < 1.70:
        assert ctrl.state == LearningState.FAULT
        assert "DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED" in ctrl.fault_reason
        assert not psu.called("output_on") and not load.called("input_on")
        assert trace[:2] == ["LOAD_OFF", "PSU_ALL_OFF"]
        assert not psu.output_commanded_on and not load.input_commanded_on
        return
    assert ctrl.state == LearningState.PRECHARGE, ctrl.fault_reason
    assert ctrl._temp_dmm_valid and ctrl.battery_temperature_C == temp.temperature_C
    assert callable(ctrl.persist_tlc_completion)
    for _ in range(3):
        ctrl.advance(0.1)
    assert ctrl.state == LearningState.PRECHARGE, ctrl.fault_reason
    assert ctrl._charge_ctrl.state == ChargeState.CHARGE_CC
    assert psu.called("output_on") and psu.output_commanded_on
    assert not load.called("input_on") and not load.input_commanded_on


@pytest.mark.parametrize("mode", [TempCompMode.OFF, TempCompMode.MONITOR_ONLY, TempCompMode.ENABLED])
def test_b23b_q49_final_charge_keeps_session_temperature_loss_policy(mode):
    ctrl, psu, load, _, temp, _, _ = _b23b_final_ready()
    ctrl._safety.temp_comp_mode = mode
    ctrl.advance(0.1)
    for _ in range(3):
        ctrl.advance(0.1)
    assert ctrl._charge_ctrl.state == ChargeState.CHARGE_CC
    assert psu.called("output_on")
    temp.simulate_temp_failure = True
    ctrl.advance(1.0)
    if mode == TempCompMode.ENABLED:
        assert ctrl.state == LearningState.FAULT
    else:
        assert ctrl.state == LearningState.FINAL_RECHARGE, ctrl.fault_reason
        if mode == TempCompMode.MONITOR_ONLY:
            assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"
            ctrl.advance(58.0)
            assert ctrl.state == LearningState.FINAL_RECHARGE, ctrl.fault_reason
            assert ctrl._safety.temperature_dmm_fault_s == pytest.approx(59.0)
            ctrl.advance(1.0)
            assert ctrl.state == LearningState.FAULT
        else:
            ctrl.advance(60.0)
            assert ctrl.state == LearningState.FINAL_RECHARGE, ctrl.fault_reason
            assert ctrl.last_warning_code != "TEMPERATURE_DMM_LOST"
            ctrl.emergency_stop("Q49_TEST_END")
    if mode != TempCompMode.OFF:
        assert "TEMPERATURE_MONITOR_LOST_CRITICAL" in ctrl.fault_reason
    assert ctrl._safety.temp_comp_mode == mode
    assert not psu.output_commanded_on and not load.input_commanded_on


@pytest.mark.parametrize("awaiting", [False, True])
@pytest.mark.parametrize("device,failure", [
    ("Load", "read_error"), ("PSU", "read_error"),
    ("Load", "read_on"), ("PSU", "read_on"),
    ("Load", "command_error"), ("PSU", "command_error"), ("both", "command_error"),
])
def test_b23b_fix4_snapshot_off_failure_preserves_saved_evidence(monkeypatch, awaiting, device, failure):
    from copy import deepcopy

    from Prog.src import traditional_learning_controller as tlc_module

    control, *_ = _b23b_final_ready(awaiting=awaiting)
    control.advance(0.1)
    assert control.state == LearningState.FINAL_RECHARGE
    expected = control.tlc_completion["learning_evidence"]["result_summary"]
    assert expected["result"] == (LEARNING_RESULT_AWAITING if awaiting else LEARNING_RESULT_VERIFIED)

    ctrl, psu, load, _, _, bq, checkpoints = _b23b_final_ready(awaiting=awaiting)
    trace, triggered = [], []
    counts = {"Load": 0, "PSU": 0}
    load_off, psu_off = load.input_off, psu.all_outputs_off

    def off(name, original):
        trace.append(name + "_OFF")
        counts[name] += 1
        if failure == "command_error" and device in (name, "both") and counts[name] == 1:
            triggered.append(name)
            raise OSError(name + " one-shot OFF failure")
        original()

    monkeypatch.setattr(load, "input_off", lambda: off("Load", load_off))
    monkeypatch.setattr(psu, "all_outputs_off", lambda: off("PSU", psu_off))
    if failure != "command_error":
        target, method = (load, "read_input_state") if device == "Load" else (psu, "read_output_state")
        original_read = getattr(target, method)

        def read():
            if not triggered:
                assert ctrl.state == LearningState.FINAL_SNAPSHOT, "Injection must hit the snapshot tick"
                triggered.append(device)
                if failure == "read_error":
                    raise OSError(device + " one-shot readback failure")
                return True
            return original_read()

        monkeypatch.setattr(target, method, read)

    original_poll = tlc_module.poll_bq_diagnostics

    def snapshot(driver):
        trace.append("SNAPSHOT")
        return original_poll(driver)

    def persist(record):
        trace.append("PERSIST")
        checkpoints.append(record)

    monkeypatch.setattr(tlc_module, "poll_bq_diagnostics", snapshot)
    ctrl.persist_tlc_completion = persist
    ctrl.on_event = lambda event: trace.append(event["event_code"])
    ctrl.advance(0.1)

    assert sorted(triggered) == (["Load", "PSU"] if device == "both" else [device])
    assert ctrl.state == LearningState.FAULT
    assert "LEARNING_FINAL_RECHARGE_OFF_UNCONFIRMED" in ctrl.fault_reason
    token = "_OFF:" if failure == "command_error" else "_READBACK:" if failure == "read_error" else "_OFF_UNCONFIRMED:"
    for name in triggered:
        assert name + token in ctrl.fault_reason
    assert ctrl._final_snapshot is not None
    assert ctrl._qmax_final == bq.qmax == 150
    assert ctrl._ra_final == tuple(bq.ra_table)
    record = ctrl.tlc_completion
    evidence = deepcopy(record["learning_evidence"])
    assert evidence is not None
    assert evidence["result_summary"] == expected
    assert ctrl.result_summary == control.result_summary
    assert evidence["final_snapshot"]["qmax"]["value"] == 150
    assert trace[:3] == ["Load_OFF", "PSU_OFF", "SNAPSHOT"]
    assert trace.index("SNAPSHOT") < trace.index("PERSIST") < trace.index("LEARNING_FAULT")
    assert len(checkpoints) == 2
    assert checkpoints[0]["learning_evidence"] == checkpoints[-1]["learning_evidence"] == evidence
    assert checkpoints[0]["final_recharge"]["status"] == "NOT_STARTED"
    assert checkpoints[-1] == record
    assert record["final_recharge"]["status"] == "INCOMPLETE"
    assert record["final_recharge"]["started_at_utc"] is None
    assert record["final_recharge"]["completed_at_utc"] is None
    assert record["final_recharge"]["next_campaign_not_before_utc"] is None
    assert "LEARNING_FINAL_RECHARGE_STARTED" not in trace and "LEARNING_DONE" not in trace
    assert not psu.called("output_on") and not load.called("input_on")
    assert not psu.output_commanded_on and not load.input_commanded_on
    before = (list(load.calls), list(psu.calls), list(bq.calls), len(checkpoints))
    for _ in range(2):
        ctrl.advance(1.0)
    assert (load.calls, psu.calls, bq.calls, len(checkpoints)) == before
    assert ctrl.tlc_completion["learning_evidence"] == evidence


@pytest.mark.parametrize("device", ["Load", "PSU"])
def test_b23b_fix4_tlc_child_rechecks_off_after_parent_gate(monkeypatch, device):
    from copy import deepcopy

    ctrl, psu, load, _, _, _, checkpoints = _b23b_final_ready()
    ctrl.advance(0.1)
    assert ctrl.state == LearningState.FINAL_RECHARGE
    evidence = deepcopy(ctrl.tlc_completion["learning_evidence"])
    confirmed = []
    original_confirm = ctrl._confirm_final_charge_off

    def confirm(**kwargs):
        result = original_confirm(**kwargs)
        confirmed.append(result)
        return result

    monkeypatch.setattr(ctrl, "_confirm_final_charge_off", confirm)
    for _ in range(3):
        ctrl.advance(0.1)
        if ctrl._charge_ctrl is not None and ctrl._charge_ctrl.state == ChargeState.PSU_PRESET:
            break
    assert ctrl.state == LearningState.FINAL_RECHARGE, ctrl.fault_reason
    assert ctrl._charge_ctrl is not None and ctrl._charge_ctrl.state == ChargeState.PSU_PRESET
    assert confirmed == [{"Load": False, "PSU": False}], "The TLC gate must pass before injection"
    assert checkpoints[-1]["final_recharge"]["status"] == "RUNNING"
    assert not psu.called("output_on") and not load.called("input_on")
    assert not psu.output_commanded_on and not load.input_commanded_on
    triggered, trace = [], []
    target, method = (load, "read_input_state") if device == "Load" else (psu, "read_output_state")

    def read_on():
        assert ctrl._charge_ctrl.state == ChargeState.PSU_PRESET
        triggered.append(device)
        return True

    load_off, psu_off = load.input_off, psu.all_outputs_off

    def off_load():
        trace.append("Load_OFF")
        load_off()

    def off_psu():
        trace.append("PSU_OFF")
        psu_off()

    monkeypatch.setattr(target, method, read_on)
    monkeypatch.setattr(load, "input_off", off_load)
    monkeypatch.setattr(psu, "all_outputs_off", off_psu)
    ctrl.advance(0.1)
    assert triggered == [device], "The child must read the changed hardware state"
    assert ctrl.state == LearningState.FAULT
    assert "LEARNING_CHARGE_OFF_UNCONFIRMED" in ctrl.fault_reason
    assert trace[:2] == ["Load_OFF", "PSU_OFF"]
    assert not psu.called("output_on") and not load.called("input_on")
    assert not psu.output_commanded_on and not load.input_commanded_on
    assert ctrl.tlc_completion["learning_evidence"] == evidence
    assert ctrl.result_summary.result == LEARNING_RESULT_VERIFIED
    assert checkpoints[-1] == ctrl.tlc_completion
    assert checkpoints[-1]["final_recharge"]["status"] == "INCOMPLETE"
