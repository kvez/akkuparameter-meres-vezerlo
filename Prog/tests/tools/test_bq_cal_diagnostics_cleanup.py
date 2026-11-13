"""Kör 20 Batch E6 (HW20-02, P2, DFAM-016) regressziós tesztjei.

Négy `Prog/tools/bq_cal_diagnostics/` alatti diagnosztikai szkript
(`bq_cal_diag_calmode_analog_registers.py`,
`bq_cal_diag_calmode_during_active_measurement.py`,
`bq_cal_diag_mid_measurement_retry.py`, `bq_verify_voltage_divider_unsealed.py`)
a takarítást (exit_cal + seal) korábban a `try` blokk VÉGÉN végezte -- bármilyen
kivétel `unseal()`/`enter_cal()` és a takarítás KÖZÖTT a fizikai BQ34Z110-et
UNSEALED, sőt CALMODE-ban hagyta. A fix a takarítást a `finally` blokkba
helyezte, egymástól független `try/except`-ekkel (rules/10-safety.md #3: "egy
OFF-hiba ne akadályozza a másikat").

A `FakeTransport` egy hívás-naplózó, hiba-injektáló stub -- SOSEM nyúl valós
soros porthoz/hardverhez.
"""
from __future__ import annotations

import itertools
import time as time_module

import pytest

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_gauge_calibration import CalState
from Prog.tools.bq_cal_diagnostics import (
    bq_cal_diag_calmode_analog_registers as calmode_analog_registers_mod,
)
from Prog.tools.bq_cal_diagnostics import (
    bq_cal_diag_calmode_during_active_measurement as calmode_active_measurement_mod,
)
from Prog.tools.bq_cal_diagnostics import (
    bq_cal_diag_mid_measurement_retry as mid_measurement_retry_mod,
)
from Prog.tools.bq_cal_diagnostics import bq_cal_phase1_full_run as full_run_mod
from Prog.tools.bq_cal_diagnostics import bq_cal_phase1_step1 as step1_mod
from Prog.tools.bq_cal_diagnostics import bq_cal_voltage_divider_run as vd_run_mod
from Prog.tools.bq_cal_diagnostics import (
    bq_verify_voltage_divider_unsealed as verify_vd_unsealed_mod,
)

_SS_BIT = 0x2000
_FAS_BIT = 0x4000
_CALMODE_BIT = 0x1000
_BCA_BIT = 0x0400
_CCA_BIT = 0x0800


class FakeTransport:
    """Bq34z110Transport-kompatibilis stub, kizárólag híváslista-naplózással
    és opcionális, névvel célzott hiba-injekcióval (`fail_on`, `fail_on_occurrence`).
    Sosem ér el valós soros portot/chipet."""

    def __init__(self, fail_on: str | None = None, fail_on_occurrence: int = 1,
                 fail_exc: Exception | None = None):
        self.calls: list[str] = []
        self.connected = False
        self.pack_configuration_block = bytes.fromhex("0961" + "00" * 30)
        self.control_status_word = _SS_BIT  # kezdeti állapot: SEALED, nincs CALMODE
        self._fail_on = fail_on
        self._fail_on_occurrence = fail_on_occurrence
        self._fail_exc = fail_exc if fail_exc is not None else RuntimeError("injected failure")
        self._occurrence_counts: dict[str, int] = {}

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name == self._fail_on:
            self._occurrence_counts[name] = self._occurrence_counts.get(name, 0) + 1
            if self._occurrence_counts[name] == self._fail_on_occurrence:
                raise self._fail_exc

    def connect(self, port, baudrate=115200, timeout=1.0):
        self._record("connect")
        self.connected = True

    def disconnect(self):
        # A disconnect() a valós szkriptekben MINDIG a finally blokk utolsó,
        # feltétel nélküli lépése -- ez a stub sosem hibáztatja, csak naplózza.
        self.calls.append("disconnect")
        self.connected = False

    def negotiate(self):
        self._record("negotiate")
        return {}

    def unseal(self, *, confirm):
        self._record("unseal")
        self.control_status_word &= ~_SS_BIT

    def full_access(self, *, confirm):
        self._record("full_access")
        self.control_status_word |= _FAS_BIT

    def seal(self, *, confirm):
        self._record("seal")
        self.control_status_word |= _SS_BIT
        self.control_status_word &= ~_FAS_BIT

    def enter_cal(self, *, confirm):
        self._record("enter_cal")
        self.control_status_word |= _CALMODE_BIT

    def exit_cal(self, *, confirm):
        self._record("exit_cal")
        self.control_status_word &= ~_CALMODE_BIT

    def board_offset_cal(self, *, confirm):
        self._record("board_offset_cal")

    def cc_offset_cal(self, *, confirm):
        self._record("cc_offset_cal")

    def cc_offset_save(self, *, confirm):
        self._record("cc_offset_save")

    def read_data_flash_block(self, subclass, block):
        assert (subclass, block) == (64, 0)
        self._record("read_data_flash_block")
        return self.pack_configuration_block

    def read_calibration_data(self):
        self._record("read_calibration_data")
        return {"cc_offset": -1374, "board_offset": -11}

    def read_voltage_divider(self):
        self._record("read_voltage_divider")
        return 12345

    def read_raw_block(self, addr, length):
        self._record("read_raw_block")
        return bytes(length)

    def read_control_subcommand_word(self, subcommand):
        self._record("read_control_subcommand_word")
        return self.control_status_word

    def read_standard_command_word(self, command):
        self._record("read_standard_command_word")
        return 3700


def _install_fake_transport(monkeypatch, module, transport: FakeTransport) -> None:
    monkeypatch.setattr(module, "PicoBQTransport", lambda **kwargs: transport)


class TestCleanupSurvivesEnterCalFailure:
    """T1: enter_cal() hibázik -- a chip UNSEALED marad CALMODE nélkül, de a
    finally blokknak MÉG mindig meg kell kísérelnie exit_cal()-t (ártalmatlanul
    hibázhat, mert nincs CALMODE-ban) ÉS seal()-t, majd disconnect()-et."""

    def test_exit_cal_and_seal_still_attempted_after_enter_cal_raises(self, monkeypatch):
        transport = FakeTransport(fail_on="enter_cal")
        _install_fake_transport(monkeypatch, calmode_analog_registers_mod, transport)

        with pytest.raises(RuntimeError, match="injected failure"):
            calmode_analog_registers_mod.main()

        assert "exit_cal" in transport.calls
        assert "seal" in transport.calls
        assert transport.calls.index("exit_cal") < transport.calls.index("seal")
        assert transport.connected is False


class TestCleanupSurvivesLaterStageFailureWhileInCalmode:
    """T2: enter_cal() SIKERES (chip ténylegesen CALMODE-ban van), majd egy
    KÉSŐBBI lépés (board_offset_cal) hibázik -- ez a legveszélyesebb eset,
    mert itt tényleges CALMODE-ban ragadás a kockázat exit_cal() nélkül."""

    def test_exit_cal_and_seal_still_attempted_after_board_offset_cal_raises(self, monkeypatch):
        transport = FakeTransport(fail_on="board_offset_cal")
        _install_fake_transport(monkeypatch, mid_measurement_retry_mod, transport)

        with pytest.raises(RuntimeError, match="injected failure"):
            mid_measurement_retry_mod.main()

        assert "enter_cal" in transport.calls  # valóban CALMODE-ba lépett előtte
        assert "exit_cal" in transport.calls
        assert "seal" in transport.calls
        assert transport.calls.index("exit_cal") < transport.calls.index("seal")
        assert transport.connected is False


class TestPollLoopTimeoutStillReachesCleanup:
    """T3: a `poll_until_clear()` helper TimeoutError-t dob (BCA/CCA sosem
    törlődik) -- ennek a kivételnek IS át kell jutnia a finally blokkig, ahol
    exit_cal()+seal() még mindig megkísérlődik. A `raise TimeoutError` maga
    NEM változott ebben a batch-ben -- csak azt ellenőrizzük, hogy a hívó
    main() finally ága nem lett megkerülve."""

    def test_timeout_error_propagates_but_cleanup_still_runs(self, monkeypatch):
        transport = FakeTransport()
        # BCA és CCA bit tartósan 1 marad -- a poll_until_clear() sosem
        # látja "törölve" állapotban, tehát a max_wait_s-es TimeoutError-agra
        # fut ki.
        transport.control_status_word |= _BCA_BIT | _CCA_BIT
        _install_fake_transport(monkeypatch, mid_measurement_retry_mod, transport)

        # A valós wall-clock várakozás elkerülésére a time.monotonic()-ot
        # úgy csaljuk meg, hogy MINDEN egyes hívás 1000-rel nagyobbat adjon
        # az előzőnél (nem csak az "első hívás"-ra hangolva -- l. MAX
        # verifier NB-5: ha valamilyen jövőbeli kód elfogyasztana egy
        # hívást ELŐBB, egy sorszám-alapú "az 1. hívás a t0" feltevés
        # csendben végtelen, valós sleep()-es ciklusba fagyna, nem bukna).
        # Így BÁRMELY két hívás közötti elapsed >= 1000 >> max_wait_s(90),
        # függetlenül attól, melyik hívás számít t0-nak. time.sleep()-et
        # NEM patcheljük -- ha a teszt mégis elérné, azonnal látszana egy
        # lassú/hibás tesztként, nem hamis zöldként.
        monotonic_values = itertools.count(0.0, 1000.0)
        monkeypatch.setattr(time_module, "monotonic", lambda: next(monotonic_values))

        with pytest.raises(TimeoutError):
            mid_measurement_retry_mod.main()

        assert "exit_cal" in transport.calls
        assert "seal" in transport.calls
        assert transport.connected is False


class TestCleanupIndependentStepsExitCalFailureDoesNotBlockSeal:
    """T4: maga az exit_cal() hibázik -- ez NEM akadályozhatja meg a seal()
    megkísérlését (rules/10-safety.md #3), és a disconnect()-nek is le kell
    futnia."""

    def test_seal_still_attempted_after_exit_cal_raises(self, monkeypatch):
        transport = FakeTransport(fail_on="exit_cal")
        _install_fake_transport(monkeypatch, calmode_analog_registers_mod, transport)

        # A szkript maga elkapja az exit_cal() hibáját (try/except a finally
        # blokkon belül) -- main() NEM dob kivételt emiatt.
        calmode_analog_registers_mod.main()

        assert "exit_cal" in transport.calls
        assert "seal" in transport.calls
        assert transport.calls.index("exit_cal") < transport.calls.index("seal")
        assert transport.connected is False


class TestHappyPathClosingStatusReflectsPostCleanupState:
    """T5: "hamis felirat" csapda (a fix-planner saját felfedezése) -- a záró
    CONTROL_STATUS-olvasásnak/kiírásnak a finally blokkban, exit_cal()+seal()
    UTÁN kell történnie, különben a boldog úton egy elavult (még CALMODE=1,
    UNSEALED) állapotot írna ki holott a chip már ténylegesen visszapecsételt
    állapotban van."""

    def test_final_control_status_read_happens_after_exit_cal_and_seal(self, monkeypatch):
        transport = FakeTransport()
        _install_fake_transport(monkeypatch, calmode_analog_registers_mod, transport)

        calmode_analog_registers_mod.main()

        exit_cal_idx = transport.calls.index("exit_cal")
        seal_idx = transport.calls.index("seal")
        last_status_read_idx = max(
            i for i, name in enumerate(transport.calls)
            if name == "read_control_subcommand_word"
        )
        assert exit_cal_idx < seal_idx < last_status_read_idx


class TestVoltageDividerScriptNeverEntersCalMode:
    """T6: negatív invariáns -- `bq_verify_voltage_divider_unsealed.py` SOHA
    nem hív enter_cal()-t, ezért a fixnek sem szabad exit_cal()-t hozzáadnia a
    finally ágához (az egy nem-CALMODE chipen értelmetlen/hamis művelet
    lenne). Csak seal() a takarítás."""

    def test_exit_cal_never_called_but_seal_is(self, monkeypatch):
        transport = FakeTransport()
        _install_fake_transport(monkeypatch, verify_vd_unsealed_mod, transport)

        verify_vd_unsealed_mod.main()

        assert "exit_cal" not in transport.calls
        assert "seal" in transport.calls
        assert transport.connected is False

    def test_seal_still_attempted_if_unseal_stage_raises_later(self, monkeypatch):
        # A "later stage" itt a MÁSODIK voltage_divider olvasás (a sorozat
        # 3. read_voltage_divider hívása) -- igazolja, hogy egy UNSEALED
        # állapotban történő későbbi hiba után is megkísérlődik a seal().
        transport = FakeTransport(fail_on="read_voltage_divider", fail_on_occurrence=3)
        _install_fake_transport(monkeypatch, verify_vd_unsealed_mod, transport)

        with pytest.raises(RuntimeError, match="injected failure"):
            verify_vd_unsealed_mod.main()

        assert "exit_cal" not in transport.calls
        assert "seal" in transport.calls
        assert transport.connected is False

    def test_final_control_status_read_survives_negotiate_failure(self, monkeypatch):
        # NB-1 (MAX verifier, Batch E6): a `driver` objektum a `negotiate()`
        # UTÁN jön létre a try blokkban -- ha `negotiate()` hibázik, a
        # `driver` sosem kap értéket. A záró CONTROL_STATUS-olvasásnak ettől
        # függetlenül működnie kell (nem `driver.read_control_status()`-t
        # hív, hanem közvetlen transport-hívást), különben a finally ága egy
        # `UnboundLocalError`-t adna a valódi hiba HELYETT.
        transport = FakeTransport(fail_on="negotiate")
        _install_fake_transport(monkeypatch, verify_vd_unsealed_mod, transport)

        with pytest.raises(RuntimeError, match="injected failure"):
            verify_vd_unsealed_mod.main()

        assert "seal" in transport.calls
        assert "read_control_subcommand_word" in transport.calls
        assert transport.calls.index("seal") < transport.calls.index("read_control_subcommand_word")
        assert transport.connected is False


class TestCalmodeDuringActiveMeasurementCleanup:
    """A 4 célfájl közül a legegyszerűbb eset (nincs záró readback-print,
    tiszta relokáció) -- külön, direkt lefedés a T1-T6 fő mintán felül, hogy
    mind a 4 fájl kapjon legalább egy hiba-injektált regressziós tesztet."""

    def test_exit_cal_and_seal_still_attempted_after_enter_cal_raises(self, monkeypatch):
        transport = FakeTransport(fail_on="enter_cal")
        _install_fake_transport(monkeypatch, calmode_active_measurement_mod, transport)

        with pytest.raises(RuntimeError, match="injected failure"):
            calmode_active_measurement_mod.main()

        assert "exit_cal" in transport.calls
        assert "seal" in transport.calls
        assert transport.calls.index("exit_cal") < transport.calls.index("seal")
        assert transport.connected is False

    def test_happy_path_calls_exit_cal_then_seal_then_disconnects(self, monkeypatch):
        transport = FakeTransport()
        _install_fake_transport(monkeypatch, calmode_active_measurement_mod, transport)

        calmode_active_measurement_mod.main()

        assert "exit_cal" in transport.calls
        assert "seal" in transport.calls
        assert transport.calls.index("exit_cal") < transport.calls.index("seal")
        assert transport.connected is False


class TestControlStatusBitDecodingSanityForFakeTransport:
    """A FakeTransport bitmintáinak önellenőrzése a `decode_control_status()`
    valós dekóderével -- ha ez a teszt megbukna, a fenti tesztek hamis
    biztonságérzetet adnának egy rosszul modellezett stub miatt."""

    def test_initial_word_decodes_to_sealed_no_calmode(self):
        from Prog.src.bq_diagnostics import decode_control_status

        transport = FakeTransport()
        cs = decode_control_status(transport.control_status_word)
        assert cs.ss is True
        assert cs.calmode is False

    def test_after_enter_cal_word_decodes_to_calmode_active(self):
        from Prog.src.bq_diagnostics import decode_control_status

        transport = FakeTransport()
        transport.enter_cal(confirm=True)
        cs = decode_control_status(transport.control_status_word)
        assert cs.calmode is True

    def test_after_seal_word_decodes_to_sealed_again(self):
        from Prog.src.bq_diagnostics import decode_control_status

        transport = FakeTransport()
        transport.unseal(confirm=True)
        transport.seal(confirm=True)
        cs = decode_control_status(transport.control_status_word)
        assert cs.ss is True


class TestReadOnlyDriverDelegatesControlStatusToTransport:
    """Igazolja, hogy a `Bq34z110ReadOnlyDriver.read_control_status()`
    ténylegesen ugyanazt a transport-hívást (`read_control_subcommand_word`)
    végzi, amit a fix a `driver` objektum nélküli, közvetlen hívással
    helyettesített a `bq_cal_diag_calmode_analog_registers.py` záró ágában --
    ez a helyettesítés csak akkor viselkedés-azonos, ha ez a delegálás igaz."""

    def test_read_control_status_delegates_to_same_transport_method(self):
        transport = FakeTransport()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        result_via_driver = driver.read_control_status()
        result_direct = transport.read_control_subcommand_word(
            Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS
        )

        assert result_via_driver == result_direct
        assert transport.calls.count("read_control_subcommand_word") == 2


class _R6B4Transport(FakeTransport):
    """[R6-B4] A FakeTransport kiegeszitese a valodi VoltageCalibration-
    Controller iro hivasaval (csak naplozas; a jelenlegi tesztek a DONE-utat
    nem erik el, a stub a controller-API teljessegeert van)."""

    def write_voltage_divider(self, value, *, confirm):
        self._record("write_voltage_divider")


class _R6B4SealFailsTransport(_R6B4Transport):
    """[R6-B4, verifier V-N1] A `seal()` RPC mindig hibazik (a valodi
    controller abort()-ja ezt csak `cleanup_failures`-be rogziti)."""

    def seal(self, *, confirm):
        self._record("seal")
        raise RuntimeError("R6B4 injected seal failure")


class _R6B4FailAfterOffsetTransport(_R6B4Transport):
    """[R6-B4] A megadott transport-hivas az elso olyan alkalommal dob, amely
    a controller sajat offset-visszaolvasasa (az ELSO
    `read_calibration_data()`, `_finish_save_offset()`) UTAN tortenik -- azaz
    a step1 sikeres offsetfazisa utani fuggetlen ellenorzo olvasasnal."""

    def __init__(self, fail_method: str, fail_exc: BaseException):
        super().__init__()
        self._fail_method = fail_method
        self._post_offset_exc = fail_exc
        self._post_offset_fired = False
        self.fired_at: int | None = None

    def _record(self, name: str) -> None:
        post_offset = "read_calibration_data" in self.calls
        super()._record(name)
        if name == self._fail_method and post_offset and not self._post_offset_fired:
            self._post_offset_fired = True
            self.fired_at = len(self.calls) - 1
            raise self._post_offset_exc


def _raise_on_nth(exc, n=1):
    state = {"k": 0}

    def _fn(*_a, **_k):
        state["k"] += 1
        if state["k"] == n:
            raise exc
    return _fn


def _print_raising_on(substr, exc):
    def _fn(*args, **_k):
        if any(substr in str(a) for a in args):
            raise exc
    return _fn


def _record_controllers(monkeypatch, module, cls_name):
    """A modul controller-osztalyat egy, a VALODI osztalyt peldanyosito es a
    peldanyt megjegyzo gyarra cseréli (allapot-ellenorzeshez)."""
    real_cls = getattr(module, cls_name)
    created = []

    def _factory(transport):
        ctrl = real_cls(transport)
        created.append(ctrl)
        return ctrl
    monkeypatch.setattr(module, cls_name, _factory)
    return created


_CC_ABORT_TAIL = ["exit_cal", "seal", "read_control_subcommand_word", "disconnect"]


def _assert_abort_then_disconnect(calls, *, exit_cal_expected):
    assert "seal" in calls
    if exit_cal_expected:
        assert calls.index("exit_cal") < calls.index("seal")
    else:
        assert "exit_cal" not in calls
    # abort (SEAL + SS-ellenorzes) a disconnect ELOTT
    assert calls.index("seal") < calls.index("disconnect")
    assert calls[-1] == "disconnect"


class TestR6B4ControllerDelegatingScriptsAbortOnAbnormalExit:
    """[regresszio, Claude-Codex loop R6 B4: R6-architecture-01, DFAM-016
    RECURRING] A harom controllert delegalo diagnosztikai szkript
    (`bq_cal_phase1_step1.py`, `bq_cal_voltage_divider_run.py`,
    `bq_cal_phase1_full_run.py`) rendellenes kilepeskor (BaseException) a
    valodi controller abort()-jat hivja a disconnect elott (SLUSB55B Rev. B
    p10 Table 3 EXIT_CAL, p11 SEALED); az eredeti kivetel propagal. A
    sikeres step1 szandekosan nyitott vegallapota valtozatlan. FakeTransport,
    valos soros port/chip nelkul."""

    def test_step1_ctrl_c_in_offset_poll_loop_aborts_before_disconnect(self, monkeypatch):
        transport = _R6B4Transport()
        _install_fake_transport(monkeypatch, step1_mod, transport)
        monkeypatch.setattr(step1_mod.time, "sleep", _raise_on_nth(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            step1_mod.main()
        assert "enter_cal" in transport.calls
        _assert_abort_then_disconnect(transport.calls, exit_cal_expected=True)

    def test_step1_broken_pipe_after_confirm_aborts(self, monkeypatch):
        transport = _R6B4Transport()
        _install_fake_transport(monkeypatch, step1_mod, transport)
        monkeypatch.setattr(step1_mod.time, "sleep", lambda _s: None)
        err = BrokenPipeError(32, "Broken pipe")
        monkeypatch.setattr(step1_mod, "print", _print_raising_on("[t=", err), raising=False)
        with pytest.raises(BrokenPipeError) as excinfo:
            step1_mod.main()
        assert excinfo.value is err
        _assert_abort_then_disconnect(transport.calls, exit_cal_expected=True)

    @pytest.mark.parametrize(
        "fail_method",
        ["read_calibration_data", "read_control_subcommand_word"],
        ids=["independent_second_cal_data_read", "final_control_status_read"],
    )
    def test_step1_post_offset_verification_read_failure_aborts(self, monkeypatch, fail_method):
        """[FINAL kotelezo (b)] A sikeres offsetfazis (AWAITING_REFERENCE_
        CURRENT) utani ket fuggetlen ellenorzo olvasas (a szkript masodik
        `transport.read_calibration_data()`-ja, ill. a zaro
        `driver.read_control_status()`) barmelyikenek hibaja rendellenes kilepes: az eredeti kivetel
        valtozatlanul propagal, a sorrend EXIT_CAL -> SEAL (-> SS-ellenorzes)
        -> disconnect. A hibamentes eset nyitva hagyasat a
        `test_step1_success_keeps_intentionally_open_end_state` rogziti."""
        err = RuntimeError(f"R6B4 injected post-offset failure: {fail_method}")
        transport = _R6B4FailAfterOffsetTransport(fail_method, err)
        _install_fake_transport(monkeypatch, step1_mod, transport)
        created = _record_controllers(monkeypatch, step1_mod, "CcGainCalibrationController")
        monkeypatch.setattr(step1_mod.time, "sleep", lambda _s: None)
        with pytest.raises(RuntimeError) as excinfo:
            step1_mod.main()
        assert excinfo.value is err
        assert not getattr(excinfo.value, "__notes__", [])
        calls = transport.calls
        assert calls.count("read_calibration_data") == 2
        assert transport.fired_at is not None
        assert calls[transport.fired_at] == fail_method
        assert calls[transport.fired_at + 1:] == _CC_ABORT_TAIL
        assert created[0].state == CalState.ABORTED

    def test_vd_ctrl_c_while_unsealed_seals_before_disconnect(self, monkeypatch):
        transport = _R6B4Transport(fail_on="read_voltage_divider", fail_exc=KeyboardInterrupt())
        _install_fake_transport(monkeypatch, vd_run_mod, transport)
        with pytest.raises(KeyboardInterrupt):
            vd_run_mod.main()
        assert "unseal" in transport.calls
        _assert_abort_then_disconnect(transport.calls, exit_cal_expected=False)

    def test_vd_broken_pipe_after_start_seals_and_closing_print_cannot_mask(self, monkeypatch):
        transport = _R6B4Transport()
        _install_fake_transport(monkeypatch, vd_run_mod, transport)
        err = BrokenPipeError(32, "Broken pipe")
        later = BrokenPipeError(32, "Broken pipe (closing print)")

        def _print(*args, **_k):
            text = " ".join(str(a) for a in args)
            if "ctrl.state" in text:
                raise err
            if "lezarva" in text:
                raise later
        monkeypatch.setattr(vd_run_mod, "print", _print, raising=False)
        with pytest.raises(BrokenPipeError) as excinfo:
            vd_run_mod.main()
        assert excinfo.value is err
        _assert_abort_then_disconnect(transport.calls, exit_cal_expected=False)

    @staticmethod
    def _full_run_env(monkeypatch, tmp_path, flag_content):
        flag = tmp_path / "gain_ready.txt"
        monkeypatch.setattr(full_run_mod, "FLAG_FILE", str(flag))
        monkeypatch.setattr(full_run_mod, "LOG_FILE", str(tmp_path / "run.log"))

        def _sleep(_s):
            if flag_content is not None and not flag.exists():
                flag.write_text(flag_content, encoding="utf-8")
        monkeypatch.setattr(full_run_mod.time, "sleep", _sleep)
        return flag

    def test_full_run_invalid_flag_file_aborts_from_awaiting_reference_current(
        self, monkeypatch, tmp_path
    ):
        transport = _R6B4Transport()
        _install_fake_transport(monkeypatch, full_run_mod, transport)
        created = _record_controllers(monkeypatch, full_run_mod, "CcGainCalibrationController")
        self._full_run_env(monkeypatch, tmp_path, "nem-szam")
        with pytest.raises(ValueError):
            full_run_mod.main()
        assert "read_calibration_data" in transport.calls  # AWAITING_REFERENCE_CURRENT elerve
        _assert_abort_then_disconnect(transport.calls, exit_cal_expected=True)
        assert created[0].state == CalState.ABORTED

    def test_full_run_ctrl_c_while_waiting_for_flag_aborts(self, monkeypatch, tmp_path):
        """[FINAL kotelezo (a)] A Ctrl+C a jelzofajlra varo ciklus
        `time.sleep(2.0)` hivasaban erkezik; elotte a controller igazoltan
        AWAITING_REFERENCE_CURRENT allapotban van, a jelzofajl nem letezik."""
        transport = _R6B4Transport()
        _install_fake_transport(monkeypatch, full_run_mod, transport)
        created = _record_controllers(monkeypatch, full_run_mod, "CcGainCalibrationController")
        flag = self._full_run_env(monkeypatch, tmp_path, None)
        seen = []

        def _sleep(s):
            if s == 2.0:
                seen.append((created[0].state, flag.exists(), list(transport.calls)))
                raise KeyboardInterrupt()
        monkeypatch.setattr(full_run_mod.time, "sleep", _sleep)
        with pytest.raises(KeyboardInterrupt):
            full_run_mod.main()
        assert len(seen) == 1
        state_at_interrupt, flag_existed, calls_at_interrupt = seen[0]
        assert state_at_interrupt == CalState.AWAITING_REFERENCE_CURRENT
        assert flag_existed is False
        assert calls_at_interrupt[-1] == "read_calibration_data"
        assert transport.calls == calls_at_interrupt + _CC_ABORT_TAIL
        assert created[0].state == CalState.ABORTED

    def test_original_exception_survives_abort_and_closing_log_failure(self, monkeypatch, tmp_path):
        transport = _R6B4Transport()
        _install_fake_transport(monkeypatch, full_run_mod, transport)
        self._full_run_env(monkeypatch, tmp_path, "nem-szam")

        def _abort_fails(self):
            raise RuntimeError("abort boom")
        monkeypatch.setattr(full_run_mod.CcGainCalibrationController, "abort", _abort_fails)
        real_log = full_run_mod.log

        def _log(msg):
            if "lezarva" in msg:
                raise OSError("log boom")
            real_log(msg)
        monkeypatch.setattr(full_run_mod, "log", _log)
        with pytest.raises(ValueError) as excinfo:
            full_run_mod.main()
        assert any("abort boom" in n for n in getattr(excinfo.value, "__notes__", []))
        assert transport.calls[-1] == "disconnect"

    def _setup_ctrl_c_after_controller_exists(self, monkeypatch, tmp_path, script,
                                              transport_cls=_R6B4Transport):
        """Ctrl+C a controller letrejotte utan, mindharom szkriptben.
        Visszaad: (modul, transport, eredeti kivetel, controller-osztalynev)."""
        orig = KeyboardInterrupt(f"R6B4 {script}")
        if script == "vd":
            transport = transport_cls(fail_on="read_voltage_divider", fail_exc=orig)
            _install_fake_transport(monkeypatch, vd_run_mod, transport)
            return vd_run_mod, transport, orig, "VoltageCalibrationController"
        module = step1_mod if script == "step1" else full_run_mod
        transport = transport_cls()
        _install_fake_transport(monkeypatch, module, transport)
        if script == "full_run":
            self._full_run_env(monkeypatch, tmp_path, None)
        monkeypatch.setattr(module.time, "sleep", _raise_on_nth(orig))
        return module, transport, orig, "CcGainCalibrationController"

    @pytest.mark.parametrize("script", ["step1", "vd", "full_run"])
    def test_abort_failure_is_noted_and_original_propagates(self, monkeypatch, tmp_path, script):
        """Az abort() Exception-hibaja nem fedi el az eredeti kivetelt: az
        eredeti peldany propagal, az abort-hiba `__notes__`-kent csatolodik,
        a disconnect lefut."""
        module, transport, orig, cls_name = self._setup_ctrl_c_after_controller_exists(
            monkeypatch, tmp_path, script)

        def _abort_fails(self):
            raise RuntimeError("abort boom")
        monkeypatch.setattr(getattr(module, cls_name), "abort", _abort_fails)
        with pytest.raises(KeyboardInterrupt) as excinfo:
            module.main()
        assert excinfo.value is orig
        assert any("abort boom" in n for n in getattr(excinfo.value, "__notes__", []))
        assert transport.calls[-1] == "disconnect"

    @pytest.mark.parametrize("seal_fails", [True, False], ids=["seal_fails", "clean_abort"])
    @pytest.mark.parametrize("script", ["step1", "vd", "full_run"])
    def test_unverified_bq_closure_after_abort_is_noted(self, monkeypatch, tmp_path, script, seal_fails):
        """[verifier V-N1] A valodi abort() az EXIT_CAL/SEAL/SS-ellenorzes
        hibait nem dobja tovabb, csak a snapshot `cleanup_failures`-ebe
        rogziti. Rendellenes kilepeskor ezt a szkript az eredeti kivetelre
        note-kent csatolja ("nem igazolt"), igy a lezaras sikere/kudarca
        megkulonboztetheto. Kontroll: tiszta abort utan nincs note."""
        transport_cls = _R6B4SealFailsTransport if seal_fails else _R6B4Transport
        module, transport, orig, _cls = self._setup_ctrl_c_after_controller_exists(
            monkeypatch, tmp_path, script, transport_cls)
        with pytest.raises(KeyboardInterrupt) as excinfo:
            module.main()
        assert excinfo.value is orig
        notes = getattr(excinfo.value, "__notes__", [])
        assert "seal" in transport.calls
        assert transport.calls[-1] == "disconnect"
        if seal_fails:
            assert any("nem igazolt" in n and "SEAL_FAILED" in n for n in notes), notes
        else:
            assert notes == []

    @pytest.mark.parametrize("script", ["step1", "vd", "full_run"])
    def test_second_ctrl_c_during_abort_propagates_without_full_preservation(
        self, monkeypatch, tmp_path, script
    ):
        """[FINAL (d), karakterizacio -- NEM teljes kivetelmegorzesi
        garancia] Ha maga az abort() kap egy MASODIK KeyboardInterrupt-ot,
        az (szandekosan, nincs elnyelve) propagal; az eredeti kivetel csak a
        `__context__`-ben marad meg, a disconnect ettol meg lefut."""
        module, transport, first, cls_name = self._setup_ctrl_c_after_controller_exists(
            monkeypatch, tmp_path, script)
        second = KeyboardInterrupt("second")

        def _abort_interrupted(self):
            raise second
        monkeypatch.setattr(getattr(module, cls_name), "abort", _abort_interrupted)
        with pytest.raises(KeyboardInterrupt) as excinfo:
            module.main()
        assert excinfo.value is second
        assert excinfo.value.__context__ is first
        assert transport.calls[-1] == "disconnect"

    @pytest.mark.parametrize("script", ["step1", "vd", "full_run"])
    def test_exception_before_controller_exists_only_disconnects(self, monkeypatch, tmp_path, script):
        """Kontroll: controller meg nincs (negotiate-hiba) -- nincs abort, a
        `ctrl = None` miatt nincs NameError, csak disconnect."""
        module = {"step1": step1_mod, "vd": vd_run_mod, "full_run": full_run_mod}[script]
        transport = _R6B4Transport(fail_on="negotiate")
        _install_fake_transport(monkeypatch, module, transport)
        if script == "full_run":
            self._full_run_env(monkeypatch, tmp_path, None)
        with pytest.raises(RuntimeError, match="injected failure"):
            module.main()
        assert transport.calls == ["connect", "negotiate", "disconnect"]

    def test_step1_success_keeps_intentionally_open_end_state(self, monkeypatch):
        """KEEP_AS_IS: a sikeres step1 szandekosan UNSEALED+CALMODE
        allapotban, AWAITING_REFERENCE_CURRENT-ben hagyja a chipet (kezi
        folytatashoz) -- se EXIT_CAL, se SEAL nem fut."""
        transport = _R6B4Transport()
        _install_fake_transport(monkeypatch, step1_mod, transport)
        created = _record_controllers(monkeypatch, step1_mod, "CcGainCalibrationController")
        monkeypatch.setattr(step1_mod.time, "sleep", lambda _s: None)
        step1_mod.main()
        assert "exit_cal" not in transport.calls
        assert "seal" not in transport.calls
        assert transport.calls.count("read_calibration_data") == 2
        assert transport.calls[-2:] == ["read_control_subcommand_word", "disconnect"]
        assert created[0].state == CalState.AWAITING_REFERENCE_CURRENT
