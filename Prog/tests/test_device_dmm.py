"""
Keysight34465ADMM unit tesztek — pyvisa resource mock-kal.
"""
import pytest
from unittest.mock import MagicMock
from Prog.drivers.device_dmm import Keysight34465ADMM
from Prog.src.exceptions import InstrumentTimeoutError, InstrumentInvalidReading


def make_dmm() -> tuple[Keysight34465ADMM, MagicMock]:
    dmm = Keysight34465ADMM()
    res = MagicMock()
    res.query.return_value = '+0,"No error"'
    dmm._resource = res
    dmm._connected = True
    return dmm, res


def writes(res: MagicMock) -> list[str]:
    """A műszerhez KIKÜLDÖTT SCPI parancsok — írásként ÉS lekérdezésként is.

    2026-09-12, INSTR-DMM-CONFIG-ATOMIC-001: a konfigurációs metódusok
    (`configure_dcv`, `configure_timed_acquisition`, `reset_to_single_sample`,
    `configure_temp_*`) azóta EGYETLEN, `*OPC?`-vel szinkronizált program
    message-ként küldik ki a parancsaikat, tehát `resource.query()`-n át, nem
    `resource.write()`-on (l. device_dmm.py::_apply_config_atomic()). A
    meglévő tesztek SZEMANTIKÁJA (melyik SCPI parancs ment ki) változatlan;
    csak azt kellett kiterjeszteni, HOL keressük a kiküldött parancsot.
    A tesztek szándékosan nem az egyetlen-üzenet FORMÁTUMÁRA állnak rá —
    azt a `TestAtomicConfigurationSequences` osztály ellenőrzi külön —, ezért
    a compound program message-eket itt vissza is bontjuk az EGYEDI SCPI
    parancsaikra, hogy a meglévő assertek (startswith/membership) pontosan a
    korábbi erősségükkel maradjanak érvényben."""
    out: list[str] = []
    for msg in only_writes(res) + queries(res):
        for part in msg.split(";"):
            part = part.lstrip(":").strip()
            if part and part != "*OPC?":
                out.append(part)
    return out


def only_writes(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.write.call_args_list]


def queries(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.query.call_args_list]


def io_order(res: MagicMock) -> list[tuple[str, str]]:
    """A `resource` felé történt hívások IDŐRENDBEN, típussal együtt:
    ("clear", ""), ("write", cmd) vagy ("query", cmd). A Device-Clear-nek a
    parancsokhoz viszonyított POZÍCIÓJA a vizsgálat tárgya (l.
    TestAtomicConfigurationSequences)."""
    events: list[tuple[str, str]] = []
    res.clear.side_effect = lambda: events.append(("clear", ""))
    res.write.side_effect = lambda c: events.append(("write", c))

    def _query(c: str) -> str:
        events.append(("query", c))
        return '+0,"No error"'

    res.query.side_effect = _query
    return events


class TestDcvConfiguration:
    def test_configure_dcv_sends_conf_volt_dc(self):
        dmm, res = make_dmm()
        dmm.configure_dcv(range_V=100, nplc=10)
        assert any("CONF:VOLT:DC" in c or "VOLT:DC" in c for c in writes(res))

    @pytest.mark.parametrize("nplc", [1, 10, 100])
    def test_configure_dcv_sets_nplc(self, nplc):
        # R13-B1: pontos parancselem, eltero NPLC-bemenetekkel.
        dmm, res = make_dmm()
        dmm.configure_dcv(range_V=100, nplc=nplc)
        assert f"VOLT:DC:NPLC {nplc}" in writes(res)

    def test_configure_dcv_disables_autorange(self):
        dmm, res = make_dmm()
        dmm.configure_dcv(range_V=100, nplc=10)
        cmds = writes(res)
        assert any("AUTO" in c or "RANG" in c for c in cmds)

    @pytest.mark.parametrize("nplc", [1, 10, 100])
    def test_set_nplc_sends_nplc_command(self, nplc):
        dmm, res = make_dmm()
        dmm.set_nplc(nplc)
        assert f"VOLT:DC:NPLC {nplc}" in writes(res)

    def test_get_nplc_queries_instrument(self):
        """2026-09-12, FAST_TRANSIENT NPLC-javítás: `[SENSe:]VOLTage[:DC]:
        NPLC?` (34460-70-Manual.txt 11421. sor, nyomtatott 382. o.) — az
        ideiglenes NPLC-váltás csak a ténylegesen ÉRVÉNYES értékre
        állítható vissza, nem egy beégetett konstansra."""
        dmm, res = make_dmm()
        res.query.return_value = "+1.00000000E+01"
        assert dmm.get_nplc() == pytest.approx(10.0)
        assert any("VOLT:DC:NPLC?" in q for q in queries(res))

    def test_get_nplc_rejects_unparseable_response(self):
        dmm, res = make_dmm()
        res.query.return_value = "NOT_A_NUMBER"
        with pytest.raises(InstrumentInvalidReading):
            dmm.get_nplc()

    def test_set_nplc_then_get_nplc_uses_low_integration_time(self):
        """A FAST_TRANSIENT réteg ténylegesen elérhető legkisebb NPLC-je a
        34465A MINDEN változatában 0,02 (uo. 11428-11433. sor) — 50 Hz-en
        0,4 ms apertúra (uo. 14028-14052. sor), tehát a 0,005 s-os
        mintavételi köz fizikailag lehetséges."""
        dmm, res = make_dmm()
        dmm.set_nplc(0.02)
        assert "VOLT:DC:NPLC 0.02" in writes(res)

    def test_set_nplc_is_one_synchronized_query_not_a_bare_write(self):
        """[regresszió, Kör 19 Batch D2, HW19-01/DFAM-039] `set_nplc()`
        korábban egyetlen, szinkronizálatlan `_write()` volt -- kitéve
        annak a Device-Clear-mechanizmusnak, amit a DFAM-039 család EZEN a
        műszeren a TÜNET-OSZTÁLY szintjén HIL-igazolt (a pontos DCL-
        mechanizmus önmagában SOURCE_REQUIRED, l. device_dmm.py::
        _apply_config_atomic() docstringje): egy fel nem dolgozott írás a
        KÖVETKEZŐ hívás Device Clear-je alatt csendben elveszhet. Most
        `_query(...;*OPC?)`-t használ -- egyetlen kimenő SCPI-üzenetként,
        nem `resource.write()`-on, hanem `resource.query()`-n át."""
        dmm, res = make_dmm()
        events = io_order(res)
        dmm.set_nplc(10)
        cmds = [c for kind, c in events if kind in ("write", "query")]
        assert cmds == ["VOLT:DC:NPLC 10;*OPC?"], (
            f"nem egyetlen szinkronizált program message: {cmds!r}"
        )
        assert not any(kind == "write" for kind, _ in events), (
            "set_nplc() résznek query()-n kell mennie, nem bare write()-on"
        )


class TestVoltageReading:
    def test_read_voltage_returns_float(self):
        dmm, res = make_dmm()
        res.query.return_value = "+1.24500000E+001"
        result = dmm.read_voltage()
        assert isinstance(result, float)
        assert abs(result - 12.45) < 0.001

    def test_read_voltage_sends_read_query(self):
        dmm, res = make_dmm()
        res.query.return_value = "+1.20000000E+001"
        dmm.read_voltage()
        assert any("READ" in q for q in queries(res))

    def test_read_voltage_tracks_last_valid(self):
        """Első mérés eltárolódik a jump-detection-hoz."""
        dmm, res = make_dmm()
        res.query.return_value = "+1.20000000E+001"
        dmm.read_voltage()
        assert dmm._last_valid_voltage is not None


class TestSup01TimedAcquisition:
    """SUP01-PULSE-003/SRC-009: determinisztikus, időzített mintavétel —
    additív driver building block, a kézikönyvben igazolt SCPI szintaxis
    (34460-70-Manual.txt 8959-9009. sor). Nincs OcvSocController-be bekötve
    — HIL validálás szükséges bekötés előtt."""

    def test_configure_timed_acquisition_sends_expected_scpi(self):
        dmm, res = make_dmm()
        dmm.configure_timed_acquisition(sample_count=30, interval_s=1.0)
        cmds = writes(res)
        assert "SAMP:COUN 30" in cmds
        assert any("SAMP:SOUR" in c and "TIM" in c for c in cmds)
        assert "SAMP:TIM 1" in cmds

    def test_arm_timed_acquisition_sends_init(self):
        dmm, res = make_dmm()
        dmm.arm_timed_acquisition()
        assert any(c.strip() == "INIT" for c in writes(res))

    def test_fetch_timed_acquisition_queries_fetc(self):
        dmm, res = make_dmm()
        res.query.return_value = "12.50,12.49,12.48"
        result = dmm.fetch_timed_acquisition()
        assert "FETC?" in queries(res)
        assert result == "12.50,12.49,12.48"

    def test_query_sample_timer_capability(self):
        dmm, res = make_dmm()
        res.query.return_value = "1.0E-04"
        dmm.query_sample_timer_capability()
        assert any("SAMP:TIM?" in q and "MIN" in q for q in queries(res))

    def test_reset_to_single_sample_restores_defaults(self):
        """WP-AK-04 review-lelet: a configure_timed_acquisition() TARTÓS
        (sticky) SAMP:COUN/SAMP:SOUR állapotát vissza kell állítani, különben
        minden ezt követő sima READ? többértékű CSV-t adna vissza."""
        dmm, res = make_dmm()
        dmm.reset_to_single_sample()
        cmds = writes(res)
        assert "SAMP:COUN 1" in cmds
        assert any("SAMP:SOUR" in c and "IMM" in c for c in cmds)


class TestAtomicConfigurationSequences:
    """2026-09-12, INSTR-DMM-ABORT-001 + INSTR-DMM-CONFIG-ATOMIC-001 —
    DISZKRIMINÁLÓ regressziós tesztek a 2026-09-12-i élő HIL-megfigyelésekre
    (Keysight 34465A MY60016569).

    Mindegyik teszt a JAVÍTÁS ELŐTTI implementáción BUKNA:
    - a régi `reset_to_single_sample()` KÉT külön `_write()`-ot adott ki
      (`SAMP:COUN 1`, `SAMP:SOUR IMM`) `ABORt` NÉLKÜL, és a `_write()`
      minden kísérlet előtt Device Clear-t küld -> a második írás
      Device Clear-je eldobhatta az első, még fel nem dolgozott parancsot
      (34460-70-Manual.txt 5391-5395. sor: "The instrument's input and
      output buffers are cleared"). Élő megfigyelés: utána `SAMP:SOUR? ->
      IMM` (2. írás érvényesült), de `SAMP:COUN? -> +2` (1. NEM).
    - `ABORt` nélkül egy függőben lévő (INIT-tel armed, FETC? nélkül maradt)
      mérést semmi nem zárt le (uo. 5527. sor: "Use ABORt to return to
      idle")."""

    def test_reset_to_single_sample_aborts_before_sample_writes(self):
        dmm, res = make_dmm()
        events = io_order(res)
        dmm.reset_to_single_sample()
        cmds = [c for kind, c in events if kind in ("write", "query")]
        joined = " ".join(cmds)
        assert "ABOR" in joined, "hiányzik az ABORt — függőben lévő INIT nem zárul le"
        assert joined.index("ABOR") < joined.index("SAMP:COUN"), (
            "az ABORt-nak a SAMPle-írások ELŐTT kell mennie"
        )

    def test_reset_to_single_sample_has_no_device_clear_between_commands(self):
        dmm, res = make_dmm()
        events = io_order(res)
        dmm.reset_to_single_sample()
        kinds = [kind for kind, _ in events]
        first_cmd = min(
            i for i, k in enumerate(kinds) if k in ("write", "query")
        )
        assert "clear" not in kinds[first_cmd:], (
            "Device Clear a konfigurációs parancsok KÖZÖTT/UTÁN — eldobhatja a "
            "műszer input pufferében még fel nem dolgozott parancsot"
        )

    def test_configure_timed_acquisition_is_one_synchronized_message(self):
        dmm, res = make_dmm()
        events = io_order(res)
        dmm.configure_timed_acquisition(sample_count=3, interval_s=0.41)
        cmds = [c for kind, c in events if kind in ("write", "query")]
        assert len(cmds) == 1, f"nem egyetlen program message: {cmds!r}"
        msg = cmds[0]
        assert msg.startswith("ABOR")
        assert msg.endswith("*OPC?"), (
            "*OPC? nélkül egy KÉSŐBBI hívás (pl. check_error() SYST:ERR?-je) "
            "Device Clear-je eldobhatja a még fel nem dolgozott SAMP:TIM írást "
            "— ez a 2026-09-12-i 2. futás mintázata (kért 0,41s / olvasott 1,0s)"
        )
        assert msg.index("SAMP:COUN") < msg.index("SAMP:SOUR") < msg.index("SAMP:TIM")

    def test_configure_dcv_defines_trigger_and_sample_mode_explicitly(self):
        """A CONFigure dokumentált mellékhatására (Samples per Trigger = 1)
        támaszkodni NEM elég: a 3. HIL-futásban a `configure_dcv()` UTÁNI
        sima READ? KÉT értéket adott vissza."""
        dmm, res = make_dmm()
        events = io_order(res)
        dmm.configure_dcv(range_V=100, nplc=10)
        cmds = [c for kind, c in events if kind in ("write", "query")]
        assert len(cmds) == 1
        msg = cmds[0]
        parts = writes(res)
        for expected in (
            "ABOR", "CONF:VOLT:DC", "VOLT:DC:NPLC", "VOLT:DC:RANG:AUTO OFF",
            "TRIG:SOUR IMM", "TRIG:COUN 1", "SAMP:COUN 1", "SAMP:SOUR IMM",
        ):
            if expected in ("TRIG:COUN 1", "SAMP:COUN 1"):
                assert expected in parts, f"hiányzó explicit üzemmód-beállítás: {expected}"
            else:
                assert expected in msg, f"hiányzó explicit üzemmód-beállítás: {expected}"

    def test_abort_measurement_sends_abor(self):
        dmm, res = make_dmm()
        dmm.abort_measurement()
        assert any(c.strip() == "ABOR" for c in only_writes(res))

    def test_disconnect_aborts_pending_measurement(self):
        """Egy processz ELHAGYÁSAKOR se maradjon armed a műszer — különben a
        KÖVETKEZŐ futás első READ?-je az előző SAMP:COUN szerinti CSV-t adja
        (2026-09-12, 3. futás)."""
        dmm, res = make_dmm()
        dmm.disconnect()
        assert any("ABOR" in c for c in only_writes(res))
        res.close.assert_called_once()

    def test_disconnect_closes_even_if_abort_fails(self):
        dmm, res = make_dmm()
        res.write.side_effect = RuntimeError("link down")
        dmm.disconnect()
        res.close.assert_called_once()
        assert dmm.is_connected() is False

    def test_configure_temp_4wire_is_one_message(self):
        """Sibling-felület (DFAM: config_write_lost_to_device_clear): a
        `SENS:TEMP:TRAN:FRTD:RES 100` csendes elvesztése metrológiailag
        közvetlenül hibás °C-értéket adna."""
        dmm, res = make_dmm()
        events = io_order(res)
        dmm.configure_temp_4wire_pt100(nplc=10)
        cmds = [c for kind, c in events if kind in ("write", "query")]
        assert len(cmds) == 1
        assert "SENS:TEMP:TRAN:FRTD:RES 100" in writes(res)
        assert cmds[0].endswith("*OPC?")

    def test_configure_thermistor_is_one_message(self):
        dmm, res = make_dmm()
        events = io_order(res)
        dmm.configure_temp_2wire_ntc_thermistor()
        cmds = [c for kind, c in events if kind in ("write", "query")]
        assert len(cmds) == 1
        assert "RES:RANG:AUTO ON" in cmds[0]


class TestVoltageValidation:
    """[N8] NaN / Inf / overload validáció driver szinten."""

    def test_nan_raises_invalid_reading(self):
        dmm, res = make_dmm()
        res.query.return_value = "NAN"
        with pytest.raises(InstrumentInvalidReading):
            dmm.read_voltage()

    def test_inf_raises_invalid_reading(self):
        dmm, res = make_dmm()
        res.query.return_value = "INF"
        with pytest.raises(InstrumentInvalidReading):
            dmm.read_voltage()

    def test_overload_9e37_raises_invalid_reading(self):
        """SCPI overload kód: 9.9E37 — float-ként rendkívül nagy."""
        dmm, res = make_dmm()
        res.query.return_value = "+9.90000000E+037"
        with pytest.raises(InstrumentInvalidReading):
            dmm.read_voltage()

    def test_large_voltage_jump_raises_invalid_reading(self):
        """Pillanatnyi 3V-os ugrás → kontakthiba gyanú."""
        dmm, res = make_dmm()
        res.query.return_value = "+1.20000000E+001"
        dmm.read_voltage()  # first: 12.0V

        res.query.return_value = "+9.00000000E+000"  # jump: -3V egyszerre
        with pytest.raises(InstrumentInvalidReading):
            dmm.read_voltage()

    def test_normal_gradual_change_does_not_raise(self):
        """Lassú, normál feszültségváltozás nem vált ki hibát."""
        dmm, res = make_dmm()
        res.query.return_value = "+1.20000000E+001"
        dmm.read_voltage()  # first: 12.0V

        res.query.return_value = "+1.21000000E+001"  # +0.1V → OK
        result = dmm.read_voltage()
        assert abs(result - 12.1) < 0.001


class TestTempConfiguration:
    def test_configure_temp_sends_conf_temp_frtd(self):
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=10)
        assert any("CONF:TEMP" in c and "FRTD" in c for c in writes(res))

    def test_configure_temp_sets_r0_100_ohm(self):
        """PT100: R0=100 Ω @ 0°C — SENS:TEMP:TRAN:FRTD:RES 100"""
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=10)
        assert "SENS:TEMP:TRAN:FRTD:RES 100" in writes(res)

    @pytest.mark.parametrize("nplc", [1, 10, 100])
    def test_configure_temp_sets_nplc(self, nplc):
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=nplc)
        assert f"SENS:TEMP:NPLC {nplc}" in writes(res)

    def test_configure_temp_sets_celsius(self):
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=10)
        assert any("UNIT:TEMP" in c and "C" in c for c in writes(res))

    def test_configure_temp_uses_iec751_type_85(self):
        """IEC 751 típuskód: 85 (alpha=0.00385 Ω/Ω/°C) — NEM R0 érték!"""
        dmm, res = make_dmm()
        dmm.configure_temp_4wire_pt100(nplc=10)
        assert "CONF:TEMP FRTD,85" in writes(res)


class TestThermistorConfiguration:
    """Sérült 4-wire PT100 RTD pótlása 2-wire 10kΩ@25°C/B=3950K NTC
    termisztorral. A DMM beépített termisztor-konverziója (CONF:TEMP
    THER,5000) kizárólag az 5kΩ/44007-típushoz kalibrált (34460-70-Manual.txt
    6982-6983/8216/11061. sor: "5000 (only available choice)") — ezért nyers
    2-eres ellenállásmérésre állítjuk (CONF:RES), a °C-átszámítást a driver
    végzi Beta-egyenlettel. Review-kör 2 (2026-09-08), Hardver/
    Műszervezérlés HW2-01 lelet: a tartomány AUTORANGE (nem fix 10kΩ), mert
    egy NTC ellenállása hőmérséklet-csökkenéssel NŐ — l.
    test_configure_thermistor_uses_autorange_not_fixed_range."""

    def test_configure_thermistor_sends_conf_res(self):
        dmm, res = make_dmm()
        dmm.configure_temp_2wire_ntc_thermistor(r25_ohm=10000.0, beta_K=3950.0, nplc=10)
        cmds = writes(res)
        assert any(c.startswith("CONF:RES") for c in cmds)

    def test_configure_thermistor_uses_autorange_not_fixed_range(self):
        """Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-01 lelet:
        a korábbi kód `RES:RANG {r25_ohm}`-mal (10000, azaz 10 kΩ) FIX
        tartományra állította a mérést. Egy NTC ellenállása a hőmérséklet
        CSÖKKENÉSÉVEL NŐ (negatív hőfoktényező) — B=3950K/R25=10kΩ mellett
        már 20°C alatt meghaladja a 10 kΩ-t, 0°C-on ~33,6 kΩ, ami a gyári
        kézikönyv szerint (34460-70-Manual.txt 10464-10466. sor) OVERLOAD-ot
        ("9.9E37") eredményez fix tartományon már kb. 21°C alatt is — a
        FIAMM nominál 15-30°C labor-ablak alsó felén (MAX verifier follow-up
        P3-1: nem csak a ma nem futó GOLDEN_GG 0°C ponton). A javítás folytonos
        autorange-re vált (`RES:RANG:AUTO ON`, uo. 10474-10497. sor),
        aminek a range-e automatikusan követi a tényleges ellenállást."""
        dmm, res = make_dmm()
        dmm.configure_temp_2wire_ntc_thermistor(r25_ohm=10000.0, beta_K=3950.0, nplc=10)
        cmds = writes(res)
        assert "RES:RANG:AUTO ON" in cmds
        assert not any(c.startswith("RES:RANG ") for c in cmds)

    @pytest.mark.parametrize("nplc", [1, 10, 100])
    def test_configure_thermistor_sets_nplc(self, nplc):
        dmm, res = make_dmm()
        dmm.configure_temp_2wire_ntc_thermistor(
            r25_ohm=10000.0, beta_K=3950.0, nplc=nplc,
        )
        assert f"RES:NPLC {nplc}" in writes(res)

    def test_configure_thermistor_does_not_use_native_ther_mode(self):
        """A műszer natív THER,5000 módja NEM erre a szondára van kalibrálva
        — nem szabad kiadni."""
        dmm, res = make_dmm()
        dmm.configure_temp_2wire_ntc_thermistor(r25_ohm=10000.0, beta_K=3950.0, nplc=10)
        assert not any("THER" in c for c in writes(res))


class TestThermistorTemperatureReading:
    def test_read_temperature_at_r25_returns_25c(self):
        """R = R25 esetén, a Beta-egyenlet szerint T = 25.0°C pontosan,
        függetlenül a konkrét B-értéktől — ez a formula triviális identitása."""
        dmm, res = make_dmm()
        dmm.configure_temp_2wire_ntc_thermistor(r25_ohm=10000.0, beta_K=3950.0, nplc=10)
        res.query.return_value = "+1.00000000E+004"  # 10000 Ω
        result = dmm.read_temperature()
        assert abs(result - 25.0) < 0.01

    def test_read_temperature_converts_resistance_at_50c(self):
        """Referenciaérték a szabványos Beta-egyenletből (R25=10000, B=3950):
        50°C-on R ≈ 3588.18 Ω — l. device_dmm.py _ntc_beta_resistance_to_celsius()."""
        dmm, res = make_dmm()
        dmm.configure_temp_2wire_ntc_thermistor(r25_ohm=10000.0, beta_K=3950.0, nplc=10)
        res.query.return_value = "+3.58818258E+003"
        result = dmm.read_temperature()
        assert abs(result - 50.0) < 0.01

    def test_read_temperature_zero_resistance_raises_invalid_reading(self):
        dmm, res = make_dmm()
        dmm.configure_temp_2wire_ntc_thermistor(r25_ohm=10000.0, beta_K=3950.0, nplc=10)
        res.query.return_value = "+0.00000000E+000"
        with pytest.raises(InstrumentInvalidReading):
            dmm.read_temperature()

    def test_pt100_mode_unaffected_by_thermistor_support(self):
        """Alapértelmezett (nem konfigurált) mód továbbra is nyers passthrough —
        visszamenőleges kompatibilitás a meglévő PT100 hívókkal."""
        dmm, res = make_dmm()
        res.query.return_value = "+2.20000000E+001"
        result = dmm.read_temperature()
        assert abs(result - 22.0) < 0.001


class TestTemperatureReading:
    def test_read_temperature_returns_float(self):
        dmm, res = make_dmm()
        res.query.return_value = "+2.20000000E+001"
        result = dmm.read_temperature()
        assert isinstance(result, float)
        assert abs(result - 22.0) < 0.001

    def test_read_temperature_sends_read_query(self):
        dmm, res = make_dmm()
        res.query.return_value = "+2.20000000E+001"
        dmm.read_temperature()
        assert any("READ" in q for q in queries(res))


class TestBasicScpi:
    def test_idn_queries_idn(self):
        dmm, res = make_dmm()
        res.query.return_value = "KEYSIGHT,34465A,SN,FW"
        result = dmm.idn()
        assert "*IDN?" in queries(res)
        assert "KEYSIGHT" in result or "34465" in result

    def test_reset_sends_rst(self):
        dmm, res = make_dmm()
        dmm.reset()
        assert any("*RST" in c for c in writes(res))

    def test_clear_status_sends_cls(self):
        dmm, res = make_dmm()
        dmm.clear_status()
        assert any("*CLS" in c for c in writes(res))

    def test_check_error_no_error_returns_empty(self):
        dmm, res = make_dmm()
        res.query.return_value = '+0,"No error"'
        errors = dmm.check_error()
        assert errors == []

    def test_check_error_bare_zero_returns_empty(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] A bare
        "0" válasz (device_psu.py-ban már kezelt forma) itt korábban hibaként
        lett volna jelentve — konzisztencia a 3 SCPI driver között."""
        dmm, res = make_dmm()
        res.query.return_value = "0"
        errors = dmm.check_error()
        assert errors == []

    def test_is_connected_true(self):
        dmm, res = make_dmm()
        assert dmm.is_connected() is True

    def test_check_error_includes_last_cmd(self):
        """[regresszió, Teljes kódbázis review 3. kör (2026-08-29), Hardver/
        Műszervezérlés P3 lelet] device_psu.py::check_error() a legutóbb
        kiküldött SCPI parancsot is jelenti diagnosztikai kontextusként —
        itt korábban hiányzott, inkonzisztencia volt a 3 SCPI driver
        között."""
        dmm, res = make_dmm()
        dmm._last_cmd = "CONF:VOLT:DC"
        res.query.return_value = '+170,"Invalid command"'
        errors = dmm.check_error()
        assert len(errors) > 0
        assert "last:" in errors[0]
        assert "CONF:VOLT:DC" in errors[0]

    def test_safe_off_idempotent(self):
        dmm, res = make_dmm()
        dmm.safe_off()
        dmm.safe_off()


class TestRetryLogic:
    def test_raises_after_max_retries(self):
        dmm, res = make_dmm()
        res.query.side_effect = Exception("LAN timeout")
        with pytest.raises(InstrumentTimeoutError):
            dmm.read_voltage()

    def test_write_without_connect_raises_clear_error_not_attribute_error(self):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] l.
        device_psu.py azonos teszt docstringje."""
        dmm = Keysight34465ADMM()
        with pytest.raises(InstrumentTimeoutError, match="nincs aktív kapcsolat"):
            dmm._write("TEST")

    def test_query_without_connect_raises_clear_error_not_attribute_error(self):
        dmm = Keysight34465ADMM()
        with pytest.raises(InstrumentTimeoutError, match="nincs aktív kapcsolat"):
            dmm._query("SYST:ERR?")

    def test_query_clears_resource_before_every_attempt_including_first(self):
        """[regresszió, teljes kódbázis review 2. kör (2026-08-28) + 3. kör
        (2026-08-29) P1 kiegészítés] l. device_psu.py azonos teszt
        docstringje."""
        dmm, res = make_dmm()
        call_count = 0

        def flaky_query(cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("LAN timeout")
            return "+1.44000000E+001"

        res.query.side_effect = flaky_query
        dmm._query("MEAS:VOLT?")

        assert res.clear.call_count == 2


class StatefulTimedAcquisitionResource:
    """Governance-v2 review-kör 1 (CF-03/HW-R1-01/DFAM-005/SPR-01): a fenti
    tesztek `MagicMock`-ja csak hívásonkénti, állapotfüggetlen választ tud
    adni — nem tudja modellezni, hogy egy `INIT`-tel armed, még folyamatban
    lévő időzített mérést a `resource.clear()` (VISA/IEEE-488 Device Clear)
    ABORTÁL. Ez a stateful fake pontosan ezt az egy állapotátmenetet
    modellezi, semmi mást — nem általános VISA-szimulátor.

    Forrás (VERIFIED_PRIMARY, l. CLAIM_LEDGER.yaml INSTR-DMM-CLEAR-001):
    34460-70-Manual.txt "Using Device Clear" (nyomtatott 198. oldal):
    "If a measurement is in progress, it is aborted. The instrument
    returns to the trigger idle state. An overlapped command [INIT] is
    terminated with no Operation Complete indication."
    """

    def __init__(self, fetch_data: str = "1.0,1.0,1.0") -> None:
        self.write_calls: list[str] = []
        self.query_calls: list[str] = []
        self.clear_count = 0
        self._armed = False
        self._fetch_data = fetch_data

    def write(self, cmd: str) -> None:
        self.write_calls.append(cmd)
        if cmd.strip() == "INIT":
            self._armed = True

    def query(self, cmd: str) -> str:
        self.query_calls.append(cmd)
        if cmd.strip() == "FETC?":
            if not self._armed:
                raise TimeoutError(
                    "FETC? with no pending measurement — INIT was never "
                    "issued or the pending measurement was aborted (e.g. "
                    "by an intervening Device Clear)."
                )
            self._armed = False
            return self._fetch_data
        return '+0,"No error"'

    def clear(self) -> None:
        self.clear_count += 1
        self._armed = False  # Device Clear aborts a pending/overlapped INIT


class TestTimedAcquisitionDeviceClearInteraction:
    """Governance-v2 review-kör 1, CF-03: `_query()` feltétel nélkül hívta
    `_clear_resource()`-t MINDEN kísérlet előtt, a `FETC?` első kísérlete
    előtt is — ez a valós műszeren aborbtálta volna az `arm_timed_
    acquisition()` (`INIT`) által armed mérést, mielőtt a `FETC?`
    egyáltalán célba érne. Fix (batch 3.3): `fetch_timed_acquisition()`
    az első kísérlethez `clear_first=False`-t ad át."""

    def test_fetch_after_arm_returns_the_armed_measurement(self):
        dmm = Keysight34465ADMM()
        res = StatefulTimedAcquisitionResource(fetch_data="1.0,1.1,1.2")
        dmm._resource = res
        dmm._connected = True

        dmm.arm_timed_acquisition()
        result = dmm.fetch_timed_acquisition()

        assert result == "1.0,1.1,1.2"

    def test_fetch_after_arm_does_not_need_more_than_one_query_attempt(self):
        """Ha a FETC? első kísérlete a clear miatt elbukik, a retry-hurok
        további clear-eket küld, de a mérés már véglegesen elveszett (nincs
        új INIT) — a hívás csak a max_retries kimerülése után hibázna.
        Ez a teszt azt bizonyítja, hogy a jelenlegi kódon EGYETLEN sikeres
        FETC?-hez sem elég egy INIT+FETC? pár."""
        dmm = Keysight34465ADMM()
        res = StatefulTimedAcquisitionResource(fetch_data="2.0,2.1,2.2")
        dmm._resource = res
        dmm._connected = True

        dmm.arm_timed_acquisition()
        dmm.fetch_timed_acquisition()

        assert res.clear_count == 1, (
            "A helyes viselkedésnél az INIT+FETC? párhoz pontosan egy "
            "sikeres FETC?-kísérlet kell, tehát legfeljebb egy clear() a "
            "write('INIT') előtt — ha ennél több clear() történt, a FETC? "
            "több kísérletet igényelt, mert a clear elvitte az armed "
            "mérést."
        )


class TestE3ClsSynchronizedAtConnect:
    """[regresszió, Kör 20 Batch E3, HW20-01, DFAM-039 4. előfordulás --
    ugyanaz a család, mint HW19-01/02/04] connect() korábban csupasz
    self._write("*CLS")-sel zárult: egy rákövetkező driver-hívás Device
    Clear-je (l. _clear_resource()) csendben eldobhatta, így az előző
    processz hibasor-maradéka az ELSŐ poll_device_errors() tickben hamis
    FAULT-ot adott volna. FIGYELEM: a modul `writes()` helpere összefésüli
    és ';' mentén szétvágja a write/query üzeneteket -- az alábbi
    asszertek SZÁNDÉKOSAN only_writes()/queries()-t használnak, különben
    a teszt a fix ELŐTT is átmenne (tautologikus lenne)."""

    def test_connect_sends_cls_as_single_synchronized_query(self):
        dmm = Keysight34465ADMM()
        res = MagicMock()
        res.query.return_value = '+0,"No error"'
        dmm.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        assert not any(c == "*CLS" for c in only_writes(res)), (
            "a *CLS nem mehet csupasz write()-ként -- egy későbbi hívás "
            "Device Clear-je eldobhatja"
        )
        assert "*CLS;*OPC?" in queries(res)

    def test_connect_cls_is_the_first_scpi_message(self):
        """SORREND-INVARIÁNS: a *CLS nem kerülhet a connect() későbbi
        parancsai mögé, különben a SAJÁT parancsaink hibáit törölné."""
        dmm = Keysight34465ADMM()
        res = MagicMock()
        events = io_order(res)
        dmm.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        cmds = [c for kind, c in events if kind in ("write", "query")]
        assert cmds and cmds[0] == "*CLS;*OPC?"


class TestR16B1NumericReadbacks:
    # R16-B1: fuggetlen SCPI-literalok, pontos ertek es egyszeri query.
    @pytest.mark.parametrize("raw,expected", [
        ("+4.10000000E-01\n", 0.41),
        ("+1.10000000E+00\n", 1.1),
    ])
    def test_sample_timer_seconds(self, raw, expected):
        dmm, res = make_dmm()
        res.query.return_value = raw

        assert dmm.get_sample_timer_s() == expected
        res.query.assert_called_once_with("SAMP:TIM?")

    @pytest.mark.parametrize("raw,expected", [
        ("+4.04500000E-02\n", "+4.04500000E-02\n"),
        ("+4.04496000E-01\n", "+4.04496000E-01\n"),
    ])
    def test_sample_timer_capability_string(self, raw, expected):
        dmm, res = make_dmm()
        res.query.return_value = raw

        assert dmm.query_sample_timer_capability() == expected
        res.query.assert_called_once_with("SAMP:TIM? MIN")

    @pytest.mark.parametrize("raw,expected", [
        ("+1.00000000E+00\n", 1.0),
        ("+1.00000000E+01\n", 10.0),
    ])
    def test_nplc(self, raw, expected):
        dmm, res = make_dmm()
        res.query.return_value = raw

        assert dmm.get_nplc() == expected
        res.query.assert_called_once_with("VOLT:DC:NPLC?")


# R16-B5a: kizárólag memóriabeli, sessionök között is megmaradó műszerállapot.
class _B5aHardware:
    def __init__(self):
        self.nplc = 10.0
        self.count = 1
        self.source = "IMM"
        self.timer = 1.0
        self.armed = False
        self.errors = []
        self.events = []

    def open_resource(self, name):
        assert name == "FAKE"
        self.events.append(("open", name))
        return _B5aResource(self)


class _B5aResource:
    def __init__(self, hardware):
        self.hardware = hardware
        self.timeout = None

    def clear(self):
        self.hardware.events.append(("clear", ""))
        self.hardware.armed = False

    def close(self):
        self.hardware.events.append(("close", ""))

    def write(self, command):
        self.hardware.events.append(("write", command))
        self._apply(command)

    def _apply(self, command):
        h = self.hardware
        for part in command.split(";"):
            part = part.lstrip(":")
            if part == "ABOR":
                h.armed = False
            elif part == "INIT":
                h.armed = True
            elif part == "*CLS":
                h.errors.clear()
            elif part.startswith("VOLT:DC:NPLC "):
                h.nplc = float(part.split()[-1])
            elif part.startswith("SAMP:COUN "):
                h.count = int(part.split()[-1])
            elif part.startswith("SAMP:SOUR "):
                h.source = part.split()[-1]
            elif part.startswith("SAMP:TIM "):
                h.timer = float(part.split()[-1])
            elif part == "*OPC?" or part.startswith((
                "CONF:VOLT:DC ", "VOLT:DC:RANG ", "VOLT:DC:RANG:AUTO ",
                "TRIG:SOUR ", "TRIG:COUN ",
            )):
                pass
            else:
                raise AssertionError(f"Nem modellezett SCPI: {part}")

    def query(self, command):
        h = self.hardware
        h.events.append(("query", command))
        if command.endswith(";*OPC?"):
            self._apply(command)
            return "1"
        if command == "VOLT:DC:NPLC?":
            return str(h.nplc)
        if command == "SAMP:TIM?":
            return str(h.timer)
        if command == "SYST:ERR?":
            return h.errors.pop(0) if h.errors else '+0,"No error"'
        if command in ("READ?", "FETC?"):
            if command == "FETC?":
                assert h.armed
                h.armed = False
            return ",".join(["12.5"] * h.count)
        raise AssertionError(f"Nem modellezett SCPI: {command}")


class _B5aObservedLock:
    """A második szál lockütközését esemény jelzi, időzítési feltétel nélkül."""
    def __init__(self):
        from threading import Event, RLock

        self.inner = RLock()
        self.attempted = Event()
        self.blocked = False

    def acquire(self, blocking=True, timeout=-1):
        from threading import current_thread

        if current_thread().name == "b5a-second" and blocking:
            acquired = self.inner.acquire(blocking=False)
            self.blocked = not acquired
            self.attempted.set()
            if acquired:
                return True
        return self.inner.acquire(blocking=blocking, timeout=timeout)

    def release(self):
        self.inner.release()


class TestR16B5aTransactions:
    def test_parallel_reads_include_validation_and_state_update(self, monkeypatch):
        from threading import Event, Thread

        dmm, resource = make_dmm()
        resource.query.side_effect = ["12.0", "13.0"]
        lock = _B5aObservedLock()
        dmm._transaction_lock = lock
        entered, proceed = Event(), Event()
        observations, results, failures = [], [], []
        validate = dmm._validate_voltage

        def gated_validate(value):
            observations.append((value, dmm._last_valid_voltage))
            if value == 12.0:
                entered.set()
                assert proceed.wait(2)  # Kizárólag deadlock-watchdog.
            validate(value)

        monkeypatch.setattr(dmm, "_validate_voltage", gated_validate)

        def read():
            try:
                results.append(dmm.read_voltage())
            except BaseException as exc:  # noqa: BLE001 -- a szál hibája a főszálon bukjon
                failures.append(exc)

        first = Thread(target=read, name="b5a-first", daemon=True)
        second = Thread(target=read, name="b5a-second", daemon=True)
        first.start()
        try:
            assert entered.wait(2)
            second.start()
            assert lock.attempted.wait(2)
            assert lock.blocked
            assert resource.query.call_count == 1
            assert dmm._last_valid_voltage is None
        finally:
            proceed.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        assert not first.is_alive() and not second.is_alive()
        assert failures == []
        assert sorted(results) == [12.0, 13.0]
        assert observations == [(12.0, None), (13.0, 12.0)]
        assert dmm._last_valid_voltage == 13.0

    def test_nested_driver_calls_are_reentrant_and_per_instance(self):
        from threading import Thread

        dmm, resource = make_dmm()
        other, _ = make_dmm()
        assert dmm._transaction_lock is not other._transaction_lock
        failures = []

        def configure():
            try:
                dmm.configure_dcv(100, 10)  # configure -> atomic -> query.
                resource.query.return_value = "12.5"
                assert dmm.read_voltage() == 12.5
            except BaseException as exc:  # noqa: BLE001 -- a szál hibája a főszálon bukjon
                failures.append(exc)

        thread = Thread(target=configure, daemon=True)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        assert failures == []

    def test_token_identity_and_stale_token_rejected_without_io(self):
        from Prog.drivers.device_dmm import DMMMeasurementBusy

        class EqualToken:
            def __eq__(self, other):
                return True

        dmm, resource = make_dmm()
        owner, impostor = EqualToken(), EqualToken()
        resource.query.return_value = "12.5"
        with pytest.raises(ValueError):
            dmm.acquire_measurement(None)
        dmm.acquire_measurement(owner)
        for token in (None, impostor):
            with pytest.raises(DMMMeasurementBusy):
                dmm.read_voltage(owner=token)
            with pytest.raises(DMMMeasurementBusy):
                dmm.release_measurement(token)
        with pytest.raises(DMMMeasurementBusy):
            dmm.acquire_measurement(owner)
        resource.query.assert_not_called()
        assert dmm.read_voltage(owner=owner) == 12.5
        dmm.release_measurement(owner)
        resource.reset_mock()
        with pytest.raises(DMMMeasurementBusy):
            dmm.read_voltage(owner=owner)
        with pytest.raises(DMMMeasurementBusy):
            dmm.release_measurement(owner)
        resource.query.assert_not_called()
        assert dmm.read_voltage() == 12.5

    @pytest.mark.parametrize("method,args", [
        ("connect", ("FAKE", MagicMock())),
        ("disconnect", ()),
        ("set_io_timeout_ms", (42,)),
        ("idn", ()),
        ("reset", ()),
        ("clear_status", ()),
        ("check_error", ()),
        ("abort_measurement", ()),
        ("configure_dcv", (100, 10)),
        ("set_nplc", (1,)),
        ("get_nplc", ()),
        ("get_sample_timer_s", ()),
        ("autorange_off", ()),
        ("configure_timed_acquisition", (2, 0.5)),
        ("arm_timed_acquisition", ()),
        ("fetch_timed_acquisition", ()),
        ("query_sample_timer_capability", ()),
        ("reset_to_single_sample", ()),
        ("configure_temp_4wire_pt100", ()),
        ("configure_temp_2wire_ntc_thermistor", ()),
        ("read_temperature", ()),
    ])
    def test_owned_device_rejects_interfering_operations(self, method, args):
        from Prog.drivers.device_dmm import DMMMeasurementBusy

        dmm, resource = make_dmm()
        owner = object()
        dmm.acquire_measurement(owner)
        with pytest.raises(DMMMeasurementBusy):
            getattr(dmm, method)(*args)
        assert resource.mock_calls == []
        assert dmm.is_connected()
        dmm.release_measurement(owner)

    def test_disconnect_and_configuration_after_release(self):
        from Prog.drivers.device_dmm import DMMMeasurementBusy

        h = _B5aHardware()
        dmm = Keysight34465ADMM()
        dmm.connect("FAKE", rm=h)
        owner = object()
        dmm.acquire_measurement(owner)
        before = list(h.events)
        with pytest.raises(DMMMeasurementBusy):
            dmm.disconnect()
        assert h.events == before and dmm.is_connected()
        dmm.release_measurement(owner)
        dmm.set_nplc(2)
        assert h.nplc == 2
        dmm.disconnect()
        assert h.events[-2:] == [("write", "ABOR"), ("close", "")]
        assert not dmm.is_connected()

    def test_poll_does_not_wait_for_owned_inflight_read_and_release_waits(self):
        from threading import Event, Thread

        from Prog.drivers.device_dmm import DMMMeasurementBusy

        dmm, resource = make_dmm()
        owner = object()
        dmm.acquire_measurement(owner)
        lock = _B5aObservedLock()
        dmm._transaction_lock = lock
        entered, proceed, released = Event(), Event(), Event()
        failures = []

        def query(command):
            assert command == "READ?"
            entered.set()
            assert proceed.wait(2)
            return "12.5"

        resource.query.side_effect = query

        def read():
            try:
                assert dmm.read_voltage(owner=owner) == 12.5
            except BaseException as exc:  # noqa: BLE001 -- a szál hibája a főszálon bukjon
                failures.append(exc)

        def release():
            try:
                dmm.release_measurement(owner)
                released.set()
            except BaseException as exc:  # noqa: BLE001 -- a szál hibája a főszálon bukjon
                failures.append(exc)

        first = Thread(target=read, daemon=True)
        second = Thread(target=release, name="b5a-second", daemon=True)
        first.start()
        try:
            assert entered.wait(2)
            with pytest.raises(DMMMeasurementBusy):
                dmm.check_error()
            second.start()
            assert lock.attempted.wait(2)
            assert lock.blocked and not released.is_set()
        finally:
            proceed.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        assert not first.is_alive() and not second.is_alive()
        assert failures == [] and released.is_set()
        assert queries(resource) == ["READ?"]

    def test_ownerless_scpi_transcript_and_exceptions(self):
        h = _B5aHardware()
        dmm = Keysight34465ADMM()
        dmm.connect("FAKE", rm=h)
        dmm.configure_dcv(100, 10)
        assert dmm.read_voltage() == 12.5
        dmm.set_nplc(1)
        assert dmm.get_nplc() == 1.0
        dmm.configure_timed_acquisition(2, 0.25)
        dmm.arm_timed_acquisition()
        assert dmm.fetch_timed_acquisition() == "12.5,12.5"
        h.errors.append('-200,"Execution error"')
        assert dmm.check_error() == ['-200,"Execution error" [last: FETC?]']
        assert dmm.check_error() == []
        dmm.reset_to_single_sample()
        assert dmm.read_voltage() == 12.5
        dmm.disconnect()
        with pytest.raises(InstrumentTimeoutError) as exc:
            dmm.read_voltage()
        assert str(exc.value) == "DMM: nincs aktív kapcsolat (connect() nem hívva): 'READ?'"
        assert h.events == [
            ("open", "FAKE"), ("clear", ""), ("query", "*CLS;*OPC?"),
            ("clear", ""),
            ("query", ("ABOR;:CONF:VOLT:DC 100;:VOLT:DC:NPLC 10;:VOLT:DC:RANG 100;"
             ":VOLT:DC:RANG:AUTO OFF;:TRIG:SOUR IMM;:TRIG:COUN 1;:SAMP:COUN 1;"
             ":SAMP:SOUR IMM;*OPC?")),
            ("clear", ""), ("query", "READ?"),
            ("clear", ""), ("query", "VOLT:DC:NPLC 1;*OPC?"),
            ("clear", ""), ("query", "VOLT:DC:NPLC?"),
            ("clear", ""),
            ("query", "ABOR;:SAMP:COUN 2;:SAMP:SOUR TIM;:SAMP:TIM 0.25;*OPC?"),
            ("clear", ""), ("write", "INIT"), ("query", "FETC?"),
            ("clear", ""), ("query", "SYST:ERR?"),
            ("clear", ""), ("query", "SYST:ERR?"),
            ("clear", ""), ("query", "ABOR;:SAMP:COUN 1;:SAMP:SOUR IMM;*OPC?"),
            ("clear", ""), ("query", "READ?"),
            ("write", "ABOR"), ("close", ""),
        ]

    def test_fake_hardware_state_survives_new_driver_session(self):
        h = _B5aHardware()
        first = Keysight34465ADMM()
        first.connect("FAKE", rm=h)
        first.set_nplc(3)
        first.configure_timed_acquisition(2, 0.5)
        first.arm_timed_acquisition()
        first_resource = first._resource
        first.disconnect()
        assert (h.nplc, h.count, h.source, h.timer, h.armed) == (3, 2, "TIM", 0.5, False)
        second = Keysight34465ADMM()
        second.connect("FAKE", rm=h)
        assert second._resource is not first_resource
        assert second.get_nplc() == 3.0
        assert second.get_sample_timer_s() == 0.5
        with pytest.raises(InstrumentInvalidReading):
            second.read_voltage()  # A sticky mintaszám megmaradt.
        second.reset_to_single_sample()
        assert second.read_voltage() == 12.5
        second.disconnect()


class TestR16B5bAccess:
    def test_toctou_owner_check_after_lock_event_gate(self):
        from threading import Event, RLock, Thread, get_ident
        from unittest.mock import Mock

        from Prog.drivers.device_dmm import DMMMeasurementBusy, Keysight34465ADMM

        dmm = Keysight34465ADMM()
        resource = Mock()
        dmm._resource = resource
        reached, proceed = Event(), Event()
        lock = RLock()
        main = get_ident()

        class Gate:
            def acquire(self, *a, **k):
                if get_ident() != main and not reached.is_set():
                    reached.set()
                    assert proceed.wait(2)
                return lock.acquire(*a, **k)
            def release(self):
                lock.release()

        dmm._transaction_lock = Gate()
        failures = []
        def disconnect():
            try:
                dmm.disconnect()
            except DMMMeasurementBusy:
                failures.append("BUSY")
        thread = Thread(target=disconnect)
        thread.start()
        owner = object()
        try:
            assert reached.wait(2)
            dmm.acquire_measurement(owner)
        finally:
            proceed.set()
            thread.join(2)
        assert not thread.is_alive()
        assert failures == ["BUSY"]
        resource.close.assert_not_called()
        dmm.release_measurement(owner)
        dmm.disconnect()
        resource.close.assert_called_once()

    def test_non_owner_operations_do_not_wait_for_inflight_voltage(self):
        from threading import Event, Thread

        from Prog.drivers.device_dmm import DMMMeasurementBusy, Keysight34465ADMM

        dmm = Keysight34465ADMM()
        entered, proceed, finished = Event(), Event(), Event()
        owner = object()
        dmm.acquire_measurement(owner)
        def query(*a, **k):
            entered.set()
            assert proceed.wait(2)
            return "12.5"
        dmm._query = query
        reader = Thread(target=lambda: dmm.read_voltage(owner=owner))
        rejected = []
        def outsider():
            try:
                for operation in [dmm.disconnect, lambda: dmm.set_nplc(1.0), dmm.read_voltage]:
                    try:
                        operation()
                    except DMMMeasurementBusy:
                        rejected.append("BUSY")
            finally:
                finished.set()
        reader.start()
        other = Thread(target=outsider)
        try:
            assert entered.wait(2)
            other.start()
            assert finished.wait(2)
            assert rejected == ["BUSY"] * 3
        finally:
            proceed.set()
            reader.join(2)
            if other.ident is not None:
                other.join(2)
            dmm.release_measurement(owner)


class TestR16B15Autozero:
    """Valódi driver, kizárólag MagicMock VISA-resource."""

    def test_enable_autozero_is_synchronized_and_read_back(self):
        dmm, resource = make_dmm()
        resource.query.side_effect = ["1", "+1"]
        dmm.enable_autozero()
        assert queries(resource) == [
            "VOLT:DC:ZERO:AUTO ON;*OPC?", "VOLT:DC:ZERO:AUTO?",
        ]

    @pytest.mark.parametrize("raw", ["0", "0.5", "2", "NaN", "inf", "bad"])
    def test_enable_autozero_rejects_invalid_readback(self, raw):
        from Prog.src.exceptions import InstrumentError

        dmm, resource = make_dmm()
        resource.query.side_effect = ["1", raw]
        with pytest.raises(InstrumentError):
            dmm.enable_autozero()



class TestR16B21aTimedOwner:
    """A valódi timed driver ugyanazzal a B5a-foglalással használható."""

    @pytest.mark.parametrize("operation", [
        "configure_timed_acquisition", "check_error", "get_sample_timer_s",
        "arm_timed_acquisition", "fetch_timed_acquisition", "reset_to_single_sample",
    ])
    def test_owner_access_and_stale_token_rejection(self, operation):
        from Prog.drivers.device_dmm import DMMMeasurementBusy

        dmm, resource = make_dmm()
        resource.query.return_value = "1"
        owner = object()
        dmm.acquire_measurement(owner)
        method = getattr(dmm, operation)
        arguments = {"sample_count": 3, "interval_s": 0.2} if operation.startswith("configure") else {}
        try:
            method(**arguments, owner=owner)
            before = list(resource.mock_calls)
            for outsider in (None, object()):
                with pytest.raises(DMMMeasurementBusy):
                    method(**arguments, owner=outsider)
                assert resource.mock_calls == before
        finally:
            dmm.release_measurement(owner)
        with pytest.raises(DMMMeasurementBusy):
            method(**arguments, owner=owner)
        assert resource.mock_calls == before

    def test_owned_roundtrip_preserves_scpi_and_fetch_clear_policy(self):
        dmm, resource = make_dmm()
        resource.query.side_effect = ["1", "0", "0.2", "13.4,13.3,13.2", "1"]
        events = []
        resource.clear.side_effect = lambda: events.append("clear")
        resource.write.side_effect = lambda command: events.append(command)
        query = resource.query

        def record(command):
            events.append(command)
            return query(command)

        resource.query = record
        owner = object()
        dmm.acquire_measurement(owner)
        try:
            dmm.configure_timed_acquisition(3, 0.2, owner=owner)
            assert dmm.check_error(owner=owner) == []
            assert dmm.get_sample_timer_s(owner=owner) == 0.2
            dmm.arm_timed_acquisition(owner=owner)
            assert dmm.fetch_timed_acquisition(owner=owner) == "13.4,13.3,13.2"
            dmm.reset_to_single_sample(owner=owner)
        finally:
            dmm.release_measurement(owner)
        assert events == [
            "clear", "ABOR;:SAMP:COUN 3;:SAMP:SOUR TIM;:SAMP:TIM 0.2;*OPC?",
            "clear", "SYST:ERR?", "clear", "SAMP:TIM?", "clear", "INIT",
            "FETC?", "clear", "ABOR;:SAMP:COUN 1;:SAMP:SOUR IMM;*OPC?",
        ]
        assert not dmm.measurement_owned

    def test_foreign_owner_acquire_is_rejected_before_waiting_for_io(self):
        from Prog.drivers.device_dmm import DMMMeasurementBusy

        dmm, _resource = make_dmm()
        owner = object()
        dmm.acquire_measurement(owner)
        original_lock = dmm._transaction_lock

        class InflightTransaction:
            def acquire(self, *args, **kwargs):
                raise AssertionError("Idegen owner mellett nem várhatunk DMM-I/O-ra")

            def release(self):
                raise AssertionError("Nem megszerzett zár nem oldható fel")

        dmm._transaction_lock = InflightTransaction()
        try:
            with pytest.raises(DMMMeasurementBusy):
                dmm.acquire_measurement(object())
            assert dmm._measurement_owner is owner
        finally:
            dmm._transaction_lock = original_lock
            dmm.release_measurement(owner)


class TestR16B21bOwnerBusy:
    def test_matching_owner_check_error_waits_for_local_transaction(self):
        from threading import Event, Thread

        dmm, resource = make_dmm()
        owner = object()
        dmm.acquire_measurement(owner)
        entered, done = Event(), Event()
        values, failures = [], []

        def poll():
            entered.set()
            try:
                values.append(dmm.check_error(owner=owner))
            except BaseException as exc:  # noqa: BLE001 - szálhiba rögzítése és továbbadása
                failures.append(exc)
            finally:
                done.set()

        dmm._transaction_lock.acquire()
        thread = Thread(target=poll)
        try:
            thread.start()
            assert entered.wait(2)
            assert not done.wait(0.03)
            resource.query.assert_not_called()
        finally:
            dmm._transaction_lock.release()
            thread.join(2)
            dmm.release_measurement(owner)
        assert not thread.is_alive()
        assert failures == [] and values == [[]]
        assert queries(resource) == ["SYST:ERR?"]

    def test_ownerless_poll_remains_nonblocking_during_transaction(self):
        from threading import Event, Thread

        from Prog.drivers.device_dmm import DMMMeasurementBusy

        dmm, resource = make_dmm()
        done, errors = Event(), []

        def poll():
            try:
                dmm.check_error()
            except DMMMeasurementBusy as exc:
                errors.append(str(exc))
            finally:
                done.set()

        dmm._transaction_lock.acquire()
        thread = Thread(target=poll)
        try:
            thread.start()
            assert done.wait(2)
            resource.query.assert_not_called()
        finally:
            dmm._transaction_lock.release()
            thread.join(2)
        assert len(errors) == 1
        assert "tranzakció" in errors[0]
