"""Prog/tools/connection_test.py pure funkcióinak tesztjei."""
import yaml


class TestIsPlaceholder:
    def test_returns_true_for_usb_placeholder(self):
        from Prog.tools.connection_test import is_placeholder
        assert is_placeholder("USB0::PLACEHOLDER::INSTR") is True

    def test_returns_false_for_real_usb_resource(self):
        from Prog.tools.connection_test import is_placeholder
        assert is_placeholder("USB0::0x05E6::0x2220::1234::INSTR") is False

    def test_returns_true_for_tcpip_placeholder(self):
        from Prog.tools.connection_test import is_placeholder
        assert is_placeholder("TCPIP0::PLACEHOLDER::inst0::INSTR") is True


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
