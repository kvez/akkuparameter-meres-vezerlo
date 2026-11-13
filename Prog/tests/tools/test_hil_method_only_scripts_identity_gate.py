"""Kör 20 Batch E7 (HW20-06, P3) regressziós tesztjei.

`Prog/tools/hil_standard_pulse_rb_method_only.py` és
`Prog/tools/hil_iec_dc_resistance_method_only.py` korábban `idn()`-t
lekérdezték és kiírták, de SOHA nem hasonlították össze az elvárt modellel --
egy felcserélt VISA resource-string esetén a driver a MÁSIK fizikai eszközre
küldte volna az energizáló parancsot (`load.input_on()`/PSU setpoint a
`PulseMeasurementService`/`IecDcResistanceController` belsejéből). Ugyanaz az
ellenőrzés, mint az éles úton már bevált `instrument_manager.py::
identify_all(expected_models=...)`.

A `FakeHilPsu`/`FakeHilLoad`/`FakeHilDmm` hívás-naplózó stubok -- SOSEM érnek
valós soros/USB porthoz."""
from __future__ import annotations

import pytest

from Prog.tools import hil_iec_dc_resistance_method_only as hil_iec_mod
from Prog.tools import hil_standard_pulse_rb_method_only as hil_pulse_mod


class FakeHilPsu:
    def __init__(self, idn="KEITHLEY INSTRUMENTS,2220-30-1,1234567,1.15-1.05"):
        self.calls: list[str] = []
        self._idn = idn

    def connect(self, resource, rm=None):
        self.calls.append("connect")

    def disconnect(self):
        self.calls.append("disconnect")

    def idn(self):
        self.calls.append("idn")
        return self._idn

    def all_outputs_off(self):
        self.calls.append("all_outputs_off")

    def set_mode_independent(self):
        self.calls.append("set_mode_independent")


class FakeHilLoad:
    def __init__(self, idn="KEITHLEY INSTRUMENTS,2380-120-60,7654321,1.03"):
        self.calls: list[str] = []
        self._idn = idn

    def connect(self, resource, rm=None):
        self.calls.append("connect")

    def disconnect(self):
        self.calls.append("disconnect")

    def idn(self):
        self.calls.append("idn")
        return self._idn

    def input_off(self):
        self.calls.append("input_off")


class FakeHilDmm:
    def __init__(self, idn="Keysight Technologies,34465A,MY60016569,A.02.17"):
        self.calls: list[str] = []
        self._idn = idn

    def connect(self, resource, rm=None):
        self.calls.append("connect")

    def disconnect(self):
        self.calls.append("disconnect")

    def idn(self):
        self.calls.append("idn")
        return self._idn

    def configure_dcv(self, range_V=None, nplc=None):
        self.calls.append("configure_dcv")

    def configure_temp_4wire_pt100(self, nplc=None):
        self.calls.append("configure_temp_4wire_pt100")

    def configure_temp_2wire_ntc_thermistor(self, r25_ohm=None, beta_K=None, nplc=None):
        self.calls.append("configure_temp_2wire_ntc_thermistor")


class _MarkerReached(Exception):
    """A teszt saját jelzése: a végrehajtás túljutott az azonosítás-kapun és
    elérte a BatteryProfile() konstrukciót -- azaz az energizáló szekvencia
    ELINDULT VOLNA."""


def _write_cfg(tmp_path, with_dmm_temperature=False):
    import yaml
    cfg = {
        "instruments": {
            "psu": {"resource": "USB0::PSU::INSTR"},
            "load": {"resource": "USB0::LOAD::INSTR"},
            "dmm_voltage": {"resource": "USB0::DMMV::INSTR"},
        }
    }
    if with_dmm_temperature:
        cfg["instruments"]["dmm_temperature"] = {"resource": "USB0::DMMT::INSTR"}
    path = tmp_path / "local_config.yaml"
    path.write_text(yaml.dump(cfg), encoding="utf-8")
    return path


class TestPulseScriptIdentityGate:
    """hil_standard_pulse_rb_method_only.py"""

    def test_aborts_before_energizing_on_psu_idn_mismatch(self, monkeypatch, tmp_path):
        fake_psu = FakeHilPsu(idn="KEITHLEY INSTRUMENTS,2380-120-60,0000000,1.00")  # Load IDN-je!
        fake_load = FakeHilLoad()
        fake_dmm = FakeHilDmm()
        monkeypatch.setattr(hil_pulse_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_pulse_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_pulse_mod, "Keysight34465ADMM", lambda: fake_dmm)
        monkeypatch.setattr(hil_pulse_mod, "CONFIG_PATH", str(_write_cfg(tmp_path)))
        monkeypatch.setattr(
            hil_pulse_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        hil_pulse_mod.main()  # nem dob -- a mismatch-ág korán return-öl

        assert "all_outputs_off" not in fake_psu.calls
        assert "input_off" not in fake_load.calls
        assert fake_psu.calls[-1] == "disconnect"
        assert fake_load.calls[-1] == "disconnect"

    def test_aborts_before_energizing_on_load_idn_mismatch(self, monkeypatch, tmp_path):
        fake_psu = FakeHilPsu()
        fake_load = FakeHilLoad(idn="KEITHLEY INSTRUMENTS,2220-30-1,0000000,1.00")  # PSU IDN-je!
        fake_dmm = FakeHilDmm()
        monkeypatch.setattr(hil_pulse_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_pulse_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_pulse_mod, "Keysight34465ADMM", lambda: fake_dmm)
        monkeypatch.setattr(hil_pulse_mod, "CONFIG_PATH", str(_write_cfg(tmp_path)))
        monkeypatch.setattr(
            hil_pulse_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        hil_pulse_mod.main()

        assert "input_off" not in fake_load.calls
        assert fake_psu.calls[-1] == "disconnect"
        assert fake_load.calls[-1] == "disconnect"

    def test_proceeds_past_identity_gate_when_both_idns_match(self, monkeypatch, tmp_path):
        # Counter-test: a kapu nem tágabb, mint kell -- helyes IDN esetén a
        # szkript túljut az azonosítás-kapun (a BatteryProfile()-ig ér).
        fake_psu = FakeHilPsu()
        fake_load = FakeHilLoad()
        fake_dmm = FakeHilDmm()
        monkeypatch.setattr(hil_pulse_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_pulse_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_pulse_mod, "Keysight34465ADMM", lambda: fake_dmm)
        monkeypatch.setattr(hil_pulse_mod, "CONFIG_PATH", str(_write_cfg(tmp_path)))
        monkeypatch.setattr(
            hil_pulse_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        with pytest.raises(_MarkerReached):
            hil_pulse_mod.main()

        assert "all_outputs_off" in fake_psu.calls
        assert "input_off" in fake_load.calls

    # R3-B2 (R3-HW-01, DFAM-003): a Load-kapu a pontos 2380-120-60 modellt
    # varja; a 2380-500-15 es a J-valtozat (SOURCE_REQUIRED) is a meglevo
    # korai kilepesen + disconnect-cleanupon at all meg.
    @pytest.mark.parametrize("load_idn", [
        pytest.param("Keithley, 2380-500-15, 802436052777870003, 1.00-1.00", id="500-15"),
        pytest.param("Keithley, 2380J-120-60, 802436052777870003, 1.00-1.00", id="J-120-60"),
    ])
    def test_r3b2_aborts_on_wrong_2380_variant(self, monkeypatch, tmp_path, capsys, load_idn):
        fake_psu = FakeHilPsu()
        fake_load = FakeHilLoad(idn=load_idn)
        fake_dmm = FakeHilDmm()
        monkeypatch.setattr(hil_pulse_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_pulse_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_pulse_mod, "Keysight34465ADMM", lambda: fake_dmm)
        monkeypatch.setattr(hil_pulse_mod, "CONFIG_PATH", str(_write_cfg(tmp_path)))
        monkeypatch.setattr(
            hil_pulse_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        hil_pulse_mod.main()  # nem dob -- a mismatch-ag koran return-ol

        assert fake_psu.calls == ["connect", "idn", "disconnect"]
        assert fake_load.calls == ["connect", "idn", "disconnect"]
        assert fake_dmm.calls == ["connect", "idn", "disconnect"]
        out = capsys.readouterr().out
        assert "'2380-120-60'" in out
        assert load_idn in out

    def test_r3b2_proceeds_with_lab_2380_120_60_idn(self, monkeypatch, tmp_path):
        # Pozitiv kontroll: a labor valos IDN-je atjut a kapun.
        fake_psu = FakeHilPsu()
        fake_load = FakeHilLoad(idn="Keithley, 2380-120-60, 802436052777870003, 1.00-1.00")
        fake_dmm = FakeHilDmm()
        monkeypatch.setattr(hil_pulse_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_pulse_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_pulse_mod, "Keysight34465ADMM", lambda: fake_dmm)
        monkeypatch.setattr(hil_pulse_mod, "CONFIG_PATH", str(_write_cfg(tmp_path)))
        monkeypatch.setattr(
            hil_pulse_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        with pytest.raises(_MarkerReached):
            hil_pulse_mod.main()

        assert "all_outputs_off" in fake_psu.calls
        assert "input_off" in fake_load.calls


class TestIecScriptIdentityGate:
    """hil_iec_dc_resistance_method_only.py"""

    def test_aborts_before_energizing_on_psu_idn_mismatch(self, monkeypatch, tmp_path):
        fake_psu = FakeHilPsu(idn="KEITHLEY INSTRUMENTS,2380-120-60,0000000,1.00")
        fake_load = FakeHilLoad()
        fake_dmm_v = FakeHilDmm()
        fake_dmm_t = FakeHilDmm()
        dmm_instances = iter([fake_dmm_v, fake_dmm_t])
        monkeypatch.setattr(hil_iec_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_iec_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_iec_mod, "Keysight34465ADMM", lambda: next(dmm_instances))
        monkeypatch.setattr(hil_iec_mod, "CONFIG_PATH", str(_write_cfg(tmp_path, with_dmm_temperature=True)))
        monkeypatch.setattr(
            hil_iec_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        hil_iec_mod.main()

        assert "all_outputs_off" not in fake_psu.calls
        assert "input_off" not in fake_load.calls
        assert fake_psu.calls[-1] == "disconnect"
        assert fake_load.calls[-1] == "disconnect"
        assert fake_dmm_v.calls[-1] == "disconnect"
        assert fake_dmm_t.calls[-1] == "disconnect"

    def test_aborts_before_energizing_on_load_idn_mismatch(self, monkeypatch, tmp_path):
        fake_psu = FakeHilPsu()
        fake_load = FakeHilLoad(idn="KEITHLEY INSTRUMENTS,2220-30-1,0000000,1.00")
        fake_dmm_v = FakeHilDmm()
        fake_dmm_t = FakeHilDmm()
        dmm_instances = iter([fake_dmm_v, fake_dmm_t])
        monkeypatch.setattr(hil_iec_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_iec_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_iec_mod, "Keysight34465ADMM", lambda: next(dmm_instances))
        monkeypatch.setattr(hil_iec_mod, "CONFIG_PATH", str(_write_cfg(tmp_path, with_dmm_temperature=True)))
        monkeypatch.setattr(
            hil_iec_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        hil_iec_mod.main()

        assert "input_off" not in fake_load.calls
        assert fake_psu.calls[-1] == "disconnect"
        assert fake_load.calls[-1] == "disconnect"

    def test_proceeds_past_identity_gate_when_both_idns_match(self, monkeypatch, tmp_path):
        fake_psu = FakeHilPsu()
        fake_load = FakeHilLoad()
        fake_dmm_v = FakeHilDmm()
        fake_dmm_t = FakeHilDmm()
        dmm_instances = iter([fake_dmm_v, fake_dmm_t])
        monkeypatch.setattr(hil_iec_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_iec_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_iec_mod, "Keysight34465ADMM", lambda: next(dmm_instances))
        monkeypatch.setattr(hil_iec_mod, "CONFIG_PATH", str(_write_cfg(tmp_path, with_dmm_temperature=True)))
        monkeypatch.setattr(
            hil_iec_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        with pytest.raises(_MarkerReached):
            hil_iec_mod.main()

        assert "all_outputs_off" in fake_psu.calls
        assert "input_off" in fake_load.calls

    # R3-B2 (R3-HW-01, DFAM-003): l. TestPulseScriptIdentityGate azonos tesztje.
    @pytest.mark.parametrize("load_idn", [
        pytest.param("Keithley, 2380-500-15, 802436052777870003, 1.00-1.00", id="500-15"),
        pytest.param("Keithley, 2380J-120-60, 802436052777870003, 1.00-1.00", id="J-120-60"),
    ])
    def test_r3b2_aborts_on_wrong_2380_variant(self, monkeypatch, tmp_path, capsys, load_idn):
        fake_psu = FakeHilPsu()
        fake_load = FakeHilLoad(idn=load_idn)
        fake_dmm_v = FakeHilDmm()
        fake_dmm_t = FakeHilDmm()
        dmm_instances = iter([fake_dmm_v, fake_dmm_t])
        monkeypatch.setattr(hil_iec_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_iec_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_iec_mod, "Keysight34465ADMM", lambda: next(dmm_instances))
        monkeypatch.setattr(hil_iec_mod, "CONFIG_PATH", str(_write_cfg(tmp_path, with_dmm_temperature=True)))
        monkeypatch.setattr(
            hil_iec_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        hil_iec_mod.main()

        assert fake_psu.calls == ["connect", "idn", "disconnect"]
        assert fake_load.calls == ["connect", "idn", "disconnect"]
        assert fake_dmm_v.calls == ["connect", "idn", "disconnect"]
        assert fake_dmm_t.calls == ["connect", "idn", "disconnect"]
        out = capsys.readouterr().out
        assert "'2380-120-60'" in out
        assert load_idn in out

    def test_r3b2_proceeds_with_lab_2380_120_60_idn(self, monkeypatch, tmp_path):
        fake_psu = FakeHilPsu()
        fake_load = FakeHilLoad(idn="Keithley, 2380-120-60, 802436052777870003, 1.00-1.00")
        fake_dmm_v = FakeHilDmm()
        fake_dmm_t = FakeHilDmm()
        dmm_instances = iter([fake_dmm_v, fake_dmm_t])
        monkeypatch.setattr(hil_iec_mod, "Keithley2220PSU", lambda: fake_psu)
        monkeypatch.setattr(hil_iec_mod, "Keithley2380Load", lambda: fake_load)
        monkeypatch.setattr(hil_iec_mod, "Keysight34465ADMM", lambda: next(dmm_instances))
        monkeypatch.setattr(hil_iec_mod, "CONFIG_PATH", str(_write_cfg(tmp_path, with_dmm_temperature=True)))
        monkeypatch.setattr(
            hil_iec_mod, "BatteryProfile",
            lambda **kw: (_ for _ in ()).throw(_MarkerReached()),
        )

        with pytest.raises(_MarkerReached):
            hil_iec_mod.main()

        assert "all_outputs_off" in fake_psu.calls
        assert "input_off" in fake_load.calls
