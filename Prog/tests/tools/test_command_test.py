"""Kör 20 Batch E7 (HW20-04, HW20-06, P2) regressziós tesztjei.

`Prog/tools/command_test.py` korábban feltétel nélkül energizált (PSU
`output_on()` / Load `input_on()`):
  (a) HW20-04(a): akkor is, ha a megelőző setpoint-írás (`set_output_voltage`/
      `set_output_current`/`set_mode_cc`/`set_current`) hibázott -- ilyenkor az
      ELŐZŐ session/előlap setpointjével energizált volna.
  (b) HW20-04(b), DFAM-044: nem volt semmilyen operátori jóváhagyási kapu
      (`--i-confirm-no-battery-connected` flag / interaktív megerősítés).
  (c) HW20-06: nem ellenőrizte a csatlakozott eszköz `*IDN?`-jét az elvárt
      modellel szemben -- egy felcserélt VISA resource-string esetén a driver
      a MÁSIK fizikai eszközre küldte volna az energizáló parancsot.

A `FakePsu`/`FakeLoad` hívás-naplózó, hiba-injektáló stubok -- SOSEM érnek
valós soros/USB porthoz."""
from __future__ import annotations

import pytest

from Prog.tools import command_test


class FakePsu:
    """Prog.drivers.device_psu.Keithley2220PSU-kompatibilis stub."""

    def __init__(self, idn: str = "KEITHLEY INSTRUMENTS,2220-30-1,1234567,1.15-1.05",
                 fail_on: str | None = None):
        self.calls: list[str] = []
        self._idn = idn
        self._fail_on = fail_on

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name == self._fail_on:
            raise RuntimeError(f"injected failure: {name}")

    def connect(self, resource_string, rm=None):
        self._record("connect")

    def disconnect(self):
        self.calls.append("disconnect")

    def idn(self):
        self._record("idn")
        return self._idn

    def clear_status(self):
        self._record("clear_status")

    def check_error(self):
        self._record("check_error")
        return []

    def set_output_voltage(self, v):
        self._record("set_output_voltage")

    def set_output_current(self, i):
        self._record("set_output_current")

    def measure_output_voltage(self):
        self._record("measure_output_voltage")
        return 0.0

    def measure_output_current(self):
        self._record("measure_output_current")
        return 0.0

    def output_on(self):
        self._record("output_on")

    def output_off(self):
        self._record("output_off")

    def all_outputs_off(self):
        self._record("all_outputs_off")

    def set_mode_independent(self):
        self._record("set_mode_independent")

    def safe_off(self):
        self.calls.append("safe_off")
        return True


class FakeLoad:
    """Prog.drivers.device_load.Keithley2380Load-kompatibilis stub."""

    def __init__(self, idn: str = "KEITHLEY INSTRUMENTS,2380-120-60,7654321,1.03",
                 fail_on: str | None = None):
        self.calls: list[str] = []
        self._idn = idn
        self._fail_on = fail_on

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name == self._fail_on:
            raise RuntimeError(f"injected failure: {name}")

    def connect(self, resource_string, rm=None):
        self._record("connect")

    def disconnect(self):
        self.calls.append("disconnect")

    def idn(self):
        self._record("idn")
        return self._idn

    def clear_status(self):
        self._record("clear_status")

    def check_error(self, clear_first=True):
        self._record("check_error")
        return []

    def set_mode_cc(self):
        self._record("set_mode_cc")

    def set_current(self, i):
        self._record("set_current")

    def measure_voltage(self):
        self._record("measure_voltage")
        return 0.0

    def measure_current(self):
        self._record("measure_current")
        return 0.0

    def measure_power(self):
        self._record("measure_power")
        return 0.0

    def input_on(self, clear_first=True):
        self._record("input_on")

    def input_off(self):
        self._record("input_off")

    def safe_off(self):
        self.calls.append("safe_off")
        return True


_CFG = {
    "instruments": {
        "psu": {"resource": "USB0::PSU::INSTR"},
        "load": {"resource": "USB0::LOAD::INSTR"},
    }
}


class TestHw20_04aPsuSetpointGate:
    """HW20-04(a): output_on() csak akkor futhat, ha MINDKÉT setpoint-írás
    (set_output_voltage, set_output_current) sikeres volt."""

    def test_output_on_skipped_when_voltage_setpoint_fails(self, monkeypatch):
        fake = FakePsu(fail_on="set_output_voltage")
        import Prog.drivers.device_psu as device_psu_mod
        monkeypatch.setattr(device_psu_mod, "Keithley2220PSU", lambda: fake)

        command_test.test_psu(_CFG)

        assert "output_on" not in fake.calls

    def test_output_on_skipped_when_current_setpoint_fails(self, monkeypatch):
        fake = FakePsu(fail_on="set_output_current")
        import Prog.drivers.device_psu as device_psu_mod
        monkeypatch.setattr(device_psu_mod, "Keithley2220PSU", lambda: fake)

        command_test.test_psu(_CFG)

        assert "output_on" not in fake.calls

    def test_output_on_still_runs_on_happy_path(self, monkeypatch):
        # Counter-test: igazolja, hogy a fix nem szélesítette túl a kaput --
        # sikeres setpoint-írás után output_on() TOVÁBBRA IS lefut.
        fake = FakePsu()
        import Prog.drivers.device_psu as device_psu_mod
        monkeypatch.setattr(device_psu_mod, "Keithley2220PSU", lambda: fake)

        command_test.test_psu(_CFG)

        assert "output_on" in fake.calls
        assert "output_off" in fake.calls


class TestHw20_04aLoadSetpointGate:
    """HW20-04(a): input_on() csak akkor futhat, ha MINDKÉT mód/setpoint-írás
    (set_mode_cc, set_current) sikeres volt."""

    def test_input_on_skipped_when_mode_cc_fails(self, monkeypatch):
        fake = FakeLoad(fail_on="set_mode_cc")
        import Prog.drivers.device_load as device_load_mod
        monkeypatch.setattr(device_load_mod, "Keithley2380Load", lambda: fake)

        command_test.test_load(_CFG)

        assert "input_on" not in fake.calls

    def test_input_on_skipped_when_current_setpoint_fails(self, monkeypatch):
        fake = FakeLoad(fail_on="set_current")
        import Prog.drivers.device_load as device_load_mod
        monkeypatch.setattr(device_load_mod, "Keithley2380Load", lambda: fake)

        command_test.test_load(_CFG)

        assert "input_on" not in fake.calls

    def test_input_on_still_runs_on_happy_path(self, monkeypatch):
        fake = FakeLoad()
        import Prog.drivers.device_load as device_load_mod
        monkeypatch.setattr(device_load_mod, "Keithley2380Load", lambda: fake)

        command_test.test_load(_CFG)

        assert "input_on" in fake.calls
        assert "input_off" in fake.calls


class TestHw20_06IdentityGate:
    """HW20-06: egy nem-elvárt *IDN? substring esetén a szkript SEMMILYEN
    setpoint-írást vagy energizáló parancsot nem küld -- a felcserélt VISA
    resource-string a leggyakoribb valós hibamód, amit ez véd."""

    def test_psu_energizing_skipped_on_idn_mismatch(self, monkeypatch):
        fake = FakePsu(idn="KEITHLEY INSTRUMENTS,2380-120-60,0000000,1.00")  # Load IDN-je!
        import Prog.drivers.device_psu as device_psu_mod
        monkeypatch.setattr(device_psu_mod, "Keithley2220PSU", lambda: fake)

        command_test.test_psu(_CFG)

        assert "set_output_voltage" not in fake.calls
        assert "set_output_current" not in fake.calls
        assert "output_on" not in fake.calls

    def test_load_energizing_skipped_on_idn_mismatch(self, monkeypatch):
        fake = FakeLoad(idn="KEITHLEY INSTRUMENTS,2220-30-1,0000000,1.00")  # PSU IDN-je!
        import Prog.drivers.device_load as device_load_mod
        monkeypatch.setattr(device_load_mod, "Keithley2380Load", lambda: fake)

        command_test.test_load(_CFG)

        assert "set_mode_cc" not in fake.calls
        assert "set_current" not in fake.calls
        assert "input_on" not in fake.calls

    def test_psu_energizing_proceeds_on_idn_match(self, monkeypatch):
        # Counter-test: a kapu nem tágabb, mint kell -- helyes IDN esetén
        # a normál futás lefut.
        fake = FakePsu()
        import Prog.drivers.device_psu as device_psu_mod
        monkeypatch.setattr(device_psu_mod, "Keithley2220PSU", lambda: fake)

        command_test.test_psu(_CFG)

        assert "output_on" in fake.calls

    def test_psu_energizing_skipped_when_idn_itself_raises(self, monkeypatch):
        # N-6 (MAX verifier, Batch E7): nem csak a substring-eltérésre, a
        # magára az idn() hívás HIBÁJÁRA (nem csak a kapott értékre) is
        # FAIL-CLOSED-nak kell lennie -- idn="" a mismatch-ággal azonos
        # eredményre vezet, mert "" sosem tartalmazza a várt substringet.
        fake = FakePsu(fail_on="idn")
        import Prog.drivers.device_psu as device_psu_mod
        monkeypatch.setattr(device_psu_mod, "Keithley2220PSU", lambda: fake)

        command_test.test_psu(_CFG)

        assert "set_output_voltage" not in fake.calls
        assert "output_on" not in fake.calls

    def test_load_energizing_skipped_when_idn_itself_raises(self, monkeypatch):
        fake = FakeLoad(fail_on="idn")
        import Prog.drivers.device_load as device_load_mod
        monkeypatch.setattr(device_load_mod, "Keithley2380Load", lambda: fake)

        command_test.test_load(_CFG)

        assert "set_mode_cc" not in fake.calls
        assert "input_on" not in fake.calls

    # R3-B2 (R3-HW-01, DFAM-003): a Load-kapu a pontos modellt varja -- egy
    # masik 2380-csaladtag (2380-500-15) vagy a J-valtozat (2380J-120-60,
    # SOURCE_REQUIRED) sem energizalhat; a meglevo finally-cleanup fut.
    @pytest.mark.parametrize("load_idn", [
        pytest.param("Keithley, 2380-500-15, 802436052777870003, 1.00-1.00", id="500-15"),
        pytest.param("Keithley, 2380J-120-60, 802436052777870003, 1.00-1.00", id="J-120-60"),
    ])
    def test_r3b2_load_wrong_2380_variant_skips_energizing(self, monkeypatch, capsys, load_idn):
        fake = FakeLoad(idn=load_idn)
        import Prog.drivers.device_load as device_load_mod
        monkeypatch.setattr(device_load_mod, "Keithley2380Load", lambda: fake)

        command_test.test_load(_CFG)

        # Korai kilepes az idn() utan, majd a meglevo finally: safe_off -> disconnect.
        assert fake.calls == ["connect", "idn", "safe_off", "disconnect"]
        out = capsys.readouterr().out
        assert "'2380-120-60'" in out
        assert load_idn in out

    def test_r3b2_load_lab_2380_120_60_energizes(self, monkeypatch):
        # Pozitiv kontroll: a labor valos IDN-je atjut a kapun.
        fake = FakeLoad(idn="Keithley, 2380-120-60, 802436052777870003, 1.00-1.00")
        import Prog.drivers.device_load as device_load_mod
        monkeypatch.setattr(device_load_mod, "Keithley2380Load", lambda: fake)

        command_test.test_load(_CFG)

        assert "input_on" in fake.calls
        assert fake.calls[-2:] == ["safe_off", "disconnect"]


class TestConfirmEnergizingAllowed:
    """HW20-04(b), DFAM-044: `_confirm_energizing_allowed()` a jóváhagyási
    kapu magja -- flag, interaktív igen/nem, és nem-interaktív (EOFError)
    eset mind lefedve."""

    def test_auto_confirmed_flag_skips_prompt(self, monkeypatch):
        def _boom():
            raise AssertionError("input() nem hivhato, ha auto_confirmed=True")
        monkeypatch.setattr("builtins.input", lambda *_: _boom())

        assert command_test._confirm_energizing_allowed(True) is True

    def test_interactive_yes_confirms(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_: "igen")
        assert command_test._confirm_energizing_allowed(False) is True

    def test_interactive_no_declines(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_: "nem")
        assert command_test._confirm_energizing_allowed(False) is False

    def test_non_interactive_eof_fails_closed(self, monkeypatch):
        def _raise_eof(*_):
            raise EOFError()
        monkeypatch.setattr("builtins.input", _raise_eof)
        assert command_test._confirm_energizing_allowed(False) is False


class TestMainConfirmGateBlocksBeforeConfig:
    """HW20-04(b): a jóváhagyási kapu a `load_config()`/energizálás ELŐTT
    fut -- egy elutasított/hiányzó megerősítés esetén main() nem-nulla
    kilépő kóddal tér vissza, és SOSEM ér el a PSU/Load teszteléséig."""

    def test_main_returns_nonzero_without_confirmation(self, monkeypatch):
        # FONTOS: a load_config()-ot NEM AssertionError-ral jelöljük, mert
        # main() saját `except Exception` ága (a config-betöltési hibákra)
        # egy itt eldobott AssertionError-t IS elnyelne, és a teszt hamis
        # zöldet adna akkor is, ha a jóváhagyási kapu ki lenne kapcsolva --
        # egy hívás-számláló flaggel diszkrimináló.
        def _raise_eof(*_):
            raise EOFError()
        monkeypatch.setattr("builtins.input", _raise_eof)

        # N-5 (MAX verifier, Batch E7): a test_psu()/test_load()/test_dmm_*()-t
        # is monkeypatch-elni kell, különben egy esetleges kapu-regresszió a
        # tesztet VALÓS pyvisa.ResourceManager().open_resource() hívásig
        # engedné (ártalmatlan a hamis resource-stringgel, de sosem
        # szándékos ebben a suite-ban -- l. rules/40-instruments.md "Műszer
        # nélküli review nem HIL-bizonyíték", itt fordítva: unit teszt sose
        # érjen el semmilyen VISA-hívást).
        called = {"load_config": False}

        def _track_load_config():
            called["load_config"] = True
            return _CFG
        monkeypatch.setattr(command_test, "load_config", _track_load_config)
        monkeypatch.setattr(command_test, "test_psu", lambda cfg: None)
        monkeypatch.setattr(command_test, "test_load", lambda cfg: None)
        monkeypatch.setattr(command_test, "test_dmm_voltage", lambda cfg: None)
        monkeypatch.setattr(command_test, "test_dmm_temperature", lambda cfg: None)

        exit_code = command_test.main([])

        assert exit_code == 1
        assert called["load_config"] is False

    def test_main_proceeds_with_confirm_flag(self, monkeypatch):
        called = {"load_config": False}

        def _fake_load_config():
            called["load_config"] = True
            return _CFG

        monkeypatch.setattr(command_test, "load_config", _fake_load_config)
        monkeypatch.setattr(command_test, "test_psu", lambda cfg: None)
        monkeypatch.setattr(command_test, "test_load", lambda cfg: None)
        monkeypatch.setattr(command_test, "test_dmm_voltage", lambda cfg: None)
        monkeypatch.setattr(command_test, "test_dmm_temperature", lambda cfg: None)

        command_test.main(["--i-confirm-no-battery-connected"])

        assert called["load_config"] is True
