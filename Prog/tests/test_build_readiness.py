"""R16-B27: spec-adatparitás és template-resource-ok, kizárólag memóriás írással."""
import ast
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from Prog import app_paths
from Prog.gui.panels import config_panel
from Prog.gui.panels.config_panel import ConfigPanel
from Prog.src.pico_provisioning_service import FIRMWARE_FILES

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "akkuteszter.spec"


def _runtime_datas():
    profile_source = ROOT / "Prog/gui/panels/config_panel.py"
    tree = ast.parse(profile_source.read_text(encoding="utf-8"))
    profile_lists = [
        call.args[0]
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "addItems"
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr == "_profile_combo"
    ]
    assert len(profile_lists) == 1
    profiles = ast.literal_eval(profile_lists[0])
    assert profiles
    paths = [
        app_paths.default_config_path(),
        app_paths.local_config_template_path(),
        app_paths.resources_dir() / "psnd.png",
        *(app_paths.pico_firmware_dir() / name for name in FIRMWARE_FILES),
        *(ROOT / "Prog/config/battery_profiles" / f"{name}.yaml" for name in profiles),
    ]
    return {(path.relative_to(ROOT).as_posix(), path.parent.relative_to(ROOT).as_posix())
            for path in paths}


def _datas_nodes(spec_text):
    tree = ast.parse(spec_text)
    analyses = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Analysis"
    ]
    assert len(analyses) == 1
    datas = next(kw.value for kw in analyses[0].keywords if kw.arg == "datas")
    assert isinstance(datas, ast.BinOp) and isinstance(datas.op, ast.Add)
    assert isinstance(datas.left, ast.List)
    return datas.left.elts


def _assert_spec_datas(spec_text):
    entries = [ast.literal_eval(node) for node in _datas_nodes(spec_text)]
    for source, destination in entries:
        assert (ROOT / source).is_file(), f"Hiányzó datas-forrás: {source}"
    missing = _runtime_datas() - set(entries)
    assert not missing, f"Hiányzó runtime datas-pár: {sorted(missing)}"


def test_spec_contains_all_runtime_data_and_existing_sources():
    _assert_spec_datas(SPEC.read_text(encoding="utf-8"))


@pytest.mark.parametrize("removed", sorted(_runtime_datas()))
def test_spec_detector_rejects_each_missing_runtime_row(removed):
    text = SPEC.read_text(encoding="utf-8")
    rows = [node for node in _datas_nodes(text) if ast.literal_eval(node) == removed]
    assert len(rows) == 1
    row = rows[0]
    lines = text.splitlines(keepends=True)
    del lines[row.lineno - 1:row.end_lineno]
    # Közvetlenül a mutált szöveget kapja a detektor; a lemez változatlan.
    with pytest.raises(AssertionError, match="Hiányzó runtime datas-pár"):
        _assert_spec_datas("".join(lines))


def test_spec_detector_rejects_nonexistent_extra_source():
    text = SPEC.read_text(encoding="utf-8").replace(
        '"INSTALL.md"', '"R16_B27_nonexistent.md"',
    )
    with pytest.raises(AssertionError, match="Hiányzó datas-forrás"):
        _assert_spec_datas(text)


def test_spec_detector_rejects_wrong_bundle_destination():
    text = SPEC.read_text(encoding="utf-8").replace(
        '"Prog/resources")', '"wrong/resources")',
    )
    with pytest.raises(AssertionError, match="Hiányzó runtime datas-pár"):
        _assert_spec_datas(text)


class _MemoryYaml:
    def __init__(self, text):
        self.text = text
        self.parent = SimpleNamespace(mkdir=lambda **kwargs: None)

    def exists(self):
        return True

    def open(self, mode="r", *, encoding):
        assert encoding == "utf-8"
        assert mode in ("r", "w")
        if mode == "r":
            return io.StringIO(self.text)
        owner = self

        class _WriteBack(io.StringIO):
            def close(self):
                if not self.closed:
                    owner.text = self.getvalue()
                super().close()

        return _WriteBack()


class _Edit:
    def __init__(self):
        self._text = ""

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text


class _Choice:
    def __init__(self, choices):
        self.choices = choices
        self.selected = None

    def findText(self, text):
        return self.choices.index(text)

    def setCurrentIndex(self, index):
        self.selected = self.choices[index]


def _load_yaml_in_memory(monkeypatch, local_text, *, save=False):
    default_text = app_paths.default_config_path().read_text(encoding="utf-8")
    monkeypatch.setattr(
        app_paths, "default_config_path", lambda: _MemoryYaml(default_text),
    )
    local_path = _MemoryYaml(local_text)
    monkeypatch.setattr(app_paths, "local_config_path", lambda: local_path)
    edits = [_Edit() for _ in range(4)]
    sensor = _Choice(["RTD_4W_PT100", "THERMISTOR_2W_NTC_10K_B3950"])
    panel = SimpleNamespace(
        _psu_res_edit=edits[0], _load_res_edit=edits[1],
        _dmm_v_res_edit=edits[2], _dmm_t_res_edit=edits[3],
        _psu_mode_combo=_Choice(["INDEPENDENT", "PARALLEL", "SERIES"]),
        _temp_comp_combo=_Choice(["OFF", "MONITOR_ONLY", "ENABLED"]),
        _temp_sensor_combo=sensor,
    )
    ConfigPanel._load_yaml(panel)
    if save:
        ConfigPanel._save_local_config(panel)
    return [edit.text() for edit in edits], sensor.selected


def test_fresh_template_leaves_all_four_resources_empty_and_keeps_sensor(monkeypatch):
    text = app_paths.local_config_template_path().read_text(encoding="utf-8")
    resources, sensor = _load_yaml_in_memory(monkeypatch, text, save=True)
    assert resources == ["", "", "", ""]
    assert sensor == "RTD_4W_PT100"
    saved = config_panel.yaml.safe_load(app_paths.local_config_path().text)
    assert [saved["instruments"][name]["resource"] for name in (
        "psu", "load", "dmm_voltage", "dmm_temperature",
    )] == ["", "", "", ""]
    assert saved["temperature_sensor_mode"] == "RTD_4W_PT100"


@pytest.mark.parametrize(
    "resource",
    ["", None, "PLACEHOLDER", "USB0::PLACEHOLDER::INSTR",
     "TCPIP0::192.168.X.X::inst0::INSTR"],
)
def test_placeholder_conventions_are_empty_in_every_resource_field(monkeypatch, resource):
    local_text = json.dumps({"instruments": {
        name: {"resource": resource}
        for name in ("psu", "load", "dmm_voltage", "dmm_temperature")
    }})
    resources, _ = _load_yaml_in_memory(monkeypatch, local_text)
    assert resources == ["", "", "", ""]


def test_real_resource_strings_are_preserved_verbatim(monkeypatch):
    expected = [
        "USB0::0x05E6::0x2220::9204604::INSTR",
        "USB0::0x05E6::0x2380::802436052777870003::INSTR",
        "TCPIP::192.168.2.80::INSTR",
        "TCPIP0::scope.example.com::hislip0::INSTR",
    ]
    local_text = json.dumps({"instruments": {
        name: {"resource": resource}
        for name, resource in zip(
            ("psu", "load", "dmm_voltage", "dmm_temperature"), expected,
        )
    }})
    resources, _ = _load_yaml_in_memory(monkeypatch, local_text)
    assert resources == expected


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
])
def test_real_resources_survive_load_and_save(monkeypatch, resource):
    from pyvisa.rname import ResourceName

    ResourceName.from_string(resource)  # Pure parser; no VISA session.
    expected = {
        "instruments": {
            name: {"resource": resource}
            for name in ("psu", "load", "dmm_voltage", "dmm_temperature")
        },
        "temperature_sensor_mode": "THERMISTOR_2W_NTC_10K_B3950",
        "unrelated": {"keep": True},
    }
    resources, sensor = _load_yaml_in_memory(
        monkeypatch, json.dumps(expected), save=True,
    )
    assert resources == [resource] * 4
    assert sensor == expected["temperature_sensor_mode"]
    assert config_panel.yaml.safe_load(app_paths.local_config_path().text) == expected
