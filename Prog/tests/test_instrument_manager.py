"""
InstrumentManager unit tesztek.
"""
from __future__ import annotations

import pytest

from Prog.src.exceptions import InstrumentError
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

    def test_rollback_also_disconnects_the_instrument_whose_own_connect_failed(self):
        """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), Hardver/
        Műszervezérlés P2 lelet] A `connected` lista korábban csak a
        SIKERES `inst.connect(resource)` visszatérése UTÁN bővült — a
        valós driverek viszont a `_resource`-t/`_connected=True`-t a
        connect() ELEJÉN állítják be, a biztonságkritikus végső lépést
        (pl. all_outputs_off()) a VÉGÉN hívják. Ha EZ hibázik, a hibázó
        műszer objektum sosem kapta meg a saját disconnect()-jét a
        rollback során — ezt szimuláljuk egy olyan mockkal, ami a
        `calls`-t megjelöli (mintha a resource már nyitva lenne), MIELŐTT
        hibázna."""
        psu = MockPSU()

        class _LoadThatFailsNearEndOfConnect(MockLoad):
            def connect(self, resource: str) -> None:
                # A valós driverek mintáját követve: a resource "megnyílt"
                # (calls rögzítve), MIELŐTT a connect() vége felé hibázna.
                self.calls.append(f"connect({resource!r})")
                raise InstrumentError("simulated failure near end of connect()")

        load = _LoadThatFailsNearEndOfConnect()
        dmm_v = MockDMM()
        dmm_t = MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        cfg = InstrumentConfig(
            psu_resource="USB::PSU", load_resource="USB::LOAD",
            dmm_voltage_resource="TCPIP::DMM_V", dmm_temperature_resource="TCPIP::DMM_T",
        )

        with pytest.raises(Exception):
            im.connect_all(cfg)

        assert psu.called("disconnect"), f"PSU calls: {psu.calls}"
        assert load.called("disconnect"), f"Load calls: {load.calls}"

    def test_rollback_calls_safe_off_before_disconnect(self):
        """MAX verifier follow-up (2026-09-08, HW2-04 F1, DFAM-016 RECURRING
        sibling): a rollback korábban KIZÁRÓLAG disconnect()-et hívott a
        sikeresen csatlakozott instrumenteken -- ez csak a VISA-erőforrást
        zárja, NEM ad ki OUTPUT/INPUT OFF parancsot. Ugyanaz a hiányosság,
        mint a main_window.py 3 bring-up hibaútjában (l. HW2-04, commit
        6948b91), amit ez a rollback-ág NEM fedett le. safe_off()-nak a
        disconnect() ELŐTT kell futnia minden megkísérelt instrumenten."""
        psu = MockPSU()
        load = MockLoad(raise_on_connect=True)
        dmm_v = MockDMM()
        dmm_t = MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        cfg = InstrumentConfig(
            psu_resource="USB::PSU", load_resource="USB::LOAD",
            dmm_voltage_resource="TCPIP::DMM_V", dmm_temperature_resource="TCPIP::DMM_T",
        )

        with pytest.raises(Exception):
            im.connect_all(cfg)

        assert psu.called("safe_off"), f"PSU calls: {psu.calls}"
        safe_off_idx = psu.calls.index("safe_off()")
        disconnect_idx = psu.calls.index("disconnect()")
        assert safe_off_idx < disconnect_idx

    def test_disconnect_all_calls_disconnect_on_all(self):
        psu = MockPSU()
        load = MockLoad()
        dmm_v = MockDMM()
        dmm_t = MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.disconnect_all()
        for inst in (psu, load, dmm_v, dmm_t):
            assert inst.called("disconnect"), f"{inst.__class__.__name__} disconnect nem hívódott"


class TestIdentifyAll:
    """WP-AK-02/INS-001: identify_all() minden eszköz *IDN?-jét lekérdezi."""

    def test_returns_all_four_identities(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        identities = im.identify_all()
        assert set(identities.keys()) == {"PSU", "Load", "DMM_V", "DMM_T"}
        assert "KEITHLEY" in identities["PSU"]
        assert "KEITHLEY" in identities["Load"]
        assert "KEYSIGHT" in identities["DMM_V"]
        assert "KEYSIGHT" in identities["DMM_T"]

    def test_calls_idn_on_every_device(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.identify_all()
        for inst in (psu, load, dmm_v, dmm_t):
            assert inst.called("idn()"), f"{inst.__class__.__name__} idn() nem hívódott"


class TestIdentifyAllExpectedModelValidation:
    """Governance-v2 review-kör 1 (Hardver reviewer, CF-38/HW-R1-06,
    DFAM-003): a *IDN? válaszok korábban bekerültek a checkpointba/
    riportba, de SEMMI nem vetette össze őket az elvárt modellel — egy
    felcserélt VISA resource-string esetén a PSU-driver a load-ra küldött
    volna parancsot és fordítva, csak a session futása közben, SCPI
    error-queue-ból derülve ki (vagy egyáltalán nem). Az elvárt minta a
    HÍVÓTÓL jön (nem itt hardkódolt egyedi sorozatszám)."""

    def test_matching_models_do_not_raise(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        identities = im.identify_all(
            expected_models={"PSU": "2220", "Load": "2380", "DMM_V": "34465A", "DMM_T": "34465A"}
        )
        assert set(identities.keys()) == {"PSU", "Load", "DMM_V", "DMM_T"}

    def test_swapped_psu_and_load_resources_raise_configuration_error(self):
        """Felcserélt VISA resource-string szimulációja: a "PSU" szerepre
        ténylegesen egy Load-identitású eszköz csatlakozott."""
        from Prog.src.exceptions import ConfigurationError

        swapped_psu_identity = MockLoad()  # a "PSU" szerepen egy Load-identitás
        im = InstrumentManager(swapped_psu_identity, MockLoad(), MockDMM(), MockDMM())

        with pytest.raises(ConfigurationError, match="PSU"):
            im.identify_all(
                expected_models={"PSU": "2220", "Load": "2380", "DMM_V": "34465A", "DMM_T": "34465A"}
            )

    def test_no_expected_models_skips_validation(self):
        """Visszamenőleges kompatibilitás: expected_models nélkül (default
        None) a viselkedés változatlan — nincs validáció."""
        im = InstrumentManager(MockLoad(), MockLoad(), MockDMM(), MockDMM())
        identities = im.identify_all()  # nem szabad kivételt dobnia
        assert "PSU" in identities


class TestReviewRound5Hw505DuplicatePhysicalInstrumentDetection:
    """[regresszió, review-kör 5, Hardver/Műszervezérlés HW5-05 (P3)] az
    *IDN? válasz tartalmazza a műszer egyedi sorozatszámát -- két
    KÜLÖNBÖZŐ fizikai eszköz sosem adhat vissza azonosat. identify_all()
    korábban ezt nem ellenőrizte: ha ugyanaz a fizikai DMM véletlenül
    mindkét VISA resource-string mögé (DMM_V ÉS DMM_T) be lett kötve, a
    két csatorna csendben ugyanazt a műszert olvasta volna, az egyik
    mérést (feszültség VAGY hőmérséklet) a másikkal versengő panel-
    átváltás torzította volna, magyarázat nélkül. A mock-eszközök (l.
    mock_dmm.py/mock_load.py/mock_psu.py `serial` mezője) alapból EGYEDI
    azonosítót kapnak példányonként (mint a valóságban minden fizikai
    műszer) -- ez a teszt explicit AZONOS `serial`-t ad meg két
    példánynak, hogy szándékosan szimulálja az "ugyanaz a fizikai
    eszköz két szerepen" hibát."""

    def test_same_physical_dmm_on_two_roles_raises_configuration_error(self):
        from Prog.src.exceptions import ConfigurationError

        dmm_v = MockDMM(serial="SN12345")
        dmm_t = MockDMM(serial="SN12345")  # UGYANAZ a fizikai eszköz, véletlenül
        im = InstrumentManager(MockPSU(), MockLoad(), dmm_v, dmm_t)

        with pytest.raises(ConfigurationError, match="DMM_V.*DMM_T|DMM_T.*DMM_V"):
            im.identify_all()

    def test_same_physical_load_used_for_psu_role_raises_configuration_error(self):
        """A duplikátum-ellenőrzés nem csak a DMM-párra korlátozódik --
        BÁRMELY két szerep ütközését jelzi."""
        from Prog.src.exceptions import ConfigurationError

        same_device = MockLoad(serial="SNXYZ")
        im = InstrumentManager(same_device, MockLoad(serial="SNXYZ"), MockDMM(), MockDMM())

        with pytest.raises(ConfigurationError, match="PSU.*Load|Load.*PSU"):
            im.identify_all()

    def test_distinct_mocks_get_distinct_default_serials(self):
        """Ellenpróba: alapértelmezésben (serial nem megadva) minden
        mock-példány EGYEDI azonosítót kap -- a normál, két-DMM-mock
        konstrukció (a teszt-csomag túlnyomó többségében így épül fel)
        NEM triggereli hamisan a duplikátum-ellenőrzést."""
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        identities = im.identify_all()  # nem szabad kivételt dobnia
        assert identities["DMM_V"] != identities["DMM_T"]


class TestReadAllSkipBatteryVoltage:
    """2026-08-23 traditional_learning OV incidens elemzés: read_all()-nak
    ki kell tudnia hagyni a DMM-feszültség csatornát, amikor a hívó (a
    controller saját, tick-konzisztens u_batt_V-je) már máshonnan
    rendelkezésre áll — enélkül minden tick-ben egy MÁSODIK, deszinkron
    DMM-olvasás történne (CLAUDE.md §9)."""

    def test_skip_battery_voltage_does_not_call_dmm(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.read_all(skip_battery_voltage=True)
        assert not dmm_v.called("read_voltage()")

    def test_skip_battery_voltage_returns_none_for_that_key(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_battery_voltage=True)
        assert result["battery_voltage_V"] is None
        # A többi csatorna továbbra is olvasódik.
        assert result["battery_temperature_C"] is not None
        assert result["psu_readback_voltage_V"] is not None

    def test_default_still_reads_battery_voltage(self):
        dmm_v = MockDMM(voltage_V=13.37)
        im = InstrumentManager(MockPSU(), MockLoad(), dmm_v, MockDMM())
        result = im.read_all()
        assert result["battery_voltage_V"] == pytest.approx(13.37)
        assert dmm_v.called("read_voltage()")

    def test_skip_flag_reflected_in_skipped_key(self):
        """[regresszió, 2026-09-15, C-11/DFAM-018] battery_voltage_V=None
        korábban MEGKÜLÖNBÖZTETHETETLEN volt a szándékos kihagyás és egy
        valódi olvasási hiba között -- a battery_voltage_V_skipped kulcs
        mostantól explicit jelzi, melyik eset áll fenn."""
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_battery_voltage=True)
        assert result["battery_voltage_V"] is None
        assert result["battery_voltage_V_skipped"] is True

    def test_read_failure_not_skip_leaves_skipped_key_false(self):
        """[regresszió, 2026-09-15, C-11/DFAM-018] Egy VALÓDI olvasási
        hiba (nem kihagyás) ugyanúgy None-t ad, DE battery_voltage_V_
        skipped=False marad -- a hívó ebből tudja megkülönböztetni a két
        esetet."""
        dmm_v = MockDMM(simulate_timeout=True)
        im = InstrumentManager(MockPSU(), MockLoad(), dmm_v, MockDMM())
        result = im.read_all(skip_battery_voltage=False)
        assert result["battery_voltage_V"] is None
        assert result["battery_voltage_V_skipped"] is False


class TestReadAllSkipBatteryTemperature:
    """Ugyanaz az elv, mint TestReadAllSkipBatteryVoltage-nál, de a
    hőmérséklet-DMM-re — a TraditionalLearningController/GoldenGgController
    saját, tick-konzisztens battery_temperature_C-je esetén a TestRunner
    ezt adja át, hogy a read_all() ne kérdezze le MÉG EGYSZER a DMM_T-t."""

    def test_skip_battery_temperature_does_not_call_dmm(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.read_all(skip_battery_temperature=True)
        assert not dmm_t.called("read_temperature()")

    def test_skip_battery_temperature_returns_none_for_that_key(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_battery_temperature=True)
        assert result["battery_temperature_C"] is None
        # A többi csatorna továbbra is olvasódik.
        assert result["battery_voltage_V"] is not None
        assert result["psu_readback_voltage_V"] is not None

    def test_default_still_reads_battery_temperature(self):
        dmm_t = MockDMM(temperature_C=25.5)
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), dmm_t)
        result = im.read_all()
        assert result["battery_temperature_C"] == pytest.approx(25.5)
        assert dmm_t.called("read_temperature()")

    def test_skip_flag_reflected_in_skipped_key(self):
        """[regresszió, 2026-09-15, C-11/DFAM-018, MAX verifier F2 lelete]
        L. TestReadAllSkipBatteryVoltage azonos nevű tesztje a teljes
        indoklásért -- ez a hőmérséklet-oldali testvér, ami korábban
        HIÁNYZOTT a lefedettségből (a battery_temperature_C_skipped
        kulcs mutáció-teszttel igazoltan lefedetlen volt)."""
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_battery_temperature=True)
        assert result["battery_temperature_C"] is None
        assert result["battery_temperature_C_skipped"] is True

    def test_read_failure_not_skip_leaves_skipped_key_false(self):
        """[regresszió, 2026-09-15, C-11/DFAM-018, MAX verifier F2 lelete]
        L. TestReadAllSkipBatteryVoltage azonos nevű tesztje a teljes
        indoklásért."""
        dmm_t = MockDMM(simulate_temp_failure=True)
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), dmm_t)
        result = im.read_all(skip_battery_temperature=False)
        assert result["battery_temperature_C"] is None
        assert result["battery_temperature_C_skipped"] is False

    def test_both_skips_combined_calls_neither_dmm(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        result = im.read_all(skip_battery_voltage=True, skip_battery_temperature=True)
        assert not dmm_v.called("read_voltage()")
        assert not dmm_t.called("read_temperature()")
        assert result["battery_voltage_V"] is None
        assert result["battery_temperature_C"] is None
        assert result["psu_readback_voltage_V"] is not None


class TestReadAllSkipPsuVoltage:
    """[teljes kódbázis review kör 6 (2026-09-15), C-08b] Ugyanaz az elv,
    mint TestReadAllSkipBatteryVoltage-nál, de a PSU-feszültség-
    csatornára -- a beágyazott ChargeController saját, tick-konzisztens
    u_psu_measured_V-je esetén a TestRunner ezt adja át, hogy a
    read_all() ne induljon egy MÁSODIK, önálló PSU-feszültség-
    lekérdezéssel."""

    def test_skip_psu_voltage_does_not_call_psu(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.read_all(skip_psu_voltage=True)
        assert not psu.called("measure_output_voltage()")

    def test_skip_psu_voltage_returns_none_for_that_key(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_psu_voltage=True)
        assert result["psu_readback_voltage_V"] is None
        # A többi csatorna továbbra is olvasódik.
        assert result["psu_readback_current_A"] is not None
        assert result["battery_voltage_V"] is not None

    def test_default_still_reads_psu_voltage(self):
        psu = MockPSU(voltage_V=15.9)
        im = InstrumentManager(psu, MockLoad(), MockDMM(), MockDMM())
        result = im.read_all()
        assert result["psu_readback_voltage_V"] == pytest.approx(15.9)
        assert psu.called("measure_output_voltage()")

    def test_skip_flag_reflected_in_skipped_key(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_psu_voltage=True)
        assert result["psu_readback_voltage_V"] is None
        assert result["psu_readback_voltage_V_skipped"] is True

    def test_read_failure_not_skip_leaves_skipped_key_false(self):
        psu = MockPSU(simulate_voltage_readback_timeout=True)
        im = InstrumentManager(psu, MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_psu_voltage=False)
        assert result["psu_readback_voltage_V"] is None
        assert result["psu_readback_voltage_V_skipped"] is False


class TestReadAllSkipPsuCurrent:
    """[teljes kódbázis review kör 6 (2026-09-15), C-08b] Ugyanaz az elv,
    mint TestReadAllSkipPsuVoltage-nál, de a PSU-áram-csatornára."""

    def test_skip_psu_current_does_not_call_psu(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.read_all(skip_psu_current=True)
        assert not psu.called("measure_output_current()")

    def test_skip_psu_current_returns_none_for_that_key(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_psu_current=True)
        assert result["psu_readback_current_A"] is None
        # A többi csatorna továbbra is olvasódik.
        assert result["psu_readback_voltage_V"] is not None
        assert result["battery_voltage_V"] is not None

    def test_default_still_reads_psu_current(self):
        psu = MockPSU(current_A=2.25)
        im = InstrumentManager(psu, MockLoad(), MockDMM(), MockDMM())
        result = im.read_all()
        assert result["psu_readback_current_A"] == pytest.approx(2.25)
        assert psu.called("measure_output_current()")

    def test_skip_flag_reflected_in_skipped_key(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_psu_current=True)
        assert result["psu_readback_current_A"] is None
        assert result["psu_readback_current_A_skipped"] is True

    def test_read_failure_not_skip_leaves_skipped_key_false(self):
        psu = MockPSU(simulate_current_readback_timeout=True)
        im = InstrumentManager(psu, MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_psu_current=False)
        assert result["psu_readback_current_A"] is None
        assert result["psu_readback_current_A_skipped"] is False

    def test_all_four_skips_combined_calls_no_instrument(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        result = im.read_all(
            skip_battery_voltage=True,
            skip_battery_temperature=True,
            skip_psu_voltage=True,
            skip_psu_current=True,
        )
        assert not dmm_v.called("read_voltage()")
        assert not dmm_t.called("read_temperature()")
        assert not psu.called("measure_output_voltage()")
        assert not psu.called("measure_output_current()")
        assert result["battery_voltage_V"] is None
        assert result["battery_temperature_C"] is None
        assert result["psu_readback_voltage_V"] is None
        assert result["psu_readback_current_A"] is None
        # A Load csatorna ezt a paraméter-négyest nem ismeri, továbbra is olvasódik.
        assert result["load_readback_voltage_V"] is not None


class TestReadAllSkipLoadCurrent:
    """Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
    ugyanaz az elv, mint TestReadAllSkipPsuCurrent-nél, de a Load-áram-
    csatornára -- a kisütő fázisban aktív composite controller saját,
    tick-konzisztens i_load_measured_A-ja esetén a TestRunner ezt adja
    át, hogy a read_all() ne induljon egy MÁSODIK, önálló Load-áram-
    lekérdezéssel. A Load-FESZÜLTSÉG csatorna nem kap ilyen, a dupla-
    olvasás ellen szóló paramétert; a Kör 20 / SW20-1 óta létező
    `skip_load_voltage` MÁS, szűkebb (halott-eszköz) szerződésre szolgál
    -- l. TestReadAllSkipLoadVoltage."""

    def test_skip_load_current_does_not_call_load(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.read_all(skip_load_current=True)
        assert not load.called("measure_current()")
        # A Load-feszültség csatorna nem érintett -- továbbra is olvasódik.
        assert load.called("measure_voltage()")

    def test_skip_load_current_returns_none_for_that_key(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_load_current=True)
        assert result["load_readback_current_A"] is None
        assert result["load_readback_voltage_V"] is not None

    def test_default_still_reads_load_current(self):
        load = MockLoad(current_A=1.75, _input_is_on=True)
        im = InstrumentManager(MockPSU(), load, MockDMM(), MockDMM())
        result = im.read_all()
        assert result["load_readback_current_A"] == pytest.approx(1.75)
        assert load.called("measure_current()")

    def test_skip_flag_reflected_in_skipped_key(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_load_current=True)
        assert result["load_readback_current_A"] is None
        assert result["load_readback_current_A_skipped"] is True

    def test_read_failure_not_skip_leaves_skipped_key_false(self):
        load = MockLoad(simulate_current_readback_failure=True)
        im = InstrumentManager(MockPSU(), load, MockDMM(), MockDMM())
        result = im.read_all(skip_load_current=False)
        assert result["load_readback_current_A"] is None
        assert result["load_readback_current_A_skipped"] is False


class TestReadAllSkipLoadVoltage:
    """Kör 20 Batch E4 (SW20-1, P2, DFAM-001+DFAM-027): a Load-FESZÜLTSÉG
    csatorna korábban FELTÉTEL NÉLKÜL olvasódott, míg a másik 5 csatorna
    (köztük a Load-ÁRAM, l. TestReadAllSkipLoadCurrent) mind kapott
    skip-paramétert -- egy bizonyítottan halott Loadra (a controller MÁR
    lefuttatta az emergency_stop("LOAD_COMM_LOST")-ot) így egy akár ~15s
    blokkoló MEAS:VOLT? ment ki a terminális visszatérés előtt
    (rules/10-safety.md #3). A `skip_load_voltage` szerződése SZŰKEBB,
    mint a `skip_load_current`-é -- l. read_all() docstringje."""

    def test_skip_load_voltage_does_not_call_load_voltage(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.read_all(skip_load_voltage=True)
        assert not load.called("measure_voltage()")
        # A Load-áram csatorna nem érintett -- továbbra is olvasódik.
        assert load.called("measure_current()")

    def test_skip_load_voltage_returns_none_and_skipped_true(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all(skip_load_voltage=True)
        assert result["load_readback_voltage_V"] is None
        assert result["load_readback_voltage_V_skipped"] is True

    def test_default_still_reads_load_voltage(self):
        load = MockLoad(voltage_V=12.34)
        im = InstrumentManager(MockPSU(), load, MockDMM(), MockDMM())
        result = im.read_all()
        assert result["load_readback_voltage_V"] == pytest.approx(12.34)
        assert load.called("measure_voltage()")
        assert result["load_readback_voltage_V_skipped"] is False

    def test_read_failure_not_skip_leaves_skipped_key_false(self):
        load = MockLoad(simulate_timeout=True)
        im = InstrumentManager(MockPSU(), load, MockDMM(), MockDMM())
        result = im.read_all(skip_load_voltage=False)
        assert result["load_readback_voltage_V"] is None
        assert result["load_readback_voltage_V_skipped"] is False

    def test_all_six_skips_combined_calls_no_instrument(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        result = im.read_all(
            skip_battery_voltage=True, skip_battery_temperature=True,
            skip_psu_voltage=True, skip_psu_current=True,
            skip_load_current=True, skip_load_voltage=True,
        )
        assert not any([
            dmm_v.called("read_voltage()"), dmm_t.called("read_temperature()"),
            psu.called("measure_output_voltage()"), psu.called("measure_output_current()"),
            load.called("measure_voltage()"), load.called("measure_current()"),
        ])
        for key in (
            "battery_voltage_V", "battery_temperature_C",
            "psu_readback_voltage_V", "psu_readback_current_A",
            "load_readback_current_A", "load_readback_voltage_V",
        ):
            assert result[key] is None
            assert result[f"{key}_skipped"] is True


class TestReadAllSkippedKeyParity:
    """Kör 20 Batch E4 (SW20-1, strukturális megelőzés, DFAM-043 §6.3):
    minden csatornának KELL `_skipped` kulcsot kapnia -- a
    load_readback_voltage_V hiánya pontosan ez a hiba volt. Ez egy
    jövőbeli, 7. csatorna bevezetésekor is elkapja, ha az elfelejt
    `_skipped` kulcsot adni."""

    _CHANNEL_KEYS = (
        "battery_voltage_V", "battery_temperature_C",
        "psu_readback_voltage_V", "psu_readback_current_A",
        "load_readback_current_A", "load_readback_voltage_V",
    )

    def test_every_channel_has_a_skipped_key(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.read_all()
        for key in self._CHANNEL_KEYS:
            assert f"{key}_skipped" in result, f"hiányzó _skipped kulcs: {key}"

    def test_channel_read_order_unchanged_when_nothing_skipped(self):
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.read_all()
        assert dmm_v.calls == ["read_voltage()"]
        assert dmm_t.calls == ["read_temperature()"]
        assert psu.calls[:2] == ["measure_output_voltage()", "measure_output_current()"]
        assert load.calls[:2] == ["measure_voltage()", "measure_current()"]


class TestSafeAllOff:
    """CLAUDE.md §10.4: safe_all_off() sosem nyelheti el csendben, hogy egy
    eszköz OFF-parancsa ténylegesen sikerült-e — per-eszköz bool eredményt
    kell adnia, hogy a bizonytalan OFF-állapot ne maradjon csendben."""

    def test_all_succeed_returns_all_true(self):
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        result = im.safe_all_off()
        assert result == {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}

    def test_psu_safe_off_failure_reported_false_others_still_attempted(self):
        psu = MockPSU(raise_on_safe_off=True)
        load = MockLoad()
        im = InstrumentManager(psu, load, MockDMM(), MockDMM())
        result = im.safe_all_off()
        assert result["PSU"] is False
        assert result["Load"] is True
        # a PSU hibája nem akadályozta meg a Load safe_off() megkísérlését
        assert load.called("safe_off()")

    def test_load_safe_off_failure_reported_false_psu_still_attempted(self):
        psu = MockPSU()
        load = MockLoad(raise_on_safe_off=True)
        im = InstrumentManager(psu, load, MockDMM(), MockDMM())
        result = im.safe_all_off()
        assert result["Load"] is False
        assert result["PSU"] is True
        assert psu.called("safe_off()")

    def test_both_psu_and_load_fail_both_reported_false(self):
        psu = MockPSU(raise_on_safe_off=True)
        load = MockLoad(raise_on_safe_off=True)
        im = InstrumentManager(psu, load, MockDMM(), MockDMM())
        result = im.safe_all_off()
        assert result["PSU"] is False
        assert result["Load"] is False


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
        assert errors[0]["error"] == '222,"Data out of range"'

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

    def test_check_error_exception_surfaces_explicit_entry_not_silently_swallowed(self):
        """[regresszió, 2026-09-15, C-11/DFAM-018+DFAM-012] Ha maga a
        device.check_error() (SYST:ERR? lekérdezés) I/O-kivétellel
        hibázik (nem egy VISSZAADOTT SCPI-hiba, hanem a kommunikáció
        maga szakad meg), ez KORÁBBAN csendben elnyelődött -- egy TARTÓS
        kommunikációs hiba emiatt sosem jutott el az INS-003 kritikus-
        SCPI-hiba→FAULT kapuig (test_runner.py), "nincs hiba"-ként
        olvasva a "nem tudom lekérdezni"-t. Mostantól a poll-hiba MAGA
        explicit ERROR_QUEUE_POLL_FAILED bejegyzésként kerül a listába,
        ami az is_critical_scpi_error()-on át alapértelmezetten
        kritikusnak minősül."""
        from Prog.src.scpi_error_policy import is_critical_scpi_error

        psu = MockPSU(simulate_check_error_failure=True)
        im = self._make_im(psu=psu)
        errors = im.poll_device_errors()  # nem szabad kivételt dobnia
        assert len(errors) == 1
        assert errors[0]["device"] == "PSU"
        assert "ERROR_QUEUE_POLL_FAILED" in errors[0]["error"]
        assert is_critical_scpi_error(errors[0]["error"]) is True


class TestPollDeviceErrorsOutputStateMismatch:
    """Review-kör 5, Hardver/Műszervezérlés HW5-02/HW5-03 (P3): a
    commanded_on/actual (read_output_state()/read_input_state())
    eltérését a MEGLÉVŐ poll_device_errors()-fogyasztói láncon jelzi —
    olvasás-only, semmilyen safety-döntést nem változtat."""

    def _make_im(self, psu=None, load=None, dmm_v=None, dmm_t=None):
        return InstrumentManager(
            psu or MockPSU(),
            load or MockLoad(),
            dmm_v or MockDMM(),
            dmm_t or MockDMM(),
        )

    def test_no_mismatch_no_extra_entries(self):
        """Alapállapot (mindkét mock OFF/OFF) nem termel hamis-pozitívot —
        [regresszió] a meglévő TestPollDeviceErrors osztály tesztjei ezt
        implicit feltételezik."""
        im = self._make_im()
        assert im.poll_device_errors() == []

    def test_psu_commanded_off_but_actually_on_is_flagged(self):
        """A biztonságkritikus irány: a szoftver OFF-nak hiszi, de a
        hardver ténylegesen ON — CLAUDE.md §6.2 "bizonytalan állapot is
        hiba"."""
        psu = MockPSU()
        psu._output_is_on = True  # hardver ON, de commanded_on=False maradt
        im = self._make_im(psu=psu)
        errors = im.poll_device_errors()
        assert len(errors) == 1
        assert errors[0]["device"] == "PSU"
        assert "OUTPUT_STATE_MISMATCH" in errors[0]["error"]
        assert "commanded=OFF" in errors[0]["error"]
        assert "actual=ON" in errors[0]["error"]

    def test_load_commanded_off_but_actually_on_is_flagged(self):
        load = MockLoad()
        load._input_is_on = True
        im = self._make_im(load=load)
        errors = im.poll_device_errors()
        assert len(errors) == 1
        assert errors[0]["device"] == "Load"
        assert "INPUT_STATE_MISMATCH" in errors[0]["error"]

    def test_psu_commanded_on_matches_actual_on_no_mismatch(self):
        psu = MockPSU()
        psu.output_commanded_on = True
        psu._output_is_on = True
        im = self._make_im(psu=psu)
        assert im.poll_device_errors() == []

    def test_readback_failure_does_not_raise_and_surfaces_poll_failed_entry(self):
        """[regresszió, 2026-09-15, C-11/DFAM-018+DFAM-012] A commanded/
        actual összevetés MAGA egy diagnosztikai kiegészítés -- ha a
        readback-lekérdezés időtúllépik, ez nem akaszthatja meg a teljes
        poll_device_errors()-t (ami a SCPI error-queue drain-t is végzi),
        és nem generálhat HAMIS mismatch-bejegyzést sem (a tényleges
        commanded/actual állapotot nem tudjuk összevetni). KORÁBBAN ez a
        kivétel CSENDBEN elnyelődött (errors == []) -- ez azt jelentette,
        hogy egy TARTÓS PSU-kommunikációs hiba sosem jutott el az INS-003
        kritikus-SCPI-hiba→FAULT kapuig. Mostantól explicit
        OUTPUT_STATE_POLL_FAILED bejegyzés jelzi a poll-hibát magát,
        UGYANAZON a csatornán, mint a valós SCPI hibák."""
        psu = MockPSU(simulate_output_state_readback_timeout=True)
        im = self._make_im(psu=psu)
        errors = im.poll_device_errors()  # nem szabad kivételt dobnia
        assert len(errors) == 1
        assert errors[0]["device"] == "PSU"
        assert "OUTPUT_STATE_POLL_FAILED" in errors[0]["error"]
        assert "OUTPUT_STATE_MISMATCH" not in errors[0]["error"]

    def test_load_readback_failure_does_not_raise_and_surfaces_poll_failed_entry(self):
        """[regresszió, 2026-09-15, C-11/DFAM-018+DFAM-012] A testvér-oldali
        (Load) `read_input_state()` poll-hibája -- l. a fenti PSU-oldali
        teszt teljes indoklását. Ez a szimmetrikus eset korábban HIÁNYZÓ
        volt a lefedettségből (csak a PSU-oldal kapott dedikált tesztet)."""
        load = MockLoad(simulate_input_state_readback_timeout=True)
        im = self._make_im(load=load)
        errors = im.poll_device_errors()  # nem szabad kivételt dobnia
        assert len(errors) == 1
        assert errors[0]["device"] == "Load"
        assert "INPUT_STATE_POLL_FAILED" in errors[0]["error"]
        assert "INPUT_STATE_MISMATCH" not in errors[0]["error"]

    def test_does_not_change_concurrent_psu_load_safety_flag(self):
        """A diagnosztika NEM módosíthatja a commanded_on/input_commanded_on
        flaget -- azok maradnak az EGYETLEN forrás, amit
        check_concurrent_psu_load() néz (CLAUDE.md §10.2 minimális diff /
        safety-döntés változatlansága)."""
        psu = MockPSU()
        psu._output_is_on = True
        load = MockLoad()
        load._input_is_on = True
        im = self._make_im(psu=psu, load=load)
        im.poll_device_errors()
        assert psu.output_commanded_on is False
        assert load.input_commanded_on is False

    def test_energy_state_uncertain_predicate_marks_only_energy_state_entries(self):
        """[regresszió, R1 F-01 + F-05, B1B2_fixplan_FINAL.md B1/1] A
        producer TÍPUSOSAN (nem szövegparse-szal) jelöli azokat a PSU/Load
        bejegyzéseket, amelyeknél az energia-állapot nem parancsolt vagy
        nem igazolt: commanded=OFF/actual=ON, *_STATE_POLL_FAILED, és a
        PSU/Load ERROR_QUEUE_POLL_FAILED InstrumentTimeoutError esetén
        (az állapotolvasás ekkor ugyanabban a pollban kimarad). Minden
        más bejegyzés (sima SCPI-hiba, commanded=ON/actual=OFF,
        QUESTIONABLE_STATUS*, nem-timeout poll-hiba, DMM) NEM jelölt."""
        from Prog.src.exceptions import InstrumentCommandError, InstrumentTimeoutError
        from Prog.src.instrument_manager import is_energy_state_uncertain

        def _only_entry(**kwargs):
            entries = self._make_im(**kwargs).poll_device_errors()
            assert len(entries) == 1, entries
            return entries[0]

        # --- JELÖLT esetek ---
        psu = MockPSU()
        psu._output_is_on = True
        assert is_energy_state_uncertain(_only_entry(psu=psu)) is True

        load = MockLoad()
        load._input_is_on = True
        assert is_energy_state_uncertain(_only_entry(load=load)) is True

        entry = _only_entry(psu=MockPSU(simulate_output_state_readback_timeout=True))
        assert "OUTPUT_STATE_POLL_FAILED" in entry["error"]
        assert is_energy_state_uncertain(entry) is True

        entry = _only_entry(load=MockLoad(simulate_input_state_readback_timeout=True))
        assert "INPUT_STATE_POLL_FAILED" in entry["error"]
        assert is_energy_state_uncertain(entry) is True

        entry = _only_entry(psu=MockPSU(simulate_check_error_failure=True))
        assert "ERROR_QUEUE_POLL_FAILED" in entry["error"]
        assert is_energy_state_uncertain(entry) is True

        # --- NEM jelölt esetek ---
        psu = MockPSU()
        psu.output_commanded_on = True  # commanded=ON, actual=OFF
        entry = _only_entry(psu=psu)
        assert "OUTPUT_STATE_MISMATCH" in entry["error"]
        assert is_energy_state_uncertain(entry) is False

        load = MockLoad()
        load.input_commanded_on = True  # commanded=ON, actual=OFF
        entry = _only_entry(load=load)
        assert "INPUT_STATE_MISMATCH" in entry["error"]
        assert is_energy_state_uncertain(entry) is False

        entry = _only_entry(psu=MockPSU(pending_errors=['-222,"Data out of range"']))
        assert is_energy_state_uncertain(entry) is False

        entry = _only_entry(load=MockLoad(questionable_status_active_bits=frozenset({"PS"})))
        assert "QUESTIONABLE_STATUS" in entry["error"]
        assert is_energy_state_uncertain(entry) is False

        entry = _only_entry(load=MockLoad(simulate_questionable_status_readback_timeout=True))
        assert "QUESTIONABLE_STATUS_POLL_FAILED" in entry["error"]
        assert is_energy_state_uncertain(entry) is False

        load = MockLoad()

        def _command_error_check_error(clear_first=True):
            load.calls.append("check_error()")
            raise InstrumentCommandError("simulated non-timeout SYST:ERR? failure")

        load.check_error = _command_error_check_error
        entry = _only_entry(load=load)
        assert "ERROR_QUEUE_POLL_FAILED" in entry["error"]
        assert is_energy_state_uncertain(entry) is False

        dmm_v = MockDMM()

        def _timeout_dmm_check_error():
            dmm_v.calls.append("check_error()")
            raise InstrumentTimeoutError("simulated DMM SYST:ERR? timeout")

        dmm_v.check_error = _timeout_dmm_check_error
        entry = _only_entry(dmm_v=dmm_v)
        assert entry["device"] == "DMM_V"
        assert "ERROR_QUEUE_POLL_FAILED" in entry["error"]
        assert is_energy_state_uncertain(entry) is False


class TestPollDeviceErrorsLoadQuestionableStatus:
    """Review-kör 5, Hardver/Műszervezérlés HW4-01 (P3): a Load
    STAT:QUES:COND? aktív bitjeit a MEGLÉVŐ poll_device_errors()-
    fogyasztói láncon jelzi -- olvasás-only, nem küld PROT:CLE-t, nem hív
    safety-döntést."""

    def _make_im(self, psu=None, load=None, dmm_v=None, dmm_t=None):
        return InstrumentManager(
            psu or MockPSU(),
            load or MockLoad(),
            dmm_v or MockDMM(),
            dmm_t or MockDMM(),
        )

    def test_no_active_bits_no_extra_entries(self):
        im = self._make_im()
        assert im.poll_device_errors() == []

    def test_active_bit_is_flagged(self):
        load = MockLoad(questionable_status_active_bits=frozenset({"OC"}))
        im = self._make_im(load=load)
        errors = im.poll_device_errors()
        assert len(errors) == 1
        assert errors[0]["device"] == "Load"
        assert "QUESTIONABLE_STATUS" in errors[0]["error"]
        assert "OC" in errors[0]["error"]

    def test_multiple_active_bits_listed_together(self):
        load = MockLoad(questionable_status_active_bits=frozenset({"OC", "PS"}))
        im = self._make_im(load=load)
        errors = im.poll_device_errors()
        assert len(errors) == 1
        assert "OC" in errors[0]["error"]
        assert "PS" in errors[0]["error"]

    def test_readback_failure_does_not_raise_and_surfaces_poll_failed_entry(self):
        """[regresszió, 2026-09-15, C-11/DFAM-018+DFAM-012] L. a testvér
        TestPollDeviceErrorsOutputStateMismatch azonos nevű tesztje a
        teljes indoklásért. KORÁBBAN csendben elnyelődött (errors == [])
        -- mostantól explicit QUESTIONABLE_STATUS_POLL_FAILED bejegyzés
        jelzi a poll-hibát."""
        load = MockLoad(simulate_questionable_status_readback_timeout=True)
        im = self._make_im(load=load)
        errors = im.poll_device_errors()  # nem szabad kivételt dobnia
        assert len(errors) == 1
        assert errors[0]["device"] == "Load"
        assert "QUESTIONABLE_STATUS_POLL_FAILED" in errors[0]["error"]

    def test_does_not_call_prot_cle_or_change_commanded_flag(self):
        """A diagnosztika TISZTÁN olvasás-only -- nem hívhat semmilyen
        latch-feloldó/parancsoló metódust, és nem módosíthatja
        input_commanded_on-t."""
        load = MockLoad(questionable_status_active_bits=frozenset({"PS"}))
        im = self._make_im(load=load)
        im.poll_device_errors()
        assert not load.called("input_on")
        assert not load.called("input_off")
        assert load.input_commanded_on is False

    def test_informational_bits_do_not_escalate(self):
        """[regresszió, HW-R5-A (P1), a HW4-01 (78100ab) közvetlen
        következménye] RS (négyvezetékes sense bekötésnél tartósan igaz),
        VON (Von alapszint MINimum, egy rákötött akku szinte mindig
        átlépi) és TBF (sikeres trace-capture normál végállapota) NEM
        hiba-jelzők -- ha ezek escalálnának a kritikus device-error
        csatornára, test_runner.py (üres NON_CRITICAL_SCPI_ERROR_
        SUBSTRINGS whitelist miatt) MINDEN kisütési/töltési sessiont
        hamis FAULT-tal állítana le az első poll-tickre valós hardveren."""
        for bit in ("RS", "VON", "TBF", "UNR"):
            load = MockLoad(questionable_status_active_bits=frozenset({bit}))
            im = self._make_im(load=load)
            assert im.poll_device_errors() == [], f"bit={bit} nem eszkalalhat"

    def test_protection_bits_still_escalate(self):
        """Ellenpróba: a valódi véd(elm)i/trip bitek továbbra is
        eszkalálnak -- a fix nem gyengítheti a tényleges hibadetekciót."""
        for bit in ("VF", "OC", "OP", "OT", "RRV", "LRV", "OV", "PS"):
            load = MockLoad(questionable_status_active_bits=frozenset({bit}))
            im = self._make_im(load=load)
            errors = im.poll_device_errors()
            assert len(errors) == 1, f"bit={bit} nem eszkalalt"
            assert bit in errors[0]["error"]

    def test_mixed_protection_and_informational_bits_only_lists_protection(self):
        load = MockLoad(
            questionable_status_active_bits=frozenset({"OC", "RS", "VON"})
        )
        im = self._make_im(load=load)
        errors = im.poll_device_errors()
        assert len(errors) == 1
        assert "OC" in errors[0]["error"]
        assert "RS" not in errors[0]["error"]
        assert "VON" not in errors[0]["error"]


class TestPollDeviceErrorsPerDeviceTimeoutShortCircuit:
    """[regresszió, R1 F-05, DFAM-027; B1B2_fixplan_FINAL.md B1/1 + B1/4]
    Ha egy PSU/Load lekérdezés InstrumentTimeoutError-ral bukik, az adott
    eszköz TOVÁBBI lekérdezései UGYANABBAN a pollban kimaradnak (a
    kimerült retry-budgetet nem költjük el újra minden további
    lekérdezésre). A rövidzár eszközönkénti: a többi eszköz pollja
    változatlan; nem-timeout kivételnél és DMM-nél nincs rövidzár. Ez a
    poll késleltetését csökkenti, a teljes OFF-időre nem garancia."""

    def _make_im(self, psu=None, load=None, dmm_v=None, dmm_t=None):
        return InstrumentManager(
            psu or MockPSU(),
            load or MockLoad(),
            dmm_v or MockDMM(),
            dmm_t or MockDMM(),
        )

    @staticmethod
    def _make_real_load(query_side_effect):
        from unittest.mock import MagicMock

        from Prog.drivers.device_load import Keithley2380Load

        load = Keithley2380Load()
        res = MagicMock()
        res.query.side_effect = query_side_effect
        load._resource = res
        load._connected = True
        load._input_cmd = "INP"
        return load, res

    def test_load_check_error_timeout_skips_load_readbacks_in_same_poll(self):
        from Prog.src.exceptions import InstrumentTimeoutError

        load = MockLoad()

        def _timeout_check_error(clear_first=True):
            load.calls.append("check_error()")
            raise InstrumentTimeoutError("simulated SYST:ERR? timeout")

        load.check_error = _timeout_check_error
        psu = MockPSU()
        dmm_v = MockDMM()
        dmm_t = MockDMM()
        im = self._make_im(psu=psu, load=load, dmm_v=dmm_v, dmm_t=dmm_t)

        errors = im.poll_device_errors()

        assert not load.called("read_input_state()")
        assert not load.called("read_questionable_status()")
        load_entries = [e for e in errors if e["device"] == "Load"]
        assert len(load_entries) == 1
        assert "ERROR_QUEUE_POLL_FAILED" in load_entries[0]["error"]
        from Prog.src.instrument_manager import is_energy_state_uncertain
        assert is_energy_state_uncertain(load_entries[0]) is True
        # a rövidzár eszközönkénti: a PSU és a DMM-ek pollja változatlan
        assert psu.called("read_output_state()")
        assert dmm_v.called("check_error()")
        assert dmm_t.called("check_error()")

    def test_real_load_driver_timeout_consumes_single_retry_budget_per_poll(self):
        """Valós Keithley2380Load driver: minden VISA-query timeoutol. A
        régi kód 3 lekérdezést (SYST:ERR?, INP?, STAT:QUES:COND?) ×
        max_retries kísérletet = 9 VISA-hívást és 3 Load-bejegyzést adott."""
        load, res = self._make_real_load(Exception("VI_ERROR_TMO"))
        im = self._make_im(load=load)

        errors = im.poll_device_errors()

        assert res.query.call_count == load.max_retries
        load_entries = [e for e in errors if e["device"] == "Load"]
        assert len(load_entries) == 1
        assert "ERROR_QUEUE_POLL_FAILED" in load_entries[0]["error"]

    def test_real_load_input_state_timeout_skips_questionable_status_in_same_poll(self):
        """FINAL B1/4 (kimaradt sibling): sikeres check_error() után az
        INP? InstrumentTimeoutError-ja is rövidre zárja ugyanabban a
        pollban a STAT:QUES:COND? lekérdezést."""

        def _query(cmd):
            if cmd == "SYST:ERR?":
                return '+0,"No error"'
            if cmd == "INP?":
                raise OSError("VI_ERROR_TMO")
            return "0"

        load, res = self._make_real_load(_query)
        im = self._make_im(load=load)

        errors = im.poll_device_errors()

        sent = [c.args[0] for c in res.query.call_args_list]
        assert "STAT:QUES:COND?" not in sent
        assert res.query.call_count == 1 + load.max_retries
        assert res.query.call_count <= 2 * load.max_retries
        load_entries = [e for e in errors if e["device"] == "Load"]
        assert len(load_entries) == 1
        assert "INPUT_STATE_POLL_FAILED" in load_entries[0]["error"]
        from Prog.src.instrument_manager import is_energy_state_uncertain
        assert is_energy_state_uncertain(load_entries[0]) is True

    def test_psu_check_error_timeout_skips_psu_readback(self):
        psu = MockPSU(simulate_check_error_failure=True)
        load = MockLoad()
        im = self._make_im(psu=psu, load=load)

        errors = im.poll_device_errors()

        assert not psu.called("read_output_state()")
        psu_entries = [e for e in errors if e["device"] == "PSU"]
        assert len(psu_entries) == 1
        assert "ERROR_QUEUE_POLL_FAILED" in psu_entries[0]["error"]
        from Prog.src.instrument_manager import is_energy_state_uncertain
        assert is_energy_state_uncertain(psu_entries[0]) is True
        # a Load pollja változatlan
        assert load.called("read_input_state()")
        assert load.called("read_questionable_status()")

    def test_non_timeout_check_error_exception_does_not_short_circuit(self):
        """Negatív kontroll (a régi kódon is zöld): nem-timeout kivétel
        (InstrumentCommandError) esetén a teljes readback-sor lefut."""
        from Prog.src.exceptions import InstrumentCommandError

        load = MockLoad()

        def _command_error_check_error(clear_first=True):
            load.calls.append("check_error()")
            raise InstrumentCommandError("simulated non-timeout SYST:ERR? failure")

        load.check_error = _command_error_check_error
        im = self._make_im(load=load)

        im.poll_device_errors()

        assert load.called("read_input_state()")
        assert load.called("read_questionable_status()")

    def test_non_timeout_input_state_exception_does_not_skip_questionable_status(self):
        """Negatív kontroll (B1 Claude MAX verifier V-1): nem-timeout INP?-hiba
        (InstrumentInvalidReading) esetén a QUES-lekérdezés lefut; a
        bejegyzés ettől még energia-állapot-bizonytalan."""
        from Prog.src.exceptions import InstrumentInvalidReading
        from Prog.src.instrument_manager import is_energy_state_uncertain

        load = MockLoad()

        def _invalid_input_state():
            load.calls.append("read_input_state()")
            raise InstrumentInvalidReading("simulated unparsable INP? response")

        load.read_input_state = _invalid_input_state
        im = self._make_im(load=load)

        errors = im.poll_device_errors()

        assert load.called("read_questionable_status()")
        load_entries = [
            e for e in errors
            if e["device"] == "Load" and "INPUT_STATE_POLL_FAILED" in e["error"]
        ]
        assert len(load_entries) == 1
        assert is_energy_state_uncertain(load_entries[0]) is True

    def test_psu_output_state_timeout_does_not_skip_load_readbacks(self):
        """Negatív kontroll (a régi kódon is zöld): a PSU OUTP?-timeoutja
        csak a PSU-t érinti, a Load pollja változatlan."""
        load = MockLoad()
        im = self._make_im(
            psu=MockPSU(simulate_output_state_readback_timeout=True), load=load
        )

        im.poll_device_errors()

        assert load.called("read_input_state()")
        assert load.called("read_questionable_status()")

    def test_dmm_check_error_timeout_does_not_skip_psu_load_readbacks(self):
        """Negatív kontroll (a régi kódon is zöld): DMM-re a rövidzár nem
        vonatkozik, és a PSU/Load pollját sem érinti."""
        from Prog.src.exceptions import InstrumentTimeoutError

        dmm_v = MockDMM()

        def _timeout_dmm_check_error():
            dmm_v.calls.append("check_error()")
            raise InstrumentTimeoutError("simulated DMM SYST:ERR? timeout")

        dmm_v.check_error = _timeout_dmm_check_error
        psu = MockPSU()
        load = MockLoad()
        dmm_t = MockDMM()
        im = self._make_im(psu=psu, load=load, dmm_v=dmm_v, dmm_t=dmm_t)

        im.poll_device_errors()

        assert psu.called("read_output_state()")
        assert load.called("read_input_state()")
        assert load.called("read_questionable_status()")
        assert dmm_t.called("check_error()")


class _VisaLikeOpenError(Exception):
    """R6-B1 verifier N-1: a pyvisa VisaIOError MRO-ja Error -> Exception, NEM
    OSError (és nem InstrumentError) -- az open_failure ág ilyennel dobjon,
    különben egy `except (InstrumentError, OSError)`-ra szűkített connect_all()
    mutáns (amely a kikapcsolt/nem található PSU-nál újra kihagyná a Load OFF-ot)
    túlélné a tesztet."""


class TestR6B1ConnectAllAttemptsBothEnergyInstruments:
    """[regresszio, Claude-Codex loop R6 B1: R5-B1-N3, DFAM-016 x DFAM-050]
    A PSU vagy a Load connect()-hibaja utan a MASIK energiamuszer
    connect()-je is megkiserlodik (a sajat OFF-ja igy lefut), a DMM-ek
    connect()-je kimarad, a rollback minden megkiserelt muszeren
    egymastol fuggetlenul safe_off() + disconnect(), es az ELSO kivetel
    propagal. Hibamentes uton a sorrend valtozatlan. Valodi driverek
    allapottarto VISA-double-lel (DFAM-005), a Load bemenete ON-bol indul.
    Unit/mock szint; fizikai OFF-ot NEM bizonyit (HIL nelkul)."""

    _CFG = InstrumentConfig(
        psu_resource="MOCK::PSU", load_resource="MOCK::LOAD",
        dmm_voltage_resource="MOCK::DMM_V", dmm_temperature_resource="MOCK::DMM_T",
    )

    @staticmethod
    def _install_rm(monkeypatch, routes):
        import sys
        import types
        from unittest.mock import MagicMock

        def _open(resource, *a, **k):
            target = routes[resource]
            if isinstance(target, BaseException):
                raise target
            return target

        monkeypatch.setitem(
            sys.modules, "pyvisa",
            types.SimpleNamespace(ResourceManager=lambda: MagicMock(open_resource=_open)),
        )

    @staticmethod
    def _load_energizing(res):
        return [m for _k, m in res.log if "INP ON" in m.upper() or "INP 1" in m.upper()]

    @pytest.mark.parametrize("psu_case", ["off_timeout", "open_failure"])
    def test_psu_failure_still_turns_inherited_load_on_off(self, monkeypatch, psu_case):
        from unittest.mock import MagicMock

        from Prog.drivers.device_load import Keithley2380Load
        from Prog.drivers.device_psu import Keithley2220PSU
        from Prog.src.exceptions import InstrumentTimeoutError
        from Prog.tests.test_device_load import _StickyLoadResource
        from Prog.tests.test_device_psu import _energizing, _StatefulConnect2220Resource

        open_exc = _VisaLikeOpenError("VI_ERROR_RSRC_NFOUND")
        if psu_case == "off_timeout":
            psu_res = _StatefulConnect2220Resource()
            psu_res.fail_before_execute = lambda msg: (
                msg == "OUTP OFF;*OPC?" and psu_res.messages.count(msg) <= 3
            )
            exc = InstrumentTimeoutError
        else:
            psu_res, exc = open_exc, _VisaLikeOpenError
        load_res = _StickyLoadResource(inp=True, short=True, timer=True)
        self._install_rm(monkeypatch, {"MOCK::PSU": psu_res, "MOCK::LOAD": load_res})
        psu, load = Keithley2220PSU(), Keithley2380Load()
        dmm_v, dmm_t = MagicMock(name="dmm_v"), MagicMock(name="dmm_t")
        im = InstrumentManager(psu, load, dmm_v, dmm_t)

        with pytest.raises(exc) as excinfo:
            im.connect_all(self._CFG)

        if psu_case == "open_failure":
            assert excinfo.value is open_exc
        else:
            assert psu_res.output_state is False and psu_res.energized == set()
            assert psu_res.closed is True
            assert _energizing(psu_res.messages) == []
        # Load: a sajat connect()-beli INP OFF + a rollback safe_off() INP OFF.
        assert load_res.msgs("Q").count("INP OFF;*OPC?") == 2
        assert load_res.state == {"INP": False, "INP:SHOR": False, "INP:TIM": False}
        assert load_res.closed is True
        assert self._load_energizing(load_res) == []
        assert load.is_connected() is False and load.input_commanded_on is False
        assert psu.is_connected() is False and psu.output_commanded_on is False
        # DMM-ek: connect() kimarad, rollback sem erinti oket.
        for dmm in (dmm_v, dmm_t):
            dmm.connect.assert_not_called()
            dmm.safe_off.assert_not_called()
            dmm.disconnect.assert_not_called()

    def test_load_failure_control_psu_off_still_sent(self, monkeypatch):
        """Kontroll (Load INP:TIM readback 1): a PSU->Load sorrend miatt a
        regi kod is eleri a PSU-t -- nem diszkriminalo, csak kontroll."""
        from unittest.mock import MagicMock

        from Prog.drivers.device_load import Keithley2380Load
        from Prog.drivers.device_psu import Keithley2220PSU
        from Prog.src.exceptions import InstrumentCommandError
        from Prog.tests.test_device_load import _StickyLoadResource
        from Prog.tests.test_device_psu import _energizing, _StatefulConnect2220Resource

        psu_res = _StatefulConnect2220Resource()
        load_res = _StickyLoadResource(replies={"INP:TIM?": "1"})
        self._install_rm(monkeypatch, {"MOCK::PSU": psu_res, "MOCK::LOAD": load_res})
        dmm_v, dmm_t = MagicMock(name="dmm_v"), MagicMock(name="dmm_t")
        im = InstrumentManager(Keithley2220PSU(), Keithley2380Load(), dmm_v, dmm_t)

        with pytest.raises(InstrumentCommandError):
            im.connect_all(self._CFG)

        assert psu_res.messages.count("OUTP OFF;*OPC?") == 2
        assert psu_res.output_state is False and psu_res.closed is True
        assert _energizing(psu_res.messages) == []
        assert load_res.state["INP"] is False and load_res.closed is True
        dmm_v.connect.assert_not_called()
        dmm_t.connect.assert_not_called()

    def test_both_energy_instruments_fail_first_exception_propagates(self, monkeypatch):
        from unittest.mock import MagicMock

        from Prog.drivers.device_load import Keithley2380Load
        from Prog.drivers.device_psu import Keithley2220PSU
        from Prog.src.exceptions import InstrumentTimeoutError
        from Prog.tests.test_device_load import _StickyLoadResource
        from Prog.tests.test_device_psu import _StatefulConnect2220Resource

        psu_res = _StatefulConnect2220Resource()
        psu_res.fail_before_execute = lambda msg: (
            msg == "OUTP OFF;*OPC?" and psu_res.messages.count(msg) <= 3
        )
        load_res = _StickyLoadResource(reject={"INP:SHOR OFF"})
        self._install_rm(monkeypatch, {"MOCK::PSU": psu_res, "MOCK::LOAD": load_res})
        im = InstrumentManager(
            Keithley2220PSU(), Keithley2380Load(), MagicMock(name="dmm_v"), MagicMock(name="dmm_t"),
        )

        with pytest.raises(InstrumentTimeoutError) as excinfo:
            im.connect_all(self._CFG)

        assert "OUTP OFF;*OPC?" in str(excinfo.value)
        assert psu_res.messages.count("OUTP OFF;*OPC?") == 4
        assert load_res.msgs("Q").count("INP OFF;*OPC?") == 2
        assert psu_res.output_state is False and load_res.state["INP"] is False

    def test_first_exception_identity_and_rollback_isolation(self):
        """Mock-szint: az ELSO kivetel (objektum-azonossag) propagal; a
        rollback sorrendje Load safe_off, Load disconnect, PSU safe_off,
        PSU disconnect; egy safe_off/disconnect-hiba nem akadalyozza a
        tobbit; a DMM-ekhez semmi nem megy."""
        from unittest.mock import MagicMock

        parent = MagicMock()
        psu, load, dmm_v, dmm_t = parent.psu, parent.load, parent.dmm_v, parent.dmm_t
        e_psu, e_load = InstrumentError("psu gate"), InstrumentError("load gate")
        psu.connect.side_effect = e_psu
        load.connect.side_effect = e_load
        load.safe_off.side_effect = RuntimeError("load off failed")
        load.disconnect.side_effect = RuntimeError("load close failed")
        psu.safe_off.side_effect = RuntimeError("psu off failed")
        im = InstrumentManager(psu, load, dmm_v, dmm_t)

        with pytest.raises(InstrumentError) as excinfo:
            im.connect_all(self._CFG)

        assert excinfo.value is e_psu
        # R6-B1 Codex verifier: a második (Load-) kivétel nem fűződhet hamis
        # __context__-ként az elsőhöz (except-ágon belüli újradobás mutánsa).
        assert excinfo.value.__context__ is None
        assert [c[0] for c in parent.mock_calls] == [
            "psu.connect", "load.connect",
            "load.safe_off", "load.disconnect", "psu.safe_off", "psu.disconnect",
        ]

    def test_load_only_failure_first_exception_is_load(self):
        from unittest.mock import MagicMock

        parent = MagicMock()
        e_load = InstrumentError("load gate")
        parent.load.connect.side_effect = e_load
        im = InstrumentManager(parent.psu, parent.load, parent.dmm_v, parent.dmm_t)
        with pytest.raises(InstrumentError) as excinfo:
            im.connect_all(self._CFG)
        assert excinfo.value is e_load
        assert [c[0] for c in parent.mock_calls] == [
            "psu.connect", "load.connect",
            "load.safe_off", "load.disconnect", "psu.safe_off", "psu.disconnect",
        ]

    def test_success_path_order_unchanged(self):
        from unittest.mock import MagicMock

        parent = MagicMock()
        im = InstrumentManager(parent.psu, parent.load, parent.dmm_v, parent.dmm_t)
        im.connect_all(self._CFG)
        assert parent.mock_calls == [
            ("psu.connect", ("MOCK::PSU",), {}),
            ("load.connect", ("MOCK::LOAD",), {}),
            ("dmm_v.connect", ("MOCK::DMM_V",), {}),
            ("dmm_t.connect", ("MOCK::DMM_T",), {}),
        ]

    def test_dmm_failure_rollback_unchanged(self):
        from unittest.mock import MagicMock

        parent = MagicMock()
        e_dmm = InstrumentError("dmm_v down")
        parent.dmm_v.connect.side_effect = e_dmm
        im = InstrumentManager(parent.psu, parent.load, parent.dmm_v, parent.dmm_t)
        with pytest.raises(InstrumentError) as excinfo:
            im.connect_all(self._CFG)
        assert excinfo.value is e_dmm
        assert [c[0] for c in parent.mock_calls] == [
            "psu.connect", "load.connect", "dmm_v.connect",
            "dmm_v.safe_off", "dmm_v.disconnect", "load.safe_off", "load.disconnect",
            "psu.safe_off", "psu.disconnect",
        ]

    def test_base_exception_not_converted(self):
        """KEEP_AS_IS: BaseException (pl. KeyboardInterrupt) a connect()-bol
        valtozatlanul, rollback nelkul propagal, mint HEAD-en."""
        from unittest.mock import MagicMock

        parent = MagicMock()
        parent.psu.connect.side_effect = KeyboardInterrupt()
        im = InstrumentManager(parent.psu, parent.load, parent.dmm_v, parent.dmm_t)
        with pytest.raises(KeyboardInterrupt):
            im.connect_all(self._CFG)
        assert [c[0] for c in parent.mock_calls] == ["psu.connect"]


class TestR16B5aDeferredDmmPoll:
    @staticmethod
    def _manager():
        from unittest.mock import MagicMock

        from Prog.drivers.device_dmm import Keysight34465ADMM

        dmm = Keysight34465ADMM()
        resource = MagicMock()
        resource.query.return_value = '+0,"No error"'
        dmm._resource = resource
        dmm._connected = True
        manager = InstrumentManager(MockPSU(), MockLoad(), dmm, MockDMM())
        return manager, dmm, resource

    def test_deferred_queue_is_not_empty_success_and_is_drained_after_release(self):
        from Prog.src.instrument_manager import is_error_queue_poll_deferred

        manager, dmm, resource = self._manager()
        pending = ['-200,"Execution error"', '+0,"No error"']
        resource.query.side_effect = lambda command: pending.pop(0)
        owner = object()
        dmm.acquire_measurement(owner)
        for _ in range(2):
            result = manager.poll_device_errors()
            assert result == [{
                "device": "DMM_V", "error": "ERROR_QUEUE_POLL_DEFERRED", "status": "HALASZTVA",
            }]
            assert is_error_queue_poll_deferred(result[0])
        assert len(pending) == 2
        resource.query.assert_not_called()
        assert manager.dmm_temperature.called("check_error")
        dmm.release_measurement(owner)
        result = manager.poll_device_errors()
        assert len(result) == 1 and result[0]["device"] == "DMM_V"
        assert result[0]["error"].startswith('-200,"Execution error"')
        assert not is_error_queue_poll_deferred(result[0])
        assert pending == []
        resource.query.side_effect = None
        resource.query.return_value = '+0,"No error"'
        assert manager.poll_device_errors() == []

    def test_read_all_defers_owned_channel_with_explicit_quality(self):
        # B5b: a B5a átmeneti raise-szerződését explicit kihagyás váltja fel.
        manager, dmm, resource = self._manager()
        owner = object()
        dmm.acquire_measurement(owner)
        readings = manager.read_all()
        assert readings["battery_voltage_V"] is None
        assert readings["battery_voltage_V_skipped"] is True
        assert readings["battery_voltage_V_quality"] == "OWNED_DEFERRED"
        assert readings["battery_temperature_C"] is not None
        assert readings["load_readback_voltage_V"] is not None
        resource.query.assert_not_called()
        dmm.release_measurement(owner)

    def test_disconnect_all_reports_busy_and_still_closes_other_devices(self):
        from Prog.drivers.device_dmm import DMMMeasurementBusy

        manager, dmm, resource = self._manager()
        owner = object()
        dmm.acquire_measurement(owner)
        with pytest.raises(DMMMeasurementBusy):
            manager.disconnect_all()
        assert manager.psu.called("disconnect")
        assert manager.load.called("disconnect")
        assert manager.dmm_temperature.called("disconnect")
        assert dmm.is_connected()
        resource.close.assert_not_called()
        dmm.release_measurement(owner)
        manager.disconnect_all()
        assert not dmm.is_connected()
        resource.close.assert_called_once()

    def test_real_poll_failure_remains_an_error(self):
        manager, _, resource = self._manager()
        resource.query.side_effect = OSError("mock communication failure")
        result = manager.poll_device_errors()
        assert len(result) == 1
        assert result[0]["device"] == "DMM_V"
        assert result[0]["error"].startswith("ERROR_QUEUE_POLL_FAILED:")
        assert "status" not in result[0]
