"""CLI kapcsolatellenorzes: pontos placeholder-szures, mockolt driverekkel."""
import importlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from Prog.tools import connection_test


class TestIsPlaceholder:
    def test_returns_true_for_usb_placeholder(self):
        from Prog.tools.connection_test import is_placeholder
        assert is_placeholder("USB0::PLACEHOLDER::INSTR") is True

    def test_returns_false_for_real_usb_resource(self):
        from Prog.tools.connection_test import is_placeholder
        assert is_placeholder("USB0::0x05E6::0x2220::1234::INSTR") is False

    def test_returns_true_for_tcpip_placeholder(self):
        from Prog.tools.connection_test import is_placeholder
        assert is_placeholder("TCPIP0::192.168.X.X::inst0::INSTR") is True


class TestLoadConfig:
    def test_loads_default_config_when_no_local(self, tmp_path):
        from Prog.tools.connection_test import load_config
        default_data = {"instruments": {"psu": {"resource": "TEST_DEFAULT"}}}
        default_path = tmp_path / "default_config.yaml"
        default_path.write_text(yaml.dump(default_data), encoding="utf-8")
        result = load_config(
            local_path=tmp_path / "local_config.yaml",
            default_path=default_path,
        )
        assert result["instruments"]["psu"]["resource"] == "TEST_DEFAULT"

    def test_loads_local_config_when_present(self, tmp_path):
        from Prog.tools.connection_test import load_config
        local_data = {"instruments": {"psu": {"resource": "TEST_LOCAL"}}}
        default_data = {"instruments": {"psu": {"resource": "TEST_DEFAULT"}}}
        local_path = tmp_path / "local_config.yaml"
        default_path = tmp_path / "default_config.yaml"
        local_path.write_text(yaml.dump(local_data), encoding="utf-8")
        default_path.write_text(yaml.dump(default_data), encoding="utf-8")
        result = load_config(local_path=local_path, default_path=default_path)
        assert result["instruments"]["psu"]["resource"] == "TEST_LOCAL"


@pytest.fixture(params=[
    ("psu", "PSU", "device_psu", "Keithley2220PSU"),
    ("load", "Load", "device_load", "Keithley2380Load"),
    ("dmm_voltage", "DMM_V", "device_dmm", "Keysight34465ADMM"),
    ("dmm_temperature", "DMM_T", "device_dmm", "Keysight34465ADMM"),
], ids=["psu", "load", "dmm_voltage", "dmm_temperature"])
def resource_check(request, monkeypatch):
    instrument, label, module_name, class_name = request.param
    module = importlib.import_module(f"Prog.drivers.{module_name}")
    device = Mock()
    device.idn.return_value = "MOCK instrument\n"
    device.read_voltage.return_value = 12.0
    device.read_temperature.return_value = 25.0
    factory = Mock(return_value=device)
    monkeypatch.setattr(module, class_name, factory)
    monkeypatch.setattr(connection_test, "_RESULTS", [])
    return SimpleNamespace(
        instrument=instrument, label=label,
        check=getattr(connection_test, f"check_{instrument}"),
        factory=factory, device=device,
    )


@pytest.mark.parametrize("resource", [
    "USB0::0x05E6::0x2220::SN-X.X-123::INSTR",
    "USB0::0x05E6::0x2380::PLACEHOLDER123::INSTR",
    "USB0::0x05E6::0x2220::SN-PLACEHOLDER-123::INSTR",
    "USB0::0x05E6::0x2220::X.X::INSTR",
    "TCPIP0::X.X::inst0::INSTR",
    "TCPIP0::MUX.XYZ::inst0::INSTR",
    "TCPIP0::X.X::hislip0::INSTR",
    "TCPIP0::MUX.XYZ::hislip0::INSTR",
    "TCPIP0::PLACEHOLDER123::inst0::INSTR",
    "TCPIP0::PLACEHOLDER123::hislip0::INSTR",
    "USB0::0x05E6::0x2220::9204604::INSTR",
    "TCPIP0::192.168.2.80::inst0::INSTR",
    "TCPIP0::scope.example.com::hislip0::INSTR",
    "TCPIP0::PLACEHOLDER::inst0::INSTR",
    "USB0::0x05E6::0x2220::PLACEHOLDER::INSTR",
])
def test_real_resources_reach_connect(resource_check, resource):
    from pyvisa.rname import ResourceName

    ResourceName.from_string(resource)  # Pure parser; no VISA session.
    resource_check.check({
        "instruments": {resource_check.instrument: {"resource": resource}},
    })

    resource_check.factory.assert_called_once_with()
    resource_check.device.connect.assert_called_once_with(resource)
    resource_check.device.idn.assert_called_once_with()
    resource_check.device.disconnect.assert_called_once_with()
    assert len(connection_test._RESULTS) == 1
    assert connection_test._RESULTS[0][:2] == ("\u2705", resource_check.label)


@pytest.mark.parametrize("resource", [
    "PLACEHOLDER",
    "USB0::PLACEHOLDER::INSTR",
    "TCPIP0::192.168.X.X::inst0::INSTR",
    "",
    None,
])
def test_unconfigured_resources_skip_without_driver(resource_check, resource):
    resource_check.check({
        "instruments": {resource_check.instrument: {"resource": resource}},
    })

    resource_check.factory.assert_not_called()
    resource_check.device.connect.assert_not_called()
    assert len(connection_test._RESULTS) == 1
    assert connection_test._RESULTS[0][:2] == ("\u26a0\ufe0f", resource_check.label)


@pytest.mark.parametrize("source", [
    "default_config.yaml", "local_config.template.yaml",
])
def test_config_sources_skip_without_driver(resource_check, source):
    path = Path(__file__).resolve().parents[2] / "config" / source
    cfg = connection_test.load_config(local_path=path, default_path=path)
    resource_check.check(cfg)

    resource_check.factory.assert_not_called()
    resource_check.device.connect.assert_not_called()
    assert len(connection_test._RESULTS) == 1
    assert connection_test._RESULTS[0][:2] == ("\u26a0\ufe0f", resource_check.label)


@pytest.mark.parametrize(("yaml_value", "expected"), [
    ('""', ""), ("", None),
])
def test_load_config_preserves_empty_yaml_resources(monkeypatch, yaml_value, expected):
    text = f"instruments:\n  psu:\n    resource: {yaml_value}\n"
    monkeypatch.setattr(
        connection_test, "open", lambda *args, **kwargs: io.StringIO(text),
        raising=False,
    )
    path = Path(__file__)
    cfg = connection_test.load_config(local_path=path, default_path=path)

    assert cfg["instruments"]["psu"]["resource"] == expected
    assert connection_test.is_placeholder(cfg["instruments"]["psu"]["resource"]) is True
