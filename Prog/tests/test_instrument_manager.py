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
