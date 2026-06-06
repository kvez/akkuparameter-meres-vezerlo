"""_search_visa_resources() unit tesztek — mock pyvisa, nincs valós műszer."""
import sys
from unittest.mock import MagicMock


def _make_mock_rm(resources, idn="KEITHLEY,2220-30-1,MOCK,1.0", idn_exc=None):
    rm = MagicMock()
    rm.list_resources.return_value = list(resources)
    inst = MagicMock()
    if idn_exc:
        inst.query.side_effect = idn_exc
    else:
        inst.query.return_value = idn
    rm.open_resource.return_value = inst
    return rm


def test_search_returns_idn_for_each_resource(monkeypatch):
    mock_rm = _make_mock_rm(["USB0::INSTR", "TCPIP::INSTR"])
    mock_pyvisa = MagicMock()
    mock_pyvisa.ResourceManager.return_value = mock_rm
    monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

    from Prog.gui.panels.device_search_dialog import _search_visa_resources
    results = _search_visa_resources()

    assert len(results) == 2
    assert all("KEITHLEY" in idn for _, idn in results)


def test_search_handles_idn_timeout(monkeypatch):
    mock_rm = _make_mock_rm(["USB0::INSTR"], idn_exc=Exception("timeout"))
    mock_pyvisa = MagicMock()
    mock_pyvisa.ResourceManager.return_value = mock_rm
    monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

    from Prog.gui.panels.device_search_dialog import _search_visa_resources
    results = _search_visa_resources()

    assert len(results) == 1
    _, msg = results[0]
    assert "sikertelen" in msg.lower() or "timeout" in msg.lower()


def test_search_handles_pyvisa_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyvisa", None)

    import importlib
    import Prog.gui.panels.device_search_dialog as mod
    importlib.reload(mod)
    results = mod._search_visa_resources()

    assert len(results) == 1
    assert results[0][0] == "ERROR"
    assert "NI-VISA" in results[0][1] or "pyvisa" in results[0][1]


def test_search_handles_no_resources(monkeypatch):
    mock_rm = _make_mock_rm([])
    mock_pyvisa = MagicMock()
    mock_pyvisa.ResourceManager.return_value = mock_rm
    monkeypatch.setitem(sys.modules, "pyvisa", mock_pyvisa)

    from Prog.gui.panels.device_search_dialog import _search_visa_resources
    results = _search_visa_resources()

    assert len(results) == 1
    assert results[0][0] == "INFO"
