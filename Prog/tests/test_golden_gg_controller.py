"""
GoldenGgController tesztek — WP-AK-11 (SUP03-GG-001..010).

Ugyanaz a minta, mint a `test_traditional_learning_controller.py`-ban: a
BQ34Z110 oldalt `PicoBQTransport` + `FakePicoSerial`-lel szimuláljuk, a
fizikai oldalt (PSU/Load/DMM) a projekt meglévő MockPSU/MockLoad/MockDMM
fixture-ei, a lassú ChargeController-t közvetlen állapot-befecskendezéssel
siettetjük át.
"""
from __future__ import annotations

import pytest

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.battery_profile import BatteryProfile
from Prog.src.charge_controller import ChargeConfig, ChargeState
from Prog.src.golden_gg_controller import (
    GoldenGgController,
    GoldenGgPhysicalConfig,
    GoldenGgState,
)
from Prog.src.golden_gg_export import GoldenGgConfig
from Prog.src.safety import PsuMode, SafetyManager, TempCompMode
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial
from Prog.tests.mock_drivers.fast_loop_charge import (
    DT_S,
    EXTRA_S,
    PERIOD_S,
    SUBSTEPS_PER_TICK,
    ScriptedChargeReadings,
    assert_parent_delta_equals_independent_expectation,
    cv_currents,
    expected_delta,
    fast_loop_charge_config,
    force_child_state,
    install_fast_loop_clock,
    run_tick,
    taper_currents,
    varying_voltages,
)
from Prog.tests.mock_drivers.mock_dmm import MockDMM
from Prog.tests.mock_drivers.mock_load import MockLoad
from Prog.tests.mock_drivers.mock_psu import MockPSU


class _FakeBqState:
    def __init__(self) -> None:
        self.chem_id = 0x0800
        self.qmax = 150
        self.ra_table = [10.0 + i for i in range(15)]
        # [SCF-011 újratervezés, 2026-09-11] a chip valós SEALED alapállapotát
        # modellezi (.claude/rules/40-instruments.md: "Mock modellezze a
        # sessionön át megmaradó hardverállapotot is") -- GoldenGgController.
        # _run_precond() mostantól unseal()-t hív a Qmax/Ra forrásolvasás
        # ELŐTT (BQ-DF-SEALED-001), a session-végi seal()-t pedig
        # _ensure_bq_sealed_or_flag() (l. golden_gg_controller.py).
        self.sealed = True

    def control_status_word(self) -> int:
        return 0x2000 if self.sealed else 0x0000

    def state_block(self) -> bytes:
        block = bytearray(32)
        block[0:2] = self.qmax.to_bytes(2, "big")
        block[4] = 0x06  # update_status
        return bytes(block)

    def ra_block(self) -> bytes:
        block = bytearray([0xFF, 0x55])
        for v in self.ra_table:
            block += int(v).to_bytes(2, "big", signed=True)
        return bytes(block)


def _data_flash_handler(fake: _FakeBqState):
    def _handler(params):
        subclass, block = params["subclass"], params["block"]
        if subclass == 82 and block == 0:
            return fake.state_block().hex()
        if subclass == 88 and block == 0:
            return fake.ra_block().hex()
        # Független 12 V / C10=7 Ah tesztprofil, SLUSB55B Table 20.
        # SC82/88 fent változatlan: a tanult Qmax nem a stabil kapu része.
        data = bytearray(32)
        for sc, blk, offset, raw in [
            (48, 0, 6, "07d0"), (48, 0, 21, "1b58"), (48, 0, 23, "20d0"),
            (48, 0, 28, "0960"), (48, 0, 30, "0960"), (48, 1, 0, "0915"),  # Q-44: 2400 mV.
            (48, 1, 13, "0a"), (36, 0, 0, "00d2"), (64, 0, 0, "0961"),
            (64, 0, 7, "06"), (80, 0, 0, "0100"), (80, 2, 3, "0708"),
        ]:
            if (subclass, block) == (sc, blk):
                value = bytes.fromhex(raw)
                data[offset:offset + len(value)] = value
        return bytes(data).hex()
    return _handler


def _make_transport(fake: _FakeBqState, data_flash_error: bool = False) -> PicoBQTransport:
    def _data_flash(params):
        if data_flash_error:
            raise RuntimeError("simulated data flash read failure")
        return _data_flash_handler(fake)(params)

    def _unseal(params):
        fake.sealed = False

    def _seal(params):
        fake.sealed = True

    results = {
        "chem_id": lambda params: fake.chem_id,
        "control_status": lambda params: fake.control_status_word(),
        "unseal": _unseal,
        "full_access": _unseal,
        "seal": _seal,
        "flags": lambda params: 0x0000,
        "cycle_count": lambda params: 3,
        "dod0": lambda params: 0,
        "device_type": lambda params: 0x0541,
        "firmware_version": lambda params: 0x0311,
        "hardware_version": lambda params: 0x0003,
        "voltage_mV": lambda params: 12500,
        "average_current_mA": lambda params: 0,
        "temperature_01K": lambda params: 2951,
        "soc_percent": lambda params: 50,
        "remaining_capacity_mAh": lambda params: 3500,
        "full_charge_capacity_mAh": lambda params: 7000,
        "data_flash_block": _data_flash,
    }
    return PicoBQTransport(serial=FakePicoSerial(results=results))


def _make_profile(capacity_Ah: float = 7.0) -> BatteryProfile:
    return BatteryProfile(
        battery_name="TEST", manufacturer="TEST", model="TST7AH",
        nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=capacity_Ah,
        C10_capacity_Ah=capacity_Ah,
        terminate_voltage_per_cell_V=1.80,
    )


def make_controller(
    dmm_voltage_V: float = 13.0,
    capacity_Ah: float = 7.0,
    config: GoldenGgPhysicalConfig | None = None,
    fake_bq: _FakeBqState | None = None,
    expected_chem_id: int | None = None,
    export_output_dir=None,
    export_config: GoldenGgConfig | None = None,
    data_flash_error: bool = False,
    charge_config=None,
    temp_comp_mode: TempCompMode = TempCompMode.MONITOR_ONLY,
):
    psu = MockPSU()
    load = MockLoad(voltage_V=dmm_voltage_V)
    dmm_v = MockDMM(voltage_V=dmm_voltage_V)
    dmm_t = MockDMM(temperature_C=22.0)
    profile = _make_profile(capacity_Ah)
    safety = SafetyManager(
        profile=profile, psu_mode=PsuMode.INDEPENDENT, temp_comp_mode=temp_comp_mode,
    )
    fake_bq = fake_bq or _FakeBqState()
    transport = _make_transport(fake_bq, data_flash_error=data_flash_error)
    cfg = config or GoldenGgPhysicalConfig(
        relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
        system_rate_current_A=2.0, sample_interval_s=5.0,
    )
    ctrl = GoldenGgController(
        psu, load, dmm_v, dmm_t, transport, profile, safety, cfg,
        expected_chem_id=expected_chem_id,
        export_output_dir=export_output_dir,
        export_config=export_config,
        charge_config=charge_config,
    )
    return ctrl, psu, load, dmm_v, dmm_t, fake_bq


def _force_charge_done(ctrl) -> None:
    if ctrl._charge_ctrl is None:
        ctrl.advance(0.0)
    assert ctrl._charge_ctrl is not None
    ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
    # Teljes kódbázis review 5. kör (2026-08-29), Metrológia/Adatintegritás
    # P1 lelet nyomán: a controller mostantól ctrl._charge_ctrl.
    # i_psu_measured_A-t olvassa a session-mintákhoz (nem egy redundáns,
    # önálló PSU-olvasást) — ez a shortcut a CC/CV/taper progressziót
    # kihagyva ugorja CHARGE_DONE-ba, tehát _i_charge sosem íródott a
    # ChargeController belső, természetes advance()-tick-jei által. A
    # hívó jellemzően psu.current_A-t állítja be a "töltési minta" előtt —
    # ezt vesszük át ide, hogy a forszírozott állapot a mért mintaáramot
    # is konzisztensen tükrözze.
    ctrl._charge_ctrl._i_charge = ctrl._psu.current_A
    # Teljes kódbázis review 2. kör (2026-09-15, R2-18, 12. writer-kör):
    # a fenti _i_charge shortcut MELLETT a validity-flaget is be kell
    # állítani -- ez a shortcut egy VALÓS, sikeres tick-olvasást szimulál
    # (a hívó a psu.current_A-t "töltési minta"-ként adja meg), tehát a
    # _psu_current_readback_valid=True konzisztens a forszírozott
    # állapottal, ugyanúgy, mint egy valódi, sikeres _read_psu_current()
    # hívás után. FONTOS: a ChargeController._advance_single() a SAJÁT
    # legelső sorában MINDIG None-ra reseteli ezt a flaget (tick-scoped
    # minta, C-08a F-1) -- ha a hívó ezután egy VALÓDI ctrl._charge_ctrl.
    # advance()-öt hívna (pl. a GoldenGgController._run_full_charge()-on
    # keresztül), a fenti forszírozott True-t azonnal felülírná None-ra,
    # MÉG MIELŐTT bármi mást olvasna -- ez egy a valós rendszerben SOSEM
    # előforduló forgatókönyvet szimulálna (egy MÁR terminális
    # ChargeController-t MÉG EGYSZER advance()-elni új olvasás nélkül).
    # A shortcut ezért lecseréli az advance()-et egy stub-ra, ami a
    # CHARGE_DONE állapotot adja vissza a fenti, kézzel beállított mezők
    # ÉRINTÉSE NÉLKÜL -- pontosan azt a VALÓDI forgatókönyvet tükrözve,
    # amikor a CHARGE_DONE-ba lépés UGYANAZON a ticken történik, mint az
    # utolsó sikeres PSU-áram-olvasás.
    ctrl._charge_ctrl._psu_current_readback_valid = True
    ctrl._charge_ctrl.advance = lambda dt_s: ChargeState.CHARGE_DONE


def _advance_until(ctrl, target_state, dt_s: float = 1.0, max_ticks: int = 20) -> None:
    """A RelaxController INIT-tickje sosem zár le relax-ot azonnal (l.
    relax_controller.py: az első hívás csak RELAXING-be lép, függetlenül a
    már eltelt idő mennyiségétől), ezért a relax-fázisokon több `advance()`
    hívás szükséges — ez a segéd addig ismétel, amíg a célállapotot (vagy
    FAULT-ot) el nem éri."""
    for _ in range(max_ticks):
        if ctrl.state in (target_state, GoldenGgState.FAULT):
            return
        ctrl.advance(dt_s)


class TestKor20BatchE13aSystemRateDischargePredictiveGate:
    def test_begin_system_rate_discharge_faults_on_concurrent_psu_load_before_energizing(self):
        """[Kör 20 Batch E13a, F20-S3, DFAM-001] `_begin_system_rate_
        discharge()` korábban a `try:` blokk BELSEJÉBEN, feltétel nélkül
        futtatta `psu.all_outputs_off()`-t, majd `load.input_on()`-t --
        egy stale `psu.output_commanded_on=True` flaget az `all_outputs_
        off()` CSENDBEN letörölt volna, MIELŐTT bármi észlelhetné a
        konfliktust. Ez a teszt a fix ELŐTTI kódon bukna: `ctrl.state`
        NEM lenne FAULT, és `load.input_commanded_on` True lenne."""
        ctrl, psu, load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        psu.output_commanded_on = True

        ctrl._u_batt = ctrl._dmm_v.voltage_V  # R1 B3b: elozo ervenyes DMM-olvasas
        ctrl._begin_system_rate_discharge()

        assert ctrl.state == GoldenGgState.FAULT
        assert "CONCURRENT_PSU_LOAD" in ctrl.fault_reason
        assert load.input_commanded_on is False

    def test_begin_system_rate_discharge_dmm_invalid_faults_before_energizing(self):
        """[regresszió, Claude-Codex loop R1 B3, F-02, DFAM-001] a
        `_begin_system_rate_discharge()` korábban ismerten hibás DMM-
        olvasás mellett is Load INPUT ON-t adott -- a per-tick DMM-kapu
        csak a KÖVETKEZŐ ticken futott."""
        ctrl, psu, load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl._dmm_valid = False

        ctrl._begin_system_rate_discharge()

        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls
        assert not any(c.startswith("set_current") for c in load.calls)
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls

    def test_relax_2h_end_with_invalid_dmm_faults_instead_of_system_rate_discharge(self):
        """[regresszió, R1 B3, F-02] lánc: a RELAX_2H vég tickjén hibás
        DMM-olvasás -> `_begin_system_rate_discharge()` DMM-őre FAULT-ot
        ad energizálás nélkül; a session-végi seal lefut."""
        fake_bq = _FakeBqState()
        ctrl, _psu, load, dmm_v, _dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND (unseal) -> FULL_CHARGE
        assert fake_bq.sealed is False
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.RELAX_2H, dt_s=0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H
        ctrl.advance(0.1)  # RelaxController INIT
        assert ctrl.state == GoldenGgState.RELAX_2H

        load.reset_calls()
        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)  # relax min. idő letelt, DE a DMM EZEN a ticken hibás

        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls
        assert load.input_commanded_on is False
        assert fake_bq.sealed is True, "a chipnek visszazárva kell lennie FAULT-nál"


class TestPrecondAndCharge:
    def test_starts_in_init_then_precond(self):
        ctrl, *_ = make_controller()
        assert ctrl.state == GoldenGgState.INIT
        ctrl.advance(0.0)
        assert ctrl.state == GoldenGgState.PRECOND

    def test_precond_to_full_charge_on_success(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE

    def test_precond_captures_gg_source_snapshot(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.gg_source_snapshot is not None
        assert ctrl.gg_source_snapshot.qmax.valid is True
        assert ctrl.gg_source_snapshot.qmax.value == 150

    def test_precond_gg_source_snapshot_emits_on_gauge_sample(self):
        """Kor 18, Batch C7 R17-16 (DFAM-021+DFAM-001): a PRECOND gg-forras
        snapshot DONTO (verdikt-kritikus), de korabban KIZAROLAG a
        controller belso mezojebe (gg_source_snapshot) kerult -- sem az
        elo BQ-diagnosztika nezetbe, sem a bq_diagnostics.csv-be nem
        jutott el. Most mar on_gauge_sample-en keresztul is emittal."""
        ctrl, *_ = make_controller()
        captured = []
        ctrl.on_gauge_sample = captured.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert len(captured) == 1
        assert captured[0] is ctrl.gg_source_snapshot

    def test_precond_emit_exception_does_not_abort_precond(self):
        """Best-effort: egy on_gauge_sample-kivetel nem szakithatja meg a
        PRECOND allapotatmenetet (DFAM-033 -- egy GUI-callback hibaja nem
        maszkolhatja/blokkolhatja a valodi BQ-tranzakciot)."""
        ctrl, *_ = make_controller()

        def _raise(_snapshot):
            raise RuntimeError("simulated GUI callback failure")
        ctrl.on_gauge_sample = _raise
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert ctrl.gg_source_snapshot is not None

    def test_precond_no_subscriber_is_safe(self):
        ctrl, *_ = make_controller()
        assert ctrl.on_gauge_sample is None
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE

    def test_precond_faults_on_chem_id_mismatch(self):
        ctrl, *_ = make_controller(expected_chem_id=0x1234)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert "CHEM_ID_MISMATCH" in ctrl.fault_reason

    def test_precond_faults_on_dmm_lost(self):
        ctrl, *_ = make_controller()
        ctrl._dmm_v.simulate_timeout = True
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT

    def test_dmm_valid_property_reflects_actual_dmm_read_result(self):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), Session/
        Workflow P2 lelet #4] a ChargeController már exponálja a dmm_valid
        property-t, ezt test_runner.py::_build_sample() a dmm_voltage_valid
        mezőhöz olvassa — a GoldenGgController hiányzott, emiatt
        getattr(controller, "dmm_valid", None) mindig None-t adott, és a
        fallback (`u_batt is not None`) gyakorlatilag MINDIG True-t írt,
        akkor is, ha a DMM-olvasás ténylegesen hibázott."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND, DMM-olvasás
        assert ctrl.dmm_valid is True

        dmm_v.dmm_valid = False
        ctrl.advance(1.0)  # a következő DMM-olvasás hibázik
        assert ctrl.dmm_valid is False
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason

    def test_precond_faults_on_bq_comm_unavailable(self):
        ctrl, *_ = make_controller(data_flash_error=False)

        def _raise_chem_id(params):
            raise RuntimeError("comm lost")

        # chem_id read maga is BQ-hívás — szimuláljuk a teljes transport hibáját.
        ctrl._bq_transport.serial.results["chem_id"] = _raise_chem_id
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert "BQ_COMM_UNAVAILABLE" in ctrl.fault_reason

    def test_charge_done_transitions_to_termination_verified(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.CHARGE_TERMINATION_VERIFIED
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

    def test_relax_tick_reads_voltage_dmm_only_once(self):
        """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet #2] Korábban egy RELAX-tick 2x olvasta a
        feszültség-DMM-et (top-level _read_dmm() + a RelaxController saját
        olvasása) — a top-level advance() mostantól _read_temperature_only()-t
        hív a RELAX-alállapotokra (CSAK a hőmérséklet-DMM-et olvassa), a
        feszültség-DMM-et KIZÁRÓLAG a RelaxController olvassa (CLAUDE.md §9).
        L. test_relax_dmm_valid_reflects_relax_ctrl_read_outcome a
        self._dmm_valid szinkronizálás igazolásához."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

        dmm_v.calls.clear()
        ctrl.advance(1.0)
        assert dmm_v.calls.count("read_voltage()") == 1

    def test_relax_u_batt_matches_relax_ctrl_last_voltage(self):
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

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
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H
        assert ctrl.dmm_valid is True

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)
        assert ctrl.dmm_valid is False

    def test_relax_first_tick_dmm_failure_does_not_overwrite_u_batt_with_fabricated_zero(self):
        """[regresszió, Kör 17 MAX-effort review, R17-11a, DFAM-024 2.
        előfordulás, l. traditional_learning_controller.py azonos tesztje
        ugyanerről a mintáról] RelaxController.last_voltage_V az ELSŐ
        (INIT-ágú) sikertelen olvasás ELŐTT egy fabrikált 0.0 V-ot ad
        vissza -- korábban ezt FELTÉTEL NÉLKÜL írtuk self._u_batt-ba,
        felülírva a töltési fázisból származó, VALÓS mért értéket."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H
        assert ctrl._relax_ctrl is None  # meg nem konstrualt -- a kovetkezo tick hozza letre
        u_batt_before_relax = ctrl._u_batt
        assert u_batt_before_relax != 0.0

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)  # RelaxController INIT-agu, sikertelen elso olvasasa
        assert ctrl._relax_ctrl is not None
        assert ctrl._relax_ctrl.last_voltage_V == 0.0  # a RelaxController fabrikalt init-erteke
        assert ctrl.dmm_valid is False
        assert ctrl._u_batt == pytest.approx(u_batt_before_relax)

    def test_relax_dmm_fault_surfaces_in_relax_dmm_fault_total_before_finalize(self):
        """[regresszió, review-kör 2 (2026-09-09), friss Szoftver/Architektúra
        F1 lelet] A relax_dmm_fault_total property korábban KIZÁRÓLAG a
        _run_relax() RELAX_DONE ágában carry-overölt _relax_dmm_fault_total-t
        adta vissza -- egy emergency_stop() a relax fázis KÖZBEN (mielőtt a
        relax természetesen lezárna) nem hívta ezt a carry-overt, tehát a
        self._relax_ctrl élő dmm_fault_count-ja nyomtalanul elveszett volna
        a report.json-ból. A testvér traditional_learning_controller.py-n ezt
        a rést a CF-05 2. MAX verifier P3-1 lelete már javította; ez a
        golden_gg_controller.py-s példány maradt ki abból a körből."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H
        assert ctrl.relax_dmm_fault_total == 0

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)  # RELAXING tick, DMM-hiba a RelaxControllerben
        dmm_v.simulate_timeout = False
        assert ctrl.state == GoldenGgState.RELAX_2H  # meg NEM finalizalt
        assert ctrl.relax_dmm_fault_total == 1  # elo tag

        ctrl.emergency_stop("MID_RELAX_TEST")
        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.relax_dmm_fault_total == 1  # a live-tag megszakitas utan is latszik

    def test_relax_dmm_fault_reaches_controller_on_event(self):
        """R2-12 (2026-09-15, DFAM-011): a beágyazott RelaxController
        on_event-je korábban SEHOL nem volt bekötve -- a relax alatti
        DMM-kiesés a session_dmm_fault_total-on kívül csak a tranziens
        live event-feedben lett volna láthatatlan, mert az esemény sosem
        jutott el a külső controller._emit()-jéig."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        events = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)
        dmm_v.simulate_timeout = False

        assert ctrl.relax_dmm_fault_total == 1
        assert any(e.get("event_code") == "RELAX_DMM_FAULT" for e in events)

    def test_forwarded_relax_event_carries_relax_substate_context(self):
        """Egy bare 'RELAX_DMM_FAULT' üzenet önmagában megkülönböztethetetlen
        lenne a RELAX_2H és RELAX_5H fázis között -- az _emit_relax_event()
        a jelenlegi self._state.value-t fűzi az üzenet elé."""
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller()
        events = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)
        dmm_v.simulate_timeout = False

        relax_events = [e for e in events if e.get("event_code") == "RELAX_DMM_FAULT"]
        assert relax_events, "nem talalhato tovabbitott RELAX_DMM_FAULT esemeny"
        assert "RELAX_2H" in relax_events[0]["event_message"]

    def test_conditioning_required_delays_full_charge(self):
        """SUP03-GG-005 lépés 1: opcionális room-temperature conditioning
        a töltés ELŐTT, ha `conditioning_required=True`."""
        ctrl, *_ = make_controller(
            config=GoldenGgPhysicalConfig(
                conditioning_required=True, conditioning_min_s=2.0,
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> CONDITIONING
        assert ctrl.state == GoldenGgState.CONDITIONING
        ctrl.advance(1.0)  # min idő még nem telt le
        assert ctrl.state == GoldenGgState.CONDITIONING
        ctrl.advance(1.5)  # min idő letelt
        assert ctrl.state == GoldenGgState.FULL_CHARGE

    def test_samples_recorded_during_charge_positive_current(self):
        """SUP03-GG-005/006: a napló a töltés kezdetétől folyamatos."""
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        ctrl.advance(1.0)
        assert len(ctrl.samples) > 0
        assert all(s.current_A >= 0 for s in ctrl.samples)


# ------------------------------------------------------------------ #
# TestGoldenGgPhaseElapsedS                                            #
# [CF-19, governance-v2 kör 2, DFAM-023] l. test_ocv_soc_controller.py #
# azonos hivatkozású teszt-osztálya. A GoldenGgController _enter()     #
# helpere korábban NEM reseteltte a phase_elapsed_s-t (degenerált      #
# átmenet-hook) -- ez a teszt pontosan ezt a hiányt fedi.               #
# ------------------------------------------------------------------ #

class TestGoldenGgPhaseElapsedS:
    def test_starts_at_zero(self):
        ctrl, *_ = make_controller()
        assert ctrl.phase_elapsed_s == 0.0

    def test_grows_while_state_unchanged(self):
        import pytest
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> FULL_CHARGE
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        ctrl.advance(5.0)
        ctrl.advance(3.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert ctrl.phase_elapsed_s == pytest.approx(8.0)

    def test_resets_on_state_transition(self):
        import pytest
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> FULL_CHARGE
        ctrl.advance(100.0)
        assert ctrl.phase_elapsed_s == pytest.approx(100.0)
        ctrl._charge_ctrl._state = ChargeState.CHARGE_DONE
        ctrl.advance(1.0)  # FULL_CHARGE -> CHARGE_TERMINATION_VERIFIED
        assert ctrl.state == GoldenGgState.CHARGE_TERMINATION_VERIFIED
        assert ctrl.phase_elapsed_s == pytest.approx(0.0)


class TestFullSequenceToExport:
    def _run_relax_to_completion(self, ctrl, dt_s: float = 2.0, max_ticks: int = 5):
        for _ in range(max_ticks):
            if ctrl.state not in (GoldenGgState.RELAX_2H, GoldenGgState.RELAX_5H):
                return
            ctrl.advance(dt_s)

    def test_full_sequence_reaches_done_with_export(self, tmp_path):
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> FULL_CHARGE
        assert ctrl.state == GoldenGgState.FULL_CHARGE

        psu.current_A = 2.5  # SUP03-GG-005/006: töltési minta pozitív előjellel
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.CHARGE_TERMINATION_VERIFIED
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

        self._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE
        assert load.input_commanded_on is True

        dmm_v.voltage_V = 9.0  # 6 * 1.75 = 10.5V alatt
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.RELAX_5H

        self._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE
        assert ctrl.package_ready_for_ti_submission is True
        assert ctrl.export_zip_path is not None
        assert (tmp_path / "golden_gg_submission.zip").exists()

        # A napló tartalmaz töltési (pozitív) ÉS kisütési (negatív) mintát is.
        assert any(s.current_A > 0 for s in ctrl.samples)
        assert any(s.current_A < 0 for s in ctrl.samples)

    def test_export_flags_relax_dmm_fault_in_metadata_limitations(self, tmp_path):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] a
        relax alatti DMM-kiesés (RelaxController.dmm_fault_count) korábban
        a _run_relax() fázisváltáskor eldobódott (self._relax_ctrl = None),
        sosem jutott el a TI-nek exportált csomag metadata_limitations.
        json-jába."""
        import json
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        self._run_relax_to_completion(ctrl)
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        self._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE

        ctrl._relax_dmm_fault_total = 2

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE

        limitations = json.loads(
            (tmp_path / "metadata_limitations.json").read_text(encoding="utf-8")
        )
        assert any("Feszültség-DMM kiesés" in f for f in limitations["missing_fields"])
        # [Kör 19 Batch D3b, F-2 MAX verifier lelet] a régi, összemosott
        # "Hőmérséklet-/feszültség-DMM" szöveg NEM adhat hamis lefedettségi
        # igényt egy tisztán feszültség-DMM hibánál (temp_dmm_fault_ticks=0).
        assert not any("Hőmérséklet-DMM kiesés" in f for f in limitations["missing_fields"])

    def test_export_flags_temperature_dmm_fault_in_metadata_limitations(self, tmp_path):
        """[regresszió, Kör 19 Batch D3b, F-2] a `temp_dmm_fault_ticks`
        korábban NEM jutott el a TI export limitáció-listájába -- csak a
        report.json-ba/DEGRADED-kapuba -- holott a gpcchem_export.py/
        golden_gg_export.py `cell_surface_temperature_C` oszlopa
        pontosan ettől a hibától lehet stale, jelzés nélkül."""
        import json
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        self._run_relax_to_completion(ctrl)
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        self._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE

        ctrl._temp_dmm_fault_ticks = 7

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE

        limitations = json.loads(
            (tmp_path / "metadata_limitations.json").read_text(encoding="utf-8")
        )
        assert any(
            "Hőmérséklet-DMM kiesés (7 " in f and "cell_surface_temperature_C" in f
            for f in limitations["missing_fields"]
        ), "hianyzik a hőmérséklet-DMM limitáció (vagy hibás a szám), jóllehet temp_dmm_fault_ticks=7"
        assert not any("Feszültség-DMM kiesés" in f for f in limitations["missing_fields"])

    def test_export_flags_both_voltage_and_temperature_dmm_faults_independently(
        self, tmp_path
    ):
        """[MAX verifier P3-1 lelete, Kör 19 Batch D3b] a két limitáció
        FÜGGETLEN if-ágból ered -- ez a teszt bizonyítja, hogy EGYÜTTES
        előfordulásnál MINDKETTŐ megjelenik, nem csak az egyik."""
        import json
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        self._run_relax_to_completion(ctrl)
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        self._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE

        ctrl._relax_dmm_fault_total = 2
        ctrl._temp_dmm_fault_ticks = 7

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE

        limitations = json.loads(
            (tmp_path / "metadata_limitations.json").read_text(encoding="utf-8")
        )
        assert any(
            "Feszültség-DMM kiesés" in f and "(2 érintett tick)" in f
            for f in limitations["missing_fields"]
        )
        assert any(
            "Hőmérséklet-DMM kiesés (7 " in f for f in limitations["missing_fields"]
        )

    def test_export_flags_temperature_never_measured_under_temp_comp_off(self, tmp_path):
        """[regresszió, Kör 19 Batch D3c, F-3 rész (a)] TempCompMode.OFF
        mellett safety.py::check_temperature_dmm_fault() FELTÉTEL NÉLKÜL
        üres SafetyResult-ot ad -- egy session-kezdettől halott hőmérséklet-
        DMM-nél SOHA nem lesz FAULT/WARNING, SOHA nem sikerül egy olvasás
        sem, tehát a `temp_dmm_fault_ticks > 0` kapu (a fenti teszt) is
        CSENDBEN marad (a hiba nem "tick"-ekként, hanem állandó kezdeti
        állapotként jelentkezik). Enélkül a fix nélkül a `cell_surface_
        temperature_C` oszlop MINDEN sora a fabrikált 20.00°C konstrukciós
        alapértéket hordozná, jelzés nélkül a TI-submission csomagban."""
        import json
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, _load, dmm_v, dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
            temp_comp_mode=TempCompMode.OFF,
        )
        dmm_t.simulate_temp_failure = True

        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        self._run_relax_to_completion(ctrl)
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        self._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE
        assert ctrl._temp_dmm_ever_read_ok is False, (
            "a temp DMM sosem olvasott sikeresen a teszt teljes futása alatt"
        )

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE

        limitations = json.loads(
            (tmp_path / "metadata_limitations.json").read_text(encoding="utf-8")
        )
        assert any(
            "SOHA nem olvasott sikeresen" in f and "cell_surface_temperature_C" in f
            for f in limitations["missing_fields"]
        ), "hianyzik a 'sosem mert homerseklet' limitacio TempCompMode.OFF alatt"
        # MEGJEGYZÉS: a `temp_dmm_fault_ticks > 0` (D3b) kapu ETTŐL
        # FÜGGETLENÜL is tüzelhet ugyanekkor (a tick-alapú számláló minden
        # sikertelen olvasási KÍSÉRLETNÉL nő, a TempCompMode.OFF csak a
        # safety FAULT/WARNING-eszkalációt tiltja le, nem magát a
        # számlálást) -- ez NEM ellentmondás, a két limitáció más-más
        # kérdésre válaszol (hányszor hibázott vs. történt-e valaha
        # sikeres olvasás), és mindkettő EGYSZERRE igaz lehet. [MAX
        # verifier F-3 lelete, Kör 19 D3c] a fenti komment állítását itt
        # explicit ellenőrizzük, nem csak dokumentáljuk:
        assert any(
            "Hőmérséklet-DMM kiesés (" in f for f in limitations["missing_fields"]
        ), "a D3b tick-alapú limitációnak IS jelen kell lennie, nem csak a D3c-nek"

    def test_export_flags_temperature_ever_read_ok_survives_full_charge_carry_over(
        self, tmp_path
    ):
        """[MAX verifier F-1 lelete, Kör 19 Batch D3c] a fenti teszt SOHA
        nem enged sikeres olvasást, tehát nem bizonyítja, hogy a `_temp_
        dmm_ever_read_ok` flag TÉNYLEGESEN átöröklődik a beágyazott
        ChargeController-ből a szülő controllerbe.

        FONTOS, a teszt megírásakor felfedezett csapda: `advance()`
        MINDEN nem-FULL_CHARGE állapotban (:655-659, PRECOND-ot is
        beleértve) a SZÜLŐ saját _read_dmm()-jét hívja, MIELŐTT a
        PRECOND -> FULL_CHARGE átmenet egyáltalán megtörténne -- tehát
        egy naiv "advance(0.0); advance(1.0); assert ever_read_ok"
        szekvencia a SZÜLŐ saját PRECOND-beli olvasását bizonyítaná, NEM
        az átöröklést (`_enter_full_charge()` a gyermeket csak
        KONSTRUÁLJA, nem tickeli -- a gyermek ELSŐ valódi tick-je csak a
        KÖVETKEZŐ advance() hívásban, _run_full_charge()-on át történik).
        Ezért itt a hőmérséklet-DMM a PRECOND-fázis alatt (az első két
        advance()-nél) SZÁNDÉKOSAN halott, csak a HARMADIK advance()-nél
        (a gyermek saját, valódi FULL_CHARGE-tick-jénél) áll helyre, majd
        a session VÉGÉIG ismét halott -- a flagnek MONOTON módon True-nak
        kell maradnia, és a "SOHA nem olvasott sikeresen" limitációnak
        TILOS megjelennie."""
        import json
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, _load, dmm_v, dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
            temp_comp_mode=TempCompMode.OFF,
        )
        dmm_t.simulate_temp_failure = True

        ctrl.advance(0.0)  # INIT -> PRECOND (szülő saját olvasása hibázik)
        ctrl.advance(1.0)  # PRECOND -> FULL_CHARGE (szülő saját olvasása
        # ITT IS hibázik -- l. fenti docstring; a gyermek MOST csak
        # KONSTRUÁLÓDIK, még nem tickelt)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert ctrl._temp_dmm_ever_read_ok is False, (
            "meta-ellenőrzés: a szülő saját PRECOND-beli olvasásai mind "
            "hibáztak, a gyermek pedig még nem tickelt -- itt MÉG False-nak "
            "kell lennie, különben a teszt szekvenciája hibás"
        )

        dmm_t.simulate_temp_failure = False  # a DMM most helyreáll
        ctrl.advance(1.0)  # a gyermek ELSŐ, VALÓDI FULL_CHARGE-tick-je --
        # ez a hívás megy _run_full_charge()-on, a gyermek._read_dmm()-jén
        # keresztül, tehát a fenti csapda itt már NEM áll fenn.
        assert ctrl._temp_dmm_ever_read_ok is True, (
            "a beágyazott ChargeController valós, sikeres FULL_CHARGE-tick "
            "olvasásának át kellett volna öröklődnie a szülőre"
        )

        # Mostantól a session VÉGÉIG a hőmérséklet-DMM halott -- a flagnek
        # MEGMARAD True-nak (monoton), NEM esik vissza False-ra.
        dmm_t.simulate_temp_failure = True
        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        self._run_relax_to_completion(ctrl)
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        self._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE
        assert ctrl._temp_dmm_ever_read_ok is True, (
            "a flag NEM eshet vissza False-ra egy KÉSŐBBI olvasási hibánál"
        )

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE

        limitations = json.loads(
            (tmp_path / "metadata_limitations.json").read_text(encoding="utf-8")
        )
        assert not any(
            "SOHA nem olvasott sikeresen" in f for f in limitations["missing_fields"]
        ), "a 'sosem mert' limitacio HAMISAN jelent meg, holott a FULL_CHARGE alatt VALODI olvasas tortent"

    def test_export_flags_unmeasured_room_temperature_with_production_default_config(
        self, tmp_path
    ):
        """CF-31 MAX verifier P2-1 lelete (2026-09-08): a `GoldenGgPhysical
        Config.conditioning_required` alapértéke False, és a produkciós
        `main_window.py::_make_golden_gg_ctrl()` sosem állítja True-ra --
        ez a teszt PONTOSAN ezt a produkciós (nem conditioning-olt) esetet
        reprodukálja `GoldenGgPhysicalConfig`-ban explicit conditioning_
        required nélkül, ugyanúgy, mint a fenti `test_export_flags_relax_
        dmm_fault_in_metadata_limitations`. Korábban ilyenkor a config.txt
        csendben a konstrukciókori (20.00C) alapértéket exportálta --
        mostantól ez explicit limitációként jelenik meg."""
        import json
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> FULL_CHARGE (NINCS conditioning)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert ctrl._ambient_temperature_C is None

        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        self._run_relax_to_completion(ctrl)
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        self._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE

        limitations = json.loads(
            (tmp_path / "metadata_limitations.json").read_text(encoding="utf-8")
        )
        assert any(
            "NEM mért érték" in f and "RoomTemperatureC" in f
            for f in limitations["missing_fields"]
        )

        # 2. MAX verifier P3-2 lelete (2026-09-08, D-Extra 2026-09-13 óta
        # ÁTÉRTELMEZVE): eredetileg annak ellenőrzése, hogy a limitáció-
        # szöveg NE hozzon létre egy második "RoomTemperatureC=" tokent a
        # config.txt-ben. D-Extra (sibling-sweep, l. GoldenGgConfig.
        # to_text() docstringje) óta a `RoomTemperatureC` teljesen
        # kikerült a config.txt-ből (project_metadata.json sidecarba) --
        # a config.txt-ben emiatt NULLA előfordulásnak kell lennie.
        config_text = (tmp_path / "config.txt").read_text(encoding="utf-8")
        assert "RoomTemperatureC" not in config_text
        project_metadata = json.loads(
            (tmp_path / "project_metadata.json").read_text(encoding="utf-8")
        )
        assert project_metadata["room_temperature_C"] == 20.0  # konstrukciókori default

    def test_export_uses_measured_ambient_temperature(self, tmp_path):
        """Governance-v2 review-kör 1 (CF-31, DFAM-003): a config.txt a
        konditálás alatt ténylegesen mért szobahőmérsékletet tartalmazza,
        nem a GoldenGgConfig konstrukciókori (feltételezett) room_
        temperature_C alapértékét -- l. chemid_controller.py::_run_export()
        SUP03-CHEMID-012 azonos mintája. Korábban ez a controller sosem
        alkalmazta a mért értéket: a config.txt mindig a konstrukciókori
        alapértéket exportálta, akkor is, ha a conditioning fázisban más
        hőmérséklet lett mérve."""
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
            room_temperature_C=20.0,
        )
        ctrl, psu, _load, dmm_v, dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                conditioning_required=True, conditioning_min_s=2.0,
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> CONDITIONING
        assert ctrl.state == GoldenGgState.CONDITIONING
        dmm_t.temperature_C = 24.5  # a "mért" szobahőmérséklet a konditálás alatt
        ctrl.advance(1.0)
        ctrl.advance(1.5)  # min idő letelt -> FULL_CHARGE
        assert ctrl.state == GoldenGgState.FULL_CHARGE

        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        self._run_relax_to_completion(ctrl)
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        self._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE

        import json as _json
        project_metadata = _json.loads(
            (tmp_path / "project_metadata.json").read_text(encoding="utf-8")
        )
        assert project_metadata["room_temperature_C"] == 24.5

        # CF-31 MAX verifier P2-2 lelete (2026-09-08): a beírt érték az
        # akku-felületi hőmérséklet-DMM méréséből ered, nem önálló ambiens
        # szenzorból -- ezt explicit limitációként kell rögzíteni, nem
        # jelöletlen "mért szobahőmérséklet"-ként exportálni.
        import json
        limitations = json.loads(
            (tmp_path / "metadata_limitations.json").read_text(encoding="utf-8")
        )
        assert any(
            "akku-felületi" in f and "NEM önálló szoba-" in f
            for f in limitations["missing_fields"]
        )

    def test_no_export_config_does_not_claim_package_ready(self):
        """Governance-v2 review-kör 1 (Session/Workflow reviewer, CF-32/F8,
        DFAM-018): ha `export_output_dir`/`export_config` None (nincs
        exportkonfiguráció), `_run_export()` teljesen kihagyja a
        csomagépítést, `package_ready_for_ti_submission` False marad — DE
        a state DONE lesz és a `GOLDEN_GG_PACKAGE_READY` esemény ("a
        csomag a TI Golden GG Maker eszköznek beküldhető") MÉGIS kiment.
        Ez hamis siker-szemantika: a GUI/riport egy olyan session után is
        "Kész"-t jelezne, aminek az egyetlen kimenete (a beküldhető
        csomag) nem készült el."""
        ctrl, *_ = make_controller()  # export_output_dir=None (default)
        events = []
        ctrl.on_event = events.append

        ctrl._run_export()

        assert ctrl.state == GoldenGgState.DONE
        assert ctrl.package_ready_for_ti_submission is False
        assert not any(e["event_code"] == "GOLDEN_GG_PACKAGE_READY" for e in events)
        assert any(e["event_code"] == "GOLDEN_GG_PACKAGE_NOT_BUILT" for e in events)

    def test_incomplete_gg_source_snapshot_faults_early_at_precond(self, tmp_path):
        """[review-lelet] a gg.csv forrás hiányosságát (Data Flash olvasási
        hiba -> Qmax/Ra table nem olvasható) MÁR PRECOND-ban észlelni kell,
        NEM csak a több órás fizikai ciklus VÉGÉN, az EXPORT_PACKAGE
        lépésben — különben egy előre észlelhető hiba miatt feleslegesen
        elveszne egy teljes töltés/2h relax/kisütés/5h relax ciklus."""
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
            data_flash_error=True,
        )
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND runs -> should fault immediately
        assert ctrl.state == GoldenGgState.FAULT
        assert "GG_SOURCE_SNAPSHOT_INCOMPLETE" in ctrl.fault_reason
        # A fizikai ciklus (töltés/relax/kisütés) SOSEM indult el.
        assert ctrl._charge_ctrl is None
        assert load._input_is_on is False
        assert psu._output_is_on is False


class TestConfigValidation:
    def test_zero_system_rate_current_rejected(self):
        """SUP03-GG-007: nincs csendes C/10 vagy C/20 fallback."""
        import pytest
        with pytest.raises(ValueError):
            GoldenGgPhysicalConfig(system_rate_current_A=0.0)

    def test_negative_system_rate_current_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            GoldenGgPhysicalConfig(system_rate_current_A=-1.0)

    def test_sample_interval_out_of_range_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            GoldenGgPhysicalConfig(system_rate_current_A=2.0, sample_interval_s=4.0)


class TestSafety:
    def test_emergency_stop_turns_off_load_and_psu(self):
        ctrl, psu, load, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.emergency_stop("MANUAL_TEST")
        assert ctrl.state == GoldenGgState.FAULT
        assert load._input_is_on is False
        assert psu._output_is_on is False

    def test_advance_after_terminal_state_is_noop(self):
        ctrl, *_ = make_controller()
        ctrl.emergency_stop("X")
        state_before = ctrl.state
        ctrl.advance(100.0)
        assert ctrl.state == state_before

    def test_system_rate_discharge_dmm_lost_faults(self):
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert load._input_is_on is False

    def test_system_rate_discharge_current_far_below_setpoint_gives_warning(self):
        """[regresszió, Hardver/Műszervezérlés HW4-02 (review-kör 4, P1,
        DFAM-001)] l. discharge_controller.py testvér tesztjének
        docstringje ugyanerről a hiányzó setpoint-vs-mért kapuról."""
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        load.current_A = 0.2  # a setpoint (2.0A) töredéke -- "ragadt"
        ctrl.advance(1.0)
        assert ctrl.state != GoldenGgState.FAULT
        assert ctrl.last_warning_code == "LOAD_CURRENT_SETPOINT_NOT_REACHED"

    def test_system_rate_discharge_setpoint_warning_tick_counter_survives_higher_priority_warning(self):
        """[regresszió, MAX verifier (review-kör 4, HW4-02, 329c1a3) N1
        lelete] l. discharge_controller.py testvér tesztjének docstringje
        ugyanerről a hiányzó, a _last_warning_code slot-tól független
        tick-számlálóról."""
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        dmm_t.temperature_C = 40.0  # profil névleges tartományán (15-30C) kívül -- MINDEN tickben aktív
        load.current_A = 0.2  # a setpoint (2.0A) töredéke -- "ragadt"
        assert ctrl.load_current_setpoint_warning_ticks == 0
        ctrl.advance(1.0)
        assert ctrl.last_warning_code == "TEMPERATURE_OUT_OF_RANGE"
        assert ctrl.load_current_setpoint_warning_ticks == 1

    def test_system_rate_discharge_load_off_failure_escalates_to_fault(self):
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert "SYSTEM_RATE_DISCHARGE_LOAD_OFF_FAILED" in ctrl.fault_reason

    def test_reverse_polarity_during_system_rate_discharge_faults(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Aktív
        SYSTEM_RATE_DISCHARGE alatt egy fizikailag ésszerűtlen (<=0V)
        DMM-olvasás korábban csendben "elértük a terminate voltage-ot"-ként
        RELAX_5H-t okozott volna, NEM FAULT-ot (CLAUDE.md §10.6/SAFE-009)."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        dmm_v.voltage_V = 0.0
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason == "REVERSE_POLARITY_OR_WIRING_FAULT"

    def test_system_rate_discharge_faults_on_load_power_limit_exceeded(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P1 lelet] A standalone DischargeController._run_cc() minden
        ticken ellenőrzi a Keithley 2380 250W hardver-teljesítménykorlátot —
        a beágyazott _run_system_rate_discharge() korábban nem, holott itt a
        system_rate_current_A explicit, nem C-ráta-korlátozott bemenet."""
        fake_bq = _FakeBqState()
        ctrl, _psu, load, _dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        load.current_A = 25.0  # 13V * 25A = 325W > 250W max
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert "LOAD_POWER_LIMIT" in ctrl.fault_reason

    def test_system_rate_discharge_faults_on_max_discharge_time_reached(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P2 lelet] a beágyazott SYSTEM_RATE_DISCHARGE fázis eddig
        KIZÁRÓLAG a feszültség-alapú leállási feltételre támaszkodott —
        egy sosem-csökkenő (pl. hibás/rögzült) DMM-olvasás mellett a
        kisütés időben korlátlanul futhatott volna."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        dmm_v.voltage_V = 13.0  # sosem éri el a terminate_voltage_pack_V-t
        ctrl.advance(86400.1)
        assert ctrl.state == GoldenGgState.FAULT
        assert "MAX_DISCHARGE_TIME_REACHED" in ctrl.fault_reason

    def test_system_rate_discharge_faults_on_max_discharge_ah_reached(self):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), Session/
        Workflow P2 lelet] ugyanaz a hiányzó backstop az Ah-oldalon."""
        fake_bq = _FakeBqState()
        ctrl, _psu, load, dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        dmm_v.voltage_V = 13.0
        load.current_A = 10.0  # nagy áram -> gyorsan túllépi a névleges kapacitás 110%-át
        ctrl.advance(3600.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert "MAX_DISCHARGE_AH_REACHED" in ctrl.fault_reason

    def test_system_rate_discharge_faults_on_max_ah_uses_c10_not_c20_base(self):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31), Szoftver/
        Architektúra P2 lelet] l. traditional_learning_controller.py azonos
        tesztjének docstringje ugyanerről a C10-vs-C20 bázisdiszkriminációról."""
        fake_bq = _FakeBqState()
        ctrl, _psu, load, dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE
        # FG-sorozat szimulációja: nominal_capacity_Ah=C20=20.0, C10=10.0 —
        # az alap max_discharge_Ah_factor=1.10 mellett a C10-plafon 11.0 Ah,
        # a C20-plafon 22.0 Ah — a lenti 15.0 Ah a kettő KÖZÖTT van.
        ctrl._profile = BatteryProfile(
            battery_name="TEST_FG", manufacturer="TEST", model="FG20AH",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=20.0,
            C10_capacity_Ah=10.0, terminate_voltage_per_cell_V=1.80,
        )

        dmm_v.voltage_V = 13.0
        load.current_A = 15.0
        ctrl.advance(3600.0)  # 15.0 Ah kisütve
        assert ctrl.state == GoldenGgState.FAULT
        assert "MAX_DISCHARGE_AH_REACHED" in ctrl.fault_reason

    def test_system_rate_discharge_max_time_respects_safety_manager_override(self):
        """[regresszió, Teljes kódbázis review 8. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet] max_discharge_time_s korábban egy privát,
        hardkódolt modul-konstansként élt itt — egy SafetyManager-en
        beállított egyedi érték NEM ért volna el ide. Mostantól
        self._safety.max_discharge_time_s a kanonikus forrás, a standalone
        DischargeController mintáját követve (5-8. kör konszolidáció)."""
        fake_bq = _FakeBqState()
        ctrl, _psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        dmm_v.voltage_V = 13.0  # sosem éri el a terminate_voltage_pack_V-t
        ctrl._safety.max_discharge_time_s = 5.0  # << a 86400s alapértéktől

        ctrl.advance(10.0)  # a szűkített limit fölött, de az alapérték alatt

        assert ctrl.state == GoldenGgState.FAULT
        assert "MAX_DISCHARGE_TIME_REACHED" in ctrl.fault_reason

    def test_concurrent_psu_during_system_rate_discharge_faults(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Aktív
        SYSTEM_RATE_DISCHARGE alatt egy PSU ON hardveres konfliktust korábban
        a beágyazott ág sosem ellenőrizte (CLAUDE.md §10.2 defense-in-depth
        hiány)."""
        fake_bq = _FakeBqState()
        ctrl, psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        psu.output_commanded_on = True
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert "CONCURRENT" in ctrl.fault_reason

    def test_charge_termination_not_verified_faults_if_dmm_invalid(self):
        ctrl, psu, load, dmm_v, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # FULL_CHARGE -> CHARGE_TERMINATION_VERIFIED
        assert ctrl.state == GoldenGgState.CHARGE_TERMINATION_VERIFIED
        dmm_v.simulate_timeout = True
        ctrl.advance(1.0)  # dispatches _run_charge_termination_verified()
        assert ctrl.state == GoldenGgState.FAULT
        assert "CHARGE_TERMINATION_NOT_VERIFIED" in ctrl.fault_reason


class TestReleaseAudit20260823AhBookkeeping:
    """Release-audit 2026-08-23 (2.7): accumulated_charge_Ah/
    accumulated_discharge_Ah property — a TestRunner._run_step() ezt a
    generikus getattr(controller, "accumulated_charge_Ah", 0.0) mintán
    keresztül olvassa a session-szintű Ah-összesítéshez (CHEMID-AH-GAP-01
    mintázatának kiterjesztése erre a controllerre). Enélkül a mező hiánya
    miatt egy teljes, valós fizikai ciklust futtató GOLDEN_GG session
    mindig 0.0000 Ah-t mutatott volna a report.json-ban/GUI-ban."""

    def test_record_sample_feeds_energy_integrator_on_charge(self):
        import pytest
        ctrl, *_ = make_controller()
        ctrl._u_batt = 13.0
        ctrl._record_sample(dt_s=3600.0, current_A=1.5)  # 1.5A*1h

        assert ctrl.accumulated_charge_Ah == pytest.approx(1.5)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(0.0)

    def test_record_sample_feeds_energy_integrator_on_discharge(self):
        import pytest
        ctrl, *_ = make_controller()
        ctrl._u_batt = 12.0
        ctrl._record_sample(dt_s=3600.0, current_A=-2.0)  # 2.0A*1h kisütve

        assert ctrl.accumulated_discharge_Ah == pytest.approx(2.0)
        assert ctrl.accumulated_charge_Ah == pytest.approx(0.0)

    def test_accumulated_ah_sums_across_charge_and_discharge_phases(self):
        """A teljes SUP03-GG-005 ciklus (FULL_CHARGE + SYSTEM_RATE_DISCHARGE)
        Ah-ja összeadódik, nem írja felül egymást."""
        import pytest
        ctrl, *_ = make_controller()
        ctrl._u_batt = 13.0
        ctrl._record_sample(dt_s=3600.0, current_A=1.5)   # töltés: 1.5 Ah
        ctrl._u_batt = 12.0
        ctrl._record_sample(dt_s=1800.0, current_A=-2.0)  # kisütés: 1.0 Ah (0.5h)

        assert ctrl.accumulated_charge_Ah == pytest.approx(1.5)
        assert ctrl.accumulated_discharge_Ah == pytest.approx(1.0)

    def test_accumulated_charge_ah_nonzero_during_live_full_charge_state(self):
        """Végigfut a valós állapotgép-dispatch-on (nem közvetlen
        _record_sample()-hívás) — a MockPSU readback-áramával.

        [frissítve, Teljes kódbázis review 5. kör (2026-08-29), Metrológia/
        Adatintegritás P1 lelet] a beágyazott ChargeController mostantól a
        SAJÁT, fault-kezelt i_psu_measured_A property-jét adja a session-
        integrátornak — ez csak akkor nem-nulla, ha a beágyazott
        ChargeController TÉNYLEGESEN eljut CHARGE_CC-be
        (INIT→PRECHECK→PSU_PRESET→CHARGE_CC, 3 saját advance()-tick, l.
        test_charge_controller.py::_advance_to_cc())."""
        import pytest
        ctrl, psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        psu.current_A = 1.5  # MockPSU alapból current_A=0.0
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + FULL_CHARGE
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        ctrl.advance(1.0)  # belső: INIT → PRECHECK
        ctrl.advance(1.0)  # belső: PRECHECK → PSU_PRESET
        ctrl.advance(1.0)  # belső: PSU_PRESET → CHARGE_CC
        ctrl.advance(3600.0)  # belső: CHARGE_CC tick — ITT olvasódik a PSU-áram

        assert ctrl.accumulated_charge_Ah > 0.0
        assert ctrl.accumulated_discharge_Ah == pytest.approx(0.0)


class TestReview2ndRoundWhBookkeeping:
    """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P2] a
    test_runner.py::_build_sample() korábban a controller PRIVÁT
    `_integrator` attribútumát kereste `getattr`-rel — ez ezen a
    controlleren (`_energy_integrator` a neve) SOSEM létezett, a Wh-mezők
    a TELJES session alatt None maradtak a samples.csv-ben."""

    def test_record_sample_feeds_wh_on_charge(self):
        import pytest
        ctrl, *_ = make_controller()
        ctrl._u_batt = 13.0
        ctrl._record_sample(dt_s=3600.0, current_A=1.5)  # 1.5A*13.0V*1h

        assert ctrl.accumulated_charge_Wh == pytest.approx(19.5)
        assert ctrl.accumulated_discharge_Wh == pytest.approx(0.0)

    def test_record_sample_feeds_wh_on_discharge(self):
        import pytest
        ctrl, *_ = make_controller()
        ctrl._u_batt = 12.0
        ctrl._record_sample(dt_s=3600.0, current_A=-2.0)  # 2.0A*12.0V*1h

        assert ctrl.accumulated_discharge_Wh == pytest.approx(24.0)
        assert ctrl.accumulated_charge_Wh == pytest.approx(0.0)


class TestUBattVProperty:
    """2026-08-23 traditional_learning OV incidens elemzés kiterjesztése
    golden_gg-re — l. test_traditional_learning_controller.py
    TestUBattVProperty docstringje ugyanerről a mintáról."""

    def test_u_batt_v_reflects_full_charge_reading(self):
        import pytest
        ctrl, *_ = make_controller(dmm_voltage_V=13.37)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + FULL_CHARGE
        assert ctrl.u_batt_V == pytest.approx(13.37)


class TestBatteryTemperatureCProperty:
    """Ugyanaz az elv, mint TestUBattVProperty-nél, de a hőmérséklet-DMM-re
    — l. test_traditional_learning_controller.py TestBatteryTemperatureCProperty
    docstringje ugyanerről a mintáról."""

    def test_battery_temperature_c_reflects_full_charge_reading(self):
        import pytest
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + FULL_CHARGE
        assert ctrl.battery_temperature_C == pytest.approx(22.0)  # MockDMM(temperature_C=22.0)


class TestChargeConfigPropagation:
    """2026-08-23 (felhasználói review, 2. kör — traditional_learning OV
    incidens) kiterjesztése golden_gg-re — l.
    test_traditional_learning_controller.py TestChargeConfigPropagation
    docstringje ugyanerről a hiányzó ChargeConfig-öröklésről."""

    def test_charge_current_override_reaches_embedded_charge_ctrl(self):
        import pytest
        charge_cfg = ChargeConfig(charge_current_A_override=0.8)
        ctrl, *_ = make_controller(dmm_voltage_V=13.0, charge_config=charge_cfg)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + FULL_CHARGE
        assert ctrl._charge_ctrl is not None
        for _ in range(5):
            if ctrl._charge_ctrl.state == ChargeState.CHARGE_CC:
                break
            ctrl._charge_ctrl.advance(1.0)
        assert ctrl._charge_ctrl._charge_current_set_A == pytest.approx(0.8)

    def test_no_explicit_charge_config_falls_back_to_default(self):
        ctrl, *_ = make_controller(dmm_voltage_V=13.0)
        assert ctrl._charge_config.charge_current_A_override == 0.0


class TestChargeConfigProperty:
    """Teljes kódbázis review 15. kör (2026-09-04), Szoftver/Architektúra
    P2 lelet: a testvér TraditionalLearningController.charge_config
    property-jével azonos, korábban hiányzó public API — l.
    main_window.py::_write_report() charge_config_snapshot bekötése."""

    def test_reflects_default_charge_config(self):
        ctrl, *_ = make_controller(dmm_voltage_V=13.0)
        assert ctrl.charge_config.charge_current_A_override == 0.0

    def test_reflects_explicit_charge_config(self):
        import pytest
        charge_cfg = ChargeConfig(charge_current_A_override=0.8)
        ctrl, *_ = make_controller(dmm_voltage_V=13.0, charge_config=charge_cfg)
        assert ctrl.charge_config is charge_cfg
        assert ctrl.charge_config.charge_current_A_override == pytest.approx(0.8)


class TestUPsuSetVAndWarningProxyProperties:
    """2026-08-23 (felhasználói jelzés: "A PSU SET V még mindíg nem mutat
    értéket") — l. test_traditional_learning_controller.py ugyanerről a
    hiányzó proxy-mintáról."""

    def test_u_psu_set_v_reflects_embedded_charge_ctrl(self):
        import pytest
        ctrl, *_ = make_controller(dmm_voltage_V=13.37)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + FULL_CHARGE
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
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + FULL_CHARGE
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
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + FULL_CHARGE
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._taper_timer_s = 42.0
        assert ctrl.taper_timer_s == 42.0

    def test_taper_timer_s_zero_when_no_charge_ctrl(self):
        ctrl, *_ = make_controller()
        assert ctrl.taper_timer_s == 0.0

    def test_u_psu_set_v_zero_when_no_charge_ctrl(self):
        ctrl, *_ = make_controller()
        assert ctrl.u_psu_set_V == 0.0

    def test_last_warning_code_reflects_embedded_charge_ctrl(self):
        ctrl, *_ = make_controller(dmm_voltage_V=13.37)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> ChargeController konstrukció + FULL_CHARGE
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._last_warning_code = "DIODE_POWER_TOO_HIGH"
        assert ctrl.last_warning_code == "DIODE_POWER_TOO_HIGH"


class TestRelaxDmmFaultTotalProperty:
    """[regresszió, Teljes kódbázis review 10. kör (2026-08-31), Metrológia/
    Adatintegritás P2 lelet] a relax-fázisok alatti feszültség-DMM-olvasási
    hibák session-szintű összege korábban csak _run_export()-ban (az
    export-zip belsejében, csak sikeres exportnál) jelent meg — nem volt
    publikus property rajta, main_window.py::_write_report() nem tudta
    elérni. FAULT-tal (export előtt) végződő session esetén a szám
    nyomtalanul elveszett."""

    def test_zero_by_default(self):
        ctrl, *_ = make_controller()
        assert ctrl.relax_dmm_fault_total == 0

    def test_reflects_internal_counter(self):
        ctrl, *_ = make_controller()
        ctrl._relax_dmm_fault_total = 3
        assert ctrl.relax_dmm_fault_total == 3


class TestTemperatureOutOfRangeCountProperty:
    """[regresszió, Teljes kódbázis review 21. kör (2026-09-07), Session/
    Workflow/Állapotgép P2 lelet] a beágyazott ChargeController (FULL_CHARGE
    alatt) temperature_out_of_range_count számlálóját a
    self._charge_ctrl = None átmenet előtt át kell menteni a
    GoldenGgController saját akkumulátorába, különben a CHARGE_DONE után
    nyomtalanul elveszne."""

    def test_zero_by_default(self):
        ctrl, *_ = make_controller()
        assert ctrl.temperature_out_of_range_count == 0

    def test_sums_own_and_embedded_charge_ctrl_counter(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> FULL_CHARGE, létrehozza a beágyazott ChargeController-t
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert ctrl._charge_ctrl is not None
        ctrl._charge_ctrl._temperature_out_of_range_count = 2
        ctrl._temperature_out_of_range_count = 1
        assert ctrl.temperature_out_of_range_count == 3

    def test_embedded_counter_transferred_before_charge_ctrl_set_to_none(self):
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        _force_charge_done(ctrl)
        ctrl._charge_ctrl._temperature_out_of_range_count = 4
        ctrl.advance(1.0)  # FULL_CHARGE -> CHARGE_TERMINATION_VERIFIED

        assert ctrl._charge_ctrl is None
        assert ctrl.temperature_out_of_range_count == 4


class TestReleaseAudit20260823TempDmmGap01Extension:
    """Release-audit 2026-08-23 (2.12, TEMP-DMM-GAP-01 kiterjesztése): a
    hőmérséklet-DMM kiesését korábban a relax/kisütés fázisokban
    _read_dmm() csendben elnyelte, FAULT-ot sosem váltott ki — a FULL_CHARGE
    fázist a beágyazott ChargeController saját védelme már fedte, a
    relax/kisütés fázisokat nem."""

    def test_temp_dmm_failure_during_relax_2h_faults(self):
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.CHARGE_TERMINATION_VERIFIED
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

        dmm_t.simulate_temp_failure = True
        ctrl.advance(70.0)  # MONITOR_ONLY timeout=60s, egy tick alatt túllépve

        assert ctrl.state == GoldenGgState.FAULT
        assert "TEMPERATURE" in ctrl.fault_reason

    def test_temp_dmm_failure_recovers_when_reading_succeeds_again(self):
        """Sikeres olvasás nullázza a hiba-időzítőt — nem kumulálódik
        örökre egy átmeneti kiesés után."""
        ctrl, psu, load, dmm_v, dmm_t, fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)  # 10s < 60s timeout, még nem faultol
        assert ctrl.state == GoldenGgState.RELAX_2H

        dmm_t.simulate_temp_failure = False
        ctrl.advance(1.0)  # sikeres olvasás -> _temp_dmm_fault_s nullázódik
        assert ctrl._safety.temperature_dmm_fault_s == 0.0

    def test_temp_dmm_warning_during_relax_2h_sets_last_warning_code(self):
        """[regresszió, Teljes kódbázis review 9. kör (2026-08-30), Szoftver/
        Architektúra P2 lelet] last_warning_code korábban KIZÁRÓLAG a
        beágyazott ChargeController-t proxyzta (FULL_CHARGE alatt) — a
        RELAX_2H fázisban a saját szintű hőmérséklet-DMM MONITOR_ONLY
        figyelmeztetés kiszámolódott, de sosem jutott el a property-ig."""
        ctrl, _psu, _load, _dmm_v, dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

        dmm_t.simulate_temp_failure = True
        ctrl.advance(10.0)  # 10s < 60s timeout, WARNING, nem FAULT

        assert ctrl.state != GoldenGgState.FAULT
        assert ctrl.last_warning_code == "TEMPERATURE_DMM_LOST"

    def test_temp_dmm_valid_true_after_successful_read(self):
        """[regresszió, review-kör 3 (2026-09-09), MAX verifier
        (a9cb161469c468fc8) P2-1] a temp_dmm_valid property (Metrológia
        M3-01, P1) egyik controlleren sem kapott önálló, controller-
        szintű regressziós tesztet -- ez a hiány engedte át a
        chemid_controller.py-beli P1-1 blokkoló hibát 1792 zöld teszt
        mellett."""
        ctrl, _psu, _load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_after_failed_read(self):
        ctrl, _psu, _load, _dmm_v, dmm_t, _fake_bq = make_controller()
        dmm_t.simulate_temp_failure = True
        ctrl.advance(0.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_valid_true_delegated_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a3f0e6dcf93588360) P2-1]
        a self._dmm_valid (feszültség) delegálás a beágyazott
        ChargeControllerből mindig megtörtént FULL_CHARGE alatt, de a
        self._temp_dmm_valid nem -- egy ott ténylegesen hibázó
        hőmérséklet-DMM a szülő szintjén hamisan True-t jelentett volna,
        mert a saját _read_dmm() ebben az állapotban ki van kapcsolva."""
        ctrl, *_ = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE

        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is True

    def test_temp_dmm_valid_false_delegated_from_embedded_charge_controller(self):
        ctrl, _psu, _load, _dmm_v, dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_valid is False

    def test_temp_dmm_fault_ticks_live_add_and_carry_over_from_embedded_charge_controller(self):
        """[regresszió, MAX verifier (review-kör 3, a306e7e764e3a717b) P2-1,
        DFAM-001 további előfordulása] l. test_chemid_controller.py azonos
        tesztjének docstringje ugyanerről a hiányzó live-add + carry-over
        mintáról."""
        ctrl, _psu, _load, _dmm_v, dmm_t, _fake_bq = make_controller()
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE

        dmm_t.simulate_temp_failure = True
        ctrl.advance(1.0)
        ctrl.advance(1.0)
        assert ctrl.temp_dmm_fault_ticks == 2

        dmm_t.simulate_temp_failure = False
        _force_charge_done(ctrl)
        ctrl.advance(1.0)  # FULL_CHARGE -> CHARGE_TERMINATION_VERIFIED
        assert ctrl._charge_ctrl is None
        assert ctrl.temp_dmm_fault_ticks == 2


class TestEmergencyStopOffFailureReported:
    """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), Hardver/
    Műszervezérlés P2 lelet] l. test_charge_controller.py::
    TestEmergencyStopOffFailureReported ugyanerről a csendben elnyelt
    OFF-kísérlet-hiba mintáról. Itt az emittált GOLDEN_GG_FAULT esemény
    event_message-ének is a kiegészített fault_reason-t kell kapnia."""

    def test_load_off_failure_surfaces_in_fault_reason_and_event(self):
        ctrl, _psu, load, _dmm_v, _dmm_t, _fake_bq = make_controller()
        events = []
        ctrl.on_event = events.append

        def _raise_on_off():
            raise RuntimeError("simulated load off failure")
        load.input_off = _raise_on_off  # type: ignore[method-assign]

        ctrl.emergency_stop("DMM_FEEDBACK_LOST")

        assert ctrl.state == GoldenGgState.FAULT
        assert "DMM_FEEDBACK_LOST" in ctrl.fault_reason
        assert "LOAD_OFF_FAILED" in ctrl.fault_reason
        assert "simulated load off failure" in ctrl.fault_reason
        fault_events = [e for e in events if e["event_code"] == "GOLDEN_GG_FAULT"]
        assert len(fault_events) == 1
        assert "LOAD_OFF_FAILED" in fault_events[0]["event_message"]

    def test_off_success_leaves_fault_reason_unchanged(self):
        ctrl, *_ = make_controller()
        ctrl.emergency_stop("DMM_FEEDBACK_LOST")
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"


class TestGaugePollThrottling:
    """CF-10a MAX verifier P1-1 sibling lelet (governance-v2, DFAM-027,
    ceiling P1, RECURRING): a _record_sample() -> poll_bq_diagnostics()
    ugyanazt a 19 mező x 3 retry x 1.0s ~= 57s szinkron blokkolást
    okozta, mint a testvér traditional_learning_controller.py (CF-10a
    fő fix, commit 07b2057) -- itt AKTÍV töltés/kisütés közben (PSU/Load
    ON), nem csak relax-ban. A _poll_bq_diagnostics_throttled() ugyanazt
    a mintát követi."""

    def test_throttled_poll_uses_single_attempt_per_field_on_total_disconnect(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.on_gauge_sample = lambda snapshot: None
        serial = ctrl._bq_transport.serial
        serial.simulate_no_response = True
        writes_before = len(serial.written_requests)

        ctrl._record_sample(5.0, current_A=0.0)

        writes_after = len(serial.written_requests)
        # 19 mező x 1 kísérlet (max_retries=1 a throttled poll alatt) --
        # a fix nélkül 19 x 3 = 57 írás történne ugyanerre az egy pollra.
        assert writes_after - writes_before == 19

    def test_throttled_poll_restores_max_retries_even_after_total_disconnect(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.on_gauge_sample = lambda snapshot: None
        assert ctrl._bq_transport.max_retries == 3
        ctrl._bq_transport.serial.simulate_no_response = True

        ctrl._record_sample(5.0, current_A=0.0)

        assert ctrl._bq_transport.max_retries == 3

    def test_precond_source_snapshot_poll_not_throttled(self, monkeypatch):
        """A PRECOND-beli gg_snapshot = poll_bq_diagnostics(...) hívás
        (_run_precond()) a Qmax/Ra table TÉNYLEGES érvényességét kapuzza
        (emergency_stop, ha hiányos) -- ez NEM veszítheti el a retry-
        robusztusságot, szemben a tisztán diagnosztikai _record_sample()-
        mel. A read_chem_id() előzetes ellenőrzés miatt egy teljes
        (simulate_no_response) kiesés MÁR a chem_id-nél elbukna, mielőtt
        a snapshot-poll egyáltalán lefutna -- ezért itt közvetlenül azt
        mérjük, milyen max_retries érték van érvényben a poll_bq_
        diagnostics() TÉNYLEGES meghívásának pillanatában."""
        import Prog.src.golden_gg_controller as ggc_module
        observed_max_retries = []
        real_poll = ggc_module.poll_bq_diagnostics

        def _spy(driver):
            observed_max_retries.append(driver.transport.max_retries)
            return real_poll(driver)
        monkeypatch.setattr(ggc_module, "poll_bq_diagnostics", _spy)

        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)

        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND: gg_snapshot poll_bq_diagnostics() hívás

        assert observed_max_retries == [3]
        assert ctrl._bq_transport.max_retries == 3


class TestGaugePollThrottleSelfReinforcingLoopFix:
    """[regresszió, 2026-09-15, C-01/DFAM-027 P0, MAX verifier V-2 lelete]
    Sibling a traditional_learning_controller.py azonos nevű test-
    osztályához (l. ott a teljes indoklást). A family (DFAM-027) 2.
    előfordulása golden_gg_controller.py::_record_sample()-ben."""

    def test_repeated_slow_polls_settle_to_nominal_cadence_not_every_tick(self, monkeypatch):
        import Prog.src.golden_gg_controller as ggc_module

        fake_bq = _FakeBqState()
        cfg = GoldenGgPhysicalConfig(
            relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
            system_rate_current_A=2.0, sample_interval_s=30.0,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)

        fake_now = [0.0]
        monkeypatch.setattr(ggc_module.time, "perf_counter", lambda: fake_now[0])

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
            ctrl._record_sample(dt_s, current_A=0.0)
            last_tick_t = tick_start_t
            fake_now[0] += NOMINAL_TICK_S

        # Névleges kadencia mellett ~40*2/(30+35) ≈ 1.2 poll várható -- a
        # HEAD-kódon (kizárás nélkül) az ELSŐ lassú poll után MINDEN
        # további tick újra pollozna, nagyságrendileg több pollt okozva.
        assert poll_count[0] <= 3


class TestBqTransportUnreachableEscalation:
    """HIL-D5 (2026-09-14, CF-10a/DFAM-027, CLAIM_LEDGER.yaml PICO-
    RECONNECT-001) -- l. test_traditional_learning_controller.py azonos
    nevű teszt-osztálya ugyanerre a sibling-konzisztens mintára."""

    def test_no_event_before_threshold(self):
        fake_bq = _FakeBqState()
        cfg = GoldenGgPhysicalConfig(
            relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
            system_rate_current_A=2.0, sample_interval_s=5.0,
            bq_transport_unreachable_after_n_polls=3,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl._bq_transport.serial.simulate_no_response = True

        # Review 2026-09-15 (C-01/DFAM-027): dt_s=100.0 (>> sample_interval_s),
        # hogy minden hívás önmagában, bőséges ráhagyással lépje át a
        # küszöböt -- a C-01 fix a poll SAJÁT (mock alatt is mérhető,
        # nem-nulla) wall-clock idejét kizárja a KÖVETKEZŐ hívás
        # akkumulációjából, ami dt_s==sample_interval_s pontos egyezésnél
        # minden további hívást egy hajszálnyival a küszöb ALÁ vetett volna.
        ctrl._record_sample(100.0, current_A=0.0)
        ctrl._record_sample(100.0, current_A=0.0)

        assert ctrl._consecutive_fully_invalid_bq_polls == 2
        assert not any(e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE" for e in events)

    def test_event_fires_exactly_at_threshold(self):
        fake_bq = _FakeBqState()
        cfg = GoldenGgPhysicalConfig(
            relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
            system_rate_current_A=2.0, sample_interval_s=5.0,
            bq_transport_unreachable_after_n_polls=3,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl._bq_transport.serial.simulate_no_response = True

        # Review 2026-09-15 (C-01/DFAM-027) -- l. test_no_event_before_
        # threshold() kommentje a dt_s=100.0 margó indoklásáért.
        ctrl._record_sample(100.0, current_A=0.0)
        ctrl._record_sample(100.0, current_A=0.0)
        ctrl._record_sample(100.0, current_A=0.0)

        matching = [e for e in events if e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE"]
        assert len(matching) == 1
        assert matching[0]["severity"] == "CRITICAL"
        assert any(
            f.startswith("BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE_AT_") for f in ctrl.quality_flags
        )

    def test_event_does_not_repeat_while_still_unreachable(self):
        fake_bq = _FakeBqState()
        cfg = GoldenGgPhysicalConfig(
            relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
            system_rate_current_A=2.0, sample_interval_s=5.0,
            bq_transport_unreachable_after_n_polls=2,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl._bq_transport.serial.simulate_no_response = True

        for _ in range(5):
            ctrl._record_sample(5.0, current_A=0.0)

        matching = [e for e in events if e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE"]
        assert len(matching) == 1

    def test_counter_resets_and_can_refire_after_recovery(self):
        fake_bq = _FakeBqState()
        cfg = GoldenGgPhysicalConfig(
            relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
            system_rate_current_A=2.0, sample_interval_s=5.0,
            bq_transport_unreachable_after_n_polls=2,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append
        ctrl._bq_transport.serial.simulate_no_response = True

        # Review 2026-09-15 (C-01/DFAM-027) -- l. test_no_event_before_
        # threshold() kommentje a dt_s=100.0 margó indoklásáért.
        ctrl._record_sample(100.0, current_A=0.0)
        ctrl._record_sample(100.0, current_A=0.0)
        assert ctrl._consecutive_fully_invalid_bq_polls == 2

        ctrl._bq_transport.serial.simulate_no_response = False
        ctrl._record_sample(100.0, current_A=0.0)
        assert ctrl._consecutive_fully_invalid_bq_polls == 0
        assert ctrl._bq_transport_unreachable_flagged is False

        ctrl._bq_transport.serial.simulate_no_response = True
        ctrl._record_sample(100.0, current_A=0.0)
        ctrl._record_sample(100.0, current_A=0.0)

        matching = [e for e in events if e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE"]
        assert len(matching) == 2

    def test_successful_poll_never_triggers_escalation(self):
        fake_bq = _FakeBqState()
        cfg = GoldenGgPhysicalConfig(
            relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
            system_rate_current_A=2.0, sample_interval_s=5.0,
            bq_transport_unreachable_after_n_polls=1,
        )
        ctrl, *_ = make_controller(fake_bq=fake_bq, config=cfg)
        events: list[dict] = []
        ctrl.on_event = events.append

        ctrl._record_sample(5.0, current_A=0.0)

        assert ctrl._consecutive_fully_invalid_bq_polls == 0
        assert not any(e["event_code"] == "BQ_TRANSPORT_PERSISTENTLY_UNREACHABLE" for e in events)


class TestBqSealSessionLifecycle:
    """[SCF-011 újratervezés, 2026-09-11, felhasználói döntés]
    GoldenGgController session-szintű BQ SEALED/UNSEALED életciklusa --
    l. traditional_learning_controller.py azonos nevű teszt-osztálya és
    golden_gg_controller.py::_run_precond()/_ensure_bq_sealed_or_flag()
    docstringje ugyanerről a mintáról. Ez a controller sosem ír a BQ-ba,
    de a Qmax/Ra forrásolvasás (BQ-DF-SEALED-001) csak UNSEALED állapotban
    megbízható, ezért a PRECOND is egyszeri unseal()-t végez, a session
    VÉGÉN (DONE/FAULT) explicit, visszaellenőrzött seal()-lel."""

    def test_precond_unseals_before_source_snapshot(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        assert fake_bq.sealed is True

        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND: unseal() + gg_snapshot poll

        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert fake_bq.sealed is False

    def test_precond_unseal_failure_faults(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl._bq_transport.serial.errors["unseal"] = ("METHOD_ERROR", "simulated unseal failure")

        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)

        assert ctrl.state == GoldenGgState.FAULT
        assert "BQ_UNSEAL_FAILED" in ctrl.fault_reason

    def test_precond_faults_when_unseal_does_not_actually_clear_ss_bit(self):
        """[SCF-011 újratervezés, MAX verifier P1-1 lelete, 2026-09-11]
        egy RPC 'ok' válasz önmagában NEM bizonyítja, hogy az SS bit
        ténylegesen törlődött -- l. a testvér traditional_learning_
        controller.py azonos nevű tesztje. Itt az unseal() RPC ok-kal
        válaszol, de a `fake.sealed`-et NEM módosítja (nem a normál
        `_unseal` handleren megy át)."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl._bq_transport.serial.results["unseal"] = lambda params: None

        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)

        assert ctrl.state == GoldenGgState.FAULT
        assert "BQ_UNSEAL_NOT_EFFECTIVE" in ctrl.fault_reason
        assert fake_bq.sealed is True, "a chip valojaban meg SEALED maradt"

    def test_chip_stays_unsealed_through_full_charge_no_repeated_unseal(self):
        """A PRECOND utáni fázisokban (FULL_CHARGE alatt a beágyazott
        ChargeController a BQ-t egyáltalán nem éri el) nincs ismételt
        unseal()/seal() hívás -- session-szintű, nem köztes-lépésenkénti
        életciklus."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert fake_bq.sealed is False

        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.CHARGE_TERMINATION_VERIFIED
        assert fake_bq.sealed is False

    def test_emergency_stop_reseals_chip_and_verifies(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert fake_bq.sealed is False

        ctrl.emergency_stop("MANUAL_TEST_FAULT")

        assert ctrl.state == GoldenGgState.FAULT
        assert fake_bq.sealed is True
        assert ctrl.quality_flags == ()

    def test_emergency_stop_seal_failure_surfaces_in_quality_flags(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        ctrl._bq_transport.serial.errors["seal"] = ("METHOD_ERROR", "simulated seal failure")
        events = []
        ctrl.on_event = events.append

        ctrl.emergency_stop("MANUAL_TEST_FAULT")

        assert ctrl.state == GoldenGgState.FAULT
        assert any(f.startswith("BQ_SEAL_RESTORE_FAILED_AT_EMERGENCY_STOP:") for f in ctrl.quality_flags)
        assert any(e.get("event_code") == "GOLDEN_GG_SEAL_RESTORE_FAILED" for e in events)

    def test_emergency_stop_flags_when_seal_accepted_but_still_unsealed(self):
        """[Round-2 MAX verifier F-2 lelet, 2026-09-11] l. a testvér
        traditional_learning_controller.py azonos nevű tesztje ugyanerről
        a korábban teszteletlen ágról: a seal() RPC 'ok'-val válaszol, de
        NEM változtatja meg ténylegesen a fake chip állapotát (nem a
        normál `_seal` handleren megy át) -- a post-seal visszaellenőrzés
        ezt a rést hivatott elkapni."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        ctrl._bq_transport.serial.results["seal"] = lambda params: None
        events = []
        ctrl.on_event = events.append

        ctrl.emergency_stop("MANUAL_TEST_FAULT")

        assert ctrl.state == GoldenGgState.FAULT
        assert fake_bq.sealed is False, "a fake chip valojaban nem zarodott vissza"
        assert any(
            f.startswith("BQ_SEAL_RESTORE_FAILED_AT_EMERGENCY_STOP:")
            and "SS bit still clear" in f
            for f in ctrl.quality_flags
        )
        assert any(e.get("event_code") == "GOLDEN_GG_SEAL_RESTORE_FAILED" for e in events)

    def test_emergency_stop_attempts_seal_even_if_initial_ss_check_read_fails(self):
        """[regresszió, Kör 19, F19-S1/DFAM-032] korábban a "már sealed?"
        gyors ellenőrzés (_ensure_bq_sealed_or_flag() ELSŐ read_control_
        status() hívása) egy olvasási hiba esetén AZONNAL `return False`-t
        adott, SOHA meg sem kísérelve a seal()-t -- egy tranziens hiba (pl.
        Pico dropout) így a chipet UNSEALED állapotban hagyta volna egy
        olyan esetben, amikor a rákövetkező seal() RPC ténylegesen sikeres
        lehetett volna. L. traditional_learning_controller.py R17-05 fixje
        ugyanerről a hibaosztályról -- ez a hiányzó testvér-javítás."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        ctrl._bq_transport.serial.errors["control_status"] = (
            "METHOD_ERROR", "simulated I2C failure"
        )
        events = []
        ctrl.on_event = events.append

        ctrl.emergency_stop("MANUAL_TEST_FAULT")

        assert ctrl.state == GoldenGgState.FAULT
        seal_calls = [
            r for r in ctrl._bq_transport.serial.written_requests
            if r.get("method") == "seal"
        ]
        assert seal_calls, "seal() SOHA nem lett megkísérelve az első olvasási hiba után"
        assert any(
            f.startswith("BQ_SEAL_STATE_VERIFY_FAILED_AT_EMERGENCY_STOP:")
            for f in ctrl.quality_flags
        )
        assert any(
            e.get("event_code") == "GOLDEN_GG_SEAL_STATE_VERIFY_FAILED" for e in events
        )

    def test_emergency_stop_reseal_confirmed_when_only_initial_ss_check_read_fails(self):
        """[MAX verifier P3-2 lelete, Kör 19] a fenti teszt
        (errors["control_status"] STATIKUS, tehát a post-seal
        visszaellenőrzést IS elrontja) csak azt az esetet fedi, amikor
        MINDKÉT olvasás hibázik. Ez a teszt kifejezetten azt az esetet
        célozza, ami a fixet ténylegesen indokolja: az ELSŐ ("már sealed?")
        olvasás hibázik, DE a seal() utáni visszaellenőrző olvasás MÁR
        sikeres -- ilyenkor a reseal-nek TÉNYLEGESEN igazoltnak kell
        lennie (_bq_reseal_confirmed=True), nem csak annak, hogy a seal()
        RPC ki lett küldve."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE

        real_read = ctrl._bq_driver.read_control_status
        calls = {"n": 0}

        def _flaky_read_control_status():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated one-shot I2C failure")
            return real_read()

        ctrl._bq_driver.read_control_status = _flaky_read_control_status

        ctrl.emergency_stop("MANUAL_TEST_FAULT")

        assert ctrl.state == GoldenGgState.FAULT
        assert calls["n"] == 2, "a masodik (post-seal) olvasasnak is meg kellett tortennie"
        assert fake_bq.sealed is True
        assert ctrl._bq_reseal_confirmed is True, (
            "a reseal-nek igazoltnak kell lennie, ha a post-seal olvasas sikeres"
        )

    def test_export_reseals_chip_on_done(self, tmp_path):
        """L. TestFullSequenceToExport.test_full_sequence_reaches_done_
        with_export() ugyanezt a fizikai szekvenciát futtatja -- itt
        kizárólag a session-végi seal-visszazárást ellenőrizzük rajta."""
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> FULL_CHARGE
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert fake_bq.sealed is False

        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.CHARGE_TERMINATION_VERIFIED
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

        TestFullSequenceToExport()._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE
        assert fake_bq.sealed is False

        dmm_v.voltage_V = 9.0  # 6 * 1.75 = 10.5V alatt
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.RELAX_5H

        TestFullSequenceToExport()._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE
        assert fake_bq.sealed is False

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE
        assert fake_bq.sealed is True
        assert ctrl.quality_flags == ()


class TestR211bPendingBqUnsealed:
    """R2-11b (2026-09-15, Teljes kódbázis review 2. kör, 14. writer-kör,
    DFAM-016 terminal_cleanup_restore_omission + DFAM-026): a
    `pending_bq_unsealed` property a `TraditionalLearningController.
    pending_bq_df_restore` (Kör 7/C-12) cross-process nyomkövetési
    mintájának GG-oldali, SZÁNDÉKOSAN KÜLÖN fogalmú testvére -- ez a
    chip SEAL-állapotának (nem egy Data Flash értéknek) a maradványát
    követi, l. golden_gg_controller.py::pending_bq_unsealed docstringje."""

    def test_none_before_any_unseal_attempt(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        assert ctrl.pending_bq_unsealed is None
        assert ctrl._bq_unseal_attempted is False

        ctrl.advance(0.0)  # INIT -> PRECOND, még nem futott a PRECOND teste

        assert ctrl.pending_bq_unsealed is None

    def test_none_when_precond_faults_before_unseal(self):
        """A CHEM_ID_MISMATCH-kapu (l. TestPrecondSourceSnapshot::test_
        precond_faults_on_chem_id_mismatch) az unseal() HÍVÁS ELŐTT fault-ol
        -- `_bq_unseal_attempted` sosem vált True-ra, tehát a property
        None marad, FÜGGETLENÜL attól, hogy az emergency_stop() saját
        _ensure_bq_sealed_or_flag()-ja mit talál (a chip ilyenkor mindvégig
        ténylegesen SEALED maradt, l. _FakeBqState.sealed alapérték)."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(expected_chem_id=0x1234, fake_bq=fake_bq)

        ctrl.advance(0.0)
        ctrl.advance(1.0)

        assert ctrl.state == GoldenGgState.FAULT
        assert "CHEM_ID_MISMATCH" in ctrl.fault_reason
        assert ctrl._bq_unseal_attempted is False
        assert ctrl.pending_bq_unsealed is None

    def test_set_after_verified_unseal_and_persists_through_cycle(self):
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert fake_bq.sealed is False

        pending = ctrl.pending_bq_unsealed
        assert pending is not None
        assert pending["controller"] == "GOLDEN_GG"
        assert pending["bq_unsealed"] is True

        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.CHARGE_TERMINATION_VERIFIED
        assert ctrl.pending_bq_unsealed is not None

    def test_cleared_after_confirmed_reseal_on_done(self, tmp_path):
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, _load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert ctrl.pending_bq_unsealed is not None

        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H

        TestFullSequenceToExport()._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.RELAX_5H

        TestFullSequenceToExport()._run_relax_to_completion(ctrl)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE

        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.DONE
        assert fake_bq.sealed is True
        assert ctrl.pending_bq_unsealed is None

    def test_still_set_when_reseal_raises(self):
        """Mutáció-bizonyíték: ha a `_bq_reseal_confirmed = True` sor
        törlődne az `_ensure_bq_sealed_or_flag()` sikeres ágából, ez a
        teszt NEM buktatná ezt -- ez a teszt kifejezetten a SIKERTELEN
        reseal ágat védi (a property NEM törlődhet, ha a záró seal()
        kivételt dob)."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        ctrl._bq_transport.serial.errors["seal"] = ("METHOD_ERROR", "simulated seal failure")

        ctrl.emergency_stop("MANUAL_TEST_FAULT")

        assert ctrl.state == GoldenGgState.FAULT
        pending = ctrl.pending_bq_unsealed
        assert pending is not None
        assert pending["bq_unsealed"] is True
        assert any(
            f.startswith("BQ_SEAL_RESTORE_FAILED_AT_EMERGENCY_STOP:") for f in ctrl.quality_flags
        )

    def test_still_set_when_seal_accepted_but_ss_bit_still_clear(self):
        """Mutáció-bizonyíték: ez a teszt konkrétan azt a hibát fogná meg,
        ha valaki a `_bq_reseal_confirmed = True`-t a PARANCS elküldése
        (nem a readback-kel igazolt SS bit) alapján állítaná be -- itt a
        seal() RPC 'ok'-val válaszol, de a fake chip ténylegesen NEM zárult
        vissza."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        ctrl._bq_transport.serial.results["seal"] = lambda params: None

        ctrl.emergency_stop("MANUAL_TEST_FAULT")

        assert ctrl.state == GoldenGgState.FAULT
        assert fake_bq.sealed is False
        pending = ctrl.pending_bq_unsealed
        assert pending is not None
        assert pending["bq_unsealed"] is True

    def test_unseal_rpc_failure_cleared_by_observed_sealed_state(self):
        """Fail-closed a `_bq_unseal_attempted`-re: az unseal() RPC maga
        hibázik (a chip TÉNYLEGESEN SEALED marad), az emergency_stop()
        saját `_ensure_bq_sealed_or_flag()`-ja a MEGFIGYELT SS bit alapján
        (nem a sikertelen unseal-kísérlet alapján) helyesen None-ra törli
        a maradványt -- nincs hamis pozitív."""
        fake_bq = _FakeBqState()
        ctrl, *_ = make_controller(fake_bq=fake_bq)
        ctrl._bq_transport.serial.errors["unseal"] = ("METHOD_ERROR", "simulated unseal failure")

        ctrl.advance(0.0)
        ctrl.advance(1.0)

        assert ctrl.state == GoldenGgState.FAULT
        assert "BQ_UNSEAL_FAILED" in ctrl.fault_reason
        assert ctrl._bq_unseal_attempted is True
        assert fake_bq.sealed is True
        assert ctrl.pending_bq_unsealed is None


class TestReviewRound5S505UnhandledStateRaises:
    """[regresszió, review-kör 5, Session/Workflow S5-05 (P3, DFAM-004
    sibling) -- l. test_charge_controller.py azonos hivatkozású tesztje
    ugyanerről a hibaosztályról] advance() korábban hallgatólagosan
    (else nélkül) no-op-olt egy le nem kezelt GoldenGgState-re."""

    def test_advance_raises_on_unhandled_state(self):
        ctrl, *_ = make_controller()
        ctrl._state = "BOGUS_FUTURE_STATE"
        with pytest.raises(ValueError, match="BOGUS_FUTURE_STATE"):
            ctrl.advance(1.0)


class TestChargeEnergyDeltaFromEmbeddedFastLoop:
    """[regresszió, R1 B8a, F-20/DFAM-035 -- a korábbi C-06
    TestChargeEnergyIntegratorIncludesFastLoopExtraTime utódja] A session-
    szintű Ah/Wh a beágyazott, VALÓDI ChargeController fast-loop
    résztickjeinek (hívásonként változó áram ÉS feszültség) elintegrált
    deltáját veszi át (l. Prog/tests/mock_drivers/fast_loop_charge.py). A
    régi C-06 teszt a _last_fast_loop_extra_s-t belső integrálás nélkül
    injektálta, és az elvárást ugyanabból a hibás modellből (utolsó áram ×
    (dt + extra)) számolta (tautologikus fixture, DFAM-006) -- a javítás
    előtti kódon ezek a tesztek buknak. Megőrzött assertek: a publikus
    last_fast_loop_extra_s proxy (MAX verifier V-5) és a dt-diagnosztika
    (max_observed_dt_s == dt + extra), nulla delta mellett is."""

    def _reach_child(self, monkeypatch, **charge_cfg_kwargs):
        install_fast_loop_clock(monkeypatch)
        cfg = fast_loop_charge_config(**charge_cfg_kwargs)
        ctrl, psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller(charge_config=cfg)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> FULL_CHARGE (beágyazott ChargeController létrejön)
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert ctrl._charge_ctrl is not None
        return ctrl, psu, dmm_v

    def test_multi_tick_variable_current_and_voltage(self, monkeypatch):
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V)
        )
        for _ in range(3):
            rec = run_tick(ctrl, child, readings)
            assert child.state == ChargeState.CHARGE_CV_DMM_CONTROL
            assert_parent_delta_equals_independent_expectation(
                rec, SUBSTEPS_PER_TICK, DT_S + EXTRA_S
            )
            assert ctrl.last_fast_loop_extra_s == child.last_fast_loop_extra_s == EXTRA_S
        assert ctrl.max_observed_dt_s == DT_S + EXTRA_S
        assert ctrl.extreme_dt_s_count == 0

    def test_partial_success_then_fault_tick_keeps_integrated_substeps(self, monkeypatch):
        """A 4. résztick PSU-áramolvasása hibázik (PSU_COMM_LOST): az első
        3 résztick már elintegrált töltése a session-szintű Ah/Wh-ba is
        bekerül (a régi kód a FAULT-tick 0,0 áramával mindet elveszítette)."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V), fail_current_at=3
        )
        rec = run_tick(ctrl, child, readings)
        assert child.state == ChargeState.FAULT
        assert child.fault_reason == "PSU_COMM_LOST"
        assert ctrl.state == GoldenGgState.FAULT
        assert_parent_delta_equals_independent_expectation(
            rec, 3, DT_S + 3 * PERIOD_S, check_last_sample_model=False
        )
        assert ctrl.max_observed_dt_s == DT_S + 3 * PERIOD_S

    def test_charge_done_tick_delta_transferred_before_child_dropped(self, monkeypatch):
        """TAPER_HOLD-ban a 4. résztick éri el a taper_hold_s-t (2,6 s):
        CHARGE_DONE, a szülő eldobja a gyermeket -- a tick teljes deltája
        előtte átkerül."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch, taper_hold_s=2.6)
        child = ctrl._charge_ctrl
        child._taper_timer_s = 0.0
        target_V = force_child_state(child, psu, ChargeState.TAPER_HOLD)
        readings = ScriptedChargeReadings(
            psu, dmm_v, taper_currents(), varying_voltages(target_V)
        )
        rec = run_tick(ctrl, child, readings)
        assert child.state == ChargeState.CHARGE_DONE
        assert ctrl._charge_ctrl is None
        assert ctrl.state == GoldenGgState.CHARGE_TERMINATION_VERIFIED
        assert_parent_delta_equals_independent_expectation(rec, 4, DT_S + 3 * PERIOD_S)
        assert ctrl.max_observed_dt_s == DT_S + 3 * PERIOD_S

    def test_zero_delta_tick_still_records_dt_diagnostics(self, monkeypatch):
        """Nem integráló gyermek-tick (INIT -> PRECHECK): nulla delta, a
        dt-diagnosztika (max_observed_dt_s) ekkor is vezetődik."""
        ctrl, _psu, _dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        assert child.state == ChargeState.INIT
        ctrl.advance(7.0)
        assert child.accumulated_charge_Ah == 0.0
        assert ctrl.accumulated_charge_Ah == 0.0
        assert ctrl.accumulated_charge_Wh == 0.0
        assert ctrl.max_observed_dt_s == 7.0

    def test_gg_export_sample_time_current_and_bq_throttle_unchanged(self, monkeypatch):
        """A GoldenGgSample (TI-export időtengely/áram) és a BQ-poll-throttle
        változatlanul dt_s + extra-t és a tick utolsó friss áramolvasását
        kapja (GG időalap, MAX verifier V-3) -- csak az Ah/Wh forrása
        változott."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V)
        )
        n0 = len(ctrl.samples)
        t0 = ctrl.samples[-1].elapsed_s if ctrl.samples else 0.0
        rec1 = run_tick(ctrl, child, readings)
        s1 = ctrl.samples[-1]
        assert len(ctrl.samples) == n0 + 1
        assert s1.elapsed_s == pytest.approx(t0 + DT_S + EXTRA_S)
        assert s1.current_A == rec1.pairs[-1][0]
        assert s1.voltage_V == rec1.pairs[-1][1]
        assert ctrl._gauge_poll_elapsed_s == pytest.approx(DT_S + EXTRA_S)
        rec2 = run_tick(ctrl, child, readings)
        s2 = ctrl.samples[-1]
        assert s2.elapsed_s == pytest.approx(t0 + 2 * (DT_S + EXTRA_S))
        assert s2.current_A == rec2.pairs[-1][0]
        # 2 × 3,75 s >= sample_interval_s (5,0 s): a poll lefutott, a
        # küszöb-akkumulátor nullázódott.
        assert ctrl._gauge_poll_elapsed_s == 0.0

    # -------------------------------------------------------------- #
    # R2-B5 (R2-session-02, DFAM-033): kivételes kilépés a gyermek
    # fast-loopjából (Ctrl+C a résztickek közötti sleep()-ben).
    @staticmethod
    def _install_interrupting_fast_loop_clock(monkeypatch, k_sleep):
        """A FastLoopClock-kal azonos kézi óra (sleep() léptet,
        perf_counter() olvas), de a k_sleep-edik fast-loop sleep()
        KeyboardInterrupt-ot dob. Csak a charge_controller modul `time`
        nevét cseréli (l. install_fast_loop_clock())."""
        from types import SimpleNamespace

        import Prog.src.charge_controller as cc_module

        clock = {"now": 0.0, "sleeps": 0}

        def perf_counter():
            return clock["now"]

        def sleep(seconds):
            clock["sleeps"] += 1
            if clock["sleeps"] == k_sleep:
                raise KeyboardInterrupt("R2-B5 scripted Ctrl+C in fast-loop sleep")
            clock["now"] += seconds

        monkeypatch.setattr(
            cc_module, "time",
            SimpleNamespace(perf_counter=perf_counter, sleep=sleep),
        )

    @pytest.mark.parametrize("k_sleep", [1, 4])
    def test_keyboard_interrupt_in_fast_loop_sleep_transfers_child_delta_once(
        self, monkeypatch, k_sleep
    ):
        """[regresszió, R2-B5 (R2-session-02), DFAM-033] Ctrl+C a gyermek
        k-adik fast-loop sleep()-jében: a gyermek által addig MÁR
        elintegrált k résztick Ah/Wh-deltája és a dt-diagnosztika
        (dt_s + (k-1)·period_s) pontosan egyszer átkerül a session-szintű
        integrátorba, az eredeti kivétel változatlanul továbbmegy. A javítás
        előtt az átadás csak az advance() normál visszatérése után állt --
        a szülő a már elintegrált töltést elveszítette."""
        ctrl, psu, dmm_v = self._reach_child(monkeypatch)
        child = ctrl._charge_ctrl
        target_V = force_child_state(child, psu, ChargeState.CHARGE_CV_DMM_CONTROL)
        readings = ScriptedChargeReadings(
            psu, dmm_v, cv_currents(), varying_voltages(target_V)
        )
        self._install_interrupting_fast_loop_clock(monkeypatch, k_sleep)
        p_ah, p_wh = ctrl.accumulated_charge_Ah, ctrl.accumulated_charge_Wh
        c_ah, c_wh = child.accumulated_charge_Ah, child.accumulated_charge_Wh
        # GG: a kivételes átadás NEM a _record_sample()-lel megy (az
        # exportsort, _log_elapsed_s-t és BQ-pollt is végezne).
        n_samples = len(ctrl._samples)
        log_elapsed_before = ctrl._log_elapsed_s
        poll_calls = []
        real_poll = ctrl._poll_bq_diagnostics_throttled

        def counting_poll():
            poll_calls.append(1)
            return real_poll()

        monkeypatch.setattr(ctrl, "_poll_bq_diagnostics_throttled", counting_poll)
        with pytest.raises(KeyboardInterrupt):
            ctrl.advance(DT_S)
        assert len(readings.pairs) == k_sleep
        exp_ah, exp_wh = expected_delta(readings.pairs, DT_S, PERIOD_S)
        assert exp_ah > 0.0 and exp_wh > 0.0
        assert child.accumulated_charge_Ah - c_ah == pytest.approx(exp_ah, rel=1e-12, abs=1e-15)
        assert child.accumulated_charge_Wh - c_wh == pytest.approx(exp_wh, rel=1e-12, abs=1e-15)
        assert ctrl.accumulated_charge_Ah - p_ah == pytest.approx(exp_ah, rel=1e-12, abs=1e-15)
        assert ctrl.accumulated_charge_Wh - p_wh == pytest.approx(exp_wh, rel=1e-12, abs=1e-15)
        assert ctrl.max_observed_dt_s == DT_S + (k_sleep - 1) * PERIOD_S
        assert len(ctrl._samples) == n_samples
        assert ctrl._log_elapsed_s == log_elapsed_before
        assert poll_calls == []


class TestLoadCurrentReadbackResetOrder:
    """MAX verifier V-1 (2026-09-15, R2-06, 12. writer-kör): F2 regressziós
    bizonyíték korábban csak a discharge_controller.py-ra létezett
    explicit módon -- ez a teszt ugyanazt a C-08a F-1 hibaosztály-mintát
    replikálja golden_gg_controller.py-ra: ha a nullázás nem `advance()`
    LEGELSŐ utasítása (minden korai-return/safety-kapu előtt), a property
    az ELŐZŐ tick értékén ragadna egy top-of-tick DMM_FEEDBACK_LOST
    ticken. Mutáció, amin bukik: a nullázás elhagyása/áthelyezése."""

    def test_resets_to_none_on_dmm_feedback_lost_gate(self):
        fake_bq = _FakeBqState()
        ctrl, _psu, load, dmm_v, _dmm_t, _fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5, relax_after_discharge_s=0.5,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.SYSTEM_RATE_DISCHARGE)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE

        load.current_A = 1.5
        ctrl.advance(1.0)
        assert ctrl.load_current_readback_valid is True
        assert ctrl.i_load_measured_A == pytest.approx(1.5)

        dmm_v.simulate_timeout = True  # top-of-tick DMM_FEEDBACK_LOST kapu
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.load_current_readback_valid is None
        assert ctrl.i_load_measured_A is None


class TestEnergyIntegratorStaleGuard:
    """Teljes kódbázis review 2. kör (2026-09-15, R2-18, 12. writer-kör):
    `i_psu_measured_A` (a beágyazott ChargeController `_i_charge`
    mezőjét tükrözi) NEM tick-scoped -- egy top-of-tick safety-kapu
    FAULT-ticken (ahol `_run_charge_cc()` nem fut, tehát `_read_psu_
    current()` sem) az ELŐZŐ, sikeres tick áram-értékén marad, miközben
    `psu_current_readback_valid` helyesen None-ra reset. Enélkül egy
    ilyen ticken (pl. a DMM_FEEDBACK_LOST kapu) elévült, nem-nulla áram
    integrálódna a session-szintű `self._energy_integrator`-ba."""

    def _reach_charge_cc_with_baseline_current(self, dmm_voltage_V=13.0, psu_current_A=1.0):
        from Prog.src.charge_controller import ChargeState

        ctrl, psu, _load, dmm_v, _dmm_t, _fake_bq = make_controller(dmm_voltage_V=dmm_voltage_V)
        ctrl.advance(0.0)  # INIT -> PRECOND
        ctrl.advance(1.0)  # PRECOND -> FULL_CHARGE
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        ctrl._charge_ctrl._charge_current_set_A = 1.5
        psu.set_output_current(1.5)
        ctrl._charge_ctrl._state = ChargeState.CHARGE_CC
        psu.current_A = psu_current_A
        ctrl.advance(2.0)  # valódi, sikeres tick -- _i_charge/valid mindkettő friss
        assert ctrl._charge_ctrl.psu_current_readback_valid is True
        assert ctrl._charge_ctrl.i_psu_measured_A == pytest.approx(psu_current_A)
        return ctrl, dmm_v

    def test_fault_gate_tick_does_not_integrate_stale_psu_current(self):
        ctrl, dmm_v = self._reach_charge_cc_with_baseline_current()
        ah_before = ctrl.accumulated_charge_Ah

        dmm_v.simulate_timeout = True  # top-of-tick DMM_FEEDBACK_LOST kapu
        ctrl.advance(3600.0)  # nagy dt_s -- ha elévült áramot integrálna, azonnal látszana

        assert ctrl._charge_ctrl.psu_current_readback_valid is None
        assert ctrl.accumulated_charge_Ah == pytest.approx(ah_before)

    def test_dt_statistics_still_recorded_on_such_tick(self):
        """Mutációs őr: a HELYES fix `0.0`-t integrál (nem `skip`-el), hogy
        a dt-statisztikák (max_observed_dt_s/extreme_dt_s_count) TOVÁBBRA
        IS frissüljenek ezen a ticken -- egy `skip`-alapú (téves) fix-
        variáns ezen a teszten bukna el."""
        ctrl, dmm_v = self._reach_charge_cc_with_baseline_current()

        dmm_v.simulate_timeout = True
        ctrl.advance(3600.0)

        assert ctrl.max_observed_dt_s >= 3600.0

    def test_gpc_sample_row_current_is_not_stale_on_fault_gate_tick(self):
        """golden_gg-specifikus kiegészítés: az utolsó `samples` sor
        current_A mezője se maradjon elévült nem-nulla értéken."""
        ctrl, dmm_v = self._reach_charge_cc_with_baseline_current()

        dmm_v.simulate_timeout = True
        ctrl.advance(3600.0)

        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.samples[-1].current_A == pytest.approx(0.0)


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


class TestR1B8cF28ExportBlockedOnOutputGapAbove100s:
    """[regresszió, R1 B8c, F-28 (P3), DFAM-003, D-B8-4] egy >100 s-os
    kimeneti adatlyuk (itt: 150 s-os relax-tickek) mellett a builder írás
    előtt kivétellel blokkol; a controller a meglévő GOLDEN_GG_EXPORT_FAILED
    úton FAULT-ra áll, a package_ready_for_ti_submission False marad és
    GOLDEN_GG_PACKAGE_READY esemény NEM megy ki (a nyers minták megmaradnak)."""

    def test_gap_above_100s_faults_export_without_package_ready(self, tmp_path):
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=1.0, relax_after_discharge_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
            export_output_dir=tmp_path, export_config=export_config,
        )
        events: list = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H
        for _ in range(5):
            if ctrl.state != GoldenGgState.RELAX_2H:
                break
            ctrl.advance(150.0)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        for _ in range(5):
            if ctrl.state != GoldenGgState.RELAX_5H:
                break
            ctrl.advance(2.0)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE
        gaps = [b.elapsed_s - a.elapsed_s for a, b in zip(ctrl.samples, ctrl.samples[1:])]
        assert max(gaps) > 100.0

        ctrl.advance(0.1)

        assert ctrl.state == GoldenGgState.FAULT
        assert "GOLDEN_GG_EXPORT_FAILED" in ctrl.fault_reason
        assert ctrl.package_ready_for_ti_submission is False
        assert ctrl.export_zip_path is None
        assert not (tmp_path / "golden_gg_submission.zip").exists()
        assert not any(e.get("event_code") == "GOLDEN_GG_PACKAGE_READY" for e in events)
        assert load.input_commanded_on is False
        assert len(ctrl.samples) > 0


class TestR1B8cFixupF28CsvRoundedGapAbove100sBlocksPackageReady:
    """[regresszió, R1 B8c fixup, F-28 N1 (Codex max verifier), DFAM-003]
    Nyers köz 100,0 s (float szerint NEM sértés), de a CSV-be írt,
    1 tizedesre kerekített időbélyegek [0.1, 100.2] -> 100,1 s köz. A
    B8c-ben a builder ezt átengedte: DONE + package_ready_for_ti_submission
    True + GOLDEN_GG_PACKAGE_READY. Elvárás: a meglévő
    GOLDEN_GG_EXPORT_FAILED út, készjelzés nélkül."""

    def test_rounded_csv_gap_above_100s_faults_without_package_ready(self, tmp_path):
        from Prog.src.golden_gg_export import GoldenGgSample
        fake_bq = _FakeBqState()
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=100.0,
        )
        ctrl, psu, load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            export_output_dir=tmp_path, export_config=export_config,
        )
        events: list = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        for _ in range(50):
            if ctrl.state != GoldenGgState.RELAX_2H:
                break
            ctrl.advance(2.0)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        for _ in range(50):
            if ctrl.state != GoldenGgState.RELAX_5H:
                break
            ctrl.advance(2.0)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE
        # A nyers minták a CSV-kerekítési határesetre cserélve (az
        # EXPORT_PACKAGE tick nem rögzít új mintát).
        raw = [0.15, 100.15]
        assert raw[1] - raw[0] <= 100.0
        ctrl._samples = [GoldenGgSample(t, 12.5, -2.0, 22.0) for t in raw]

        ctrl.advance(0.1)

        assert ctrl.state == GoldenGgState.FAULT
        assert "GOLDEN_GG_EXPORT_FAILED" in ctrl.fault_reason
        assert "100.1" in ctrl.fault_reason
        assert ctrl.package_ready_for_ti_submission is False
        assert ctrl.export_zip_path is None
        assert not (tmp_path / "golden_gg_submission.zip").exists()
        assert not any(e.get("event_code") == "GOLDEN_GG_PACKAGE_READY" for e in events)
        assert load.input_commanded_on is False


class TestR1F43OutOfTiRangeQmaxBlocksPackageReady:
    """[regresszió, R1 F-43 (P2, NS-13), DFAM-003] Teljes lánc: a BQ DF
    Qmax-szava 0xFFFF / 0x8000 (előjeles I2 -> -1 / -32768, .valid=True).
    HEAD c05b511-en a ciklus DONE + package_ready_for_ti_submission True +
    GOLDEN_GG_PACKAGE_READY lett. Elvárás: a PRECOND :900 kapu változatlan
    (csak .valid; D-F43-1, KEEP_AS_IS), az EXPORT_PACKAGE ticken a meglévő
    GOLDEN_GG_EXPORT_FAILED út (Load OFF, PSU OFF, seal), készjelzés nélkül,
    a nyers minták megmaradnak."""

    @pytest.mark.parametrize("raw_word, decoded", [(0xFFFF, -1), (0x8000, -32768)])
    def test_out_of_ti_range_qmax_faults_export_without_package_ready(
        self, tmp_path, raw_word, decoded,
    ):
        fake_bq = _FakeBqState()
        fake_bq.qmax = raw_word
        export_config = GoldenGgConfig(
            num_cell_series=6, battery_model="TST7AH", battery_sample_id="S1",
            system_rate_current_A=2.0, sample_interval_s=5.0,
        )
        ctrl, psu, load, dmm_v, _dmm_t, fake_bq = make_controller(
            dmm_voltage_V=13.5, fake_bq=fake_bq,
            export_output_dir=tmp_path, export_config=export_config,
        )
        events: list = []
        ctrl.on_event = events.append
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        # :900 KEEP_AS_IS (D-F43-1): a PRECOND csak .valid-ot néz.
        assert ctrl.state == GoldenGgState.FULL_CHARGE
        assert ctrl._gg_source_snapshot.qmax.valid is True
        assert ctrl._gg_source_snapshot.qmax.value == decoded
        psu.current_A = 2.5
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        ctrl.advance(0.1)
        for _ in range(50):
            if ctrl.state != GoldenGgState.RELAX_2H:
                break
            ctrl.advance(2.0)
        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE
        dmm_v.voltage_V = 9.0
        ctrl.advance(1.0)
        for _ in range(50):
            if ctrl.state != GoldenGgState.RELAX_5H:
                break
            ctrl.advance(2.0)
        assert ctrl.state == GoldenGgState.EXPORT_PACKAGE

        ctrl.advance(0.1)

        assert ctrl.state == GoldenGgState.FAULT
        assert "GOLDEN_GG_EXPORT_FAILED" in ctrl.fault_reason
        assert f"OUT_OF_TI_RANGE(value={decoded})" in ctrl.fault_reason
        assert ctrl.package_ready_for_ti_submission is False
        assert ctrl.export_zip_path is None
        assert not (tmp_path / "golden_gg_submission.zip").exists()
        assert not any(e.get("event_code") == "GOLDEN_GG_PACKAGE_READY" for e in events)
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert fake_bq.sealed is True
        assert len(ctrl.samples) > 0


class TestR1B3bDischargeStartBatteryVoltageGate:
    """[regresszió, Claude-Codex loop R1 B3b: NS-02 (DFAM-001)]

    `_begin_system_rate_discharge()` a B3 DMM-őr után `check_battery_voltage()` nélkül adott
    Load INPUT ON-t: egy érvényes, de a meglévő safety-limiten
    (`SafetyManager.check_battery_voltage()`: U_batt <= 0 V vagy
    > `batt_absolute_max_V`) kívüli U_batt csak a KÖVETKEZŐ tick per-tick
    kapujában (`_run_system_rate_discharge()`) okozott FAULT-ot, az energizálás UTÁN. Elvárt
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
        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason == expected_reason
        assert "input_on()" not in load.calls
        assert not any(c.startswith("set_current") for c in load.calls)
        assert load.input_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls

    @staticmethod
    def _make():
        ctrl, psu, load, *_ = make_controller()
        return ctrl, psu, load

    @pytest.mark.parametrize("case", ["zero", "negative", "above_abs_max"])
    def test_bad_voltage_faults_before_energizing(self, case):
        ctrl, psu, load = self._make()
        u, reason = self._bad_voltage(ctrl, case)
        ctrl._u_batt = u

        ctrl._begin_system_rate_discharge()

        self._assert_faulted_without_energize(ctrl, psu, load, reason)
        load.reset_calls()
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert "input_on()" not in load.calls

    def test_dmm_guard_precedes_voltage_gate(self):
        """Sorrend a (karakterizáció): DMM-hiba + 0 V -> a B3 DMM-őr nyer."""
        ctrl, _psu, load = self._make()
        ctrl._dmm_valid = False
        ctrl._u_batt = 0.0

        ctrl._begin_system_rate_discharge()

        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert "input_on()" not in load.calls

    def test_voltage_gate_precedes_concurrent_gate(self):
        """Sorrend b: PSU commanded ON + 0 V -> a feszültségkapu nyer, nem a concurrent-kapu."""
        ctrl, psu, load = self._make()
        psu.output_commanded_on = True
        ctrl._u_batt = 0.0

        ctrl._begin_system_rate_discharge()

        assert ctrl.fault_reason == "REVERSE_POLARITY_OR_WIRING_FAULT"
        assert "input_on()" not in load.calls

    def test_exact_abs_max_still_energizes(self):
        """Határ (karakterizáció): pontosan batt_absolute_max_V mellett még
        energizál -- nincs új, szigorúbb limit."""
        ctrl, _psu, load = self._make()
        ctrl._u_batt = ctrl._profile.batt_absolute_max_V

        ctrl._begin_system_rate_discharge()

        assert "input_on()" in load.calls
        assert ctrl.state != GoldenGgState.FAULT

    def test_chain_relax_2h_end_zero_volt_faults_before_system_rate_energize(self):
        """Lánc: valódi tick, friss, érvényes 0 V DMM-olvasás a RELAX_2H végén
        -> FAULT a system-rate energizálás ELŐTT, a BQ visszazárva."""
        fake_bq = _FakeBqState()
        ctrl, psu, load, dmm_v, _dmm_t, fake_bq = make_controller(
            fake_bq=fake_bq,
            config=GoldenGgPhysicalConfig(
                relax_after_charge_s=0.5,
                relax_after_discharge_s=0.5,
                system_rate_current_A=2.0,
                sample_interval_s=5.0,
            ),
        )
        ctrl.advance(0.0)
        ctrl.advance(1.0)
        _force_charge_done(ctrl)
        ctrl.advance(1.0)
        _advance_until(ctrl, GoldenGgState.RELAX_2H, dt_s=0.1)
        ctrl.advance(0.1)
        assert ctrl.state == GoldenGgState.RELAX_2H
        load.reset_calls()
        psu.reset_calls()
        dmm_v.voltage_V = 0.0

        ctrl.advance(1.0)

        self._assert_faulted_without_energize(
            ctrl, psu, load, "REVERSE_POLARITY_OR_WIRING_FAULT"
        )
        assert fake_bq.sealed is True


class TestR8B1GoldenGgStartPowerGate:
    """R8-B1: a system-rate konfiguracio arama adja az inditasi teljesitmenyt."""

    def test_over_power_faults_before_setup(self):
        cfg = GoldenGgPhysicalConfig(system_rate_current_A=30.0)
        ctrl, psu, load, *_ = make_controller(dmm_voltage_V=12.5, config=cfg)
        ctrl._u_batt = 12.5
        events = []
        ctrl.on_event = events.append

        ctrl._begin_system_rate_discharge()

        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason == "LOAD_POWER_LIMIT_EXCEEDED"
        assert load.input_commanded_on is False
        assert psu.output_commanded_on is False
        assert "input_off()" in load.calls
        assert "all_outputs_off()" in psu.calls
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)
        assert any(
            e.get("event_code") == "GOLDEN_GG_FAULT"
            and e.get("event_message") == "LOAD_POWER_LIMIT_EXCEEDED" for e in events
        )
        load.reset_calls()
        psu.reset_calls()
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert not any(c.startswith(("set_mode_cc", "set_current", "input_on")) for c in load.calls)
        assert not any(c.startswith(("set_output_", "output_on")) for c in psu.calls)

    @pytest.mark.parametrize("current_A", [19.9, 20.0])
    def test_at_or_below_limit_starts(self, current_A):
        cfg = GoldenGgPhysicalConfig(system_rate_current_A=current_A)
        ctrl, _psu, load, *_ = make_controller(dmm_voltage_V=12.5, config=cfg)
        ctrl._u_batt = 12.5

        ctrl._begin_system_rate_discharge()

        assert ctrl.state == GoldenGgState.SYSTEM_RATE_DISCHARGE
        assert load.input_commanded_on is True
        assert f"set_current({current_A})" in load.calls


class TestR16B16aNormalFloorSibling:
    @pytest.mark.parametrize("phase", ["setup", "sample", "config"])
    @pytest.mark.parametrize("failed_off", ["none", "load", "psu"])
    def test_common_floor_fault_cleanup_event_and_no_restart(self, phase, failed_off):
        parts = make_controller()
        ctrl, psu, load, dmm = parts[:4]
        ctrl._state = GoldenGgState.SYSTEM_RATE_DISCHARGE
        ctrl._dmm_valid = True
        ctrl._u_batt = dmm.voltage_V = 10.79 if phase != "config" else 12.5
        if phase == "config":
            ctrl._profile.terminate_voltage_per_cell_V = 1.60
        if phase == "sample":
            # Az első cutoff-mintát a meglévő terminate-ág rendesen lezárja.
            load.input_commanded_on = True
            ctrl._u_batt = dmm.voltage_V = 10.8
            ctrl.advance(1.0)
            assert ctrl.state == GoldenGgState.RELAX_5H
            ctrl._u_batt = dmm.voltage_V = 10.79
        calls, events = [], []
        def load_off():
            calls.append("load_off")
            if failed_off == "load":
                raise RuntimeError("load OFF error")
            load.input_commanded_on = False
        def psu_off():
            calls.append("psu_off")
            if failed_off == "psu":
                raise RuntimeError("psu OFF error")
            psu.output_commanded_on = False
        load.input_off, psu.all_outputs_off = load_off, psu_off
        ctrl.on_event = events.append
        if phase == "sample":
            load.input_commanded_on = True
            ctrl.advance(1.0)
        else:
            ctrl._begin_system_rate_discharge()
        assert ctrl.state.value == "FAULT"
        reason = "DISCHARGE_CONFIG_INVALID" if phase == "config" else "BATTERY_UNDERVOLTAGE"
        assert ctrl.fault_reason.startswith(reason)
        assert calls[:2] == ["load_off", "psu_off"]
        assert any(e["event_code"] == reason for e in events)
        ctrl.advance(1.0)
        assert not load.called("input_on")


class TestR16B16aF1OvershootCleanup:
    @pytest.mark.parametrize("seal_fails", [False, True])
    def test_throwing_callback_follows_all_bq_cleanup_attempts(self, monkeypatch, seal_fails):
        ctrl, psu, load, dmm, _temp, fake_bq = make_controller()
        ctrl._state = GoldenGgState.SYSTEM_RATE_DISCHARGE
        ctrl._dmm_valid = True
        ctrl._u_batt = dmm.voltage_V = 12.5
        ctrl._begin_system_rate_discharge()
        fake_bq.sealed = False
        calls = []
        read_status = ctrl._bq_driver.read_control_status
        real_seal = ctrl._bq_transport.seal
        load_off, psu_off = load.input_off, psu.all_outputs_off
        def off_load():
            calls.append("load")
            load_off()
        def off_psu():
            calls.append("psu")
            psu_off()
        def read():
            calls.append("read")
            if calls.count("read") == 1:
                raise RuntimeError("BQ read hiba")
            return read_status()
        def seal(*, confirm):
            calls.append("seal")
            if seal_fails:
                raise RuntimeError("BQ seal hiba")
            return real_seal(confirm=confirm)
        def fail_event(_event):
            calls.append("event")
            raise RuntimeError("naplóhiba")
        monkeypatch.setattr(load, "input_off", off_load)
        monkeypatch.setattr(psu, "all_outputs_off", off_psu)
        monkeypatch.setattr(ctrl._bq_driver, "read_control_status", read)
        monkeypatch.setattr(ctrl._bq_transport, "seal", seal)
        ctrl.on_event = fail_event
        dmm.voltage_V = 10.799
        with pytest.raises(RuntimeError, match="naplóhiba"):
            ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert not load.input_commanded_on and not psu.output_commanded_on
        # Az első esemény a túllövés; a cleanup-események csak a seal/verify után jönnek.
        expected = ["event", "load", "psu", "read", "seal"]
        if not seal_fails:
            expected += ["read"]
        assert calls == expected + ["event"]
        assert ctrl._bq_reseal_confirmed is (not seal_fails)

    def test_final_relax_drain_cannot_hide_second_loaded_undershoot(self):
        from types import SimpleNamespace

        ctrl, psu, load, dmm, _temp, _bq = make_controller()
        ctrl._state = GoldenGgState.RELAX_5H
        ctrl._normal_discharge_cutoff_seen = True
        ctrl._relax_exit_pending = True
        ctrl._u_batt = dmm.voltage_V = 12.5
        load.input_commanded_on = True
        child = SimpleNamespace(
            dmm_valid=True, last_voltage_V=12.5, dmm_fault_count=0, missed_deadline_count=0,
        )
        def drain():
            child.last_voltage_V = 10.799
            return True
        child.poll_sampling_stop = drain
        ctrl._relax_ctrl = child
        events = []
        ctrl.on_event = events.append
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason == "BATTERY_UNDERVOLTAGE"
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert any(e["event_code"] == "BATTERY_UNDERVOLTAGE" for e in events)


class _R16B16aF2Source:
    """Szál és műszer nélküli rekordforrás, külön lezáró drainnel."""

    epoch = 1602
    shutdown_error = None

    def __init__(self):
        self.records = []
        self.tail = []
        self.released = False
        self._clock = lambda: 103.1

    def drain(self):
        from Prog.src.relax_controller import RelaxSampleBatch

        batch = RelaxSampleBatch(tuple(self.records), False, self.released)
        self.records.clear()
        return batch

    def stop(self):
        self.records.extend(self.tail)
        self.tail.clear()

    def snapshot_for(self, record):
        from Prog.src.relax_controller import RelaxSamplingService

        return RelaxSamplingService.snapshot_for(self, record)


class TestR16B16aF2RawRelaxGate:
    @pytest.mark.parametrize("cells", [6, 12])
    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("drain", ["normal", "final_pending", "final_released"])
    @pytest.mark.parametrize("failed_off", ["none", "load", "psu"])
    def test_every_loaded_record_faults_before_parent_progress(
        self, monkeypatch, cells, reverse, drain, failed_off,
    ):
        from Prog.src.relax_controller import RelaxSample

        cfg = GoldenGgPhysicalConfig(
            relax_after_discharge_s=100.0 if drain == "normal" else 0.0,
            system_rate_current_A=2.0,
        )
        ctrl, psu, load, dmm, _temp, _bq = make_controller(config=cfg)
        ctrl._profile.cell_count = cells
        ctrl._profile.nominal_voltage_V = 2.0 * cells
        minimum = 1.8 * cells
        ctrl._state = GoldenGgState.SYSTEM_RATE_DISCHARGE
        load.input_commanded_on = True
        dmm.voltage_V = minimum
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.RELAX_5H
        assert ctrl._normal_discharge_cutoff_seen and not load.input_commanded_on

        source = _R16B16aF2Source()
        source.records = [RelaxSample(source.epoch, 1, 99.9, 100.1, 100.0, minimum + 0.1)]
        ctrl.prepare_relax_sampling = lambda child: child.attach_sampling_service(source)
        ctrl.advance(0.0)
        child = ctrl._relax_ctrl
        assert child is not None
        values = [minimum - 0.001, minimum + 0.001]
        if reverse:
            values.reverse()
        records = [
            RelaxSample(source.epoch, n + 2, t - 0.1, t + 0.1, t, voltage)
            for n, (t, voltage) in enumerate(zip((101.5, 103.0), values))
        ]
        if drain == "normal":
            source.records = list(records)
        else:
            source.tail = list(records)
            source.released = drain == "final_released"

        # A rendes OFF után fennmaradt terhelés; egyik OFF-hiba sem nyelheti el a másikat.
        load.input_commanded_on = True
        calls, events = [], []
        load_off, psu_off = load.input_off, psu.all_outputs_off
        def off_load():
            calls.append("load")
            if failed_off == "load":
                raise RuntimeError("F2 Load OFF hiba")
            load_off()
        def off_psu():
            calls.append("psu")
            if failed_off == "psu":
                raise RuntimeError("F2 PSU OFF hiba")
            psu_off()
        def event(payload):
            assert ctrl.state == GoldenGgState.FAULT
            assert calls[:2] == ["load", "psu"]
            events.append(payload)
        monkeypatch.setattr(load, "input_off", off_load)
        monkeypatch.setattr(psu, "all_outputs_off", off_psu)
        ctrl.on_event = event
        energy = (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh)
        on_before = sum("input_on" in call for call in load.calls)

        ctrl.advance(1.0)

        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason.startswith("BATTERY_UNDERVOLTAGE")
        assert calls == ["load", "psu"]
        assert not psu.output_commanded_on
        assert load.input_commanded_on is (failed_off == "load")
        assert any(e["event_code"] == "BATTERY_UNDERVOLTAGE" for e in events)
        assert any(e["event_code"] == "GOLDEN_GG_FAULT" for e in events)
        assert ctrl._relax_ctrl is child
        # A kapu nem írja át a nyers rekordok OLS-bélyegeit, sorrendjét vagy az Ah/Wh-t.
        assert list(child._dvdt_samples)[-2:] == [(r.sample_time, r.voltage_V) for r in records]
        assert child.last_voltage_V == values[-1]
        assert (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh) == energy
        assert ctrl._u_batt == minimum - 0.001
        for _ in range(3):
            ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert sum("input_on" in call for call in load.calls) == on_before
        assert calls == ["load", "psu"]

    @pytest.mark.parametrize("loaded", [False, True])
    @pytest.mark.parametrize("drain", ["normal", "final_released"])
    def test_safe_records_keep_sampling_and_parent_transition(self, loaded, drain):
        from Prog.src.relax_controller import RelaxSample

        cfg = GoldenGgPhysicalConfig(
            relax_after_discharge_s=100.0 if drain == "normal" else 0.0,
            system_rate_current_A=2.0,
        )
        ctrl, _psu, load, _dmm, *_ = make_controller(config=cfg)
        ctrl._state = GoldenGgState.RELAX_5H
        ctrl._normal_discharge_cutoff_seen = True
        source = _R16B16aF2Source()
        # OFF mellett az alacsony relaxfeszültség sem terhelt alaminta.
        voltage = 10.801 if loaded else 10.799
        source.records = [RelaxSample(source.epoch, 1, 99.9, 100.1, 100.0, voltage)]
        ctrl.prepare_relax_sampling = lambda child: child.attach_sampling_service(source)
        load.input_commanded_on = loaded
        ctrl.advance(0.0)
        child = ctrl._relax_ctrl
        assert child is not None
        record = RelaxSample(source.epoch, 2, 101.4, 101.6, 101.5, voltage)
        if drain == "normal":
            source.records = [record]
        else:
            source.tail = [record]
            source.released = True
        energy = (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh)
        ctrl.advance(1.0)
        expected = GoldenGgState.RELAX_5H if drain == "normal" else GoldenGgState.EXPORT_PACKAGE
        assert ctrl.state == expected
        assert list(child._dvdt_samples) == [(100.0, voltage), (101.5, voltage)]
        assert (ctrl.accumulated_discharge_Ah, ctrl.accumulated_discharge_Wh) == energy

    @pytest.mark.parametrize("bad", ["error", "missing", "nan"])
    @pytest.mark.parametrize("drain", ["normal", "final_released"])
    def test_invalid_raw_record_cannot_be_hidden_by_good_record(self, monkeypatch, bad, drain):
        from Prog.src.relax_controller import RelaxSample

        cfg = GoldenGgPhysicalConfig(
            relax_after_discharge_s=100.0 if drain == "normal" else 0.0,
            system_rate_current_A=2.0,
        )
        ctrl, psu, load, _dmm, *_ = make_controller(config=cfg)
        ctrl._state = GoldenGgState.RELAX_5H
        ctrl._normal_discharge_cutoff_seen = True
        source = _R16B16aF2Source()
        source.records = [RelaxSample(source.epoch, 1, 99.9, 100.1, 100.0, 10.801)]
        ctrl.prepare_relax_sampling = lambda child: child.attach_sampling_service(source)
        ctrl.advance(0.0)
        child = ctrl._relax_ctrl
        assert child is not None
        voltage = None if bad == "missing" else float("nan") if bad == "nan" else 10.801
        records = [
            RelaxSample(source.epoch, 2, 101.4, 101.6, 101.5, voltage,
                        "DMM_READ_ERROR" if bad == "error" else None),
            RelaxSample(source.epoch, 3, 102.9, 103.1, 103.0, 10.801),
        ]
        if drain == "normal":
            source.records = records
        else:
            source.tail = records
            source.released = True
        calls, events = [], []
        off_load, off_psu = load.input_off, psu.all_outputs_off
        def load_off():
            calls.append("load")
            off_load()
        def psu_off():
            calls.append("psu")
            off_psu()
        monkeypatch.setattr(load, "input_off", load_off)
        monkeypatch.setattr(psu, "all_outputs_off", psu_off)
        ctrl.on_event = events.append
        load.input_commanded_on = True
        on_before = sum("input_on" in call for call in load.calls)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
        assert calls == ["load", "psu"]
        assert not load.input_commanded_on and not psu.output_commanded_on
        assert any(e["event_code"] == "GOLDEN_GG_FAULT" for e in events)
        ctrl.advance(1.0)
        assert ctrl.state == GoldenGgState.FAULT
        assert sum("input_on" in call for call in load.calls) == on_before


class TestR16B18RelaxDiagnosticCleanup:
    @pytest.mark.parametrize("error_type", [None, RuntimeError, KeyboardInterrupt])
    @pytest.mark.parametrize("quality", ["READ_ERROR", "STALE", "INSUFFICIENT_DATA"])
    @pytest.mark.parametrize(
        ("temperature_C", "expected_fault"),
        [(22.0, "DMM_FEEDBACK_LOST"), (59.0, "DMM_FEEDBACK_LOST"),
         (60.0, "BATTERY_OVERTEMPERATURE"), (61.0, "BATTERY_OVERTEMPERATURE")],
    )
    def test_not_fresh_event_follows_totals_and_cleanup(
        self, monkeypatch, error_type, quality, temperature_C, expected_fault,
    ):
        from types import SimpleNamespace

        ctrl, psu, load, _dv, dt, fake = make_controller()
        ctrl._enter(GoldenGgState.RELAX_2H)
        ctrl._battery_temperature_C = dt.temperature_C = temperature_C
        ctrl._temp_dmm_valid = True
        fake.sealed = False
        psu.output_commanded_on = load.input_commanded_on = True
        ctrl._relax_dmm_fault_total = 7
        ctrl._relax_missed_deadline_total = 11
        ctrl._relax_ctrl = SimpleNamespace(
            poll_sampling_stop=lambda: True, dmm_valid=True,
            last_voltage_V=13.0, sampling_mode="SERVICE",
            read_handoff_voltage=lambda *, emit=None: SimpleNamespace(
                quality=quality, age_s=5.0, read_start=123.0,
            ),
            dmm_fault_count=2, missed_deadline_count=3,
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
        record_call(ctrl, "_ensure_bq_sealed_or_flag", "seal")

        def on_event(event):
            events.append(event)
            if event["event_code"] != "GG_RELAX_DMM_NOT_FRESH":
                return
            # A diagnosztikai fogyasztó hibája már nem szakíthatja meg a lezárást.
            assert trace == ["load", "psu", "seal"]
            assert ctrl.state == GoldenGgState.FAULT
            assert ctrl.fault_reason == expected_fault
            assert not load.input_commanded_on and not psu.output_commanded_on
            assert fake.sealed
            assert ctrl._relax_ctrl is None and not ctrl._relax_exit_pending
            assert ctrl._relax_dmm_fault_total == 9
            assert ctrl._relax_missed_deadline_total == 14
            trace.append("event")
            if error_type is not None:
                raise error_type("B18 frissesség")

        ctrl.on_event = on_event
        load.calls.clear()
        psu.calls.clear()
        if error_type is None:
            ctrl.advance(0.0)
        else:
            with pytest.raises(error_type, match="B18 frissesség"):
                ctrl.advance(0.0)

        assert trace == ["load", "psu", "seal", "event"]
        fault, = [e for e in events if e["event_code"] == "GOLDEN_GG_FAULT"]
        assert fault["event_message"] == expected_fault
        event, = [e for e in events if e["event_code"] == "GG_RELAX_DMM_NOT_FRESH"]
        reason = "DMM_READ_ERROR" if quality == "READ_ERROR" else quality
        assert event["dmm_reason"] == reason
        assert event["sample_age_s"] == 5.0 and event["read_start"] == 123.0
        assert event["event_message"].startswith(
            GoldenGgState.RELAX_2H.value + f": {reason};",
        )
        assert load.calls == ["input_off()"]
        assert psu.calls == ["all_outputs_off()"]
        before = (list(load.calls), list(psu.calls))
        ctrl.advance(1.0)
        assert (load.calls, psu.calls) == before
        assert ctrl._relax_dmm_fault_total == 9
        assert ctrl._relax_missed_deadline_total == 14


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

        ctrl, psu, load, dv, dt, fake = make_controller()
        phase = GoldenGgState.RELAX_2H
        ctrl._enter(phase)
        fake.sealed = False

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
        record_call(ctrl, "_ensure_bq_sealed_or_flag", "seal")
        cleanup = ["load", "psu", "seal"]

        def on_event(event):
            events.append(event)
            if event["event_code"] != "RELAX_DMM_FAULT":
                return
            assert trace == cleanup
            assert ctrl.state == GoldenGgState.FAULT
            assert ctrl.fault_reason == expected_fault
            assert not load.input_commanded_on and not psu.output_commanded_on
            assert fake.sealed
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
        assert ctrl.state == GoldenGgState.FAULT
        assert ctrl.fault_reason == expected_fault
        assert ctrl._relax_ctrl is None and not ctrl._relax_exit_pending
        assert ctrl._relax_dmm_fault_total == 10
        assert ctrl._relax_missed_deadline_total == 14
        assert fake.sealed
        if callback is not None:
            event, = [e for e in events if e["event_code"] == "RELAX_DMM_FAULT"]
            assert event["event_message"] == (
                phase.value + ": Átadási DMM_READ_ERROR: MockDMM: dmm_valid=False"
            )
            fault, = [e for e in events if e["event_code"] == "GOLDEN_GG_FAULT"]
            assert fault["event_message"] == expected_fault
        if callback == "record":
            diagnostic, = [
                e for e in events if e["event_code"] == "GG_RELAX_DMM_NOT_FRESH"
            ]
            assert diagnostic["dmm_reason"] == "DMM_READ_ERROR"
            assert diagnostic["read_start"] == 123.0 and diagnostic["sample_age_s"] == 0.0
        assert load.calls == ["input_off()"]
        assert psu.calls == ["all_outputs_off()"]
        # A terminális újabb tick sem ismétel totalátvételt vagy ON parancsot.
        before = (list(load.calls), list(psu.calls), list(trace), list(events))
        ctrl.advance(1.0)
        assert (load.calls, psu.calls, trace, events) == before
        assert ctrl._relax_dmm_fault_total == 10
        assert ctrl._relax_missed_deadline_total == 14


class TestR16B7F1PrecondConsistency:
    @pytest.mark.parametrize("conditioning", [False, True])
    @pytest.mark.parametrize("failure", ["energy", "scale", "x10", "readback"])
    def test_gate_blocks_before_conditioning_or_charge(self, monkeypatch, conditioning, failure):
        ctrl, psu, load, _dv, _dt, fake = make_controller(
            config=GoldenGgPhysicalConfig(
                conditioning_required=conditioning, conditioning_min_s=1.0,
                system_rate_current_A=2.0, sample_interval_s=5.0,
            ),
        )
        trace = []
        reads = []
        transport = ctrl._bq_transport
        original_read = transport.read_data_flash_block

        def read(subclass, block):
            assert fake.sealed is False
            reads.append((subclass, block))
            if failure == "readback" and subclass == 48:
                raise RuntimeError("R16-B7F1 readback")
            data = bytearray(original_read(subclass, block))
            patch = {
                "energy": (48, 0, 23, b"\x00\x01"),
                "scale": (48, 1, 13, b"\x01"),
                "x10": (64, 0, 0, b"\x09\xe1"),
            }.get(failure)
            if patch and (subclass, block) == patch[:2]:
                data[patch[2]:patch[2] + len(patch[3])] = patch[3]
            return bytes(data)

        monkeypatch.setattr(transport, "read_data_flash_block", read)
        for obj, method, label in [
            (load, "input_off", "load_off"), (psu, "all_outputs_off", "psu_off"),
            (transport, "seal", "seal"),
        ]:
            original = getattr(obj, method)

            def record(*args, _original=original, _label=label, **kwargs):
                trace.append(_label)
                return _original(*args, **kwargs)

            monkeypatch.setattr(obj, method, record)
        events = []
        ctrl.on_event = lambda event: events.append((event, fake.sealed))
        ctrl.advance(0.0)
        ctrl.advance(1.0)

        assert ctrl.state is GoldenGgState.FAULT
        assert "GOLDEN_GG_CONFIGURATION_" in ctrl.fault_reason
        if failure in {"energy", "scale", "x10"}:
            assert {"energy": "design_energy_cWh", "scale": "design_energy_scale",
                    "x10": "pack_configuration"}[failure] in ctrl.fault_reason
        elif failure == "readback":
            assert "BLOCKED_BQ_DF_READBACK_FAILED" in ctrl.fault_reason
        assert reads
        assert trace[-3:] == ["load_off", "psu_off", "seal"]
        assert fake.sealed is True
        assert psu.output_commanded_on is False
        assert load.input_commanded_on is False
        assert ctrl._charge_ctrl is None
        assert all("on(" not in call for call in psu.calls + load.calls)
        assert any(e["event_code"] == "GOLDEN_GG_FAULT" and sealed for e, sealed in events)
        before = (list(psu.calls), list(load.calls), list(reads))
        ctrl.advance(1.0)
        assert (psu.calls, load.calls, reads) == before


def test_r16b20f1_precond_logs_existing_b7_scale_readback_once(monkeypatch):
    import Prog.src.golden_gg_controller as module

    ctrl, _psu, _load, _dmm_v, _dmm_t, fake_bq = make_controller()
    events = []
    ctrl.on_event = events.append
    original = module.evaluate_provisioning_consistency
    gate_calls = []

    def gate(profile, service):
        assert fake_bq.sealed is False
        report = original(profile, service)
        gate_calls.append(report)
        return report

    monkeypatch.setattr(module, "evaluate_provisioning_consistency", gate)
    ctrl.advance(0.0)  # INIT -> PRECOND
    ctrl.advance(0.0)  # PRECOND -> charge
    assert len(gate_calls) == 1
    logged = [e for e in events if e["event_code"] == "GG_BQ_DF_CROSS_CHECK_PASSED"]
    assert len(logged) == 1
    values = {c.field_name: c.bq_readback for c in gate_calls[0].comparisons}
    assert values["design_energy_scale"] == 10
    assert values["pack_configuration"] == 0
    assert logged[0]["event_message"].endswith(
        "source=B7 PRECOND UNSEALED; Design Energy Scale=10; X10=0"
    )
    ctrl.advance(0.0)
    assert len(gate_calls) == 1
    assert sum(e["event_code"] == "GG_BQ_DF_CROSS_CHECK_PASSED" for e in events) == 1


@pytest.mark.parametrize("method", ["read_chem_id", "read_control_status"])
def test_r16b25_gg_bq_read_siblings_preserve_abort(monkeypatch, method):
    from unittest.mock import Mock

    from Prog.src.exceptions import TestAbortedByUser

    ctrl, psu, load, _dmm, _temp, _bq = make_controller()
    ctrl._u_batt = 12.8
    original = getattr(ctrl._bq_driver, method)
    aborted, reads = TestAbortedByUser("B25 GG BQ abort"), []
    write = Mock(wraps=ctrl._bq_transport.write_data_flash_block)
    monkeypatch.setattr(ctrl._bq_transport, "write_data_flash_block", write)

    def read():
        reads.append(True)
        assert len(reads) == 1, "B25 megszakadt GG BQ-olvasás nem ismételhető"
        raise aborted

    monkeypatch.setattr(ctrl._bq_driver, method, read)
    try:
        with pytest.raises(TestAbortedByUser) as caught:
            ctrl._run_precond()
        assert caught.value is aborted and reads == [True]
        assert "input_on()" not in load.calls
        write.assert_not_called()
    finally:
        monkeypatch.setattr(ctrl._bq_driver, method, original)
        ctrl.emergency_stop("B25_TEST_CLEANUP")
    assert not psu.output_commanded_on and not load.input_commanded_on
