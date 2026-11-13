"""
Keithley2380Load unit tesztek — pyvisa resource mock-kal.
"""
import pytest
from unittest.mock import MagicMock
from Prog.drivers.device_dmm import Keysight34465ADMM
from Prog.drivers.device_load import Keithley2380Load
from Prog.drivers.device_psu import Keithley2220PSU
from Prog.src.exceptions import (
    InstrumentCommandError,
    InstrumentInvalidReading,
    InstrumentTimeoutError,
)


def make_load() -> tuple[Keithley2380Load, MagicMock]:
    load = Keithley2380Load()
    res = MagicMock()
    res.timeout = load.timeout_ms
    res.query.return_value = '+0,"No error"'
    # R16-B10: alapból pozitív ON-nyugta; az explicit tesztválasz megmarad.
    res.query.side_effect = lambda cmd: (
        "1" if cmd == "INP?" and res.query.return_value == NO_ERROR_REPLY
        else res.query.return_value
    )
    load._resource = res
    load._connected = True
    load._input_cmd = "INP"
    return load, res


NO_ERROR_REPLY = '+0,"No error"'


def _connect_query_ok(cmd, *args, **kwargs):
    """[R2-B2] connect()-kori VISA-double: a dokumentált INP:SHOR? /
    INP:TIM? lekérdezésre "0" (2380 txt 5814-5858: Returns 0|1), minden
    másra a korábbi hibamentes SYST:ERR?-válasz."""
    if cmd in ("INP:SHOR?", "INP:TIM?"):
        return "0"
    return NO_ERROR_REPLY


def writes(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.write.call_args_list]


def queries(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.query.call_args_list]


class TestConnectSafeState:
    """SAFE-001/INS-002: connect() után a bemenet explicit OFF állapotban legyen."""

    def test_connect_sends_input_off(self):
        load = Keithley2380Load()
        res = MagicMock()
        res.query.side_effect = _connect_query_ok
        load.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        # [Kör 19 Batch D2, HW19-02] input_off() mostantól `_query(...;
        # *OPC?)`-t hasznal, NEM `_write()`-ot -- l. writes(res)/queries(res)
        # kulon-kulon ellenorzeset lent, a device_dmm.py azonos migraciojanak
        # tesztjei mintajat koveteve.
        assert any("OFF" in c for c in writes(res) + queries(res))

    def test_connect_sets_input_commanded_off(self):
        load = Keithley2380Load()
        res = MagicMock()
        res.query.side_effect = _connect_query_ok
        load.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        assert load.input_commanded_on is False


class TestConnectClearsVonLatch:
    """Review-kör 5, Hardver/Műszervezérlés HW-R5-I (P3, felhasználói döntés
    2026-09-10): a Von latch (alapértelmezés ON, 2380 txt 6492-6494. sor)
    egy korábbi session/front panel nem-nulla Von-szintje mellett
    megakadályozhatja a bemenet tényleges bekapcsolását, miközben az SCPI
    válasz (INP?) sikeresnek látszik -- 2380 txt 2335-2338. sor: "If the
    load cannot be turned on, check whether the Von latch function is
    enabled. If yes, set Von value to 0 to clear the fault." connect()
    explicit VOLT:ON 0-t küld, hogy ez a sticky állapot ne öröklődjön át."""

    def test_connect_clears_von_level(self):
        load = Keithley2380Load()
        res = MagicMock()
        res.query.side_effect = _connect_query_ok
        load.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        assert "VOLT:ON 0" in writes(res)

    def test_von_clear_failure_does_not_block_connect(self):
        """Best-effort, NEM safety-invariáns -- egy kommunikációs hiba itt
        nem rosszabb, mint a fix előtti állapot (a régi Von-érték marad),
        tehát nem szabad, hogy meghiúsítsa a teljes connect()-et."""
        load = Keithley2380Load()
        res = MagicMock()

        def _write_side_effect(cmd: str) -> None:
            if cmd == "VOLT:ON 0":
                raise Exception("SCPI timeout")

        res.write.side_effect = _write_side_effect
        res.query.side_effect = _connect_query_ok
        load.connect(  # nem szabad kivételt dobnia
            "MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res)
        )
        assert load.is_connected()


class TestConnectNeutralizesInheritedFunctionMode:
    """[regresszió, Kör 19 D9, HW19-03, DFAM-032 review_search-tengely] a
    `[SOURce:]FUNCtion:MODE {FIXed|LIST}` (VERIFIED_PRIMARY, Keithley 2380
    kézikönyv 5928. sor) korábban SOSEM lett explicit beállítva connect()-kor
    -- ha egy korábbi session/front-panel a loadot LIST módban hagyta, a
    SOUR:FUNC CURR/CURR:RANG/SOUR:CURR parancsok többé nem kormányozzák a
    süllyesztőáramot. connect() mostantól best-effort FIX-re állítja,
    ugyanaz a minta, mint a VOLT:ON 0 Von-latch fixnél."""

    def test_connect_sends_function_mode_fix(self):
        load = Keithley2380Load()
        res = MagicMock()
        res.query.side_effect = _connect_query_ok
        load.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        assert "SOUR:FUNC:MODE FIX" in writes(res)

    def test_function_mode_fix_failure_does_not_block_connect(self):
        """Best-effort, NEM safety-invariáns -- egy kommunikációs hiba itt
        nem rosszabb, mint a fix előtti állapot (a régi FUNCtion:MODE
        megmarad), tehát nem szabad, hogy meghiúsítsa a teljes connect()-et."""
        load = Keithley2380Load()
        res = MagicMock()

        def _write_side_effect(cmd: str) -> None:
            if cmd == "SOUR:FUNC:MODE FIX":
                raise Exception("SCPI timeout")

        res.write.side_effect = _write_side_effect
        res.query.side_effect = _connect_query_ok
        load.connect(  # nem szabad kivételt dobnia
            "MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res)
        )
        assert load.is_connected()


class TestE3ClsSynchronizedAtConnect:
    """[regresszió, Kör 20 Batch E3, HW20-01, DFAM-039 5. előfordulás]
    l. device_dmm.py azonos tesztosztályának indoklását."""

    def test_connect_sends_cls_as_single_synchronized_query(self):
        load = Keithley2380Load()
        res = MagicMock()
        res.query.side_effect = _connect_query_ok
        load.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        assert not any(c == "*CLS" for c in writes(res))
        assert "*CLS;*OPC?" in queries(res)

    def test_cls_failure_does_not_prevent_input_off(self):
        """DISZKRIMINÁLÓ, KÉT IRÁNYBAN:
        - a HEAD-beli kódon bukik (nincs "*CLS;*OPC?" query);
        - a try/except NÉLKÜLI, "naiv" fixen is bukik (a kivétel kiszökne,
          és az input_off() SOHA nem futna le).
        Ez a teszt a fix safety-magja: a *CLS egy NEM-safety lépés egy
        safety-kritikus lépés (SAFE-001/INS-002 input_off()) ELŐTT."""
        load = Keithley2380Load()
        res = MagicMock()

        def _query_side_effect(cmd, *args, **kwargs):
            if cmd.startswith("*CLS"):
                raise RuntimeError("SCPI timeout")
            return _connect_query_ok(cmd)

        res.query.side_effect = _query_side_effect
        load.connect(  # nem szabad kivételt dobnia
            "MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res)
        )
        assert any(q == "*CLS;*OPC?" for q in queries(res)), (
            "a *CLS-t egyáltalán meg kellett kísérelni (HEAD-diszkrimináció)"
        )
        assert any(
            q.startswith("INP OFF") for q in queries(res) + writes(res)
        ), (
            "a safety-kritikus input_off() a *CLS hibája UTÁN is le kell "
            "hogy fusson (naiv-fix-diszkrimináció)"
        )
        assert load.input_commanded_on is False
        assert load.is_connected()

    def test_connect_cls_precedes_input_off(self):
        """SORREND-INVARIÁNS: a *CLS nem kerülhet az input_off() UTÁNRA --
        ott egy elutasított "INP OFF" hibabejegyzését törölné ki."""
        load = Keithley2380Load()
        res = MagicMock()
        res.query.side_effect = _connect_query_ok
        load.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        qs = queries(res)
        assert qs.index("*CLS;*OPC?") < next(
            i for i, q in enumerate(qs) if q.startswith("INP OFF")
        )


class TestInputOnOff:
    def test_input_on_sends_input_on(self):
        load, res = make_load()
        load.input_on()
        assert any("ON" in c for c in writes(res))

    def test_input_off_sends_input_off(self):
        load, res = make_load()
        load.input_off()
        # [Kör 19 Batch D2, HW19-02] input_off() mostantól `_query(...;
        # *OPC?)`-t hasznal, NEM `_write()`-ot -- l. a lenti, kifejezetten
        # az atomikus-uzenet-forma ellenorzesere irt tesztet is.
        assert any("OFF" in c for c in queries(res))
        # A kritikus "INP OFF" mar nem write()-on megy -- a TRAN 0 (nem-
        # kritikus, kulon-kezelt melleklepes) VALTOZATLANUL write()-ot hasznal.
        assert not any(c.startswith("INP") for c in writes(res))

    def test_input_off_critical_command_is_one_synchronized_query(self):
        """[regresszió, Kör 19 Batch D2, HW19-02/DFAM-001+DFAM-039] a
        KRITIKUS "INP OFF" korábban egyetlen, szinkronizálatlan `_write()`
        volt -- a `safe_off()` "sikerét" kizárólag abból vezette le, hogy
        a hívás nem dobott kivételt, ami semmit nem bizonyít arról, hogy
        a parancs ténylegesen végrehajtódott. A strukturálisan azonos
        PSU-oldali safe-off út már `_query(...;*OPC?)`-re lett cserélve
        pontosan emiatt (DFAM-039/F7) -- ez a testvér-javítás."""
        load, res = make_load()
        load.input_off()
        critical = [c for c in queries(res) if c.startswith("INP")]
        assert critical == ["INP OFF;*OPC?"], (
            f"nem egyetlen szinkronizált program message: {critical!r}"
        )
        assert not any(c.startswith("INP") for c in writes(res))

    def test_input_on_sets_commanded_true(self):
        """[N9] Commanded state nyilvántartás."""
        load, res = make_load()
        assert load.input_commanded_on is False
        load.input_on()
        assert load.input_commanded_on is True

    def test_input_on_sets_commanded_true_even_on_write_failure(self):
        """Teljes kódbázis review 2. kör (2026-09-16, R2-08-F1 testvér-lelete,
        P2, DFAM-032): a flag-beállítás korábban a write UTÁN, GUARD NÉLKÜL
        állt -- ha a "INP ON" write a max_retries kimerülése után dobott,
        miközben a parancs fizikailag már átment, a flag False-on ragadt
        volna egy ÉLŐ bemenet mellett -- a hamis False a VESZÉLYES irány
        (safety.py::check_concurrent_psu_load() egy élő Load mellett
        engedné a PSU-t). Ugyanaz a minta, mint input_off()-nál (CF-13/
        HW-R1-04) és device_psu.py::output_on()-nál (R2-08-F1). Mutáció,
        amin bukik: a try/finally szétszedése (flag csak sikeres write
        után)."""
        load, res = make_load()
        res.write.side_effect = InstrumentTimeoutError("simulated timeout")

        with pytest.raises(InstrumentTimeoutError):
            load.input_on()

        assert load.input_commanded_on is True

    def test_input_on_write_failure_still_propagates_exception(self):
        """Fail-closed guard: a try/finally bevezetése NEM szabad, hogy
        elnyelje a kivételt -- a hívónak érdemi hibát kell kapnia, nem
        csendes sikert."""
        load, res = make_load()
        res.write.side_effect = InstrumentTimeoutError("simulated timeout")

        with pytest.raises(InstrumentTimeoutError):
            load.input_on()

    def test_input_off_sets_commanded_false(self):
        load, res = make_load()
        load.input_on()
        load.input_off()
        assert load.input_commanded_on is False

    def test_safe_off_idempotent(self):
        """[v1.2] safe_off nem dob kivételt ha már off."""
        load, res = make_load()
        load.safe_off()
        load.safe_off()

    def test_safe_off_returns_true_on_success(self):
        load, res = make_load()
        assert load.safe_off() is True

    def test_safe_off_returns_false_on_write_failure(self):
        """CLAUDE.md §10.4: a hívónak (InstrumentManager) módja kell legyen
        megtudni, hogy az INPUT OFF-parancs ténylegesen sikeres volt-e —
        safe_off() NEM nyelheti el csendben a hibát visszatérési érték nélkül.
        [Kör 19 Batch D2, HW19-02 frissítés] a kritikus "INP OFF" mostantól
        `_query()`-n megy -- a hibát ott kell szimulálni."""
        load, res = make_load()
        res.query.side_effect = InstrumentTimeoutError("simulated timeout")
        assert load.safe_off() is False

    def test_safe_off_clears_commanded_on_flag_even_on_write_failure(self):
        """[regresszió, Teljes kódbázis review 8. kör (2026-08-30), Hardver/
        Műszervezérlés P1 lelet] safe_off() korábban nem garantálta, hogy
        input_commanded_on False-ra álljon, ha a KRITIKUS "INP OFF" write
        hibázott (input_off() ekkor a flag-frissítés ELŐTT dob kivételt) —
        ellentétben device_psu.py::safe_off()-szal, ami ugyanezt a mintát
        már explicit, dokumentáltan kezeli. A flag az EGYETLEN forrás, amit
        check_concurrent_psu_load() mind a 6 controllerben olvas. [Kör 19
        Batch D2, HW19-02 frissítés] a kritikus "INP OFF" mostantól
        `_query()`-n megy -- a hibát ott kell szimulálni."""
        load, res = make_load()
        load.input_on()
        assert load.input_commanded_on is True
        res.query.side_effect = InstrumentTimeoutError("simulated timeout")

        assert load.safe_off() is False
        assert load.input_commanded_on is False


class TestReadInputState:
    """Review-kör 5, Hardver/Műszervezérlés HW5-03 (P3): read_input_state()
    olvasás-only diagnosztika — 2380 txt 5793-5810. sor "Returns 0|1"."""

    def test_on(self):
        load, res = make_load()
        res.query.return_value = "1"
        assert load.read_input_state() is True
        assert any(q == "INP?" for q in queries(res))

    def test_off(self):
        load, res = make_load()
        res.query.return_value = "0"
        assert load.read_input_state() is False

    def test_unknown_response_raises_invalid_reading(self):
        load, res = make_load()
        res.query.return_value = "GARBAGE"
        with pytest.raises(InstrumentInvalidReading):
            load.read_input_state()

    def test_accepts_signed_float_nr1(self):
        """Teljes kódbázis review 2. kör (2026-09-15, R2-03, P3, PREVENTÍV,
        HIL_PENDING): a `read_questionable_status()` mintáját követve
        toleráns a firmware esetlegesen lebegőpontos NR1 alakjára
        ("1.0"/"+0"), de csak pontos 0.0/1.0-ra -- konzisztencia-célú
        elővigyázatosság, nem megfigyelt hiba javítása."""
        load, res = make_load()
        res.query.return_value = "1.0"
        assert load.read_input_state() is True
        res.query.return_value = "+0"
        assert load.read_input_state() is False

    def test_rejects_out_of_range_numeric(self):
        load, res = make_load()
        for bad in ("2", "2.0", "-1", "NAN", "INF"):
            res.query.return_value = bad
            with pytest.raises(InstrumentInvalidReading):
                load.read_input_state()


class TestReadQuestionableStatus:
    """Review-kör 5, Hardver/Műszervezérlés HW4-01 (P3):
    read_questionable_status() -- STAT:QUES:COND? bit-tábla dekódolás.
    Forrás: 2380 txt 3405-3514. sor (bit-tábla), 4760-4776. sor (a query
    "read-only ... real-time (unlatched) ... not channel specific")."""

    def test_all_clear_returns_all_false(self):
        load, res = make_load()
        res.query.return_value = "0"
        status = load.read_questionable_status()
        assert set(status.keys()) == {
            "VF", "OC", "RS", "OP", "OT", "RRV", "UNR", "LRV", "OV", "PS", "VON", "TBF",
        }
        assert not any(status.values())

    def test_queries_stat_ques_cond(self):
        load, res = make_load()
        res.query.return_value = "0"
        load.read_questionable_status()
        assert any(q == "STAT:QUES:COND?" for q in queries(res))

    def test_bit1_oc_decoded(self):
        load, res = make_load()
        res.query.return_value = str(1 << 1)  # OC
        status = load.read_questionable_status()
        assert status["OC"] is True
        assert status["VF"] is False

    def test_bit13_ps_decoded(self):
        """PS (bit13) a tényleges "input kikapcsolva" protection-shutdown
        trip jelzője (2380 txt 3475-3479. sor)."""
        load, res = make_load()
        res.query.return_value = str(1 << 13)  # PS
        status = load.read_questionable_status()
        assert status["PS"] is True

    def test_multiple_bits_decoded_together(self):
        """OC (bit1) + PS (bit13) egyszerre -- a manual szerint egy
        elhúzódó overcurrent PONTOSAN ezt a párost állítja be."""
        load, res = make_load()
        res.query.return_value = str((1 << 1) | (1 << 13))
        status = load.read_questionable_status()
        assert status["OC"] is True
        assert status["PS"] is True
        assert status["OT"] is False

    def test_float_formatted_nr1_response_tolerated(self):
        """Néhány firmware "13.0"-t adhat vissza NR1 helyett -- a driver
        toleráns erre (l. a többi readback float()-parse mintája)."""
        load, res = make_load()
        res.query.return_value = "13.0"  # bit0(VF)+bit2(RS)+bit3(OP)
        status = load.read_questionable_status()
        assert status["VF"] is True
        assert status["RS"] is True
        assert status["OP"] is True


class TestCurrentMode:
    def test_set_mode_cc_sends_func_curr(self):
        load, res = make_load()
        load.set_mode_cc()
        assert any("CURR" in c for c in writes(res))

    def test_set_current_sends_sour_curr(self):
        """R2-09 (2026-09-15, 13. writer-kör): set_current() mostantól
        egyetlen atomikus SCPI-message-en (query()-n) megy, nem két külön
        write()-on -- l. TestSetCurrentAtomicScpiDispatch a mechanizmus
        részletes tesztjeiért."""
        load, res = make_load()
        load.set_current(1.40)
        assert queries(res) == ["CURR:RANG 1.4;:SOUR:CURR 1.4;*OPC?"]

    def test_set_current_zero(self):
        load, res = make_load()
        load.set_current(0.0)
        assert any("CURR" in c for c in queries(res))


class TestSetCurrentAutoSelectsRange:
    """Review-kör 5, Hardver/Műszervezérlés HW-R5-E (P2, felhasználói döntés
    2026-09-10): set_current() a beállítandó árammal hívja meg CURR:RANG-ot
    is, MIELŐTT a tényleges SOUR:CURR-ot kiküldené -- a műszer a kapott
    értékhez automatikusan a megfelelő tartományt (Low 0-6A / High 0-60A)
    választja (2380 txt 6006-6045. sor).

    R2-09 (2026-09-15, 13. writer-kör): a korábbi 2 külön write() most
    egyetlen atomikus SCPI-message (query()), a sorrend a message-en
    BELÜLI pozícióval bizonyítható, nem külön hívásokkal."""

    def test_range_write_precedes_current_write(self):
        load, res = make_load()
        load.set_current(2.5)
        msg = queries(res)[-1]
        assert msg.index("CURR:RANG") < msg.index("SOUR:CURR")

    def test_low_current_sends_matching_range_value(self):
        """Kis áram (a Low 0-6A tartományba eső) esetén a CURR:RANG a
        pontos beállítandó értéket kapja -- a műszer ebből MAGA választ
        Low-t (a legjobb felbontás elve, 2380 txt 6013-6014. sor)."""
        load, res = make_load()
        load.set_current(2.5)
        assert "CURR:RANG 2.5" in queries(res)[-1]

    def test_high_current_sends_matching_range_value(self):
        """Nagy áram (pl. IEC DC belső ellenállás Ib=20×I10 pontja egy
        nagyobb pakknál) esetén ugyanígy a pontos érték megy ki -- a
        műszer ebből választ High-ot, mert 6A fölött csak a High
        tartomány éri el."""
        load, res = make_load()
        load.set_current(12.4)
        assert queries(res)[-1] == "CURR:RANG 12.4;:SOUR:CURR 12.4;*OPC?"


class TestSetCurrentAtomicScpiDispatch:
    """Teljes kódbázis review 2. kör (2026-09-15, R2-09, 13. writer-kör,
    DFAM-039 4. érintése): set_current() korábban 2 külön _write()
    hívással küldte ki a CURR:RANG/SOUR:CURR párt -- a _write()/_query()
    minden kísérlet ELŐTT feltétel nélkül Device Clear-t küld, ami
    eldobhatja a szekvencia korábban kiküldött, még fel nem dolgozott
    tagját. Javítás: _apply_config_atomic() egyetlen, ";:" root
    specifierrel összefűzött, *OPC?-vel szinkronizált SCPI message-ben
    küldi mindkét parancsot."""

    def test_set_current_sends_single_atomic_message(self):
        load, res = make_load()
        load.set_current(2.5)
        assert len(queries(res)) == 1
        assert writes(res) == []

    def test_set_current_uses_root_specifier_between_units(self):
        """A ';:' (root specifier) NEM stílus -- a 2380 kézikönyv szerint
        enélkül a műszer 'CURR:SOUR:CURR'-ként (érvénytelen head path)
        értelmezné a második parancsot."""
        load, res = make_load()
        load.set_current(2.5)
        assert queries(res)[-1] == "CURR:RANG 2.5;:SOUR:CURR 2.5;*OPC?"

    def test_set_current_is_opc_synchronized(self):
        load, res = make_load()
        load.set_current(2.5)
        assert queries(res)[-1].endswith("*OPC?")
        assert res.query.called
        assert not res.write.called

    def test_set_current_transport_failure_raises_not_swallowed(self):
        load, res = make_load()
        res.query.side_effect = Exception("simulated transport failure")
        with pytest.raises(InstrumentTimeoutError):
            load.set_current(2.5)
        assert res.query.call_count == load.max_retries

    def test_set_current_clears_resource_before_the_whole_sequence_only(self):
        """A family gyökerének közvetlen bizonyítéka: egy sikeres
        set_current() hívás EGYETLEN Device Clear-t indít (az atomikus
        message ELŐTT), nem kettőt (a régi, 2-write kódon ez 2 lett
        volna -- mindkét _write() saját clear_first=True-val hívott)."""
        load, res = make_load()
        load.set_current(2.5)
        assert res.clear.call_count == 1


class TestMeasurement:
    def test_measure_voltage_returns_float(self):
        load, res = make_load()
        res.query.return_value = "+1.24000000E+001"
        result = load.measure_voltage()
        assert isinstance(result, float)
        assert abs(result - 12.4) < 0.01

    @pytest.mark.parametrize(
        "reply, expected",
        [("+1.40000000E+000", 1.4), ("+2.75000000E-001", 0.275)],
    )
    def test_measure_current_returns_float(self, reply, expected):
        # R15-B3: a numerikus SCPI-valasz atvitele amperben.
        load, res = make_load()
        res.query.return_value = reply
        result = load.measure_current()
        assert isinstance(result, float)
        assert result == pytest.approx(expected, rel=0, abs=1e-12)

    @pytest.mark.parametrize(
        "reply, expected",
        [("+1.73600000E+001", 17.36), ("+3.41000000E+000", 3.41)],
    )
    def test_measure_power_returns_float(self, reply, expected):
        # R15-B3: a numerikus SCPI-valasz atvitele wattban.
        load, res = make_load()
        res.query.return_value = reply
        result = load.measure_power()
        assert isinstance(result, float)
        assert result == pytest.approx(expected, rel=0, abs=1e-12)

    def test_measure_voltage_queries_meas_volt(self):
        load, res = make_load()
        res.query.return_value = "+1.20000000E+001"
        load.measure_voltage()
        assert any("MEAS:VOLT" in q for q in queries(res))

    def test_measure_current_queries_meas_curr(self):
        load, res = make_load()
        res.query.return_value = "+1.40000000E+000"
        load.measure_current()
        assert any("MEAS:CURR" in q for q in queries(res))

    def test_measure_power_queries_meas_pow(self):
        load, res = make_load()
        res.query.return_value = "+1.73600000E+001"
        load.measure_power()
        assert any("MEAS:POW" in q for q in queries(res))


class TestMeasurementValidation:
    """CLAUDE.md §10.4: aktív kisütés alatti Load readback NaN/Inf/overload
    esete sem maradhat csendben — Python float() ezeket kivétel nélkül
    elfogadja, ezért driver-szintű explicit validáció kell (a
    device_dmm.py::_validate_voltage() mintájára)."""

    def test_nan_voltage_raises_invalid_reading(self):
        load, res = make_load()
        res.query.return_value = "NAN"
        with pytest.raises(InstrumentInvalidReading):
            load.measure_voltage()

    def test_inf_current_raises_invalid_reading(self):
        load, res = make_load()
        res.query.return_value = "INF"
        with pytest.raises(InstrumentInvalidReading):
            load.measure_current()

    def test_overload_9e37_current_raises_invalid_reading(self):
        """SCPI overload kód: 9.9E37 — float-ként rendkívül nagy."""
        load, res = make_load()
        res.query.return_value = "+9.90000000E+037"
        with pytest.raises(InstrumentInvalidReading):
            load.measure_current()

    def test_overload_9e37_power_raises_invalid_reading(self):
        load, res = make_load()
        res.query.return_value = "+9.90000000E+037"
        with pytest.raises(InstrumentInvalidReading):
            load.measure_power()

    def test_negative_overload_voltage_raises_invalid_reading(self):
        load, res = make_load()
        res.query.return_value = "-9.90000000E+037"
        with pytest.raises(InstrumentInvalidReading):
            load.measure_voltage()


class TestBasicScpi:
    def test_idn_queries_idn(self):
        load, res = make_load()
        res.query.return_value = "KEITHLEY,2380-120-60,SN,FW"
        result = load.idn()
        assert "*IDN?" in queries(res)
        assert "KEITHLEY" in result

    def test_reset_sends_rst(self):
        load, res = make_load()
        load.reset()
        assert any("*RST" in c for c in writes(res))

    def test_clear_status_sends_cls(self):
        load, res = make_load()
        load.clear_status()
        assert any("*CLS" in c for c in writes(res))

    def test_check_error_no_error_returns_empty(self):
        load, res = make_load()
        res.query.return_value = '+0,"No error"'
        errors = load.check_error()
        assert errors == []

    def test_check_error_bare_zero_returns_empty(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] A bare
        "0" válasz (device_psu.py-ban már kezelt forma) itt korábban hibaként
        lett volna jelentve — konzisztencia a 3 SCPI driver között."""
        load, res = make_load()
        res.query.return_value = "0"
        errors = load.check_error()
        assert errors == []

    def test_is_connected_true(self):
        load, res = make_load()
        assert load.is_connected() is True

    def test_check_error_includes_last_cmd(self):
        """[regresszió, Teljes kódbázis review 3. kör (2026-08-29), Hardver/
        Műszervezérlés P3 lelet] device_psu.py::check_error() a legutóbb
        kiküldött SCPI parancsot is jelenti diagnosztikai kontextusként —
        itt korábban hiányzott, inkonzisztencia volt a 3 SCPI driver
        között."""
        load, res = make_load()
        load._last_cmd = "CURR 1.500"
        res.query.return_value = '+170,"Invalid command"'
        errors = load.check_error()
        assert len(errors) > 0
        assert "last:" in errors[0]
        assert errors[0] == '+170,"Invalid command" [last: CURR 1.500]'

    def test_query_updates_last_cmd(self):
        """[regresszió, Kör 17 MAX-effort review, R17-18, DFAM-001+
        DFAM-015, 3. testvér egy kétszer már javított mintának --
        l. device_dmm.py/device_psu.py azonos hivatkozású tesztje]
        korábban KIZÁRÓLAG _write() frissítette self._last_cmd-et --
        egy _query()-n keresztül kiadott parancs (pl. egy readback)
        utáni SCPI-hiba a "[last: ...]" diagnosztikai kontextusban a
        LEGUTÓBBI ÍRÁST mutatta volna, nem a ténylegesen hibázó
        lekérdezést."""
        load, res = make_load()
        res.query.return_value = "1.400"
        load._query("MEAS:VOLT?")
        assert load._last_cmd == "MEAS:VOLT?"

    def test_check_error_called_twice_preserves_original_last_cmd(self):
        """[regresszió, Kör 17 MAX-effort review, R17-18] instrument_
        manager.py a hibasort akár TÖBBSZÖR (max 32x/eszköz/tick) üríti
        egy tickben -- a check_error() SAJÁT "SYST:ERR?" queryja MOST,
        hogy a _query() is frissíti self._last_cmd-et, önmagát írná
        felül a mentés/visszaállítás nélkül, a 2. hívástól kezdve már
        "[last: SYST:ERR?]"-t mutatva, elveszítve az EREDETI, hibát
        okozó parancsot."""
        load, res = make_load()
        load._last_cmd = "CURR 1.500"
        res.query.return_value = '+170,"Invalid command"'

        first = load.check_error()
        second = load.check_error()

        assert first[0] == '+170,"Invalid command" [last: CURR 1.500]'
        assert second[0] == '+170,"Invalid command" [last: CURR 1.500]'
        assert "SYST:ERR?" not in second[0]

    def test_is_connected_false_when_not_connected(self):
        load = Keithley2380Load()
        assert load.is_connected() is False


def _make_bare_driver(cls):
    driver = cls()
    res = MagicMock()
    res.query.return_value = '+0,"No error"'
    driver._resource = res
    driver._connected = True
    return driver, res


class TestDriverLastCmdParity:
    """[strukturális prevenció, Kör 17 MAX-effort review, R17-18,
    DFAM-001/DFAM-015 3. előfordulás lezárása] a self._last_cmd
    diagnosztikai mező mindhárom SCPI driveren (DMM/PSU/Load) azonos
    szerződést kell tartson: _query() frissíti, check_error() a SAJÁT
    "SYST:ERR?" hívása körül menti/visszaállítja. A `cls` parametrizált
    lista jelenleg KÉZZEL karbantartott (nem a driver-modulokból
    származtatott) -- egy jövőbeli 4. driver hozzáadásakor a listát
    KÉZZEL kell bővíteni, a teszt maga NEM deríti fel automatikusan az
    új driverosztályt."""

    @pytest.mark.parametrize("cls", [Keysight34465ADMM, Keithley2220PSU, Keithley2380Load])
    def test_query_updates_last_cmd(self, cls):
        driver, res = _make_bare_driver(cls)
        res.query.return_value = "1.400"
        driver._query("MEAS:VOLT?")
        assert driver._last_cmd == "MEAS:VOLT?"

    @pytest.mark.parametrize("cls", [Keysight34465ADMM, Keithley2220PSU, Keithley2380Load])
    def test_check_error_called_twice_preserves_original_last_cmd(self, cls):
        driver, res = _make_bare_driver(cls)
        driver._last_cmd = "CURR 1.500"
        res.query.return_value = '+170,"Invalid command"'

        first = driver.check_error()
        second = driver.check_error()

        assert first[0] == '+170,"Invalid command" [last: CURR 1.500]'
        assert second[0] == '+170,"Invalid command" [last: CURR 1.500]'
        assert "SYST:ERR?" not in second[0]


class TestSup01TransientPulse:
    """SUP01-PULSE-002/004: hardveres tranziens PULSE + trace — additív,
    a kézikönyvben igazolt SCPI szintaxis (2380-120-60-900-01_A_Nov_2015.txt
    6241-6339, 4128-4149, 5606-5622, 5460-5522. sor). Csak driver-szintű
    building block, nincs OcvSocController-be bekötve — HIL validálás
    szükséges bekötés előtt."""

    def test_configure_transient_pulse_sends_pulse_mode(self):
        load, res = make_load()
        res.query.side_effect = lambda cmd: "0" if cmd == "INP?" else "1"
        load.configure_transient_pulse(pulse_A=1.4, baseline_A=0.0, pulse_width_s=30.0)
        assert queries(res) == [
            "INP?",
            (
                "CURR:RANG 1.4;:CURR:TRAN:MODE PULS;:CURR:TRAN:ALEV 1.4;"
                ":CURR:TRAN:BLEV 0;:CURR:TRAN:AWID 30;:TRIG:SOUR BUS;:TRAN 1;*OPC?"
            ),
        ]
        assert writes(res) == []

    def test_configure_transient_pulse_enables_transient_generator(self):
        """[SUP01-PULSE HIL 2026-08-23 lelet, elso valos hardveres futtataskor
        talalva]: a [SOURce:]TRANsient[:STATe] alapertelmezese OFF
        (kezikonyv 5938-5960. sor) - enelkul a FORCe:TRIGger hatastalan,
        a load a nem-tranziens aramszinten marad. Valos HIL derítette ki
        (a trace laposan, lepcso nelkul jott vissza)."""
        load, res = make_load()
        res.query.side_effect = lambda cmd: "0" if cmd == "INP?" else "1"
        load.configure_transient_pulse(pulse_A=1.4, baseline_A=0.0, pulse_width_s=30.0)
        assert queries(res)[-1].split(";:")[-1] == "TRAN 1;*OPC?"

    def test_input_off_disables_transient_generator(self):
        """A tranziens allapot sticky - input_off()-nak vissza kell allitania,
        kulonben a kovetkezo, nem-tranziens hasznalatot meglepné."""
        load, res = make_load()
        res.query.side_effect = lambda cmd: "0" if cmd == "INP?" else "1"
        load.configure_transient_pulse(pulse_A=1.0, baseline_A=0.0, pulse_width_s=1.0)
        load.input_off()
        cmds = writes(res)
        assert any(c.strip().upper() in ("TRAN 0", "TRAN OFF") for c in cmds)

    def test_fire_transient_pulse_sends_force_trigger(self):
        load, res = make_load()
        load.fire_transient_pulse()
        assert any("FORC:TRIG" in c for c in writes(res))

    def test_configure_trace_sends_expected_scpi(self):
        load, res = make_load()
        load.configure_trace(feed="CURR", interval_s=0.001, points=1000)
        assert res.method_calls == [
            ("clear", (), {}),
            ("query", ((
                "TRAC:FEED:CONT NEV;:TRAC:CLE;:TRAC:FEED CURR;"
                ":TRAC:TIM 0.001;:TRAC:POIN 1000;*OPC?"
            ),), {}),
            ("clear", (), {}),
            ("write", ("TRAC:FEED:CONT NEXT",), {}),
        ]

    def test_configure_trace_rejects_invalid_feed(self):
        load, res = make_load()
        with pytest.raises(ValueError):
            load.configure_trace(feed="BOGUS")

    def test_fetch_trace_queries_trac_data(self):
        load, res = make_load()
        res.query.return_value = "1.400,1.399,1.401"
        result = load.fetch_trace()
        assert "TRAC:DATA?" in queries(res)
        assert result == "1.400,1.399,1.401"


class _StatefulTraceCaptureResource:
    """Review-kör 2 (2026-09-08), HW2-08 + MAX verifier follow-up (P1-1/
    P2-2) -- a device_dmm.py azonos "StatefulTimedAcquisitionResource"
    mintáját követi, de a capture kezdetét HELYESEN a `TRAC:FEED:CONT
    NEXT` write-hoz köti, NEM a `FORC:TRIG`-hez (kézikönyv p.97: "When
    NEXT is selected, the storage process starts, fills the buffer and
    then stops") -- ez a köztes `check_error()`/`input_on()`/
    `fire_transient_pulse()` hívásokat is a capture-ablakon BELÜLRE
    helyezi, pontosan a valós `pulse_measurement_service.py::
    _step_hardware_pulse()` sorrendjét követve. 2380-120-60-900-01:
    "Using Device Clear" (p.66/77), GPIB interfész-függvény tábla
    "Device Clear" bejegyzése (p.50: "clear any activity that would
    prevent it from receiving and executing a new command"), `*WAI`
    leírás (p.75: "trigger actions are overlapped... aborted only by
    ... GPIB DCL")."""

    def __init__(self, trace_data: str = "1.0,1.0,1.0") -> None:
        self.clear_count = 0
        self.clears_during_capture = 0
        self._capturing = False
        self._trace_data = trace_data

    def write(self, cmd: str) -> None:
        stripped = cmd.strip()
        if stripped == "TRAC:FEED:CONT NEXT":
            self._capturing = True
        elif stripped == "TRAC:CLE":
            self._capturing = False

    def query(self, cmd: str) -> str:
        if cmd.strip() == "TRAC:DATA?":
            if not self._capturing:
                raise TimeoutError(
                    "TRAC:DATA? with no pending trace capture — "
                    "TRAC:FEED:CONT NEXT was never issued or the "
                    "in-progress capture was aborted (e.g. by an "
                    "intervening Device Clear)."
                )
            self._capturing = False
            return self._trace_data
        return '+0,"No error"'

    def clear(self) -> None:
        self.clear_count += 1
        if self._capturing:
            self.clears_during_capture += 1
        self._capturing = False  # Device Clear elviszi a folyamatban lévő capture-t


class TestFetchTraceDeviceClearInteraction:
    """Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-08 +
    MAX verifier follow-up (P1-1) -- device_dmm.py CF-03/HW-R1-01/
    INSTR-DMM-CLEAR-001 testvér-hibája: `_query()`/`_write()` korábban
    feltétel nélkül hívott `_clear_resource()`-t MINDEN kísérlet előtt.
    Az eredeti HW2-08 fix csak `fetch_trace()`-t javította, mert
    tévesen a `FORC:TRIG`-hez kötötte a capture kezdetét -- valójában a
    `configure_trace()` utolsó write-ja (`TRAC:FEED:CONT NEXT`) MÁR
    elindítja azt, tehát a KÖZTES `check_error()`/`input_on()`/
    `fire_transient_pulse()` hívások is veszélyeztették volna a
    capture-t. Ez a teszt a TELJES, valós hívási sorrendet vezérli."""

    def _run_real_call_sequence(self, load: Keithley2380Load) -> str:
        """A pulse_measurement_service.py::_step_hardware_pulse() Load-
        specifikus hívási sorrendjének pontos utánzata, a capture
        elindítása (configure_trace()) és lekérdezése (fetch_trace())
        között lévő MINDHÁROM köztes hívással."""
        load.configure_trace(feed="CURR", interval_s=0.001, points=1000)
        errors = load.check_error(clear_first=False)
        assert errors == []
        load.input_on(clear_first=False)
        load.fire_transient_pulse(clear_first=False)
        return load.fetch_trace()

    def test_fetch_trace_after_full_sequence_returns_the_captured_trace(self):
        load = Keithley2380Load()
        res = _StatefulTraceCaptureResource(trace_data="1.40,1.39,1.41")
        load._resource = res
        load._connected = True

        result = self._run_real_call_sequence(load)

        assert result == "1.40,1.39,1.41"

    def test_full_sequence_does_not_clear_inside_the_capture_window(self):
        load = Keithley2380Load()
        res = _StatefulTraceCaptureResource(trace_data="2.00,2.01,2.02")
        load._resource = res
        load._connected = True

        self._run_real_call_sequence(load)

        assert res.clears_during_capture == 0, (
            "A capture-ablakon BELÜL (TRAC:FEED:CONT NEXT UTÁN, TRAC:DATA? "
            "ELŐTT) egyetlen hívásnak (check_error/input_on/"
            "fire_transient_pulse/fetch_trace) sem szabad Device Clear-t "
            "küldenie -- ha ez megtörténik, a capture elveszik, és a "
            "retry-hurok TimeoutError-ral bukik el (nincs új "
            "TRAC:FEED:CONT NEXT). A configure_trace() SAJÁT, capture-"
            "indítás ELŐTTI write-jai (TRAC:CLE/FEED/TIM/POIN) továbbra is "
            "clear-elhetnek, az ártalmatlan -- ezért a clear_count (nem "
            "clears_during_capture) itt szándékosan nem 0."
        )

    def test_check_error_outside_the_capture_window_still_clears_by_default(self):
        """A capture-ablakon KÍVÜLI hívóhelyek (pl. connect()-kori
        diagnosztika) nem kapnak `clear_first=False`-t -- a
        `check_error()` alapértelmezése változatlanul `True`."""
        load, res = make_load()
        load.check_error()
        assert res.clear.call_count == 1


class TestRetryLogic:
    def test_raises_after_max_retries(self):
        """[MÓDOSÍTVA, R1 B2 F-03, 2026-10-25] a generikus retry-kimerülés
        hordozója korábban az input_on() volt -- az mostantól szándékosan
        egylövéses (rules/10-safety.md 6. pont, l. TestR1F03InputOnSingle
        Attempt), ezért a teszt egy NEM energizáló írásra (reset() ->
        "*RST") került át, explicit kísérletszám-asserttel. Szándék-
        megőrzés, nem gyengítés: a retry-kimerülés továbbra is kivételt
        dob, és most a kísérletek száma is ellenőrzött."""
        load, res = make_load()
        res.write.side_effect = Exception("persistent error")
        with pytest.raises(InstrumentTimeoutError):
            load.reset()
        assert res.write.call_count == load.max_retries

    def test_write_without_connect_raises_clear_error_not_attribute_error(self):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] l.
        device_psu.py azonos teszt docstringje."""
        load = Keithley2380Load()
        with pytest.raises(InstrumentTimeoutError, match="nincs aktív kapcsolat"):
            load._write("TEST")

    def test_query_without_connect_raises_clear_error_not_attribute_error(self):
        load = Keithley2380Load()
        with pytest.raises(InstrumentTimeoutError, match="nincs aktív kapcsolat"):
            load._query("SYST:ERR?")

    def test_write_clears_resource_before_every_attempt_including_first(self):
        """[regresszió, teljes kódbázis review 2. kör (2026-08-28) + 3. kör
        (2026-08-29) P1 kiegészítés] l. device_psu.py azonos teszt
        docstringje."""
        load, res = make_load()
        call_count = 0

        def flaky_write(cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("comm error")

        res.write.side_effect = flaky_write
        load._write("TEST")

        assert res.clear.call_count == 2


class TestReleaseAudit20260823InputOffAtomicFlag:
    """Release-audit 2026-08-23 (2.15): input_commanded_on frissítése
    közvetlenül a biztonságkritikus INP OFF write UTÁN történjen, a
    nem-kritikus TRAN 0 ELŐTT — ha a TRAN 0 elhasal, a flag akkor is
    tükrözze a fizikailag már megtörtént OFF-ot (ne maradjon hamisan
    True, ami egy KÉSŐBBI legitim PSU-bekapcsolást hamis
    CONCURRENT_PSU_LOAD_ON FAULT-tal blokkolhatna)."""

    def test_flag_false_when_tran_0_fails_after_successful_input_off(self):
        """[FRISSÍTVE, review-kör 5, Hardver/Műszervezérlés HW5-06 (P3)]:
        a TRAN 0 hibája mostantól NEM szivárog ki input_off()-ból (l. ott
        a kommentet) -- korábban ez a teszt egy pytest.raises(
        InstrumentTimeoutError)-t várt el, ami a hívóknál (mind a 11
        controller-hívóhely) megkülönböztethetetlen "LOAD_OFF_FAILED"
        jelzést okozott egy pusztán kozmetikus hiba miatt, holott a
        biztonságkritikus INP OFF (és vele a flag) már sikeresen
        lezajlott."""
        load, res = make_load()
        load.input_on()
        assert load.input_commanded_on is True

        def _write_side_effect(cmd: str) -> None:
            if cmd == "TRAN 0":
                raise Exception("SCPI timeout")

        res.write.side_effect = _write_side_effect

        load.input_off()  # NEM dob kivételt -- a TRAN 0 hibája elnyelve

        assert load.input_commanded_on is False

    def test_flag_false_on_normal_successful_input_off(self):
        load, res = make_load()
        load.input_on()
        load.input_off()
        assert load.input_commanded_on is False


class TestCf13CommandedFlagOnCriticalWriteFailure:
    """Governance-v2 review-kör 1 (Hardver reviewer, CF-13/HW-R1-04):
    a fenti (2026-08-23) fix csak a NEM-kritikus TRAN 0 hibáját védte ki —
    ha maga a KRITIKUS "INP OFF" write hibázik (pl. max_retries kimerül),
    a kivétel a `self.input_commanded_on = False` sor ELŐTT szakítja meg
    `input_off()`-ot, és a flag a hívás előtti (jellemzően True) értéken
    ragad. Mind a 6 controller `emergency_stop()`-ja KÖZVETLENÜL
    `input_off()`-ot hívja (nem a már védett `safe_off()`-ot), tehát ez a
    leggyakoribb kódúton fordulhat elő. Stale True flag → egy KÉSŐBBI,
    legitim PSU-bekapcsolást hamis CONCURRENT_PSU_LOAD_ON FAULT-tal
    blokkolna. Ez a teszt a JELENLEGI kódon bukik."""

    def test_flag_false_when_critical_input_off_write_fails(self):
        """[Kör 19 Batch D2, HW19-02 frissítés] a KRITIKUS "INP OFF" mostantól
        `_query(...;*OPC?)`-n megy -- a hibát a query()-n kell szimulálni,
        NEM a write()-on (a write() innentől csak a nem-kritikus TRAN 0-t
        érinti, l. TestReviewRound5Hw506TranZeroFailureNotClassifiedAsLoadOffFailed)."""
        load, res = make_load()
        load.input_on()
        assert load.input_commanded_on is True

        res.query.side_effect = Exception("SCPI timeout")

        with pytest.raises(InstrumentTimeoutError):
            load.input_off()

        assert load.input_commanded_on is False


class TestReviewRound5Hw506TranZeroFailureNotClassifiedAsLoadOffFailed:
    """[regresszió, review-kör 5, Hardver/Műszervezérlés HW5-06 (P3)] a
    nem-kritikus TRAN 0 write hibája korábban ugyanúgy kiszivárgott
    input_off()-ból, mint a kritikus INP OFF hibája -- mind a 11 hívóhely
    (controllerek + pulse_measurement_service.py) egy generikus `except
    Exception` mögött megkülönböztethetetlen "LOAD_OFF_FAILED" jelzést
    adott, holott a bemenet fizikailag már biztonságosan OFF volt."""

    def test_input_off_succeeds_when_only_tran_0_fails(self):
        load, res = make_load()
        load.input_on()

        def _write_side_effect(cmd: str) -> None:
            if cmd == "TRAN 0":
                raise Exception("SCPI timeout")

        res.write.side_effect = _write_side_effect

        # A KRITIKUS INP OFF sikeres -- input_off() ne dobjon kivételt
        # pusztán a kozmetikus TRAN 0 hibája miatt (ami egy hívónak most
        # már megkülönböztethetetlenül "LOAD_OFF_FAILED"-ként tűnne fel).
        load.input_off()

        assert load.input_commanded_on is False

    def test_critical_input_off_failure_still_propagates(self):
        """Ellenpróba: a VALÓDI, kritikus INP OFF hiba továbbra is
        kiszivárog -- a fix kizárólag a TRAN 0 ágat érinti. [Kör 19
        Batch D2, HW19-02 frissítés] a kritikus hiba mostantól query()-n
        szimulálandó, l. a fenti osztály azonos frissítésű tesztje."""
        load, res = make_load()
        load.input_on()
        res.query.side_effect = Exception("SCPI timeout")

        with pytest.raises(InstrumentTimeoutError):
            load.input_off()


class TestMockLoadSafeOffConformance:
    """[regresszió, Kör 17 MAX-effort review, R17-21, DFAM-005+
    DFAM-015] MockLoad.safe_off() a hibaágon korábban NEM tükrözte a
    valós Keithley2380Load.safe_off() feltétel nélküli flag-nullázását
    -- egy hamis komment állította ennek ellenkezőjét. Ez a teszt
    mindkét implementációt UGYANAZZAL a hívássorozattal hajtja végre
    és összeveti az eredményt (mock_psu.py::safe_off() már helyesen
    modellezi ugyanezt a mintát a PSU oldalán)."""

    def test_mock_and_real_driver_agree_on_commanded_flag_after_failed_safe_off(self):
        """[Kör 19 Batch D2, HW19-02 frissítés] a valós driveren a kritikus
        "INP OFF" mostantól `_query()`-n megy -- a hibát ott kell
        szimulálni, hogy a mock/valós összevetés továbbra is azonos
        hibaágat gyakoroljon."""
        from Prog.tests.mock_drivers.mock_load import MockLoad

        mock = MockLoad(raise_on_safe_off=True)
        mock.input_commanded_on = True
        mock_result = mock.safe_off()

        real, res = make_load()
        real.input_commanded_on = True
        res.query.side_effect = Exception("simulated comm failure")
        real_result = real.safe_off()

        assert mock_result is False
        assert real_result is False
        assert mock.input_commanded_on is False
        assert real.input_commanded_on is False


class TestR1F03InputOnSingleAttempt:
    """[regresszió, R1 B2 F-03, 2026-10-25, rules/10-safety.md 6. pont] az
    "INP ON" korábban a retry-zó `_write()`-on ment: egy átmeneti/elveszett
    write-hiba után a driver VAKON újraküldte (clear_first=False esetén a
    retry Device Clear-rel, a trace-capture ablakon belül), és a második
    siker eltüntette a bizonytalanságot a hívó elől. Mostantól egylövéses
    (single_attempt=True); a kivétel propagál, a flag konzervatívan True.
    Az "INP OFF;*OPC?" (query út) és a többi írás retry-ja VÁLTOZATLAN."""

    def test_input_on_lost_ack_sent_once_and_raises(self):
        load, res = make_load()
        state = {"failed": False}

        def _write_side_effect(cmd: str) -> None:
            if cmd == "INP ON" and not state["failed"]:
                state["failed"] = True
                raise TimeoutError("simulated lost write")

        res.write.side_effect = _write_side_effect
        with pytest.raises(InstrumentTimeoutError):
            load.input_on()
        assert writes(res).count("INP ON") == 1
        assert load.input_commanded_on is True

    def test_input_on_clear_first_false_single_attempt_sends_no_clear(self):
        """A trace-capture úton (clear_first=False) a régi retry a 2.
        kísérlettől Device Clear-t küldött -- egylövéses módban sem clear,
        sem második írás nincs."""
        load, res = make_load()
        res.write.side_effect = Exception("simulated timeout")
        with pytest.raises(InstrumentTimeoutError):
            load.input_on(clear_first=False)
        assert res.clear.call_count == 0
        assert res.write.call_count == 1
        assert load.input_commanded_on is True

    def test_single_attempt_write_with_clear_first_clears_once(self):
        load, res = make_load()
        res.write.side_effect = Exception("simulated timeout")
        with pytest.raises(InstrumentTimeoutError):
            load._write("X", single_attempt=True)
        assert res.write.call_count == 1
        assert res.clear.call_count == 1

    def test_default_write_still_retries_max_retries_times(self):
        load, res = make_load()
        res.write.side_effect = Exception("simulated timeout")
        with pytest.raises(InstrumentTimeoutError):
            load._write("X")
        assert res.write.call_count == load.max_retries

    def test_input_off_still_retries_after_first_failure(self):
        """Guard: a safety-kritikus Load OFF a TÉNYLEGES query úton
        ("INP OFF;*OPC?") továbbra is retry-zik."""
        load, res = make_load()
        load.input_commanded_on = True
        state = {"failed": False}

        def _query_side_effect(cmd: str) -> str:
            if cmd == "INP OFF;*OPC?" and not state["failed"]:
                state["failed"] = True
                raise TimeoutError("simulated lost *OPC? reply")
            return "1"

        res.query.side_effect = _query_side_effect
        load.input_off()  # a második kísérlet sikeres -> nincs kivétel
        assert queries(res).count("INP OFF;*OPC?") == 2
        assert load.input_commanded_on is False

    def test_max_retries_unchanged(self):
        assert Keithley2380Load().max_retries == 3


class _StickyLoadResource:
    """[R2-B2] SCPI-szintű, állapottartó VISA-double (DFAM-005): az INP /
    INP:SHOR / INP:TIM állapot a kapcsolódáson át megmarad (2380 txt
    5814-5858, Default OFF, Returns 0|1). Worst-case DFAM-039 modell: a
    Device Clear a még fel nem dolgozott (szinkronizálatlan) write-ot
    eldobja; egy query a teljes üzenetét sorrendben végrehajtja.
    FINAL C2: a bemenet alapértelmezésben ON-ból indul (`inp=True`), hogy a
    végső OFF-állapotot igazoló assertek diszkriminálók legyenek."""

    def __init__(self, inp=True, short=True, timer=True, reject=(), timeout_on=(), replies=None):
        self.state = {"INP": inp, "INP:SHOR": short, "INP:TIM": timer}
        self.reject = set(reject)
        self.timeout_on = set(timeout_on)
        self.replies = dict(replies or {})
        self.pending = None
        self.log = []
        self.timeout = None
        self.closed = False

    @staticmethod
    def _units(msg):
        return [u.lstrip(":").strip() for u in msg.split(";")]

    def clear(self):
        self.log.append(("DCL", ""))
        self.pending = None

    def close(self):
        self.closed = True

    def _maybe_timeout(self, msg):
        if any(u in self.timeout_on for u in self._units(msg)):
            raise OSError("VI_ERROR_TMO")

    def _flush(self):
        if self.pending is not None:
            for unit in self._units(self.pending):
                self._execute(unit)
            self.pending = None

    def _execute(self, unit):
        if not unit or unit in self.reject:
            return
        head, _, arg = unit.partition(" ")
        if head.upper() in self.state and arg:
            self.state[head.upper()] = arg.strip().upper() in ("1", "ON")

    def _answer(self, unit):
        if unit in self.replies:
            return self.replies[unit]
        if unit == "*OPC?":
            return "1"
        if unit == "SYST:ERR?":
            return NO_ERROR_REPLY
        key = unit[:-1].upper()
        if key in self.state:
            return "1" if self.state[key] else "0"
        return "0"

    def write(self, msg):
        self.log.append(("W", msg))
        self._maybe_timeout(msg)
        self._flush()
        self.pending = msg

    def query(self, msg):
        self.log.append(("Q", msg))
        self._maybe_timeout(msg)
        self._flush()
        out = None
        for unit in self._units(msg):
            if unit.endswith("?"):
                out = self._answer(unit)
            else:
                self._execute(unit)
        return out

    def msgs(self, kind):
        return [m for k, m in self.log if k == kind]


def _connect_sticky(res):
    load = Keithley2380Load()
    load.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
    return load


# [R2-B2] A nyolc fail-closed hibaváltozat -- a driver- és a manager-szintű
# regresszió KÖZÖS listája.
_R2B2_FAIL_CASES = [
    ({"reject": {"INP:SHOR OFF"}}, InstrumentCommandError),
    ({"reject": {"INP:TIM OFF"}}, InstrumentCommandError),
    ({"timeout_on": {"INP:SHOR OFF"}}, InstrumentTimeoutError),
    ({"timeout_on": {"INP:SHOR?"}}, InstrumentTimeoutError),
    ({"timeout_on": {"INP:TIM?"}}, InstrumentTimeoutError),
    ({"replies": {"INP:SHOR?": ""}}, InstrumentInvalidReading),
    ({"replies": {"INP:SHOR?": "2"}}, InstrumentInvalidReading),
    ({"replies": {"INP:TIM?": NO_ERROR_REPLY}}, InstrumentInvalidReading),
]
_R2B2_FAIL_IDS = [
    "short_rejected", "timer_rejected", "atomic_timeout", "short_readback_timeout",
    "timer_readback_timeout", "short_empty", "short_undocumented_2", "timer_wrong_reply",
]


class TestR2B2ConnectNeutralizesInheritedShortAndTimer:
    """[regresszió, Claude-Codex loop R2 B2: R2-HW-01, DFAM-032 inherited-
    state axis, DFAM-039] connect() a *CLS és az elsődleges INP OFF után
    egyetlen szinkronizált üzenettel kikapcsolja az öröklött INP:SHOR /
    INP:TIM állapotot, és readbackkel igazolja. FAIL-CLOSED: elutasítás,
    timeout, hibás válasz vagy megmaradó ON -> connect() kivételt dob (a
    meglévő connect_all() rollback fut), INPUT ON nem megy ki. Unit/mock
    szint; a fizikai megmaradás és a timer-lejárat HIL_PENDING."""

    _ATOMIC = "INP:SHOR OFF;:INP:TIM OFF;*OPC?"

    def test_inherited_short_and_timer_off_after_connect_and_energizing_chain(self):
        res = _StickyLoadResource(short=True, timer=True)
        load = _connect_sticky(res)
        load.set_mode_cc()
        load.set_current(0.7)
        load.input_on()
        assert res.state["INP:SHOR"] is False
        assert res.state["INP:TIM"] is False

    def test_neutralization_is_one_synchronized_message_then_readback(self):
        res = _StickyLoadResource()
        _connect_sticky(res)
        qs = res.msgs("Q")
        assert self._ATOMIC in qs
        assert not any(w.upper().startswith(("INP:SHOR", "INP:TIM")) for w in res.msgs("W"))
        i = qs.index(self._ATOMIC)
        assert qs.index("*CLS;*OPC?") < qs.index("INP OFF;*OPC?") < i
        assert i < qs.index("INP:SHOR?")
        assert i < qs.index("INP:TIM?")

    def test_neutralization_precedes_best_effort_von_and_mode_writes(self):
        """A meglévő best-effort VOLT:ON 0 / SOUR:FUNC:MODE FIX írások UTÁN
        nem kerülhet új Device Clear-rel kezdődő hívás a connect()-en belül."""
        res = _StickyLoadResource()
        _connect_sticky(res)
        log = [m for _k, m in res.log]
        assert log.index(self._ATOMIC) < log.index("VOLT:ON 0") < log.index("SOUR:FUNC:MODE FIX")
        assert log.index("INP:TIM?") < log.index("VOLT:ON 0")
        # R2-B2 verifier N-2: a connect() utolsó két hívása a best-effort írás;
        # utánuk nem lehet második semlegesítés vagy readback.
        calls = [e for e in res.log if e[0] != "DCL"]
        assert calls[-2:] == [("W", "VOLT:ON 0"), ("W", "SOUR:FUNC:MODE FIX")]

    def test_already_off_control_connects(self):
        res = _StickyLoadResource(short=False, timer=False)
        load = _connect_sticky(res)
        assert load.is_connected()
        assert load.input_commanded_on is False

    def test_readback_with_visa_line_terminator_is_accepted(self):
        """R2-B2 verifier N-2: a valós VISA sorvégjelet is ad; a .strip()
        utáni pontos "0" elfogadott (a .strip() nélküli mutáns itt bukik)."""
        res = _StickyLoadResource(
            replies={"INP:SHOR?": "0\n", "INP:TIM?": " 0\r\n"}
        )
        load = _connect_sticky(res)
        assert load.is_connected()

    @pytest.mark.parametrize("reply", ["OFF", "+0", "0.0"])
    def test_undocumented_off_like_reply_is_rejected(self, reply):
        """R2-B2 verifier N-2: csak a dokumentált "0" (2380 txt 5814-5858:
        Returns 0|1) fogadható el; OFF-szerű, de nem dokumentált válasz is
        InstrumentInvalidReading (szigorú parse)."""
        res = _StickyLoadResource(replies={"INP:SHOR?": reply})
        with pytest.raises(InstrumentInvalidReading):
            _connect_sticky(res)

    @pytest.mark.parametrize("res_kwargs, exc", _R2B2_FAIL_CASES, ids=_R2B2_FAIL_IDS)
    def test_fail_closed_connect_raises_and_never_energizes(self, res_kwargs, exc):
        res = _StickyLoadResource(**res_kwargs)
        load = Keithley2380Load()
        with pytest.raises(exc):
            load.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        all_msgs = [m for _k, m in res.log]
        assert "INP OFF;*OPC?" in res.msgs("Q")
        assert load.input_commanded_on is False
        assert res.state["INP"] is False
        assert not any(m.upper().startswith("INP ON") for m in all_msgs)


class TestR2B2ManagerConnectAllFailClosedFromOnState:
    """[regresszió, R2 B2, FINAL C2] A fail-closed garancia a manager/GUI
    úton: a VALÓDI `InstrumentManager.connect_all()` + valódi
    `Keithley2380Load` (állapottartó VISA-double, a bemenet ON-ból indul).
    Mind a nyolc hibaváltozatnál: a kivétel típusa változatlanul propagál
    (a `_InstrumentConnectWorker.run()` `except Exception` ága ebből EGY
    `connect_failed`-et ad, `connect_ready` nélkül -- a worker-szint itt
    szándékosan nincs: a nem-GUI suite PySide6-mentes); a rollback a Load-on
    safe_off + disconnect, a PSU-n safe_off + disconnect; a DMM-ekhez nem
    jut el; a Load bemenete a double-ban OFF, INP ON sosem ment ki.
    Unit/mock szint, nem HIL."""

    @pytest.mark.parametrize("res_kwargs, exc", _R2B2_FAIL_CASES, ids=_R2B2_FAIL_IDS)
    def test_connect_all_rolls_back_and_never_energizes(self, monkeypatch, res_kwargs, exc):
        import sys
        import types

        from Prog.src.instrument_manager import InstrumentConfig, InstrumentManager

        res = _StickyLoadResource(**res_kwargs)
        assert res.state["INP"] is True
        fake_rm = MagicMock(open_resource=lambda *_: res)
        # A connect_all() rm nélkül hívja a driver connect()-jét -- valódi
        # VISA helyett a double-t adó pyvisa-modul.
        monkeypatch.setitem(
            sys.modules, "pyvisa", types.SimpleNamespace(ResourceManager=lambda: fake_rm),
        )
        psu, dmm_v, dmm_t = MagicMock(name="psu"), MagicMock(name="dmm_v"), MagicMock(name="dmm_t")
        load = Keithley2380Load()
        manager = InstrumentManager(psu, load, dmm_v, dmm_t)

        with pytest.raises(exc):
            manager.connect_all(InstrumentConfig())

        # PSU: csatlakozott, majd a rollback safe_off + disconnect.
        psu.connect.assert_called_once()
        psu.safe_off.assert_called_once_with()
        psu.disconnect.assert_called_once_with()
        # A hibás Load után a DMM-ekhez nem jut el.
        dmm_v.connect.assert_not_called()
        dmm_t.connect.assert_not_called()
        # Load: a connect() saját INP OFF-ja + a rollback safe_off() INP OFF-ja.
        assert res.msgs("Q").count("INP OFF;*OPC?") == 2
        assert res.closed is True
        assert load.is_connected() is False
        assert load.input_commanded_on is False
        assert res.state["INP"] is False
        assert not any(m.upper().startswith("INP ON") for _k, m in res.log)


class _R15B2TraceResource(_StickyLoadResource):
    """R15-B2: H2 sticky control; kesleltetett write = DFAM-039 modell."""

    def __init__(self, *, control="NEVER", deferred=False, timeout_on=()):
        super().__init__(inp=False, short=False, timer=False, timeout_on=timeout_on)
        self.control = control
        self.deferred = deferred
        self.feed = "CURRENT"
        self.interval = 0.001
        self.points = 1000
        self.errors = []
        self.capturing = False
        self.clears_during_capture = 0

    def clear(self):
        if self.capturing:
            self.clears_during_capture += 1
        self.capturing = False
        super().clear()  # control, konfiguracio es hibasor megmarad.

    def write(self, msg):
        if self.deferred:
            return super().write(msg)
        self.log.append(("W", msg))
        self._maybe_timeout(msg)
        for unit in self._units(msg):
            self._execute(unit)

    def _execute(self, unit):
        head, _, arg = unit.partition(" ")
        if head == "TRAC:FEED:CONT":
            self.control = "NEVER" if arg in ("NEV", "NEVER") else arg
            self.capturing = self.control == "NEXT"
        elif head == "TRAC:CLE":
            self.capturing = False
        elif head == "TRAC:FEED":
            self.feed = {"CURR": "CURRENT", "VOLT": "VOLTAGE", "TWO": "TWO"}[arg]
        elif head == "TRAC:TIM":
            self.interval = float(arg)
        elif head == "TRAC:POIN":
            if self.control == "NEXT":
                self.errors.append('-221,"Settings conflict"')
            else:
                self.points = int(arg)
        else:
            super()._execute(unit)

    def _answer(self, unit):
        if unit == "TRAC:FEED:CONT?":
            return self.control
        if unit == "TRAC:FEED?":
            return self.feed
        if unit == "TRAC:TIM?":
            return str(self.interval)
        if unit == "TRAC:POIN?":
            return str(self.points)
        if unit == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else NO_ERROR_REPLY
        if unit == "TRAC:DATA?":
            if not self.capturing:
                raise TimeoutError("no trace capture")
            self.capturing = False
            return "1.40,1.39,1.41"
        return super()._answer(unit)


class TestR14B1R15B2TraceReconfiguration:
    """R15-B2: H2 reprodukcio, teljes uzenetek es capture-ablak."""

    def test_fake_matches_h2_and_device_clear_preserves_control(self):
        res = _R15B2TraceResource(control="NEXT")
        res.clear()
        assert res.query("TRAC:FEED:CONT?") == "NEXT"
        res.write("TRAC:CLE")
        res.write("TRAC:POIN 2")
        assert res.query("SYST:ERR?") == '-221,"Settings conflict"'
        assert res.query("TRAC:POIN?") == "1000"
        res.write("TRAC:FEED:CONT NEV")
        res.clear()
        assert res.query("TRAC:FEED:CONT?") == "NEVER"
        res.write("TRAC:POIN 2")
        res.clear()
        assert res.query("TRAC:POIN?") == "2"
        assert res.query("SYST:ERR?") == NO_ERROR_REPLY

    @pytest.mark.parametrize("deferred", [False, True])
    @pytest.mark.parametrize("feed,long_feed", [
        ("CURR", "CURRENT"), ("VOLT", "VOLTAGE"), ("TWO", "TWO"),
    ])
    def test_repeated_configuration_preserves_values_and_capture(self, deferred, feed, long_feed):
        res = _R15B2TraceResource(deferred=deferred)
        load = _connect_sticky(res)
        transcripts = []
        for points, interval, message in [
            (7, 0.002,
             "TRAC:FEED:CONT NEV;:TRAC:CLE;:TRAC:FEED " + feed
             + ";:TRAC:TIM 0.002;:TRAC:POIN 7;*OPC?"),
            (11, 0.003,
             "TRAC:FEED:CONT NEV;:TRAC:CLE;:TRAC:FEED " + feed
             + ";:TRAC:TIM 0.003;:TRAC:POIN 11;*OPC?"),
        ]:
            res.log.clear()
            load.configure_trace(feed=feed, interval_s=interval, points=points)
            # A readback elott a tenylegesen alkalmazott elokeszites is kesz.
            assert (res.feed, res.interval, res.points) == (long_feed, interval, points)
            transcripts.append((list(res.log), message))
            assert load._query("TRAC:FEED:CONT?", clear_first=False) == "NEXT"
            assert load.check_error(clear_first=False) == []
            clears_before = res.clears_during_capture
            load.input_on(clear_first=False)
            load.fire_transient_pulse(clear_first=False)
            assert load.fetch_trace() == "1.40,1.39,1.41"
            assert res.clears_during_capture == clears_before
            assert res.control == "NEXT"
        for transcript, message in transcripts:
            assert transcript == [
                ("DCL", ""), ("Q", message),
                ("DCL", ""), ("W", "TRAC:FEED:CONT NEXT"),
            ]

    @pytest.mark.parametrize("deferred", [False, True])
    def test_new_driver_connection_handles_inherited_next(self, deferred):
        res = _R15B2TraceResource(deferred=deferred)
        previous = _connect_sticky(res)
        previous.configure_trace(points=2)
        assert previous._query("TRAC:FEED:CONT?", clear_first=False) == "NEXT"
        previous.disconnect()
        res.closed = False
        load = _connect_sticky(res)
        assert load is not previous
        assert res.control == "NEXT"
        assert res.state["INP"] is False
        res.log.clear()
        load.configure_trace(feed="VOLT", interval_s=0.004, points=3)
        assert (res.feed, res.interval, res.points) == ("VOLTAGE", 0.004, 3)
        assert res.log == [
            ("DCL", ""),
            ("Q", (
                "TRAC:FEED:CONT NEV;:TRAC:CLE;:TRAC:FEED VOLT;"
                ":TRAC:TIM 0.004;:TRAC:POIN 3;*OPC?"
            )),
            ("DCL", ""),
            ("W", "TRAC:FEED:CONT NEXT"),
        ]
        assert load._query("TRAC:FEED:CONT?", clear_first=False) == "NEXT"
        assert load.check_error(clear_first=False) == []
        assert res.state["INP"] is False

    @pytest.mark.parametrize("unit", [
        "TRAC:FEED:CONT NEV", "TRAC:CLE", "TRAC:FEED CURR",
        "TRAC:TIM 0.002", "TRAC:POIN 7",
    ])
    def test_preparation_timeout_never_sends_next(self, unit):
        res = _R15B2TraceResource(timeout_on={unit})
        load = _connect_sticky(res)
        res.log.clear()
        with pytest.raises(InstrumentTimeoutError):
            load.configure_trace(interval_s=0.002, points=7)
        assert res.log == [
            ("DCL", ""),
            ("Q", (
                "TRAC:FEED:CONT NEV;:TRAC:CLE;:TRAC:FEED CURR;"
                ":TRAC:TIM 0.002;:TRAC:POIN 7;*OPC?"
            )),
        ] * load.max_retries
        assert res.state["INP"] is False
        assert res.control == "NEVER"
        assert res.capturing is False


class TestR16B9RangeSemantics:
    """Plafon, szintvágás és szinkronizáció: állapottartó fake, hardver nélkül."""

    @staticmethod
    def _connect(res):
        return _connect_sticky(res)

    def test_fake_preserves_limits_across_clear_and_new_session(self):
        from Prog.tests.mock_drivers.mock_load import StatefulLoadRangeResource

        res = StatefulLoadRangeResource()
        first = self._connect(res)
        first.set_current(0.1)
        first._query("CURR:TRAN:ALEV 0.1;:CURR:TRAN:BLEV 0.02;*OPC?")
        first._query("CURR:RANG 0.01;*OPC?")
        assert tuple(res.levels.values()) == (0.01, 0.01, 0.01)
        assert first.check_error() == []
        first.disconnect()
        second = self._connect(res)
        assert second is not first
        res.clear()
        assert res.ceiling == 0.01
        assert tuple(res.levels.values()) == (0.01, 0.01, 0.01)
        # A nagyobb plafon önmagában nem állítja vissza a levágott szinteket.
        second._query("CURR:RANG 0.1;*OPC?")
        assert tuple(res.levels.values()) == (0.01, 0.01, 0.01)
        for head in ("SOUR:CURR", "CURR:TRAN:ALEV", "CURR:TRAN:BLEV"):
            second._query(f"{head} 0.2;*OPC?")
            assert res.levels[head] == 0.01
            assert second.check_error()[0].startswith('120,"Parameter overflowed"')

    @pytest.mark.parametrize("deferred", [False, True])
    @pytest.mark.parametrize("pulse,baseline", [(0.2, 0.1), (0.05, 0.2)])
    def test_cc_then_transient_sets_final_levels_after_final_ceiling(self, deferred, pulse, baseline):
        from Prog.tests.mock_drivers.mock_load import StatefulLoadRangeResource

        res = StatefulLoadRangeResource(deferred=deferred)
        load = self._connect(res)
        load.set_current(0.1)
        res.log.clear()
        load.configure_transient_pulse(pulse, baseline, 1.0)
        assert res.ceiling == 0.2
        assert (res.levels["CURR:TRAN:ALEV"], res.levels["CURR:TRAN:BLEV"]) == (pulse, baseline)
        assert load.check_error() == []
        assert [m for k, m in res.log if k == "Q"][:2] == [
            "INP?",
            (
                f"CURR:RANG 0.2;:CURR:TRAN:MODE PULS;:CURR:TRAN:ALEV {pulse:.6g};"
                f":CURR:TRAN:BLEV {baseline:.6g};:CURR:TRAN:AWID 1;:TRIG:SOUR BUS;:TRAN 1;*OPC?"
            ),
        ]

    def test_decreasing_cc_keeps_range_then_immediate_and_correct_final_state(self):
        from Prog.tests.mock_drivers.mock_load import StatefulLoadRangeResource

        res = StatefulLoadRangeResource(deferred=True)
        load = self._connect(res)
        load.set_current(0.2)
        load._query("CURR:TRAN:ALEV 0.2;:CURR:TRAN:BLEV 0.1;*OPC?")
        res.log.clear()
        load.set_current(0.05)
        assert res.log == [("DCL", ""), ("Q", "CURR:RANG 0.05;:SOUR:CURR 0.05;*OPC?")]
        assert res.ceiling == 0.05
        assert tuple(res.levels.values()) == (0.05, 0.05, 0.05)
        assert load.check_error() == []

    @pytest.mark.parametrize("reply,exc", [
        ("1", InstrumentCommandError), ("2", InstrumentInvalidReading),
        ("nan", InstrumentInvalidReading),
    ])
    def test_transient_requires_actual_off_before_any_config(self, reply, exc):
        from Prog.tests.mock_drivers.mock_load import StatefulLoadRangeResource

        res = StatefulLoadRangeResource()
        load = self._connect(res)
        assert load.input_commanded_on is False
        res.replies["INP?"] = reply
        res.log.clear()
        with pytest.raises(exc):
            load.configure_transient_pulse(0.2, 0.1, 1.0)
        assert res.log == [("DCL", ""), ("Q", "INP?")]
        assert res.ceiling == 0.01

    def test_transient_level_readback_returns_applied_not_commanded_values(self):
        from Prog.tests.mock_drivers.mock_load import StatefulLoadRangeResource

        res = StatefulLoadRangeResource()
        load = self._connect(res)
        load.configure_transient_pulse(0.2, 0.1, 1.0)
        load._query("CURR:RANG 0.05;*OPC?")
        assert load.read_transient_current_levels() == (0.05, 0.05)
        assert load.check_error() == []


class _R16B10Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _R16B10Resource:
    """Állapottartó transport: az ON csak a harmadik pollra nyugtázott."""

    def __init__(self, clock, *, never=False, reply=None, query_delay=0.0):
        self.clock = clock
        self.never = never
        self.reply = reply
        self.query_delay = query_delay
        self.timeout = 5000
        self.pending = False
        self.actual = False
        self.polls = 0
        self.log = []

    def clear(self):
        self.log.append(("DCL", "", self.clock.now, self.timeout))

    def write(self, cmd):
        self.log.append(("W", cmd, self.clock.now, self.timeout))
        if cmd == "INP ON":
            self.pending = True

    def query(self, cmd):
        self.log.append(("Q", cmd, self.clock.now, self.timeout))
        if cmd == "INP?":
            if self.pending:
                self.polls += 1
                self.clock.now += self.query_delay
                if isinstance(self.reply, Exception):
                    raise self.reply
                if self.reply is not None:
                    return self.reply
                if not self.never and self.polls >= 3:
                    self.pending = False
                    self.actual = True
            return "1" if self.actual else "0"
        if cmd == "INP OFF;*OPC?":
            self.pending = self.actual = False
            return "1"
        if cmd == "CURR:RANG 1.4;:SOUR:CURR 1.4;*OPC?":
            return "1"
        if cmd == "MEAS:VOLT?":
            return "12.5"
        if cmd == "MEAS:CURR?":
            return "1.4" if self.actual else "0"
        if cmd == "STAT:QUES:COND?":
            return "0"
        if cmd == "SYST:ERR?":
            return NO_ERROR_REPLY
        raise AssertionError(f"Nem várt fake lekérdezés: {cmd}")

    def commands(self, kind):
        return [cmd for op, cmd, _, _ in self.log if op == kind]


class TestR16B10:
    @staticmethod
    def make(monkeypatch, **kwargs):
        from types import SimpleNamespace

        import Prog.drivers.device_load as module

        clock = _R16B10Clock()
        monkeypatch.setattr(
            module, "time",
            SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
            raising=False,
        )
        res = _R16B10Resource(clock, **kwargs)
        load = Keithley2380Load(max_retries=5)
        load._resource = res
        return load, res, clock

    def test_delayed_ack_is_clear_free_and_on_is_once(self, monkeypatch):
        load, res, clock = self.make(monkeypatch)
        load.input_on()
        assert load.input_commanded_on is True
        assert res.actual is True
        assert res.polls == 3
        assert clock.now == pytest.approx(0.1)
        assert res.commands("W") == ["INP ON"]
        assert res.commands("Q") == ["INP?"] * 3
        assert [op for op, _, _, _ in res.log] == ["DCL", "W", "Q", "Q", "Q"]
        assert res.timeout == 5000

    def test_never_ack_has_one_monotonic_deadline(self, monkeypatch):
        load, res, clock = self.make(monkeypatch, never=True)
        with pytest.raises(InstrumentInvalidReading, match="nyugtaablak"):
            load.input_on()
        assert clock.now == pytest.approx(1.0)
        assert load.input_commanded_on is True
        assert res.commands("W") == ["INP ON"]
        assert sum(op == "DCL" for op, _, _, _ in res.log) == 1
        reads = [(t, timeout) for op, _, t, timeout in res.log if op == "Q"]
        assert 2 < len(reads) <= 21
        assert all(0 <= t < 1.0 and 1 <= timeout <= 1000 for t, timeout in reads)
        assert reads[-1][1] < reads[0][1]
        assert all(timeout <= max(1, (1.0 - t) * 1000) for t, timeout in reads)
        assert res.timeout == 5000

    @pytest.mark.parametrize("reply,error", [
        ("nan", InstrumentInvalidReading),
        ("2", InstrumentInvalidReading),
        ("", InstrumentInvalidReading),
        (TimeoutError("fake timeout"), InstrumentTimeoutError),
    ])
    def test_bad_ack_fails_without_retry_or_clear(self, monkeypatch, reply, error):
        load, res, _clock = self.make(monkeypatch, reply=reply)
        with pytest.raises(error):
            load.input_on()
        assert load.input_commanded_on is True
        assert res.commands("W") == ["INP ON"]
        assert res.commands("Q") == ["INP?"]
        assert sum(op == "DCL" for op, _, _, _ in res.log) == 1
        assert res.timeout == 5000

    def test_late_positive_response_cannot_pass_deadline(self, monkeypatch):
        load, res, _clock = self.make(monkeypatch, reply="1", query_delay=1.01)
        with pytest.raises(InstrumentInvalidReading, match="nyugtaablak"):
            load.input_on()
        assert res.commands("Q") == ["INP?"]
        assert res.commands("W") == ["INP ON"]
        assert load.input_commanded_on is True
        assert res.timeout == 5000

    def test_capture_path_keeps_exact_on_only_sequence(self, monkeypatch):
        load, res, clock = self.make(monkeypatch)
        load.input_on(clear_first=False)
        assert res.log == [("W", "INP ON", 0.0, 5000)]
        assert clock.now == 0.0
        assert load.input_commanded_on is True

    def test_software_pulse_keeps_on_origin_without_ack_wait(self, monkeypatch):
        from Prog.tests.test_pulse_measurement_service import make_service

        load, res, clock = self.make(monkeypatch)
        service, _load, _dmm = make_service(time_source=clock.monotonic)
        service._load = load
        # A konfigurációs fake csak az ON sorrendjét vizsgálja itt.
        monkeypatch.setattr(load, "set_mode_cc", lambda: None)
        monkeypatch.setattr(load, "set_current", lambda _current: None)
        assert service.begin_pulse(pulse_current_A=1.4, duration_s=30.0)
        assert res.log == [("DCL", "", 0.0, 5000), ("W", "INP ON", 0.0, 5000)]
        assert service._t0 == clock.now == 0.0

    @pytest.mark.parametrize("case", ["delayed", "never", "drop", "opposite"])
    def test_production_runner_chain(self, monkeypatch, case):
        from Prog.src.discharge_controller import DischargeState
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.test_runner import TestPlan, TestRunner, TestRunnerConfig
        from Prog.tests.test_discharge_controller import make_discharge_controller

        load, res, clock = self.make(monkeypatch, never=case == "never")
        ctrl, psu, _load, dmm = make_discharge_controller()
        ctrl._load = load
        ctrl._state = DischargeState.DISCHARGE_CC_SETUP
        ctrl._time_source = clock.monotonic
        manager = InstrumentManager(psu, load, dmm, ctrl._dmm_t)
        logger = MagicMock()
        events = []
        runner = TestRunner(
            instrument_manager=manager, safety=ctrl._safety, logger=logger,
            profile=ctrl._profile,
            config=TestRunnerConfig(sleep_enabled=False, device_error_poll_interval_s=0.0),
            charge_ctrl_factory=lambda: None, discharge_ctrl_factory=lambda: ctrl,
            relax_ctrl_factory=lambda _step: None,
        )
        runner.on_event = events.append

        def sample(_row):
            # Az első ON utáni poll még 0,1 s-nál fut; a következő tick lezár.
            dmm.voltage_V = 10.5
            if case == "drop":
                res.pending = res.actual = False

        runner.on_sample = sample
        if case == "opposite":
            psu._output_is_on = True
        result = runner.run(TestPlan.discharge_only())
        assert result.status == ("DONE" if case == "delayed" else "FAULT"), result.reason
        assert not load.input_commanded_on and not res.actual
        assert not psu.output_commanded_on and not psu._output_is_on
        assert not psu.called("output_on()")
        assert res.commands("W").count("INP ON") == (0 if case == "opposite" else 1)
        assert "INP OFF;*OPC?" in res.commands("Q")
        assert psu.called("all_outputs_off()")
        if case == "delayed":
            assert res.polls == 3
            assert clock.now == pytest.approx(0.1)
        else:
            assert any(e.get("event_code") == "EMERGENCY_STOP" for e in events)
            logger.log_event.assert_any_call("EMERGENCY_STOP", result.reason, is_critical=True)
            if case == "never":
                assert result.reason.startswith("DISCHARGE_CC_SETUP_FAILED:")
                assert "nyugtaablak" in result.reason
                assert clock.now == pytest.approx(1.0)
            elif case == "drop":
                assert result.reason.startswith(
                    "SCPI_CRITICAL_ERROR[Load]: INPUT_STATE_MISMATCH: commanded=ON actual=OFF"
                )
            else:
                assert result.reason.startswith("SCPI_CRITICAL_ERROR[PSU]: OUTPUT_STATE_MISMATCH")


# R16-B10F2: kizárólag fake VISA, megszakítás után nincs sikeres ON-nyugta.
class TestR16B10F2LoadStop:
    @pytest.mark.parametrize("reply", ["0", "1"])
    def test_stop_inside_first_query(self, monkeypatch, reply):
        from Prog.src.exceptions import TestAbortedByUser

        load, res, clock = TestR16B10.make(monkeypatch, reply=reply)
        stopped = False
        original = res.query

        def query(cmd):
            nonlocal stopped
            response = original(cmd)
            if cmd == "INP?":
                stopped = True
            return response

        monkeypatch.setattr(res, "query", query)
        with pytest.raises(TestAbortedByUser):
            load.input_on(stop_requested=lambda: stopped)
        assert clock.now == 0.0
        assert res.commands("Q") == ["INP?"]
        assert res.commands("W") == ["INP ON"]
        assert res.timeout == 5000
        assert load.input_commanded_on is True

    def test_stop_during_poll_wait_prevents_next_query(self, monkeypatch):
        from Prog.src.exceptions import TestAbortedByUser

        load, res, clock = TestR16B10.make(monkeypatch, never=True)
        stopped = False
        original = clock.sleep

        def sleep(seconds):
            nonlocal stopped
            original(seconds)
            stopped = True

        monkeypatch.setattr(clock, "sleep", sleep)
        # A driver időobjektuma a make() során kötött metódust kapott.
        import Prog.drivers.device_load as module
        monkeypatch.setattr(module.time, "sleep", sleep)
        with pytest.raises(TestAbortedByUser):
            load.input_on(stop_requested=lambda: stopped)
        assert res.commands("Q") == ["INP?"]
        assert clock.now == pytest.approx(0.05)

    @pytest.mark.parametrize("reply,error", [
        ("nan", InstrumentInvalidReading),
        (TimeoutError("mockolt nyugtahiba"), InstrumentTimeoutError),
    ])
    def test_read_failure_retains_priority_over_stop(self, monkeypatch, reply, error):
        load, res, _clock = TestR16B10.make(monkeypatch, reply=reply)
        stopped = False
        original = res.query

        def query(cmd):
            nonlocal stopped
            if cmd == "INP?":
                stopped = True
            return original(cmd)

        monkeypatch.setattr(res, "query", query)
        with pytest.raises(error):
            load.input_on(stop_requested=lambda: stopped)
        assert res.commands("Q") == ["INP?"]
        assert res.timeout == 5000

    @pytest.mark.parametrize("hook", [None, lambda: False])
    def test_none_and_false_hook_preserve_transcript(self, monkeypatch, hook):
        original, original_res, original_clock = TestR16B10.make(monkeypatch)
        original.input_on()
        load, res, clock = TestR16B10.make(monkeypatch)
        load.input_on(stop_requested=hook)
        assert res.log == original_res.log
        assert clock.now == original_clock.now
        assert load.input_commanded_on is original.input_commanded_on is True


# R16-B10bF1: közös driver/mock ON-nyugta-lifecycle, valódi I/O nélkül.
class TestR16B10bF1Lifecycle:
    @staticmethod
    def make(monkeypatch, implementation):
        from types import SimpleNamespace

        from Prog.tests.mock_drivers import mock_load

        real, res, clock = TestR16B10.make(monkeypatch, never=True)
        clock.now = 100.0
        monkeypatch.setattr(
            mock_load, "time", SimpleNamespace(monotonic=clock.monotonic), raising=False,
        )
        load = real if implementation == "driver" else mock_load.MockLoad()
        return load, res, clock

    @pytest.mark.parametrize("implementation", ["driver", "mock"])
    @pytest.mark.parametrize("clear_first,wait_for_ack", [
        (True, False), (False, True), (False, False),
    ])
    def test_async_on_readback_and_reactivation(
        self, monkeypatch, implementation, clear_first, wait_for_ack,
    ):
        load, res, clock = self.make(monkeypatch, implementation)
        assert load.input_on_commanded_at is None
        load.input_on(clear_first=clear_first, wait_for_ack=wait_for_ack)
        assert load.input_commanded_on is True
        assert load.input_on_commanded_at == 100.0
        if implementation == "mock":
            load._input_is_on = False
        clock.now = 100.3
        assert load.read_input_state() is False
        assert load.input_on_commanded_at == 100.0
        # Olvasási hiba sem nyugta, és nem indíthat új ablakot.
        if implementation == "mock":
            load.simulate_input_state_readback_timeout = True
        else:
            res.reply = TimeoutError("mockolt INP? timeout")
        with pytest.raises(InstrumentTimeoutError):
            load.read_input_state()
        assert load.input_on_commanded_at == 100.0
        if implementation == "mock":
            load.simulate_input_state_readback_timeout = False
            load._input_is_on = True
        else:
            res.reply = None
            res.pending, res.actual = False, True
        assert load.read_input_state() is True
        assert load.input_on_commanded_at is None
        if implementation == "mock":
            load._input_is_on = False
        else:
            res.actual = False
        assert load.read_input_state() is False
        assert load.input_on_commanded_at is None
        load.input_off()
        clock.now = 200.0
        load.input_on(clear_first=clear_first, wait_for_ack=wait_for_ack)
        assert load.input_on_commanded_at == 200.0

    @pytest.mark.parametrize("implementation", ["driver", "mock"])
    def test_waited_on_is_already_acknowledged(self, monkeypatch, implementation):
        load, res, _clock = self.make(monkeypatch, implementation)
        res.never = False
        load.input_on()
        assert load.input_commanded_on is True
        assert load.input_on_commanded_at is None

    @pytest.mark.parametrize("implementation", ["driver", "mock"])
    @pytest.mark.parametrize("safe", [False, True])
    @pytest.mark.parametrize("failed", [False, True])
    def test_off_always_clears_timestamp(self, monkeypatch, implementation, safe, failed):
        load, _res, _clock = self.make(monkeypatch, implementation)
        load.input_on(wait_for_ack=False)
        assert load.input_on_commanded_at == 100.0
        if failed and implementation == "driver":
            def unavailable(*args, **kwargs):
                raise InstrumentTimeoutError("mockolt OFF timeout")
            monkeypatch.setattr(load, "_query", unavailable)
        elif failed and safe:
            load.raise_on_safe_off = True
        elif failed:
            # A mock OFF első műveletén dobunk, a finally-t is ellenőrizzük.
            class FailedCalls(list):
                def append(self, item):
                    raise InstrumentTimeoutError("mockolt OFF timeout")
            load.calls = FailedCalls()
        if safe:
            assert load.safe_off() is (not failed)
        elif failed:
            with pytest.raises(InstrumentTimeoutError):
                load.input_off()
        else:
            load.input_off()
        assert load.input_commanded_on is False
        assert load.input_on_commanded_at is None


# R16-B9b: valódi driver, kizárólag mock VISA-válaszokkal.
@pytest.mark.parametrize("field", ["ALEV", "BLEV"])
@pytest.mark.parametrize("reply", ["nan", "inf", "-inf"])
def test_r16b20_transient_nonfinite_readback_rejected(field, reply):
    load, resource = make_load()
    replies = {"CURR:TRAN:ALEV?": "0.125", "CURR:TRAN:BLEV?": "0.0125"}
    replies[f"CURR:TRAN:{field}?"] = reply
    resource.query.side_effect = lambda command: replies[command]
    with pytest.raises(InstrumentInvalidReading, match=field):
        load.read_transient_current_levels()
    expected = ["CURR:TRAN:ALEV?"]
    if field == "BLEV":
        expected.append("CURR:TRAN:BLEV?")
    assert [c.args[0] for c in resource.query.call_args_list] == expected
