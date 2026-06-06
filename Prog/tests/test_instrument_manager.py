"""
InstrumentManager unit tesztek.
"""
from __future__ import annotations

import pytest

from Prog.src.instrument_manager import InstrumentManager, InstrumentConfig
from Prog.tests.mock_drivers.mock_psu import MockPSU
from Prog.tests.mock_drivers.mock_load import MockLoad
from Prog.tests.mock_drivers.mock_dmm import MockDMM


class TestConnectAllRollback:
    """V2: connect_all() részleges failure esetén rollback."""

    def test_partial_connect_failure_rollbacks_connected_instruments(self):
        psu = MockPSU()
        load = MockLoad(raise_on_connect=True)  # Load connect hibázik
        dmm_v = MockDMM()
        dmm_t = MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        cfg = InstrumentConfig(
            psu_resource="USB::PSU",
            load_resource="USB::LOAD",
            dmm_voltage_resource="TCPIP::DMM_V",
            dmm_temperature_resource="TCPIP::DMM_T",
        )

        with pytest.raises(Exception):
            im.connect_all(cfg)

        # PSU sikeresen csatlakozott, majd rollback hívja disconnect()-et
        assert psu.called("disconnect"), f"PSU calls: {psu.calls}"

    def test_disconnect_all_calls_disconnect_on_all(self):
        psu = MockPSU()
        load = MockLoad()
        dmm_v = MockDMM()
        dmm_t = MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.disconnect_all()
        for inst in (psu, load, dmm_v, dmm_t):
            assert inst.called("disconnect"), f"{inst.__class__.__name__} disconnect nem hívódott"


class TestPollDeviceErrors:
    """poll_device_errors() — SCPI error queue drain minden eszközre."""

    def _make_im(self, psu=None, load=None, dmm_v=None, dmm_t=None):
        return InstrumentManager(
            psu or MockPSU(),
            load or MockLoad(),
            dmm_v or MockDMM(),
            dmm_t or MockDMM(),
        )

    def test_no_errors_returns_empty(self):
        im = self._make_im()
        assert im.poll_device_errors() == []

    def test_psu_single_error_returned(self):
        psu = MockPSU(pending_errors=['222,"Data out of range"'])
        im = self._make_im(psu=psu)
        errors = im.poll_device_errors()
        assert len(errors) == 1
        assert errors[0]["device"] == "PSU"
        assert "222" in errors[0]["error"]

    def test_load_error_labeled_correctly(self):
        load = MockLoad(pending_errors=['+350,"Queue overflow"'])
        im = self._make_im(load=load)
        errors = im.poll_device_errors()
        assert errors[0]["device"] == "Load"

    def test_multiple_errors_drained(self):
        """Egy eszközön több hiba → mind visszajön egy tick alatt."""
        psu = MockPSU(pending_errors=['err1', 'err2', 'err3'])
        im = self._make_im(psu=psu)
        errors = im.poll_device_errors()
        assert len(errors) == 3

    def test_errors_from_multiple_devices(self):
        """PSU és Load is hibával → mind visszajön."""
        psu = MockPSU(pending_errors=['psu_err'])
        load = MockLoad(pending_errors=['load_err'])
        im = self._make_im(psu=psu, load=load)
        errors = im.poll_device_errors()
        devices = {e["device"] for e in errors}
        assert "PSU" in devices and "Load" in devices
