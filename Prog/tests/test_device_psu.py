"""
Keithley2220PSU unit tesztek — pyvisa resource mock-kal.
Fizikai hardware nélkül futtatható.
"""
import pytest
from unittest.mock import MagicMock
from Prog.drivers.device_psu import Keithley2220PSU
from Prog.src.safety import PsuMode
from Prog.src.exceptions import (
    InstrumentInvalidReading,
    InstrumentTimeoutError,
)


def make_psu(mode: PsuMode = PsuMode.INDEPENDENT) -> tuple[Keithley2220PSU, MagicMock]:
    """Előre összekötött PSU mock resource-szal."""
    psu = Keithley2220PSU(combination_mode=mode)
    res = MagicMock()
    res.timeout = psu.timeout_ms
    res.query.return_value = '+0,"No error"'
    actual_mode = {PsuMode.INDEPENDENT: "OFF", PsuMode.SERIES: "SER", PsuMode.PARALLEL: "PAR"}[mode]
    output = False

    def reply(cmd):
        nonlocal output
        if cmd in ("SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"):
            return str(int(actual_mode == ("SER" if "SER?" in cmd else "PAR")))
        if cmd == "OUTP ON;*OPC?":
            output = True
        elif cmd == "OUTP OFF;*OPC?":
            output = False
        if cmd == "OUTP?" and output:
            return "1"
        return res.query.return_value

    res.query.side_effect = reply
    psu._resource = res
    psu._connected = True
    psu._output_cmd = "OUTP"
    return psu, res


def writes(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.write.call_args_list]


def queries(res: MagicMock) -> list[str]:
    return [c.args[0] for c in res.query.call_args_list]


NO_ERROR_REPLY = '+0,"No error"'


def _connect_query_ok(cmd, *args, **kwargs):
    return NO_ERROR_REPLY


def ordered_calls(res: MagicMock) -> list[tuple[str, str]]:
    """2026-09-12, DFAM-039/F7: a `_apply_config_atomic()` bevezetése óta a
    korábban külön `_write()`-okként kiadott szekvenciák egyetlen `_query()`
    hívásként (`a;:b;*OPC?`) mennek ki -- ez a helper a `write`/`query`
    hívásokat EGYETLEN, valós idősorrendet tükröző listába fűzi (a
    `res.mock_calls` már ezt a sorrendet tartja), hogy a tesztek a
    write/query határon átnyúló relatív sorrendet is ellenőrizhessék."""
    return [(c[0], c[1][0]) for c in res.mock_calls if c[0] in ("write", "query")]


class TestChannelSelect:
    def test_set_output_voltage_selects_ch1_first(self):
        """2026-09-12, DFAM-039/F7: set_output_voltage() mostantol EGYETLEN,
        atomikus SCPI uzenetkent (_apply_config_atomic()) kuldi ki a
        csatorna-valasztast + a setpontot -- a sorrend az uzeneten BELUL
        ellenorzendo, nem kulon write()-hivasok kozott."""
        psu, res = make_psu()
        psu.set_output_voltage(14.0)
        q = queries(res)[-1]
        assert q.index("CH1") < q.index("VOLT")

    def test_set_output_current_selects_ch1_first(self):
        psu, res = make_psu()
        psu.set_output_current(1.5)
        q = queries(res)[-1]
        assert q.index("CH1") < q.index("CURR")


class TestOutputVoltageAndCurrent:
    def test_set_output_voltage_sends_sour_volt(self):
        psu, res = make_psu()
        psu.set_output_voltage(14.4)
        # R13-B1: a teljes atomikus setpoint-uzenet pontos egyezese.
        assert queries(res) == ["INST:SEL CH1;:SOUR:VOLT 14.4;*OPC?"]

    def test_set_output_voltage_series_mode(self):
        psu, res = make_psu(PsuMode.SERIES)
        psu.set_output_voltage(28.8)
        assert queries(res) == ["INST:SEL CH1;:SOUR:VOLT 28.8;*OPC?"]

    def test_set_output_current_sends_sour_curr(self):
        psu, res = make_psu()
        psu.set_output_current(1.75)
        assert queries(res) == ["INST:SEL CH1;:SOUR:CURR 1.75;*OPC?"]


class TestOutputOnOff:
    def test_output_on_sends_outp_on(self):
        # 2026-09-12, DFAM-039/F7 (V6): a bare "OUTP ON" mostantol *OPC?-vel
        # szinkronizalt query()-n megy, nem write()-on.
        psu, res = make_psu()
        psu.output_on()
        assert any("OUTP" in c and "ON" in c for c in queries(res))

    def test_output_off_sends_outp_off(self):
        psu, res = make_psu()
        psu.output_off()
        assert any("OUTP" in c and "OFF" in c for c in queries(res))

    def test_all_outputs_off_sends_outp_off(self):
        psu, res = make_psu()
        psu.all_outputs_off()
        assert any("OUTP" in c for c in queries(res))

    def test_output_on_sets_commanded_true(self):
        """[N9] Commanded state nyilvántartás."""
        psu, res = make_psu()
        assert psu.output_commanded_on is False
        psu.output_on()
        assert psu.output_commanded_on is True

    def test_output_off_sets_commanded_false(self):
        psu, res = make_psu()
        psu.output_on()
        psu.output_off()
        assert psu.output_commanded_on is False

    def test_all_outputs_off_sets_commanded_false(self):
        psu, res = make_psu()
        psu.output_on()
        psu.all_outputs_off()
        assert psu.output_commanded_on is False

    def test_output_off_sets_commanded_false_even_on_write_failure(self):
        """Governance-v2 review-kör 1 (sibling finding CF-13 fixe közben
        talált, ugyanaz a DFAM-001 family): a `output_off()` — szemben az
        `all_outputs_off()` elsődleges ágával (l. ott a 8. kör dokumentált
        try/finally-jét) és a `safe_off()`-fal — NEM try/finally-ben hívta a
        kritikus OUTP OFF write-ot, tehát write-hiba esetén a
        `output_commanded_on` flag a hívás előtti (jellemzően True) értéken
        ragadt volna. `charge_controller.py`-ban ez az EGYETLEN
        `output_off()` hívóhely (CHARGE_DONE, a normál befejezési út)."""
        psu, res = make_psu()
        psu.output_on()
        assert psu.output_commanded_on is True

        # 2026-09-12, DFAM-039/F7 (V6): a bare "OUTP OFF" mostantol query()-n
        # megy (all_outputs_off() "OUTP" aga).
        res.query.side_effect = Exception("SCPI timeout")

        with pytest.raises(InstrumentTimeoutError):
            psu.output_off()

        assert psu.output_commanded_on is False

    def test_output_on_in_enab_fallback_also_sends_bare_outp_on(self):
        """Governance-v2 review-kör 1 (CF-12/HW-R1-02): fallback módban
        (`_output_cmd == "OUTP:ENAB"`) a korábbi kód `f"{self._output_cmd} ON"`-t
        küldött, ami fallback módban MÉGEGYSZER "OUTP:ENAB ON" — a csupasz
        "OUTP ON" SOHA nem ment ki, a kimenet fizikailag OFF maradt, miközben
        `output_commanded_on=True` lett. A bare "OUTP ON" mostantól mindig
        kimegy (HIL_PENDING a pontos OUTP vs OUTP:ENAB szemantikára,
        l. CLAIM_LEDGER.yaml INSTR-PSU-COMB-001 rokon megjegyzése).

        R1 B2 (F-03, 2026-10-25): a bare "OUTP ON" fallback dialektuson sem
        "best-effort" többé -- hibája propagál (l. test_output_on_bare_outp_
        failure_in_fallback_propagates). Ez a teszt csak a sikeres utat
        ellenőrzi, az elvárása változatlan."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"  # a fallback-detekció eredménye
        psu.output_on()
        # 2026-09-12, DFAM-039/F7 (V6): a bare "OUTP ON" mostantol
        # *OPC?-vel szinkronizalt query()-n megy.
        assert any(q.startswith("OUTP ON") for q in queries(res))
        assert psu.output_commanded_on is True

    def test_output_on_bare_outp_failure_in_fallback_propagates(self):
        """[MÓDOSÍTVA, R1 B2 F-03, 2026-10-25 -- korábban test_output_on_
        bare_outp_failure_in_fallback_does_not_abort] Korábbi elvárás: ha
        ezen a firmware-en a bare "OUTP ON" ténylegesen elutasított (ezért
        váltott fallback-re a detekció), a best-effort kísérlet ne akassza
        meg az output_on()-t — az OUTP:ENAB ON már kiment.

        R2-08 (2026-09-15, 13. writer-kör) javítás: a bare "OUTP ON" a
        DFAM-039/F7 (V6) óta query()-n megy write() helyett -- ez a teszt
        korábban res.write.side_effect-et állított, ami emiatt SOSEM
        tüzelt, tehát a teszt TAUTOLOGIKUS volt (a régi, hibás kódon is
        zöld lett volna). Javítva: res.query.side_effect a tartalom
        szerint szűrve.

        Safety-indoklás az elvárásváltáshoz (R1 F-03, rules/10-safety.md 6.
        pont; R2-08 MAX F2 reziduum): `_output_cmd == "OUTP:ENAB"` BÁRMELY
        detektálási `_query`-kivételre beáll (tiszta olvasási timeoutra
        is), tehát egy félredetektált primer firmware-en a bare "OUTP ON"
        a TÉNYLEGES energizáló parancs -- a hibájának elnyelése a
        bizonytalanságot eltitkolta a hívó (charge_controller.py::
        _run_psu_preset() -> emergency_stop) elől. A kivétel mostantól
        propagál, a parancs EGYSZER megy ki (egylövéses), a flag
        konzervatívan True. Szigorúbb elvárás, nem gyengítés."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"

        normal_reply = res.query.side_effect

        def _query_side_effect(cmd: str):
            if cmd.startswith("OUTP ON"):
                raise Exception("unknown command")
            return normal_reply(cmd)

        res.query.side_effect = _query_side_effect
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()
        assert psu.output_commanded_on is True
        assert queries(res).count("OUTP ON;*OPC?") == 1

    def test_output_on_primary_dialect_query_failure_raises(self):
        """R2-08 (2026-09-15, 13. writer-kör, P2, DFAM-039 4. érintése):
        a primer ("OUTP") dialektuson a bare "OUTP ON" az EGYETLEN
        energia-bekapcsoló parancs -- hibáját TILOS csendben elnyelni
        (CLAUDE.md §6; rules/10-safety.md 6. pont). Mutáció, amin bukik:
        a régi `except Exception: pass` visszaállítása."""
        psu, res = make_psu()  # _output_cmd == "OUTP" (make_psu() default)

        normal_reply = res.query.side_effect

        def _query_side_effect(cmd: str):
            if cmd.startswith("OUTP ON"):
                raise Exception("SCPI timeout")
            return normal_reply(cmd)

        res.query.side_effect = _query_side_effect
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()

    def test_output_on_primary_dialect_failure_still_marks_commanded_on(self):
        """R2-08: a commanded-flag KONZERVATÍVAN True marad akkor is, ha a
        primer dialektuson a parancs hibázott -- egy elveszett *OPC? válasz
        NEM bizonyítja, hogy a kimenet OFF, és egy hamis False lenne a
        VESZÉLYES irány (check_concurrent_psu_load() egy esetleg élő
        kimenet mellett engedné a Loadot). Ez a teszt kifogja azt a
        mutációt, ami hiba esetén False-ra állítaná a flaget."""
        psu, res = make_psu()

        normal_reply = res.query.side_effect

        def _query_side_effect(cmd: str):
            if cmd.startswith("OUTP ON"):
                raise Exception("SCPI timeout")
            return normal_reply(cmd)

        res.query.side_effect = _query_side_effect
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()
        assert psu.output_commanded_on is True

    def test_output_on_fallback_dialect_ch1_enable_failure_still_marks_commanded_on(self):
        """Teljes kódbázis review 2. kör (2026-09-16, R2-08-F1, DFAM-032
        3. előfordulásának LEZÁRÁSA): a metódus ELEJÉN álló CH1 OUTP:ENAB
        engedélyező blokk korábban NEM volt try/finally-ben -- a fallback
        dialektuson EZ a tényleges energia-bekapcsolás, tehát egy itteni
        hiba a flag ÉRINTÉSE NÉLKÜL terjedt volna a hívóhoz, a flag a
        hívás előtti (jellemzően False) értéken ragadva egy esetleg MÁR
        energizált kimenet mellett -- a hamis False a VESZÉLYES irány
        (safety.py::check_concurrent_psu_load()). Mutáció, amin bukik: a
        try/finally szétszedése két külön blokká."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"

        normal_reply = res.query.side_effect

        def _query_side_effect(cmd: str):
            if cmd.startswith("INST:SEL CH1;:OUTP:ENAB ON"):
                raise Exception("SCPI timeout")
            return normal_reply(cmd)

        res.query.side_effect = _query_side_effect
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()
        assert psu.output_commanded_on is True

    def test_output_on_series_mode_ch2_enable_failure_still_marks_commanded_on(self):
        """L. test_output_on_fallback_dialect_ch1_enable_failure_still_
        marks_commanded_on docstringje -- ugyanaz a minta a SER/PAR-módú
        CH2 engedélyező blokkra."""
        psu, res = make_psu(PsuMode.SERIES)

        normal_reply = res.query.side_effect

        def _query_side_effect(cmd: str):
            if cmd.startswith("INST:SEL CH2"):
                raise Exception("SCPI timeout")
            return normal_reply(cmd)

        res.query.side_effect = _query_side_effect
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()
        assert psu.output_commanded_on is True

    def test_output_on_ch1_enable_failure_still_propagates_exception(self):
        """Fail-closed guard a fenti két testre: a try/finally bevezetése
        NEM szabad, hogy elnyelje a kivételt -- a hívónak (charge_
        controller.py::_run_psu_preset()) ÉRDEMI hibát kell kapnia, nem
        csendes sikert. Mutáció, amin bukik: a `raise` elhagyása/egy
        bare `except Exception: pass` bevezetése a finally mellett."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"

        normal_reply = res.query.side_effect

        def _query_side_effect(cmd: str):
            if cmd.startswith("INST:SEL CH1;:OUTP:ENAB ON"):
                raise Exception("SCPI timeout")
            return normal_reply(cmd)

        res.query.side_effect = _query_side_effect
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()

    def test_output_on_series_mode_also_enables_ch2(self):
        """Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-02 lelet:
        `output_on()` korábban KIZÁRÓLAG CH1-et engedélyezte
        (`INST:SEL CH1` + `OUTP:ENAB ON`), miközben `all_outputs_off()`
        mindkét csatorna `OUTP:ENAB`-ját explicit letiltja (fallback ágon) —
        egy korábbi `all_outputs_off()`/`safe_off()`/`connect()` után CH2
        engedélye OFF marad. SERIES módban ez fél feszültséget eredményezett
        volna, mert a fizikai kimenethez MINDKÉT csatorna szükséges.

        MAX verifier follow-up (P1-1): a CH2 engedélyezésnek a bare
        "OUTP ON" ELŐTT kell mennie -- a kézikönyv szerint a bare OUTP
        "minden ENGEDÉLYEZETT csatornára" vonatkozik, tehát ha OUTP ON
        előbb megy ki, mint CH2 ENAB-ja, a fix hatástalan maradhat."""
        psu, res = make_psu(PsuMode.SERIES)
        psu.output_on()
        # 2026-09-12, DFAM-039/F7: a CH2-valasztas + engedelyezes + vissza-
        # valtas CH1-re EGYETLEN atomikus SCPI uzenetkent megy ki (query()).
        # A bare "OUTP ON" (V6 ota SZINTEN query(), *OPC?-vel szinkronizalva)
        # -- nem feltetlenul az UTOLSO elem a listaban, ezert TARTALOM
        # alapjan keressuk meg mindkettot, nem index[-1]-gyel.
        q = next(x for x in queries(res) if "INST:SEL CH2" in x)
        assert q.index("INST:SEL CH2") < q.index("OUTP:ENAB ON") < q.index("INST:SEL CH1")
        calls = ordered_calls(res)
        query_idx = calls.index(("query", q))
        outp_on_idx = calls.index(("query", "OUTP ON;*OPC?"))
        assert query_idx < outp_on_idx

    def test_output_on_parallel_mode_also_enables_ch2(self):
        """L. test_output_on_series_mode_also_enables_ch2 -- PARALLEL módban
        ugyanez a hiány fél áramot eredményezett volna."""
        psu, res = make_psu(PsuMode.PARALLEL)
        psu.output_on()
        q = next(x for x in queries(res) if "INST:SEL CH2" in x)
        assert q.index("INST:SEL CH2") < q.index("OUTP:ENAB ON") < q.index("INST:SEL CH1")
        calls = ordered_calls(res)
        query_idx = calls.index(("query", q))
        outp_on_idx = calls.index(("query", "OUTP ON;*OPC?"))
        assert query_idx < outp_on_idx

    def test_output_on_series_mode_in_enab_fallback_is_rejected(self):
        """R16-B14: a korabbi fallback-aktivalas helyett explicit tiltas."""
        psu, res = make_psu(PsuMode.SERIES)
        psu._output_cmd = "OUTP:ENAB"
        with pytest.raises(InstrumentInvalidReading, match="fallback"):
            psu.output_on()
        assert _energizing(queries(res)) == []
        assert psu.combination_mode == PsuMode.SERIES

    def test_output_on_independent_mode_never_enables_ch2(self):
        """Review-kör 2 HW2-02 fix scope-ja: INDEPENDENT módban a driver
        sosem programoz CH2-re setpontot (`set_output_voltage()`/
        `set_output_current()` mindig CH1-et választ) -- CH2 visszakapcsolása
        itt egy korábbi, MÁS kombinációs módból maradt setpontot energizálna,
        ezért `output_on()`-nak INDEPENDENT módban NEM szabad hozzányúlnia
        CH2-höz.

        [MÓDOSÍTVA, R1 B2 F-04, 2026-10-25 -- korábban test_output_on_
        independent_mode_does_not_touch_ch2] A "nem nyúl CH2-höz" elvárás
        HELYESBÍTVE "explicit tiltja, sosem engedélyezi"-re: egy korábbi
        SER/PAR session után a CH2 OUTP:ENAB sticky ON maradhat (HIL-SCF010),
        és a bare OUTP ON "all of the enabled output channels"-t bekapcsolja
        (2220-30-1/prog.txt, [SOURce:]OUTPut[:STATe][:ALL]) -- a "nem nyúl"
        viselkedés éppen ezt hagyta nyitva. Az invariáns (INDEPENDENT-ben a
        driver SOSEM engedélyezi a CH2-t) szigorúbban marad meg."""
        # 2026-09-12, DFAM-039/F7 (V1, fuggetlen verifier -- ez a biztonsagi
        # invarians-teszt "writes(res)"-re nezve VAKKA valt, mert a CH2-
        # cimzes SERIES/PARALLEL modban is atomikus query()-be kerult, tehat
        # SOHA nem jelenik meg writes()-ben -- a teszt korabban SERIES/
        # PARALLEL modban is (hibasan) atment volna. `queries(res)`-re
        # ellenorzunk, hogy tenylegesen kimutassa a kulonbseget a modok
        # kozott.
        psu, res = make_psu(PsuMode.INDEPENDENT)
        psu.output_on()
        ch2_queries = [q for q in queries(res) if "INST:SEL CH2" in q]
        assert all("OUTP:ENAB ON" not in q for q in ch2_queries), ch2_queries
        assert ch2_queries == ["INST:SEL CH2;:OUTP:ENAB OFF;:INST:SEL CH1;*OPC?"]

    def test_output_on_series_mode_does_touch_ch2(self):
        """A fenti teszt ELLENPARJA (fuggetlen verifier V1 lelete): SERIES/
        PARALLEL modban a CH2-cimzesnek TENYLEGESEN meg kell tortennie --
        enelkul a fenti teszt csak azt igazolna, hogy `output_on()` SOHA
        nem cimzi CH2-t, ami HAMIS (l. SER/PAR ag)."""
        psu, res = make_psu(PsuMode.SERIES)
        psu.output_on()
        assert any("INST:SEL CH2" in q for q in queries(res))

    def test_output_on_series_mode_reselects_ch1_before_bare_outp_on(self):
        """A mód-agnosztikus API [N7] hívói (set_output_voltage/current) a
        `output_on()` UTÁN is elvárhatják, hogy CH1 legyen kiválasztva --
        `output_on()`-nak a CH2 engedélyezése után vissza kell váltania,
        MIELŐTT a (csatorna-független) bare "OUTP ON" kimegy."""
        psu, res = make_psu(PsuMode.SERIES)
        psu.output_on()
        # 2026-09-12, DFAM-039/F7: az "INST:SEL CH1" mostantol az ATOMIKUS
        # SER/PAR uzenet UTOLSO tagja (a zaro *OPC? elott), nem egy kulon,
        # kozvetlenul a bare "OUTP ON" elotti write() -- a bare "OUTP ON"
        # (V6 ota query()) kulon uzenetkent, KESOBB kovetkezik.
        q = next(x for x in queries(res) if "INST:SEL CH2" in x)
        assert q.endswith("INST:SEL CH1;*OPC?")
        calls = ordered_calls(res)
        query_idx = calls.index(("query", q))
        outp_on_idx = calls.index(("query", "OUTP ON;*OPC?"))
        assert query_idx < outp_on_idx
        assert queries(res)[queries(res).index("OUTP ON;*OPC?") - 2:queries(res).index("OUTP ON;*OPC?")] == [
            "SOUR:OUTP:SER?", "SOUR:OUTP:PAR?",
        ]

    def test_output_off_delegates_to_all_outputs_off_in_fallback(self):
        """Governance-v2 review-kör 1 (CF-12/HW-R1-02): a korábbi
        `output_off()` nem hívott `_select_ch1()`-et és nem hívta
        `all_outputs_off()`-ot, tehát fallback módban csak az UTOLJÁRA
        kiválasztott csatornát tiltotta le — SERIES módban (24V pack) ez
        CH2-t energizálva hagyhatta a sikeres CHARGE_DONE úton (az EGYETLEN
        `output_off()` hívóhely a kódbázisban)."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"  # a fallback-detekció eredménye
        psu.output_off()
        # 2026-09-12, DFAM-039/F7: minden csatorna valasztas+OFF EGYETLEN
        # atomikus queryken megy (l. all_outputs_off()).
        qs = queries(res)
        assert sum(1 for q in qs if "INST:SEL CH1" in q and "OUTP:ENAB OFF" in q) == 1
        assert sum(1 for q in qs if "INST:SEL CH2" in q and "OUTP:ENAB OFF" in q) == 1
        assert psu.output_commanded_on is False

    def test_safe_off_idempotent_no_exception(self):
        """[v1.2] safe_off nem dob kivételt ha már off — idempotens."""
        psu, res = make_psu()
        psu.safe_off()
        psu.safe_off()  # második hívás sem dob

    def test_safe_off_returns_true_on_success(self):
        psu, res = make_psu()
        assert psu.safe_off() is True

    def test_safe_off_returns_false_on_write_failure(self):
        """CLAUDE.md §10.4: a hívónak (InstrumentManager) módja kell legyen
        megtudni, hogy az OFF-parancs ténylegesen sikeres volt-e — safe_off()
        NEM nyelheti el csendben a hibát visszatérési érték nélkül."""
        psu, res = make_psu()
        # 2026-09-12, DFAM-039/F7 (V6): a bare "OUTP OFF" mostantol query()-n
        # megy (all_outputs_off() "OUTP" aga).
        res.query.side_effect = InstrumentTimeoutError("simulated timeout")
        assert psu.safe_off() is False
        # a bizonytalan hardveres állapot ellenére a parancsolt (szoftver-
        # oldali) állapot False-ra áll, mert ez a KÍSÉRLET tényét tükrözi
        assert psu.output_commanded_on is False

    def test_all_outputs_off_bare_outp_failure_still_updates_flag(self):
        """[regresszió, Teljes kódbázis review 8. kör (2026-08-30), Hardver/
        Műszervezérlés P1 lelet] az elsődleges ("OUTP") ág — amit
        emergency_stop() mind a 6 controllerben KÖZVETLENÜL hív, nem a
        safe_off()-on keresztül — korábban CSAK sikeres write után frissítette
        az output_commanded_on flaget. Ha a write InstrumentTimeoutError-t
        dobott, a flag a hívás előtti (jellemzően True) értéken ragadt —
        holott a fallback-ág és a safe_off() már dokumentáltan, feltétel
        nélkül mindig False-ra állítja ugyanezt a flaget."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP"  # az elsődleges (nem-fallback) ág
        psu.output_on()
        # 2026-09-12, DFAM-039/F7 (V6): a bare "OUTP OFF" mostantol query()-n
        # megy.
        res.query.side_effect = InstrumentTimeoutError("simulated timeout")

        with pytest.raises(InstrumentTimeoutError):
            psu.all_outputs_off()

        assert psu.output_commanded_on is False

    def test_all_outputs_off_in_enab_fallback_disables_both_channels(self):
        """[regresszió, release-audit 2026-08-23 2.5] Ha a
        _detect_output_command() a csatorna-scope-olt OUTP:ENAB fallback-re
        váltott (pl. tranziens hiba miatt a bare OUTP OFF-nál), az
        all_outputs_off() akkor is MINDKÉT csatornát letiltja, nem csak az
        aktuálisan kiválasztottat (CLAUDE.md #10.1)."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"  # a fallback-detekció eredménye
        psu.all_outputs_off()
        qs = queries(res)
        assert sum(1 for q in qs if "INST:SEL CH1" in q and "OUTP:ENAB OFF" in q) == 1
        assert sum(1 for q in qs if "INST:SEL CH2" in q and "OUTP:ENAB OFF" in q) == 1

    def test_all_outputs_off_ch1_failure_still_attempts_ch2_and_updates_flag(self):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P0] ha a
        CH1 write minden retry után is elhasal, a kivétel korábban
        megszakította a ciklust — a CH2 OFF-parancsa SOHA nem ment ki, és az
        output_commanded_on flag sem frissült. CLAUDE.md §10.3: "az egyik
        OFF hiba nem akadályozhatja a másik OFF megkísérlését"."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"  # a fallback-detekció eredménye

        # 2026-09-12, DFAM-039/F7: a CH1 valasztas+OFF EGYETLEN atomikus
        # queryken megy -- a kommunikacios hibat itt kell szimulalni.
        normal_reply = res.query.side_effect

        def _query_side_effect(cmd: str) -> str:
            if "INST:SEL CH1" in cmd:
                raise Exception("comm error")
            return normal_reply(cmd)

        res.query.side_effect = _query_side_effect

        with pytest.raises(InstrumentTimeoutError, match=r"\bINST:SEL CH1\b"):
            psu.all_outputs_off()

        qs = queries(res)
        # CH1 valasztas+OFF minden retry-t kimerit, ezert az a query sosem
        # sikerul -- DE a CH2 valasztas+OFF atomikus querye igen.
        assert sum(1 for q in qs if "INST:SEL CH2" in q and "OUTP:ENAB OFF" in q) == 1
        # A parancsolt (szoftver-oldali) állapot a kísérlet tényét tükrözi,
        # a bizonytalan hardveres állapot ellenére is False-ra áll — a hívó
        # (safe_off()/InstrumentManager.safe_all_off()) a propagált
        # kivételből dönt a bizonytalanság jelzéséről.
        assert psu.output_commanded_on is False

    def test_all_outputs_off_bare_outp_does_not_select_channels(self):
        """A normál (nem-fallback) bare OUTP OFF út nem kell hogy csatornát
        válasszon — a gyári kézikönyv szerint ez már minden csatornára hat."""
        # 2026-09-12, DFAM-039/F7 (V6): a bare "OUTP OFF" mostantol query()-n
        # megy.
        psu, res = make_psu()
        psu.all_outputs_off()
        cmds = writes(res)
        qs = queries(res)
        assert "INST:SEL CH1" not in cmds and not any("INST:SEL CH1" in q for q in qs)
        assert "INST:SEL CH2" not in cmds and not any("INST:SEL CH2" in q for q in qs)
        assert any(q.startswith("OUTP OFF") for q in qs)

    def test_detect_output_command_fallback_on_exception(self):
        """_detect_output_command(): ha a bare OUTP OFF minden retry után is
        elhasal, a driver OUTP:ENAB-ra vált (nem omlik össze)."""
        # 2026-09-12, DFAM-039/F7 (V5): a proba-iras ("OUTP OFF") mostantol
        # a SYST:ERR?-rel egyutt, atomikus query()-n megy -- a hiba-
        # injekciot ennek megfeleloen kell adni.
        psu = Keithley2220PSU()
        res = MagicMock()

        def _query_side_effect(cmd: str) -> str:
            if "OUTP OFF" in cmd:
                raise Exception("SCPI timeout")
            return '+0,"No error"'

        res.query.side_effect = _query_side_effect
        psu._resource = res
        psu._connected = True
        psu._detect_output_command()
        assert psu._output_cmd == "OUTP:ENAB"

    def test_detect_output_command_fallback_on_silent_scpi_rejection(self):
        """[regresszió, teljes kódbázis review 1. kör, 2026-08-26] Ha az
        "OUTP OFF" write VISA-szinten sikeres (nem dob kivételt), de a
        műszer SCPI error-queue-jában elutasításként jelenik meg, a
        driver korábban ezt sosem észlelte (csak a kivétel-ágat nézte) —
        most check_error()-rel is ellenőriz."""
        psu = Keithley2220PSU()
        res = MagicMock()
        # Az "OUTP OFF" write nem dob kivételt, DE a következő SYST:ERR?
        # lekérdezés egy elutasítást jelent (csendes SCPI-szintű hiba).
        res.query.return_value = '-113,"Undefined header"'
        psu._resource = res
        psu._connected = True
        psu._detect_output_command()
        assert psu._output_cmd == "OUTP:ENAB"
        assert not any("OUTP:ENAB" in c.args[0] for c in res.write.call_args_list)

    def test_detect_output_command_stays_outp_when_accepted(self):
        """Ha az OUTP OFF-ot a műszer ténylegesen elfogadja (nincs kivétel,
        tiszta error-queue), a driver NEM vált feleslegesen OUTP:ENAB-ra."""
        psu, res = make_psu()
        res.query.return_value = '+0,"No error"'
        psu._detect_output_command()
        assert psu._output_cmd == "OUTP"


class TestD9ClsFoldedIntoDetectAtomicQuery:
    """[regresszió, Kör 19 D9, HW19-04, DFAM-039 ugyanaz a család, mint
    HW19-01/02] connect()-ben korábban egy KÜLÖN self._write("*CLS") hívás
    előzte meg _detect_output_command()-et -- egy közbeékelődő Device
    Clear a *CLS ÉS az OUTP OFF/SYST:ERR? páros KÖZÖTT egyetlen, véletlenül
    bentmaradt hibasor-üzenetet "kiszivárogtathatott" volna a has_error
    kiértékelésbe, ami csendben az OUTP:ENAB dialektusra zárná a sessiont.
    A *CLS mostantól az EGYETLEN atomikus query() része."""

    def test_detect_output_command_atomic_query_starts_with_cls(self):
        psu, res = make_psu()
        res.query.return_value = '+0,"No error"'
        psu._detect_output_command()
        qs = queries(res)
        assert any(q.startswith("*CLS;:OUTP OFF") for q in qs)

    def test_connect_no_longer_sends_standalone_cls_write(self):
        """A *CLS mostantól query()-n megy (a detektálással egy üzenetben),
        NEM külön write()-on -- ez a teszt a régi kódon BUKNA (a régi
        connect() önálló self._write("*CLS")-t is küldött volna, tehát
        "*CLS" szerepelt volna a writes(res) listában is)."""
        psu, res = make_psu()
        res.query.side_effect = _connect_query_ok
        psu.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: res))
        assert not any(c == "*CLS" for c in writes(res))
        assert any(q.startswith("*CLS;:") for q in queries(res))


class TestConnect:
    def test_connect_forces_all_outputs_off_bare_outp(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P0]
        connect() a _detect_output_command() próba-írására támaszkodás
        helyett explicit all_outputs_off()-ot hív, hogy CLAUDE.md §10.1
        (csatlakozáskor PSU ALL OUTPUTS OFF) garantáltan teljesüljön."""
        psu = Keithley2220PSU()
        res = MagicMock()
        res.query.side_effect = _connect_query_ok
        rm = MagicMock()
        rm.open_resource.return_value = res
        psu.connect("USB0::mock", rm=rm)
        # 2026-09-12, DFAM-039/F7 (V5+V6): a próba-írás ("OUTP OFF") ÉS a
        # bare all_outputs_off() OFF-parancsa is atomikus query()-n megy.
        qs = queries(res)
        assert any("OUTP OFF" in q for q in qs)

    def test_connect_forces_all_outputs_off_both_channels_in_fallback(self):
        """[regresszió, teljes kódbázis review 2. kör, 2026-08-28, P0]
        Ha a _detect_output_command() a csatorna-scope-olt OUTP:ENAB
        fallback-re vált, a connect()-beli all_outputs_off() hívás akkor is
        MINDKÉT csatornát letiltja — nem csak azt, amelyiken a próba-írás
        történt. Enélkül egy korábbi session/front-panel művelet miatt
        bekapcsolva maradt CH2 kimenet connect() után is ÉLŐ maradhatna."""
        psu = Keithley2220PSU()
        res = MagicMock()

        # 2026-09-12, DFAM-039/F7 (V5): a proba-iras ("OUTP OFF") mostantol
        # a SYST:ERR?-rel egyutt, atomikus query()-n megy.
        def _query_side_effect(cmd: str) -> str:
            if "OUTP OFF" in cmd:
                raise Exception("SCPI timeout")
            if cmd in ("SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"):
                return "0"
            return _connect_query_ok(cmd)

        res.query.side_effect = _query_side_effect
        rm = MagicMock()
        rm.open_resource.return_value = res
        psu.connect("USB0::mock", rm=rm)
        cmds = writes(res)
        qs = queries(res)
        # 2026-09-12, DFAM-039/F7: az all_outputs_off() es a _disable_output_
        # timer() csatornankenti valasztas+parancs parjai mostantol atomikus
        # queryken mennek (l. _apply_config_atomic()) -- csak a
        # _disable_output_timer() ZARO finally: self._select_ch1()-je marad
        # kulon write()-kent.
        assert cmds.count("INST:SEL CH1") == 1
        assert sum(1 for q in qs if "INST:SEL CH1" in q and "OUTP:ENAB OFF" in q) == 1
        assert sum(1 for q in qs if "INST:SEL CH2" in q and "OUTP:ENAB OFF" in q) == 1
        assert sum(1 for q in qs if "INST:SEL CH1" in q and "OUTP:TIM:STAT OFF" in q) == 1
        assert sum(1 for q in qs if "INST:SEL CH2" in q and "OUTP:TIM:STAT OFF" in q) == 1
        # A detektor nem tilt csatornát módigazolás nélkül; a fenti két
        # atomikus all_outputs_off-parancs továbbra is mindkettőt letiltja.
        assert "OUTP:ENAB OFF" not in cmds


class TestConnectDisablesOutputTimer:
    """Review-kör 5, Hardver/Műszervezérlés HW-R5-C (P3, felhasználói döntés
    2026-09-10): a PSU Output Timer funkciója (2220-30-1/prog.txt 1577-1642. sor)
    egy korábbi session/front panel beállítása miatt sticky maradhat -- egy
    aktivált, nem-alapértelmezett időtartamú timer egy hosszú CHARGE lépést
    a szoftver tudta nélkül lekapcsolhatna. connect() mindkét csatornán
    explicit OFF-ra állítja."""

    def test_connect_disables_output_timer_on_both_channels(self):
        psu = Keithley2220PSU()
        res = MagicMock()
        res.query.side_effect = _connect_query_ok
        rm = MagicMock()
        rm.open_resource.return_value = res
        psu.connect("USB0::mock", rm=rm)
        # 2026-09-12, DFAM-039/F7: a csatorna-valasztas + timer-tiltas
        # EGYETLEN atomikus queryken megy csatornankent (l.
        # _apply_config_atomic()), nem kulon write()-okkent.
        qs = queries(res)
        assert sum(1 for q in qs if "OUTP:TIM:STAT OFF" in q) == 2
        assert any(q.startswith("INST:SEL CH1;") and "OUTP:TIM:STAT OFF" in q for q in qs)
        assert any(q.startswith("INST:SEL CH2;") and "OUTP:TIM:STAT OFF" in q for q in qs)

    def test_timer_disable_failure_does_not_block_connect(self):
        """Best-effort, NEM safety-invariáns -- egy kommunikációs hiba itt
        nem rosszabb, mint a fix előtti állapot, tehát nem szabad, hogy
        meghiúsítsa a teljes connect()-et (ellentétben all_outputs_off()-fal,
        aminek a hibáját connect() TOVÁBBRA IS terjeszti)."""
        psu = Keithley2220PSU()
        res = MagicMock()

        def _write_side_effect(cmd: str) -> None:
            if cmd == "OUTP:TIM:STAT OFF":
                raise Exception("SCPI timeout")

        res.write.side_effect = _write_side_effect
        res.query.side_effect = _connect_query_ok
        rm = MagicMock()
        rm.open_resource.return_value = res
        psu.connect("USB0::mock", rm=rm)  # nem szabad kivételt dobnia
        assert psu.is_connected()

    def test_connect_leaves_ch1_selected(self):
        """[regresszió, independent-verifier lelet, ugyanaz a hibaosztály,
        mint HW-R5-G] _disable_output_timer() a ciklusban CH2-n hagyta
        volna a kiválasztást -- connect() befejeztével a PSU-nak CH1-re
        kell visszaállnia, konzisztensen a többi driver-metódus
        konvenciójával."""
        psu = Keithley2220PSU()
        res = MagicMock()
        res.query.side_effect = _connect_query_ok
        rm = MagicMock()
        rm.open_resource.return_value = res
        psu.connect("USB0::mock", rm=rm)
        assert writes(res)[-1] == "INST:SEL CH1"

    def test_connect_leaves_ch1_selected_even_if_timer_disable_fails(self):
        psu = Keithley2220PSU()
        res = MagicMock()

        def _write_side_effect(cmd: str) -> None:
            if cmd == "OUTP:TIM:STAT OFF":
                raise Exception("SCPI timeout")

        res.write.side_effect = _write_side_effect
        res.query.side_effect = _connect_query_ok
        rm = MagicMock()
        rm.open_resource.return_value = res
        psu.connect("USB0::mock", rm=rm)
        assert writes(res)[-1] == "INST:SEL CH1"


class TestMeasurement:
    def test_measure_output_voltage_returns_float(self):
        psu, res = make_psu()
        res.query.return_value = "+1.44000000E+001"
        result = psu.measure_output_voltage()
        assert isinstance(result, float)
        assert abs(result - 14.4) < 0.01

    def test_measure_output_current_returns_float(self):
        psu, res = make_psu()
        res.query.return_value = "+1.75000000E+000"
        result = psu.measure_output_current()
        assert isinstance(result, float)
        assert abs(result - 1.75) < 0.001

    def test_measure_voltage_queries_meas_volt(self):
        psu, res = make_psu()
        res.query.return_value = "+1.40000000E+001"
        psu.measure_output_voltage()
        assert any("MEAS:VOLT" in q for q in queries(res))

    def test_measure_current_queries_meas_curr(self):
        psu, res = make_psu()
        res.query.return_value = "+1.40000000E+000"
        psu.measure_output_current()
        assert any("MEAS:CURR" in q for q in queries(res))


class TestR13B2MeasurementAddressing:
    """R13-UD-PSU-SERIES-MEAS supersedes the September SCF-010 interpretation."""

    @pytest.mark.parametrize("mode", list(PsuMode))
    @pytest.mark.parametrize("method, command, reply, expected", [
        ("measure_output_voltage", "MEAS:VOLT? CH1", "28.0", 28.0),
        ("measure_output_current", "MEAS:CURR? CH1", "0.125", 0.125),
    ])
    def test_exact_transcript(self, mode, method, command, reply, expected):
        from unittest.mock import call

        psu, res = make_psu(mode)
        res.query.return_value = reply
        assert getattr(psu, method)() == expected
        assert ordered_calls(res) == [("query", command)]
        assert res.mock_calls == [call.clear(), call.query(command)]

    @pytest.mark.parametrize("mode", [PsuMode.SERIES, PsuMode.PARALLEL])
    @pytest.mark.parametrize("method, command, value", [
        ("measure_output_voltage", "MEAS:VOLT? CH1", 0.997345),
        ("measure_output_current", "MEAS:CURR? CH1", 0.0),
    ])
    def test_hil_combined_mode_ch2_timeout(self, mode, method, command, value):
        psu, res = make_psu(mode)

        def hil_query(message):
            if message in ("MEAS:VOLT? CH2", "MEAS:CURR? CH2"):
                raise TimeoutError("HIL: parameterized CH2 does not respond")
            assert message == command
            return str(value)

        res.query.side_effect = hil_query
        assert getattr(psu, method)() == value
        assert ordered_calls(res) == [("query", command)]

    @pytest.mark.parametrize("mode", list(PsuMode))
    @pytest.mark.parametrize("reply, error", [
        ("GARBAGE", ValueError),
        ("NAN", InstrumentInvalidReading),
        ("INF", InstrumentInvalidReading),
        ("1E38", InstrumentInvalidReading),
    ])
    def test_voltage_validation_preserved(self, mode, reply, error):
        psu, res = make_psu(mode)
        res.query.return_value = reply
        with pytest.raises(error):
            psu.measure_output_voltage()
        assert ordered_calls(res) == [("query", "MEAS:VOLT? CH1")]


class TestMeasurementValidation:
    """CLAUDE.md §10.4: aktív töltés alatti PSU readback NaN/Inf/overload
    esete sem maradhat csendben — Python float() ezeket kivétel nélkül
    elfogadja, ezért driver-szintű explicit validáció kell (a
    device_dmm.py::_validate_voltage() mintájára)."""

    def test_nan_voltage_raises_invalid_reading(self):
        psu, res = make_psu()
        res.query.return_value = "NAN"
        with pytest.raises(InstrumentInvalidReading):
            psu.measure_output_voltage()

    def test_inf_current_raises_invalid_reading(self):
        psu, res = make_psu()
        res.query.return_value = "INF"
        with pytest.raises(InstrumentInvalidReading):
            psu.measure_output_current()

    def test_overload_9e37_current_raises_invalid_reading(self):
        """SCPI overload kód: 9.9E37 — float-ként rendkívül nagy."""
        psu, res = make_psu()
        res.query.return_value = "+9.90000000E+037"
        with pytest.raises(InstrumentInvalidReading):
            psu.measure_output_current()

    def test_overload_9e37_voltage_raises_invalid_reading(self):
        psu, res = make_psu()
        res.query.return_value = "+9.90000000E+037"
        with pytest.raises(InstrumentInvalidReading):
            psu.measure_output_voltage()

    def test_negative_overload_raises_invalid_reading(self):
        psu, res = make_psu()
        res.query.return_value = "-9.90000000E+037"
        with pytest.raises(InstrumentInvalidReading):
            psu.measure_output_current()


class TestParseBoolResponse:
    """Teljes kódbázis review 2. kör (2026-09-15, R2-03, P3, PREVENTÍV,
    HIL_PENDING): a numerikus ág toleráns a firmware esetlegesen
    lebegőpontos NR1 dialektusára, konzisztensen a driver
    `read_regulation_state()`/`read_questionable_status()` mintájával —
    ez utóbbi mintát megfigyelt hiba nélkül, kizárólag konzisztencia-célból
    követi (l. `_parse_bool_response()` docstringje). Kizárólag pontos
    0.0/1.0 értéket fogad el — nem szabad naiv `bool(int(float()))`-ot
    használni, mert az hibásan igazra értelmezne egy "2"-t is."""

    def test_accepts_signed_and_float_nr1(self):
        from Prog.drivers.device_psu import _parse_bool_response

        assert _parse_bool_response("1.0", "x") is True
        assert _parse_bool_response("0.0", "x") is False
        assert _parse_bool_response("+1", "x") is True
        assert _parse_bool_response("-0", "x") is False

    def test_rejects_out_of_range_or_nonfinite_numeric(self):
        from Prog.drivers.device_psu import _parse_bool_response

        for bad in ("2", "2.0", "-1", "NAN", "INF", "-INF"):
            with pytest.raises(InstrumentInvalidReading):
                _parse_bool_response(bad, "x")


class TestReadOutputState:
    """Review-kör 5, Hardver/Műszervezérlés HW5-02 (P3): read_output_state()
    olvasás-only diagnosztika — elsődleges (bare OUTP?) és fallback
    (csatorna-scope-olt OUTP:ENAB?) mód, mindkét dokumentált válaszformátum
    (numerikus "1"/"0" és szöveges "Enabled"/"Disabled"), valamint
    ismeretlen válasz hibakezelése (CLAUDE.md §6.4: nincs csendes
    hamis-OFF helyettesítés)."""

    def test_primary_mode_queries_bare_outp(self):
        psu, res = make_psu()
        res.query.return_value = "1"
        result = psu.read_output_state()
        assert result is True
        assert any(q == "OUTP?" for q in queries(res))

    def test_primary_mode_off(self):
        psu, res = make_psu()
        res.query.return_value = "0"
        assert psu.read_output_state() is False

    def test_fallback_mode_queries_both_channels(self):
        # 2026-09-12, DFAM-039/F7 (V4): a csatorna-valasztas + allapot-
        # lekerdezes mostantol EGYETLEN atomikus query()-kent megy
        # csatornankent (l. _apply_atomic_query()), nem kulon write()+query().
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"
        res.query.return_value = "0"
        psu.read_output_state()
        qs = queries(res)
        assert sum(1 for q in qs if "OUTP:ENAB?" in q) == 2
        ch1_idx = next(i for i, q in enumerate(qs) if q.startswith("INST:SEL CH1"))
        ch2_idx = next(i for i, q in enumerate(qs) if q.startswith("INST:SEL CH2"))
        assert ch1_idx < ch2_idx

    def test_fallback_mode_any_channel_enabled_is_true(self):
        """INDEPENDENT fallback esetén bármely engedélyezett csatorna
        potenciálisan élő. Kombinált módban külön regresszió ellenőrzi
        a globális OUTP? használatát, az enable-bitektől függetlenül."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"
        res.query.side_effect = ["0", "0", "0", "1"]
        assert psu.read_output_state() is True

    def test_fallback_mode_both_disabled_is_false(self):
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"
        res.query.side_effect = ["0", "0", "0", "0"]
        assert psu.read_output_state() is False

    def test_fallback_mode_accepts_documented_text_response(self):
        """A gyári kézikönyv OUTP:ENAB?-re "Enabled"/"Disabled" szöveges
        választ dokumentál (2220-30-1/prog.txt 1479-1480. sor) — nem a numerikus
        1|0-t, mint a bare OUTP?."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"
        res.query.side_effect = ["0", "0", "Disabled", "Enabled"]
        assert psu.read_output_state() is True

    def test_fallback_mode_leaves_ch1_selected_after_success(self):
        """[regresszió, HW-R5-G (P3)] a fallback ág CH2-n hagyta a
        csatorna-kiválasztást a hívás után -- a `_select_ch1()`-lel kezdő
        többi metódus (set_output_voltage/set_output_current/output_on)
        konvenciójával konzisztensen, a hívásnak CH1-re kell visszaállnia."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"
        res.query.side_effect = ["0", "0", "0", "1"]
        psu.read_output_state()
        assert writes(res)[-1] == "INST:SEL CH1"

    def test_fallback_mode_leaves_ch1_selected_even_on_error(self):
        """A finally-nek kivétel esetén (pl. értelmezhetetlen CH2-válasz)
        is le kell futnia -- a hívó ne maradjon bizonytalan
        csatorna-állapotban egy hibás readback után sem."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"
        res.query.side_effect = ["0", "0", "0", "GARBAGE"]
        with pytest.raises(InstrumentInvalidReading):
            psu.read_output_state()
        assert writes(res)[-1] == "INST:SEL CH1"

    def test_unknown_response_raises_invalid_reading(self):
        psu, res = make_psu()
        res.query.return_value = "GARBAGE"
        with pytest.raises(InstrumentInvalidReading):
            psu.read_output_state()

    def test_primary_mode_accepts_float_nr1(self):
        """R2-03: bare OUTP? dialektus lebegőpontos NR1 alakban ("1.0")."""
        psu, res = make_psu()
        res.query.return_value = "1.0"
        assert psu.read_output_state() is True

    def test_fallback_mode_accepts_float_nr1(self):
        """R2-03: OUTP:ENAB? dialektus lebegőpontos NR1 alakban ("0.0")."""
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"
        res.query.side_effect = ["0", "0", "0.0", "0.0"]
        assert psu.read_output_state() is False


class TestReadRegulationState:
    """Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2, felhasználói döntés
    2026-09-10): read_regulation_state() -- STAT:OPER:INST:ISUM1:COND?
    bit-dekódolás. Forrás: 2220-30-1/prog.txt 1955-1972. sor (a query maga),
    2560-2584. sor (Table 3-4 OSR bit-tábla: bit0=CV, bit1=CC, bit3=ON),
    2017-2018. sor (kézikönyv saját példája: 9 = ON+CV)."""

    def test_queries_stat_oper_inst_isum1_cond(self):
        psu, res = make_psu()
        res.query.return_value = "0"
        psu.read_regulation_state()
        assert any(q == "STAT:OPER:INST:ISUM1:COND?" for q in queries(res))

    def test_all_clear_returns_all_false(self):
        psu, res = make_psu()
        res.query.return_value = "0"
        state = psu.read_regulation_state()
        assert state == {"CV": False, "CC": False, "ON": False}

    def test_manual_example_9_decodes_as_on_and_cv(self):
        """A kézikönyv saját példája (2017-2018. sor): 9 = ON (bit3) + CV
        (bit0) -- "channel 1 is turned on and in constant voltage mode"."""
        psu, res = make_psu()
        res.query.return_value = "9"
        state = psu.read_regulation_state()
        assert state == {"CV": True, "CC": False, "ON": True}

    def test_cc_mode_decoded(self):
        psu, res = make_psu()
        res.query.return_value = str((1 << 1) | (1 << 3))  # CC + ON
        state = psu.read_regulation_state()
        assert state == {"CV": False, "CC": True, "ON": True}

    def test_float_formatted_nr1_response_tolerated(self):
        psu, res = make_psu()
        res.query.return_value = "9.0"
        state = psu.read_regulation_state()
        assert state["ON"] is True
        assert state["CV"] is True


class TestCombinationMode:
    def test_set_mode_independent_sends_ser_off_and_par_off(self):
        # 2026-09-12, DFAM-039/F7 (V3): atomikus kuldes -- l.
        # _apply_config_atomic().
        psu, res = make_psu()
        psu.set_mode_independent()
        cmds = queries(res)
        assert any("SOUR:OUTP:SER" in c and "OFF" in c for c in cmds)
        assert any("SOUR:OUTP:PAR" in c and "OFF" in c for c in cmds)

    def test_set_mode_series_sends_ser_on(self):
        psu, res = make_psu()
        psu.set_mode_series()
        cmds = queries(res)
        assert any("SOUR:OUTP:SER" in c and "ON" in c for c in cmds)
        assert any("SOUR:OUTP:PAR" in c and "OFF" in c for c in cmds)

    def test_set_mode_parallel_sends_par_on(self):
        psu, res = make_psu()
        psu.set_mode_parallel()
        cmds = queries(res)
        assert any("SOUR:OUTP:PAR" in c and "ON" in c for c in cmds)
        assert any("SOUR:OUTP:SER" in c and "OFF" in c for c in cmds)

    def test_query_combination_mode_independent_returns_off(self):
        psu, res = make_psu()
        res.query.return_value = "0"
        result = psu.query_combination_mode()
        assert any("SOUR:OUTP:SER?" in q for q in queries(res))
        assert result == "OFF"

    def test_query_combination_mode_series_returns_ser(self):
        psu, res = make_psu()
        res.query.side_effect = ["1", "0"]  # SER=1, PAR=0
        result = psu.query_combination_mode()
        assert result == "SER"

    def test_query_combination_mode_parallel_returns_par(self):
        psu, res = make_psu()
        res.query.side_effect = ["0", "1"]  # SER=0, PAR=1
        result = psu.query_combination_mode()
        assert result == "PAR"

    def test_query_combination_mode_unparseable_response_raises(self):
        """Teljes kódbázis review 2. kör (2026-09-16, R2-03-F1, DFAM-004):
        korábban a `== "1"` szigorú összehasonlítás MINDEN nem-pontos-"1"
        választ (pl. csonka/elcsúszott válasz) csendben "OFF"-ra
        értelmezett -- INDEPENDENT módban ez a hívó (main_window.py) által
        ELFOGADOTT válasz, tehát egy értelmezhetetlen readback a kaput
        átengedte volna, miközben a PSU fizikailag SER/PAR módban lehet.
        Mutáció, amin bukik: a `_parse_bool_response()` visszaállítása
        `== "1"`-re."""
        psu, res = make_psu()
        res.query.side_effect = ["GARBAGE", "0"]
        with pytest.raises(InstrumentInvalidReading):
            psu.query_combination_mode()

    def test_query_combination_mode_word_form_series_detected(self):
        """A gyártói kézikönyv a KÖZVETLENÜL szomszédos OUTP:ENAB?
        lekérdezéshez szó-formát dokumentál ("Disabled"/"Enabled") -- ez a
        két lekérdezés (SOUR:OUTP:SER?/PAR?) esetén NEM dokumentált
        explicit, de a driver már toleráns rá a testvér readbackekhez
        hasonlóan (l. _parse_bool_response() docstringje)."""
        psu, res = make_psu()
        res.query.side_effect = ["Enabled", "Disabled"]
        assert psu.query_combination_mode() == "SER"

    def test_query_combination_mode_word_form_both_off_returns_off(self):
        psu, res = make_psu()
        res.query.side_effect = ["Disabled", "Disabled"]
        assert psu.query_combination_mode() == "OFF"

    def test_query_combination_mode_float_nr1_series_detected(self):
        psu, res = make_psu()
        res.query.side_effect = ["+1.0", "0"]
        assert psu.query_combination_mode() == "SER"

    def test_set_mode_updates_combination_mode(self):
        psu, res = make_psu()
        psu.set_mode_series()
        assert psu.combination_mode == PsuMode.SERIES

    def test_set_mode_parallel_updates_combination_mode(self):
        psu, res = make_psu()
        psu.set_mode_parallel()
        assert psu.combination_mode == PsuMode.PARALLEL


class TestLastCmdTracking:
    def test_last_cmd_updated_after_write(self):
        psu, res = make_psu()
        psu.set_output_voltage(14.4)
        assert "SOUR:VOLT" in psu._last_cmd

    def test_check_error_includes_last_cmd(self):
        psu, res = make_psu()
        psu._last_cmd = "SOUR:VOLT 15.30"
        res.query.return_value = '+170,"Invalid command"'
        errors = psu.check_error()
        assert len(errors) > 0
        assert "last:" in errors[0]
        assert "SOUR:VOLT" in errors[0]

    def test_no_error_does_not_include_last_cmd(self):
        psu, res = make_psu()
        psu._last_cmd = "SOUR:VOLT 14.4"
        res.query.return_value = '+0,"No error"'
        errors = psu.check_error()
        assert errors == []


class TestBasicScpi:
    def test_idn_queries_idn(self):
        psu, res = make_psu()
        res.query.return_value = "KEITHLEY,2220-30-1,SN,FW"
        result = psu.idn()
        assert "*IDN?" in queries(res)
        assert "KEITHLEY" in result

    def test_reset_sends_rst(self):
        psu, res = make_psu()
        psu.reset()
        assert any("*RST" in c for c in writes(res))

    def test_clear_status_sends_cls(self):
        psu, res = make_psu()
        psu.clear_status()
        assert any("*CLS" in c for c in writes(res))

    def test_check_error_no_error_returns_empty(self):
        psu, res = make_psu()
        res.query.return_value = '+0,"No error"'
        errors = psu.check_error()
        assert errors == []

    def test_check_error_with_error_returns_list(self):
        psu, res = make_psu()
        res.query.return_value = '-102,"Syntax error"'
        errors = psu.check_error()
        assert len(errors) > 0

    def test_is_connected_true_when_connected(self):
        psu, res = make_psu()
        assert psu.is_connected() is True

    def test_is_connected_false_when_not_connected(self):
        psu = Keithley2220PSU()
        assert psu.is_connected() is False


class TestRetryLogic:
    def test_write_retries_on_exception(self):
        psu, res = make_psu()
        call_count = 0

        def flaky_write(cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("comm error")

        res.write.side_effect = flaky_write
        psu.set_output_voltage(14.0)  # INST:SEL CH1 fails once, then succeeds

    def test_write_raises_after_max_retries(self):
        psu, res = make_psu()
        # 2026-09-12, DFAM-039/F7: set_output_voltage() mostantol _query()-n
        # megy (_apply_config_atomic()) -- a retry-kimerules szimulaciojat
        # ennek megfeleloen kell injektalni.
        res.query.side_effect = Exception("persistent comm error")
        with pytest.raises(InstrumentTimeoutError):
            psu.set_output_voltage(14.0)

    def test_write_without_connect_raises_clear_error_not_attribute_error(self):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] ha
        connect() sosem történt meg (self._resource is None), a hívásnak
        explicit, egyértelmű kivételt kell adnia — nem egy AttributeError-t
        3× feleslegesen újrapróbálva, félrevezető "write failed after 3
        retries" üzenettel."""
        psu = Keithley2220PSU()
        with pytest.raises(InstrumentTimeoutError, match="nincs aktív kapcsolat"):
            psu._write("TEST")

    def test_query_without_connect_raises_clear_error_not_attribute_error(self):
        psu = Keithley2220PSU()
        with pytest.raises(InstrumentTimeoutError, match="nincs aktív kapcsolat"):
            psu._query("SYST:ERR?")

    def test_write_clears_resource_before_every_attempt_including_first(self):
        """[regresszió, teljes kódbázis review 2. kör (2026-08-28) + 3. kör
        (2026-08-29) P1 kiegészítés] VISA device-clear-t küldünk MINDEN
        kísérlet előtt, nem csak retry előtt — a hívások HATÁRÁN (egy előző
        hívás retry-kimerülése UTÁN a KÖVETKEZŐ hívás ELSŐ kísérlete előtt)
        is szükséges a puffertisztítás, analóg a pico_bq_transport.py-ban
        dokumentáltan kijavított puffer-szinkron-vesztés hibával."""
        psu, res = make_psu()
        call_count = 0

        def flaky_write(cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("comm error")

        res.write.side_effect = flaky_write
        psu._write("TEST")

        assert res.clear.call_count == 2  # 1x az első (elhasaló), 1x a retry előtt

    def test_query_clears_resource_even_when_first_attempt_succeeds(self):
        psu, res = make_psu()
        psu._query("SYST:ERR?")
        assert res.clear.call_count == 1


# ---------------------------------------------------------------------- #
# R1 B2 (2026-10-25): F-03 (egylövéses energizáló üzenetek) + F-04        #
# (INDEPENDENT: CH2 OUTP:ENAB OFF az első ON-t hordozó üzenet előtt).     #
# Terv: Folyamatok/work/collab/R1/B1B2_fixplan.md "B2" +                   #
# B1B2_fixplan_FINAL.md "B2 kötelező változtatások".                       #
# ---------------------------------------------------------------------- #

_CH1_ENABLE_ON = "INST:SEL CH1;:OUTP:ENAB ON;*OPC?"
_CH2_ENABLE_ON = "INST:SEL CH2;:OUTP:ENAB ON;:INST:SEL CH1;*OPC?"
_CH2_DISABLE = "INST:SEL CH2;:OUTP:ENAB OFF;:INST:SEL CH1;*OPC?"
_BARE_OUTP_ON = "OUTP ON;*OPC?"
_BARE_OUTP_OFF = "OUTP OFF;*OPC?"


def _fail_first_matching_query(res: MagicMock, target: str) -> None:
    """Elveszett-nyugta modell MagicMock resource-on: a `target`-tel
    PONTOSAN egyező ELSŐ query dob, minden további (ugyanilyen is) "1"-et
    ad -- tehát egy vak retry SIKERÜLNE. Ez teszi diszkriminálóvá az
    egylövéses teszteket: a régi (retry-zó) kód itt kivétel nélkül,
    kétszer küldte ki ugyanazt az ON-üzenetet."""
    state = {"failed": False}

    normal_reply = res.query.side_effect

    def _side_effect(cmd: str) -> str:
        if cmd == target and not state["failed"]:
            state["failed"] = True
            raise TimeoutError("simulated lost *OPC? reply")
        return normal_reply(cmd)

    res.query.side_effect = _side_effect


class _Stateful2220Resource:
    """Állapotot tartó Keithley 2220-30-1 VISA-resource fake (R1 F-04,
    DFAM-005/006: a mock a sessionön át megmaradó hardverállapotot is
    modellezze -- rules/40-instruments.md).

    A viselkedés a gyári kézikönyv SZÖVEGÉBŐL jön
    (Leírások/műszer_manualok/2220-30-1/prog.txt), NEM a driver
    feltevéséből:
      * `INSTrument:SELect {CH1|CH2|CH3}` -- "This command is used to
        switch the current channel command." (prog.txt ~1117-1128);
      * `[SOURce:]OUTPut:ENABle` -- "This command enables or disables the
        current channel. This command performs the same function as the
        'Channel Enable' selection in the menu." (prog.txt ~1477-1495) --
        tehát MENÜ-állapot, ami a műszeren a VISA-sessionön túl is
        megmarad: a konstruktor a KORÁBBI sessionből (pl. egy SER/PAR
        output_on() után, HIL-SCF010) örökölt CH2-engedélyt kapja;
      * `[SOURce:]OUTPut[:STATe][:ALL] {0|1|ON|OFF}` -- "This command
        turns all of the enabled output channels on or off." (prog.txt
        ~1553-1568) -- a bare OUTP ON a kiadás pillanatában ENGEDÉLYEZETT
        MINDEN csatornát energizálja.
    Modell-korlátok (nem a kézikönyvből, ezért szándékosan NEM modellezve):
    a manual az OUTP:ENAB argumentumaira csak "0|1"-et ír, a driver
    "ON"/"OFF"-ot küld (HIL-SCF010-ben a műszer elfogadta: CODE_OBSERVED/
    HIL) -- a fake mindkettőt elfogadja; egy MÁR bekapcsolt kimenet
    melletti OUTP:ENAB-változás hatását a manual nem írja le, a fake nem
    módosítja az `energized` halmazt. Ismeretlen SCPI-egység ->
    AssertionError (ne nyeljen el csendben egy nem modellezett
    parancsot).

    R16-B14: a mód/enable kölcsönhatás külön HIL-forrása a 2026-11-08-i
    PSU_SERIES_ENAB_CHAR két futása (SN9204604, fw 1.15-1.05): SER ON
    engedélyez 1/1-re; globális OFF és ENAB ON megőrzi a SERIES-t;
    ENAB OFF kiléptet, de nem tilt. Gyártói szerződés SOURCE_REQUIRED,
    a globális OUTP ON SERIES-beli fizikai hatása HIL_PENDING.
    """

    def __init__(self, ch2_enabled_from_previous_session: bool = True) -> None:
        self.selected = "CH1"
        self.actual_mode = "OFF"
        self.enabled = {"CH1": True, "CH2": ch2_enabled_from_previous_session}
        self.output_state = False
        self.energized: set[str] = set()
        self.messages: list[str] = []
        self.clear_calls = 0
        self.timeout: object = None
        # callable(msg) -> bool: True esetén a TELJES üzenet végrehajtás
        # ELŐTT elvész (transzportkivétel, a műszer nem kapta meg).
        self.fail_before_execute = None

    def clear(self) -> None:
        self.clear_calls += 1

    def write(self, msg: str) -> None:
        self._handle(msg)

    def query(self, msg: str) -> str:
        return self._handle(msg)

    def _handle(self, msg: str) -> str:
        self.messages.append(msg)
        if self.fail_before_execute is not None and self.fail_before_execute(msg):
            raise TimeoutError("simulated transport failure (message not executed)")
        response = ""
        for raw_unit in msg.split(";"):
            unit = raw_unit.strip().lstrip(":").upper()
            result = self._execute(unit)
            if result is not None:
                response = result
        return response

    def _execute(self, unit: str) -> "str | None":
        if unit == "*OPC?":
            return "1"
        if unit.startswith("INST:SEL "):
            ch = unit.split()[1]
            assert ch in self.enabled, f"ismeretlen csatorna: {ch!r}"
            self.selected = ch
            return None
        if unit in ("SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"):
            return str(int(self.actual_mode == ("SER" if "SER?" in unit else "PAR")))
        if unit.startswith(("SOUR:OUTP:SER ", "SOUR:OUTP:PAR ")):
            header, arg = unit.split()
            if arg in ("ON", "1"):
                self.actual_mode = "SER" if header.endswith("SER") else "PAR"
                if self.actual_mode == "SER":
                    self.enabled = {"CH1": True, "CH2": True}
            elif self.actual_mode == ("SER" if header.endswith("SER") else "PAR"):
                self.actual_mode = "OFF"
            return None
        if unit == "OUTP:ENAB?":
            return str(int(self.enabled[self.selected]))
        if unit.startswith("OUTP:ENAB "):
            arg = unit.split()[1]
            assert arg in ("0", "1", "ON", "OFF"), unit
            # R16 HIL: SERIES-ben OFF módvesztést okoz, de nem tilt;
            # ON megtartja a módot. Globális OFF nem módosítja az enable-t.
            if self.actual_mode == "SER" and arg in ("0", "OFF"):
                self.actual_mode = "OFF"
            else:
                self.enabled[self.selected] = arg in ("1", "ON")
            return None
        if unit == "OUTP?":
            return "1" if self.output_state else "0"
        if unit.startswith("OUTP "):
            arg = unit.split()[1]
            assert arg in ("0", "1", "ON", "OFF"), unit
            if arg in ("1", "ON"):
                self.output_state = True
                self.energized = {ch for ch, en in self.enabled.items() if en}
            else:
                self.output_state = False
                self.energized = set()
            return None
        if unit.startswith(("SOUR:VOLT ", "SOUR:CURR ")):
            return None  # setpontokat a fake nem modellez
        raise AssertionError(f"a fake nem modellezi ezt az SCPI-egységet: {unit!r}")


def _make_psu_on_stateful_fake(
    mode: PsuMode = PsuMode.INDEPENDENT,
    output_cmd: str = "OUTP",
    ch2_enabled_from_previous_session: bool = True,
) -> tuple[Keithley2220PSU, _Stateful2220Resource]:
    psu = Keithley2220PSU(combination_mode=mode)
    fake = _Stateful2220Resource(ch2_enabled_from_previous_session)
    fake.actual_mode = {PsuMode.INDEPENDENT: "OFF", PsuMode.SERIES: "SER", PsuMode.PARALLEL: "PAR"}[mode]
    if mode == PsuMode.SERIES:
        fake.enabled = {"CH1": True, "CH2": True}
    psu._resource = fake
    psu._connected = True
    psu._output_cmd = output_cmd
    return psu, fake


class TestR1F03EnergizingCommandsSingleAttempt:
    """[regresszió, R1 B2 F-03, 2026-10-25, rules/10-safety.md 6. pont:
    "OUTPUT/INPUT ON parancsot ne ismételj vakon, ha az előző kimenetele
    bizonytalan"] az output_on() mindhárom ON-t hordozó üzenete (CH1
    OUTP:ENAB ON, SER/PAR CH2 OUTP:ENAB ON, bare OUTP ON) korábban a
    retry-zó _query()-n ment: egy elveszett *OPC? válasz után a driver
    VAKON újraküldte, és a második siker eltüntette a bizonytalanságot a
    hívó elől. Mostantól hívásonként egylövéses (single_attempt=True), a
    kivétel propagál, a flag konzervatívan True. Az OFF- és olvasási
    utak retry-ja VÁLTOZATLAN (guard tesztek)."""

    def test_bare_outp_on_lost_ack_sent_once_and_raises(self):
        psu, res = make_psu()
        _fail_first_matching_query(res, _BARE_OUTP_ON)
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()
        assert queries(res).count(_BARE_OUTP_ON) == 1
        assert psu.output_commanded_on is True

    @pytest.mark.parametrize("dialect", ["OUTP", "OUTP:ENAB"])
    def test_ch1_enable_on_lost_ack_sent_once_raises_no_bare_outp_on(self, dialect):
        psu, res = make_psu()
        psu._output_cmd = dialect
        _fail_first_matching_query(res, _CH1_ENABLE_ON)
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()
        assert queries(res).count(_CH1_ENABLE_ON) == 1
        assert _BARE_OUTP_ON not in queries(res)
        assert psu.output_commanded_on is True

    @pytest.mark.parametrize("mode", [PsuMode.SERIES, PsuMode.PARALLEL])
    def test_ser_par_ch2_enable_on_lost_ack_sent_once_raises(self, mode):
        psu, res = make_psu(mode)
        _fail_first_matching_query(res, _CH2_ENABLE_ON)
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()
        assert queries(res).count(_CH2_ENABLE_ON) == 1
        assert _BARE_OUTP_ON not in queries(res)
        assert psu.output_commanded_on is True

    def test_single_attempt_query_sends_once_after_one_clear(self):
        """Az egylövéses primitív: pontosan egy kísérlet, az első kísérlet
        előtti Device Clear (PSU: mindig) MEGMARAD."""
        psu, res = make_psu()
        res.query.side_effect = Exception("simulated timeout")
        with pytest.raises(InstrumentTimeoutError):
            psu._query("X", single_attempt=True)
        assert res.query.call_count == 1
        assert res.clear.call_count == 1

    def test_default_query_still_retries_max_retries_times(self):
        psu, res = make_psu()
        res.query.side_effect = Exception("simulated timeout")
        with pytest.raises(InstrumentTimeoutError):
            psu._query("X")
        assert res.query.call_count == psu.max_retries

    def test_all_outputs_off_primary_still_retries_after_first_failure(self):
        """Guard: a safety-kritikus OFF-út retry-ja NEM változhat."""
        psu, res = make_psu()
        psu.output_commanded_on = True
        _fail_first_matching_query(res, _BARE_OUTP_OFF)
        psu.all_outputs_off()  # a második kísérlet sikeres -> nincs kivétel
        assert queries(res).count(_BARE_OUTP_OFF) == 2
        assert psu.output_commanded_on is False

    def test_all_outputs_off_fallback_channel_off_still_retries(self):
        psu, res = make_psu()
        psu._output_cmd = "OUTP:ENAB"
        psu.output_commanded_on = True
        target = "INST:SEL CH1;:OUTP:ENAB OFF;*OPC?"
        _fail_first_matching_query(res, target)
        psu.all_outputs_off()
        assert queries(res).count(target) == 2
        assert psu.output_commanded_on is False

    def test_max_retries_unchanged(self):
        assert Keithley2220PSU().max_retries == 3


class TestR1F04IndependentCh2DisabledBeforeFirstOn:
    """[regresszió, R1 B2 F-04, 2026-10-25, DFAM-032 kiemelés] INDEPENDENT
    módban az output_on() korábban NEM nyúlt a CH2-höz -- egy korábbi
    SER/PAR session (vagy előlapi menü) után a CH2 OUTP:ENAB sticky ON
    maradhat (HIL-SCF010), és a bare OUTP ON a kézikönyv szerint "all of
    the enabled output channels"-t bekapcsolja (prog.txt,
    [SOURce:]OUTPut[:STATe][:ALL]) -- a CH2 egy más kombinációs módból
    maradt setponttal energizálódott volna. Mostantól a try ELSŐ
    műveleteként `INST:SEL CH2;:OUTP:ENAB OFF;:INST:SEL CH1;*OPC?` megy ki,
    NORMÁL (OFF-típusú) retry-jal.

    A "hiba után nincs ON" garancia KIZÁRÓLAG a tesztelt
    TRANSZPORTKIVÉTELRE szól: egy firmware-oldali SCPI-elutasítás nem
    szükségképpen Python-kivétel (`_apply_config_atomic()` nem olvassa a
    hibasort). A fizikai elfogadás HIL_PENDING."""

    @pytest.mark.parametrize("dialect", ["OUTP", "OUTP:ENAB"])
    def test_independent_disables_ch2_before_first_on_message(self, dialect):
        psu, res = make_psu(PsuMode.INDEPENDENT)
        psu._output_cmd = dialect
        res.query.return_value = "1"
        psu.output_on()
        calls = ordered_calls(res)
        i_ch2_off = calls.index(("query", _CH2_DISABLE))
        i_ch1_on = calls.index(("query", _CH1_ENABLE_ON))
        i_bare_on = calls.index(("query", _BARE_OUTP_ON))
        assert i_ch2_off < i_ch1_on < i_bare_on
        before = [cmd for _kind, cmd in calls[:i_ch2_off]]
        assert not any("ENAB ON" in c or c.startswith("OUTP ON") for c in before), before

    @pytest.mark.parametrize("dialect", ["OUTP", "OUTP:ENAB"])
    def test_independent_sticky_ch2_enable_from_previous_session_not_energized(self, dialect):
        """Állapotot tartó fake: a korábbi sessionből CH2 OUTP:ENAB=ON
        maradt. A régi kódon a bare OUTP ON CH1-et ÉS CH2-t is
        energizálta."""
        psu, fake = _make_psu_on_stateful_fake(PsuMode.INDEPENDENT, dialect)
        psu.output_on()
        assert fake.energized == {"CH1"}
        assert fake.enabled["CH2"] is False
        assert fake.selected == "CH1"
        assert psu.output_commanded_on is True

    def test_independent_ch2_disable_failure_raises_and_sends_no_on(self):
        psu, fake = _make_psu_on_stateful_fake(PsuMode.INDEPENDENT)
        fake.fail_before_execute = lambda msg: msg.startswith("INST:SEL CH2")
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()
        assert fake.messages.count(_CH2_DISABLE) == psu.max_retries
        assert not any("ENAB ON" in m or m.startswith("OUTP ON") for m in fake.messages)
        assert fake.energized == set()
        assert fake.enabled["CH2"] is True  # a sticky állapot megmaradt -- ezért nem mehet ON
        assert psu.output_commanded_on is True  # konzervatív flag-szerződés

    def test_independent_ch2_disable_transient_failure_retried_then_on_messages_sent_once(self):
        psu, fake = _make_psu_on_stateful_fake(PsuMode.INDEPENDENT)
        state = {"failed": False}

        def _fail_first_ch2_disable(msg: str) -> bool:
            if msg == _CH2_DISABLE and not state["failed"]:
                state["failed"] = True
                return True
            return False

        fake.fail_before_execute = _fail_first_ch2_disable
        psu.output_on()  # az OFF-típusú üzenet retry-ja sikeres -> nincs kivétel
        assert fake.messages.count(_CH2_DISABLE) == 2
        assert fake.messages.count(_CH1_ENABLE_ON) == 1
        assert fake.messages.count(_BARE_OUTP_ON) == 1
        assert fake.energized == {"CH1"}

    @pytest.mark.parametrize("mode", [PsuMode.SERIES, PsuMode.PARALLEL])
    def test_series_parallel_send_no_ch2_disable(self, mode):
        """Scope-pin: a CH2-tiltás KIZÁRÓLAG INDEPENDENT módra szól -- SER/PAR
        módban a CH2-nek engedélyezettnek kell lennie (HW2-02). Nem
        diszkrimináló (a régi kódon is zöld), a fix határát rögzíti."""
        psu, fake = _make_psu_on_stateful_fake(
            mode, ch2_enabled_from_previous_session=False,
        )
        psu.output_on()
        assert not any("ENAB OFF" in m for m in fake.messages), fake.messages
        assert fake.messages.count(_CH2_ENABLE_ON) == 1
        assert fake.energized == {"CH1", "CH2"}


class TestR1F03EnergizingScpiLiteralsUseSingleAttempt:
    """[R1 B2 F-03, miner SPR (b), B1B2_fixplan_FINAL.md B2/1] AST-alapú
    meta-teszt (nem grep: l. feedback_grep_sibling_sweep_misses_aliased_
    api_calls) a SCPI-driverekre: MINDEN " ON"-t hordozó string-literál
    (f-string konstans része is) egy `_query`/`_write`/
    `_apply_config_atomic` hívás argumentuma, `single_attempt=True`
    kulcsszóval -- kivéve az explicit allowlistben felsorolt, NEM
    energizáló ON-okat (kombinációs mód, DMM autorange).

    Dokumentált scope-határ (NS-04, P3 látens): a Load `FORC:TRIG`
    (fire_transient_pulse()) a retry-zó `_write`-on marad -- csak a nem
    produkciós HARDWARE_PULSE triggerúton érhető el. A teszt rögzíti, hogy
    ez a literál létezik (a kivétel ne rothadjon el észrevétlenül)."""

    _DRIVERS = ("device_psu.py", "device_load.py", "device_dmm.py")
    _NON_ENERGIZING_ON_ALLOWLIST = frozenset({
        ("device_psu.py", "SOUR:OUTP:SER ON"),
        ("device_psu.py", "SOUR:OUTP:PAR ON"),
        ("device_dmm.py", "RES:RANG:AUTO ON"),
        # Az autozero nem kapcsol energiaátadást; Q-6 rev4 ON forma.
        ("device_dmm.py", "VOLT:DC:ZERO:AUTO ON;*OPC?"),
    })
    _SCOPE_BOUNDARY = frozenset({("device_load.py", "FORC:TRIG")})
    _KNOWN_ENERGIZING = frozenset({
        ("device_psu.py", "OUTP:ENAB ON"),
        ("device_psu.py", "OUTP ON;*OPC?"),
        ("device_load.py", "{} ON"),
    })
    _PRIMITIVES = frozenset({"_query", "_write", "_apply_config_atomic"})

    @staticmethod
    def _literal_text(node):
        import ast

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
            )
        return None

    def _scan(self):
        import ast
        import pathlib
        import re

        on_token = re.compile(r" ON\b")
        drivers_dir = pathlib.Path(__file__).resolve().parents[1] / "drivers"
        found = []  # (file, text, lineno, call_name, single_attempt)
        for name in self._DRIVERS:
            tree = ast.parse((drivers_dir / name).read_text(encoding="utf-8"))
            parents = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            for node in ast.walk(tree):
                text = self._literal_text(node)
                if text is None:
                    continue
                parent = parents.get(node)
                if isinstance(parent, ast.JoinedStr):
                    continue  # az f-string egészeként már számolva
                if isinstance(parent, ast.Expr):
                    continue  # docstring / önálló string-kifejezés
                if not (on_token.search(text) or "FORC:TRIG" in text):
                    continue
                call = parent
                while call is not None and not isinstance(call, ast.Call):
                    call = parents.get(call)
                call_name = None
                single = False
                if call is not None and node in call.args:
                    if isinstance(call.func, ast.Attribute):
                        call_name = call.func.attr
                    single = any(
                        kw.arg == "single_attempt"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                        for kw in call.keywords
                    )
                found.append((name, text, node.lineno, call_name, single))
        return found

    def test_every_energizing_on_literal_goes_through_single_attempt(self):
        found = self._scan()
        violations = [
            f for f in found
            if (f[0], f[1]) not in self._NON_ENERGIZING_ON_ALLOWLIST
            and (f[0], f[1]) not in self._SCOPE_BOUNDARY
            and not (f[3] in self._PRIMITIVES and f[4])
        ]
        assert violations == [], (
            "ON-t hordozó SCPI-literál nem egylövéses úton (vagy új, nem "
            f"allowlistelt ON): {violations!r}"
        )

    def test_known_energizing_literals_are_found_not_vacuous(self):
        found = {(f[0], f[1]) for f in self._scan()}
        assert self._KNOWN_ENERGIZING <= found, found

    def test_allowlist_and_scope_boundary_entries_still_exist(self):
        found = {(f[0], f[1]) for f in self._scan()}
        stale = (self._NON_ENERGIZING_ON_ALLOWLIST | self._SCOPE_BOUNDARY) - found
        assert stale == set(), f"elavult allowlist/scope-határ bejegyzés: {stale!r}"


class _StatefulConnect2220Resource(_Stateful2220Resource):
    """Connect-capable stateful fake: inherited outputs, error queue and timer."""

    def __init__(self, output_on_from_previous_session=True,
                 ch2_enabled_from_previous_session=False):
        super().__init__(ch2_enabled_from_previous_session)
        self.syst_err_reply = NO_ERROR_REPLY
        self.closed = False
        if output_on_from_previous_session:
            self.output_state = True
            self.energized = {ch for ch, en in self.enabled.items() if en}

    def close(self):
        self.closed = True

    def _execute(self, unit):
        if unit == "*CLS":
            return None
        if unit == "SYST:ERR?":
            return self.syst_err_reply
        if unit == "OUTP:TIM:STAT OFF":
            return None
        return super()._execute(unit)


def _connect_stateful(fake):
    psu = Keithley2220PSU()
    psu.connect("MOCK::RESOURCE", rm=MagicMock(open_resource=lambda *_: fake))
    return psu


def _energizing(messages):
    import re

    on = re.compile(r"(^|;|:)\s*(OUTP|OUTP:ENAB)\s+(ON|1)\b", re.IGNORECASE)
    return [m for m in messages if on.search(m)]


class _UnsupportedStartupCommandResource(_StatefulConnect2220Resource):
    """A forbidden startup command would time out or report error 170."""

    def __init__(self, failure):
        super().__init__(ch2_enabled_from_previous_session=True)
        self.failure = failure
        self.errors = []
        self.unsupported_attempts = 0

    def _execute(self, unit):
        if unit.startswith("OUTP:" + "PO" + "N"):
            self.unsupported_attempts += 1
            self.errors.append('170, "Invalid command"')
            if self.failure == "timeout":
                raise TimeoutError("unsupported startup command")
            return "" if self.failure == "empty" else '170, "Invalid command"'
        if unit == "SYST:ERR?" and self.errors:
            return self.errors.pop(0)
        return super()._execute(unit)


class TestR13B4ConnectTranscript:
    @pytest.mark.parametrize("failure", ["timeout", "empty", "error"])
    @pytest.mark.parametrize("fallback", [False, True], ids=["bare", "fallback"])
    def test_connect_omits_unsupported_startup_command(self, failure, fallback):
        from time import perf_counter

        fake = _UnsupportedStartupCommandResource(failure)
        if fallback:
            fake.syst_err_reply = '-113,"Undefined header"'
        start = perf_counter()
        psu = _connect_stateful(fake)
        elapsed = perf_counter() - start

        assert elapsed < 1.0
        assert psu.is_connected()
        assert fake.unsupported_attempts == 0
        assert fake.errors == []
        assert psu.output_commanded_on is False
        assert fake.output_state is False and fake.energized == set()
        off = [
            "OUTP OFF;*OPC?",
            "SOUR:OUTP:SER?", "SOUR:OUTP:PAR?",
            "INST:SEL CH1;:OUTP:ENAB OFF;*OPC?",
            "INST:SEL CH2;:OUTP:ENAB OFF;*OPC?",
        ] if fallback else ["OUTP OFF;*OPC?"]
        assert fake.messages == [
            "*CLS;:OUTP OFF;:SYST:ERR?",
            *off,
            "INST:SEL CH1;:OUTP:TIM:STAT OFF;*OPC?",
            "INST:SEL CH2;:OUTP:TIM:STAT OFF;*OPC?",
            "INST:SEL CH1",
        ]
        if fallback:
            assert fake.enabled == {"CH1": False, "CH2": False}
        assert fake.selected == "CH1"
        assert _energizing(fake.messages) == []


class TestR14B2RegulationValidity:
    """R14-B2: SERIES ISUM nem igazol kombinált CV/CC-regulációt."""

    @pytest.mark.parametrize("mode", list(PsuMode))
    @pytest.mark.parametrize(
        ("raw", "cv", "cc", "on"),
        [
            ("0", False, False, False),
            ("8", False, False, True),
            ("9", True, False, True),
            ("10", False, True, True),
            ("11", True, True, True),
        ],
    )
    def test_payload_and_read_only_query(self, mode, raw, cv, cc, on):
        psu, res = make_psu(mode)
        res.query.return_value = raw

        state = psu.read_regulation_state()

        if mode == PsuMode.SERIES:
            assert state == {"CVCC_VALID": False, "ON": on}
        else:
            assert state == {"CV": cv, "CC": cc, "ON": on}
        assert queries(res) == ["STAT:OPER:INST:ISUM1:COND?"]
        assert writes(res) == []
        assert psu.combination_mode == mode
        assert psu.output_commanded_on is False


class TestR14B2MockRegulationValidity:
    @pytest.mark.parametrize("mode", ["OFF", "SER", "PAR"])
    @pytest.mark.parametrize(
        ("cv", "cc", "on"),
        [
            (False, False, False),
            (False, False, True),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        ],
    )
    def test_mock_matches_driver_contract(self, mode, cv, cc, on):
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        psu = MockPSU(
            combination_mode=mode,
            regulation_cv=cv,
            regulation_cc=cc,
            regulation_on=on,
        )

        state = psu.read_regulation_state()

        if mode == "SER":
            assert state == {"CVCC_VALID": False, "ON": on}
        else:
            assert state == {"CV": cv, "CC": cc, "ON": on}
        assert psu.calls == ["read_regulation_state()"]

    @pytest.mark.parametrize("mode", ["OFF", "SER", "PAR"])
    def test_timeout_remains_explicit(self, mode):
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        psu = MockPSU(
            combination_mode=mode,
            simulate_regulation_state_readback_timeout=True,
        )
        with pytest.raises(InstrumentTimeoutError):
            psu.read_regulation_state()


class TestR16B14ModeGuard:
    """R16 HIL-szerződés; a fake állapota független a driver kért módjától."""

    @pytest.mark.parametrize("stored", list(PsuMode))
    def test_mode_mismatch_blocks_all_on_and_keeps_requested_mode(self, stored):
        psu, fake = _make_psu_on_stateful_fake(stored)
        fake.actual_mode = "OFF" if stored != PsuMode.INDEPENDENT else "SER"
        before = dict(fake.enabled)
        with pytest.raises(InstrumentInvalidReading, match="mód eltérés"):
            psu.output_on()
        assert _energizing(fake.messages) == []
        assert fake.enabled == before
        assert psu.combination_mode == stored
        assert not any("SOUR:OUTP:SER " in m or "SOUR:OUTP:PAR " in m for m in fake.messages)

    @pytest.mark.parametrize("query", ["SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"])
    def test_mode_read_error_blocks_all_on(self, query):
        psu, fake = _make_psu_on_stateful_fake(PsuMode.SERIES)
        fake.fail_before_execute = lambda msg: msg == query
        with pytest.raises(InstrumentTimeoutError):
            psu.output_on()
        assert _energizing(fake.messages) == []

    @pytest.mark.parametrize("mode", [PsuMode.SERIES, PsuMode.PARALLEL])
    def test_fallback_combined_activation_is_rejected(self, mode):
        psu, fake = _make_psu_on_stateful_fake(mode, "OUTP:ENAB")
        with pytest.raises(InstrumentInvalidReading, match="fallback"):
            psu.output_on()
        assert _energizing(fake.messages) == []
        assert psu.combination_mode == mode

    @pytest.mark.parametrize("after, count", [
        (_CH1_ENABLE_ON, 1), (_CH2_ENABLE_ON, 2), (_BARE_OUTP_ON, 3),
    ])
    def test_mode_loss_between_commands_or_after_on_is_detected(self, after, count, monkeypatch):
        psu, fake = _make_psu_on_stateful_fake(PsuMode.SERIES)
        original = fake.query

        def query(msg):
            reply = original(msg)
            if msg == after:
                fake.actual_mode = "OFF"
            return reply

        monkeypatch.setattr(fake, "query", query)
        with pytest.raises(InstrumentInvalidReading, match="mód eltérés"):
            psu.output_on()
        assert len(_energizing(fake.messages)) == count
        assert psu.combination_mode == PsuMode.SERIES

    def test_on_readback_must_be_true(self, monkeypatch):
        psu, fake = _make_psu_on_stateful_fake(PsuMode.SERIES)
        original = fake.query
        monkeypatch.setattr(fake, "query", lambda msg: "0" if msg == "OUTP?" else original(msg))
        with pytest.raises(InstrumentInvalidReading, match="bekapcsolás nem igazolt"):
            psu.output_on()

    @pytest.mark.parametrize("mode", list(PsuMode))
    def test_primary_normal_path_reads_mode_before_each_on_and_after(self, mode):
        psu, fake = _make_psu_on_stateful_fake(mode)
        psu.output_on()
        for i, msg in enumerate(fake.messages):
            if _energizing([msg]):
                assert fake.messages[i-2:i] == ["SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"]
        assert fake.messages[-3:] == ["OUTP?", "SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"]
        assert fake.output_state is True
        assert fake.actual_mode == {PsuMode.INDEPENDENT: "OFF", PsuMode.SERIES: "SER", PsuMode.PARALLEL: "PAR"}[mode]

    @pytest.mark.parametrize("requested", list(PsuMode))
    def test_fallback_off_in_actual_series_uses_global_off_and_preserves_mode(self, requested):
        psu, fake = _make_psu_on_stateful_fake(requested, "OUTP:ENAB")
        fake.actual_mode = "SER"
        fake.enabled = {"CH1": True, "CH2": True}
        fake.output_state = True
        assert psu.safe_off() is True
        assert _BARE_OUTP_OFF in fake.messages
        assert not any("ENAB OFF" in m for m in fake.messages)
        assert fake.actual_mode == "SER" and all(fake.enabled.values())
        assert psu.read_output_state() is False
        assert _energizing(fake.messages) == []
        assert not any("SOUR:OUTP:SER " in m for m in fake.messages)

    @pytest.mark.parametrize("failure", ["unsupported", "read_error", "still_on"])
    def test_fallback_off_never_confirms_unknown_or_live_output(self, failure, monkeypatch):
        psu, fake = _make_psu_on_stateful_fake(PsuMode.SERIES, "OUTP:ENAB")
        fake.output_state = True
        original = fake.query

        def query(msg):
            if msg == _BARE_OUTP_OFF and failure in ("unsupported", "still_on"):
                fake.messages.append(msg)
                return "1"  # OPC nyugta, de a globális OFF nem hajtódott végre.
            if msg == "OUTP?" and failure in ("unsupported", "read_error"):
                fake.messages.append(msg)
                raise TimeoutError("globális állapot nem olvasható")
            return original(msg)

        monkeypatch.setattr(fake, "query", query)
        assert psu.safe_off() is False
        assert _BARE_OUTP_OFF in fake.messages
        assert not any("ENAB OFF" in m for m in fake.messages)
        if failure == "still_on":
            assert psu.read_output_state() is True
        else:
            with pytest.raises(InstrumentTimeoutError):
                psu.read_output_state()
        assert psu.output_commanded_on is False

    def test_fallback_unknown_mode_still_attempts_global_off(self):
        psu, fake = _make_psu_on_stateful_fake(output_cmd="OUTP:ENAB")
        fake.fail_before_execute = lambda msg: msg == "SOUR:OUTP:SER?"
        assert psu.safe_off() is True
        assert _BARE_OUTP_OFF in fake.messages
        assert not any("ENAB OFF" in m for m in fake.messages)
        with pytest.raises(InstrumentTimeoutError):
            psu.read_output_state()

    @pytest.mark.parametrize("channel", ["CH1", "CH2"])
    def test_fake_hil_contract_and_state_survives_new_driver(self, channel):
        fake = _Stateful2220Resource(False)
        fake.query("SOUR:OUTP:PAR OFF;:SOUR:OUTP:SER ON;*OPC?")
        assert fake.actual_mode == "SER" and all(fake.enabled.values())
        fake.query("OUTP OFF;*OPC?")
        assert fake.actual_mode == "SER" and all(fake.enabled.values())
        fake.query(f"INST:SEL {channel};:OUTP:ENAB ON;*OPC?")
        assert fake.actual_mode == "SER" and all(fake.enabled.values())
        fake.query(f"INST:SEL {channel};:OUTP:ENAB OFF;*OPC?")
        assert fake.actual_mode == "OFF" and all(fake.enabled.values())
        fake.query(f"INST:SEL {channel};:OUTP:ENAB OFF;*OPC?")
        assert fake.enabled[channel] is False
        psu = Keithley2220PSU(combination_mode=PsuMode.SERIES)
        psu._resource = fake
        with pytest.raises(InstrumentInvalidReading):
            psu.output_on()


class TestR16B14SiblingModeModels:
    def test_mock_psu_keeps_actual_mode_and_enable_on_global_off(self):
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        psu = MockPSU()
        psu.set_mode_series()
        assert psu.query_combination_mode() == "SER"
        assert all(psu.channel_enabled.values())
        assert psu.safe_off() is True
        assert psu.query_combination_mode() == "SER"
        assert all(psu.channel_enabled.values())
        assert psu.read_output_state() is False
        psu.actual_combination_mode = "OFF"
        with pytest.raises(InstrumentInvalidReading):
            psu.output_on()
        assert psu.combination_mode == "SER"

    @pytest.mark.parametrize("channel", ["CH1", "CH2"])
    def test_inventory_fake_models_series_enable_contract(self, channel):
        from Prog.tests.tools.test_hil_scpi_command_inventory import FakeRM

        fake = FakeRM().resources["psu"]
        fake.query("SOUR:OUTP:PAR OFF;:SOUR:OUTP:SER ON;*OPC?")
        assert fake.query("SOUR:OUTP:SER?") == "1"
        assert all(c["OUTP:ENAB"] == "1" for c in fake.channel_values.values())
        fake.query(f"OUTP OFF;:INST:SEL {channel};:OUTP:ENAB ON;*OPC?")
        assert fake.query("SOUR:OUTP:SER?") == "1"
        fake.query(f"INST:SEL {channel};:OUTP:ENAB OFF;*OPC?")
        assert fake.query("SOUR:OUTP:SER?") == "0"
        assert all(c["OUTP:ENAB"] == "1" for c in fake.channel_values.values())


class _R16B14F1Clock:
    """Determinista monoton óra; a falióra nem része a nyugtakapunak."""

    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert 0.0 < seconds <= 0.05
        self.sleeps.append(seconds)
        self.now += seconds


def _r16_b14f1_probe(monkeypatch, mode=PsuMode.SERIES, output_zeros=2, mode_zeros=0):
    from Prog.drivers import device_psu

    clock = _R16B14F1Clock()
    monkeypatch.setattr(device_psu, "time", clock, raising=False)
    psu, fake = _make_psu_on_stateful_fake(mode)
    fake.timeout = 5000
    state = {"active": False, "polls": 0, "reads": [], "clears": 0}
    original_query, original_clear = fake.query, fake.clear

    def query(msg):
        reply = original_query(msg)
        if msg == _BARE_OUTP_ON:
            state["active"] = True
        elif state["active"] and msg in ("OUTP?", "SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"):
            state["reads"].append((msg, clock.now, fake.timeout))
            if msg == "OUTP?":
                state["polls"] += 1
                if output_zeros is None or state["polls"] <= output_zeros:
                    return "0"
            elif mode_zeros is None or state["polls"] <= mode_zeros:
                # A késő állapotnyugta a kért módtól különböző, érvényes mód.
                delayed = "SER" if mode == PsuMode.INDEPENDENT else "OFF"
                return str(int(delayed == ("SER" if "SER?" in msg else "PAR")))
        return reply

    def clear():
        if state["active"]:
            state["clears"] += 1
        original_clear()

    monkeypatch.setattr(fake, "query", query)
    monkeypatch.setattr(fake, "clear", clear)
    return psu, fake, clock, state


class TestR16B14F1ActivationAck:
    @pytest.mark.parametrize("mode", list(PsuMode))
    @pytest.mark.parametrize("output_zeros, mode_zeros", [(2, 0), (0, 2), (2, 2)])
    def test_delayed_output_and_mode_ack_without_fault_or_repeated_on(
        self, monkeypatch, mode, output_zeros, mode_zeros,
    ):
        psu, fake, clock, state = _r16_b14f1_probe(
            monkeypatch, mode, output_zeros, mode_zeros,
        )
        psu.output_on()
        assert state["polls"] == 3
        assert clock.now == pytest.approx(100.1)
        assert psu.output_commanded_on is True
        expected_on = [_CH1_ENABLE_ON, _BARE_OUTP_ON]
        if mode != PsuMode.INDEPENDENT:
            expected_on.insert(1, _CH2_ENABLE_ON)
        assert _energizing(fake.messages) == expected_on
        assert state["clears"] == 0
        assert fake.timeout == 5000
        assert not any("SOUR:OUTP:SER " in m or "SOUR:OUTP:PAR " in m for m in fake.messages)

    @pytest.mark.parametrize("output_zeros, mode_zeros, reason", [
        (None, 0, "bekapcsolás nem igazolt"),
        (0, None, "mód eltérés"),
        (None, None, "mód eltérés"),
    ])
    def test_common_deadline_diagnoses_missing_ack(self, monkeypatch, output_zeros, mode_zeros, reason):
        psu, fake, clock, state = _r16_b14f1_probe(
            monkeypatch, output_zeros=output_zeros, mode_zeros=mode_zeros,
        )
        with pytest.raises(InstrumentInvalidReading, match=reason) as exc:
            psu.output_on()
        assert "nyugtaablak lejárt: 1 s" in str(exc.value)
        assert clock.now == pytest.approx(101.0)
        assert 2 < state["polls"] <= 21
        assert all(t < 101.0 and 0 < timeout <= 1000 for _, t, timeout in state["reads"])
        assert state["clears"] == 0
        assert fake.timeout == 5000
        assert psu.output_commanded_on is True
        assert _energizing(fake.messages) == [_CH1_ENABLE_ON, _CH2_ENABLE_ON, _BARE_OUTP_ON]

    @pytest.mark.parametrize("query", ["OUTP?", "SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"])
    @pytest.mark.parametrize("failure", ["invalid", "timeout"])
    def test_read_error_is_explicit_without_retry_and_restores_timeout(self, monkeypatch, query, failure):
        psu, fake, clock, state = _r16_b14f1_probe(monkeypatch)
        original = fake.query
        attempts = []

        def failing(msg):
            reply = original(msg)
            if state["active"] and msg == query:
                attempts.append(msg)
                if failure == "timeout":
                    raise TimeoutError("nyugta olvasási hiba")
                return "ismeretlen"
            return reply

        monkeypatch.setattr(fake, "query", failing)
        error = InstrumentTimeoutError if failure == "timeout" else InstrumentInvalidReading
        with pytest.raises(error):
            psu.output_on()
        assert attempts == [query]
        assert clock.now == 100.0
        assert fake.timeout == 5000
        assert state["clears"] == 0
        assert psu.output_commanded_on is True
        assert _energizing(fake.messages) == [_CH1_ENABLE_ON, _CH2_ENABLE_ON, _BARE_OUTP_ON]

    @pytest.mark.parametrize("late_query", ["OUTP?", "SOUR:OUTP:PAR?"])
    def test_late_success_cannot_pass_or_start_another_query(self, monkeypatch, late_query):
        psu, fake, clock, state = _r16_b14f1_probe(monkeypatch, output_zeros=0)
        original = fake.query

        def late(msg):
            reply = original(msg)
            if state["active"] and msg == late_query:
                clock.now += 1.01
            return reply

        monkeypatch.setattr(fake, "query", late)
        with pytest.raises(InstrumentInvalidReading, match="nyugtaablak lejárt"):
            psu.output_on()
        assert state["polls"] == 1
        assert state["reads"][-1][0] == late_query
        assert fake.timeout == 5000

    @pytest.mark.parametrize("original_timeout", [7, None, 5000])
    def test_read_timeout_is_capped_by_remaining_budget_and_restored(self, monkeypatch, original_timeout):
        psu, fake, clock, state = _r16_b14f1_probe(monkeypatch)
        fake.timeout = original_timeout
        psu.output_on()
        for _, started, timeout in state["reads"]:
            assert 0 < timeout <= max(1, int((101.0 - started) * 1000))
            if original_timeout is not None:
                assert timeout <= original_timeout
        assert fake.timeout == original_timeout
        assert clock.sleeps


class TestR16B14F2FallbackOffOrdering:
    @pytest.mark.parametrize("mode", list(PsuMode))
    @pytest.mark.parametrize("failed_query", ["SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"])
    @pytest.mark.parametrize("entry", ["all_outputs_off", "output_off", "safe_off"])
    def test_global_off_precedes_slow_mode_failures(self, monkeypatch, mode, failed_query, entry):
        psu, fake = _make_psu_on_stateful_fake(mode, "OUTP:ENAB")
        fake.output_state = True
        fake.energized = {"CH1", "CH2"}
        psu.output_commanded_on = True
        initial_mode = fake.actual_mode
        clock = _R16B14F1Clock()
        events = []
        original = fake.query

        def query(msg):
            events.append((msg, clock.now, fake.output_state))
            if msg == failed_query:
                fake.messages.append(msg)
                clock.now += psu.timeout_ms / 1000.0
                raise TimeoutError("időt fogyasztó módolvasási hiba")
            return original(msg)

        monkeypatch.setattr(fake, "query", query)
        result = getattr(psu, entry)()
        assert result is (True if entry == "safe_off" else None)
        assert events[0] == (_BARE_OUTP_OFF, 100.0, True)
        failures = [event for event in events if event[0] == failed_query]
        assert failures == [
            (failed_query, 100.0, False),
            (failed_query, 105.0, False),
            (failed_query, 110.0, False),
        ]
        assert psu.max_retries == 3 and psu.timeout_ms == 5000
        assert clock.now == 115.0
        assert fake.output_state is False and fake.energized == set()
        assert not any("ENAB OFF" in msg for msg in fake.messages)
        assert _energizing(fake.messages) == []
        assert fake.actual_mode == initial_mode and psu.combination_mode == mode
        assert psu.output_commanded_on is False

    @pytest.mark.parametrize("mode", list(PsuMode))
    @pytest.mark.parametrize("failure", ["off_timeout", "read_timeout", "invalid", "still_on"])
    def test_unknown_global_off_is_explicitly_unsuccessful(self, monkeypatch, mode, failure):
        psu, fake = _make_psu_on_stateful_fake(mode, "OUTP:ENAB")
        fake.output_state = True
        psu.output_commanded_on = True
        original = fake.query

        def query(msg):
            if msg in (_BARE_OUTP_OFF, "SOUR:OUTP:SER?", "OUTP?"):
                fake.messages.append(msg)
                if msg == "SOUR:OUTP:SER?" or (msg == _BARE_OUTP_OFF and failure == "off_timeout"):
                    raise TimeoutError("bizonytalan végrehajtás")
                if msg == "OUTP?":
                    if failure == "read_timeout":
                        raise TimeoutError("állapot nem olvasható")
                    return "ismeretlen" if failure == "invalid" else "1"
                return "1"  # OPC-nyugta végrehajtás nélkül.
            return original(msg)

        monkeypatch.setattr(fake, "query", query)
        assert psu.safe_off() is False
        assert fake.messages[0] == _BARE_OUTP_OFF
        assert fake.output_state is True
        assert not any("ENAB OFF" in msg for msg in fake.messages)
        assert _energizing(fake.messages) == []
        assert psu.output_commanded_on is False

    def test_global_off_timeout_still_allows_verified_independent_channel_off(self, monkeypatch):
        psu, fake = _make_psu_on_stateful_fake(output_cmd="OUTP:ENAB")
        fake.output_state = True
        psu.output_commanded_on = True
        original = fake.query

        def query(msg):
            if msg == _BARE_OUTP_OFF:
                fake.messages.append(msg)
                raise TimeoutError("globális OFF nem támogatott")
            return original(msg)

        monkeypatch.setattr(fake, "query", query)
        assert psu.safe_off() is True
        assert fake.messages == [
            *([_BARE_OUTP_OFF] * psu.max_retries),
            "SOUR:OUTP:SER?", "SOUR:OUTP:PAR?",
            "INST:SEL CH1;:OUTP:ENAB OFF;*OPC?",
            "INST:SEL CH2;:OUTP:ENAB OFF;*OPC?",
        ]
        assert fake.enabled == {"CH1": False, "CH2": False}
        assert psu.read_output_state() is False
        assert psu.output_commanded_on is False

    @pytest.mark.parametrize("mode", [PsuMode.SERIES, PsuMode.PARALLEL])
    @pytest.mark.parametrize("entry", ["all_outputs_off", "output_off", "safe_off"])
    def test_requested_combined_actual_independent_never_disables_channels(self, mode, entry):
        # A tényleges INDEPENDENT mód nem írhatja felül a kért kombinált módot.
        psu, fake = _make_psu_on_stateful_fake(mode, "OUTP:ENAB")
        fake.actual_mode = "OFF"
        fake.output_state = True
        fake.energized = {ch for ch, enabled in fake.enabled.items() if enabled}
        psu.output_commanded_on = True
        initial_enabled = fake.enabled.copy()

        result = getattr(psu, entry)()

        assert result is (True if entry == "safe_off" else None)
        assert fake.messages == [
            _BARE_OUTP_OFF, "SOUR:OUTP:SER?", "SOUR:OUTP:PAR?", "OUTP?",
        ]
        assert not any("ENAB OFF" in msg for msg in fake.messages)
        assert fake.enabled == initial_enabled
        assert fake.actual_mode == "OFF" and psu.combination_mode == mode
        assert fake.output_state is False and fake.energized == set()
        assert _energizing(fake.messages) == []
        assert psu.output_commanded_on is False

    @pytest.mark.parametrize("mode", list(PsuMode))
    @pytest.mark.parametrize("actual_mode", ["SER", "PAR", None])
    @pytest.mark.parametrize("entry", ["all_outputs_off", "output_off", "safe_off"])
    def test_global_off_timeout_remains_failure_despite_zero_readback(
        self, monkeypatch, mode, actual_mode, entry,
    ):
        # A későbbi OUTP?=0 nem törölheti a globális OFF transzporthibáját.
        psu, fake = _make_psu_on_stateful_fake(mode, "OUTP:ENAB")
        fake.actual_mode = actual_mode or "OFF"
        fake.output_state = True
        fake.energized = {ch for ch, enabled in fake.enabled.items() if enabled}
        psu.output_commanded_on = True
        initial_enabled = fake.enabled.copy()
        original = fake.query

        def query(msg):
            if msg == _BARE_OUTP_OFF:
                original(msg)  # A művelet megtörténhetett, de a nyugta elveszett.
                raise TimeoutError("globális OFF nyugtája elveszett")
            if actual_mode is None and msg == "SOUR:OUTP:SER?":
                fake.messages.append(msg)
                raise TimeoutError("tényleges mód nem olvasható")
            return original(msg)  # Az OUTP? minden esetben 0-t adna.

        monkeypatch.setattr(fake, "query", query)
        if entry == "safe_off":
            assert psu.safe_off() is False
        else:
            with pytest.raises(InstrumentTimeoutError, match="OUTP OFF") as caught:
                getattr(psu, entry)()
            assert isinstance(caught.value.__cause__, TimeoutError)
            assert str(caught.value.__cause__) == "globális OFF nyugtája elveszett"
        assert fake.messages[:psu.max_retries] == [_BARE_OUTP_OFF] * psu.max_retries
        assert fake.output_state is False and fake.energized == set()
        assert fake.enabled == initial_enabled
        assert fake.actual_mode == (actual_mode or "OFF") and psu.combination_mode == mode
        assert not any("ENAB OFF" in msg for msg in fake.messages)
        assert _energizing(fake.messages) == []
        assert psu.output_commanded_on is False


# R16-B10F2: minden nyugtaolvasás előtt és után élő megszakítási kapu.
class TestR16B10F2PsuStop:
    @pytest.mark.parametrize("mode", list(PsuMode))
    @pytest.mark.parametrize("stop_query", ["OUTP?", "SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"])
    @pytest.mark.parametrize("output_zeros", [0, None])
    def test_stop_inside_ack_query(self, monkeypatch, mode, stop_query, output_zeros):
        from Prog.src.exceptions import TestAbortedByUser

        psu, res, clock, state = _r16_b14f1_probe(
            monkeypatch, mode=mode, output_zeros=output_zeros,
        )
        stopped = False
        original = res.query

        def query(cmd):
            nonlocal stopped
            response = original(cmd)
            if state["active"] and cmd == stop_query:
                stopped = True
            return response

        monkeypatch.setattr(res, "query", query)
        with pytest.raises(TestAbortedByUser):
            psu.output_on(stop_requested=lambda: stopped)
        expected = ["OUTP?", "SOUR:OUTP:SER?", "SOUR:OUTP:PAR?"]
        assert [cmd for cmd, _, _ in state["reads"]] == expected[:expected.index(stop_query) + 1]
        assert clock.now == 100.0 and not clock.sleeps
        assert res.timeout == 5000
        assert state["clears"] == 0
        assert psu.output_commanded_on is True
        assert res.messages.count(_BARE_OUTP_ON) == 1

    def test_stop_during_wait_prevents_next_query(self, monkeypatch):
        from Prog.src.exceptions import TestAbortedByUser

        psu, _res, clock, state = _r16_b14f1_probe(monkeypatch, output_zeros=None)
        stopped = False
        original = clock.sleep

        def sleep(seconds):
            nonlocal stopped
            original(seconds)
            stopped = True

        monkeypatch.setattr(clock, "sleep", sleep)
        with pytest.raises(TestAbortedByUser):
            psu.output_on(stop_requested=lambda: stopped)
        assert state["polls"] == 1
        assert clock.now == pytest.approx(100.05)

    @pytest.mark.parametrize("failure,error", [
        ("invalid", InstrumentInvalidReading),
        ("timeout", InstrumentTimeoutError),
    ])
    def test_read_failure_retains_priority_over_stop(self, monkeypatch, failure, error):
        psu, res, _clock, state = _r16_b14f1_probe(monkeypatch)
        stopped = False
        original = res.query

        def query(cmd):
            nonlocal stopped
            response = original(cmd)
            if state["active"] and cmd == "OUTP?":
                stopped = True
                if failure == "timeout":
                    raise TimeoutError("mockolt nyugtahiba")
                return "nan"
            return response

        monkeypatch.setattr(res, "query", query)
        with pytest.raises(error):
            psu.output_on(stop_requested=lambda: stopped)
        assert [cmd for cmd, _, _ in state["reads"]] == ["OUTP?"]
        assert res.timeout == 5000

    @pytest.mark.parametrize("hook", [None, lambda: False])
    def test_none_and_false_hook_preserve_transcript(self, monkeypatch, hook):
        original, original_res, original_clock, original_state = _r16_b14f1_probe(monkeypatch)
        original.output_on()
        psu, res, clock, state = _r16_b14f1_probe(monkeypatch)
        psu.output_on(stop_requested=hook)
        assert res.messages == original_res.messages
        assert state == original_state
        assert clock.now == original_clock.now
        assert psu.output_commanded_on is original.output_commanded_on is True


@pytest.mark.parametrize("actual_mode", ["SER", "PAR"])
@pytest.mark.parametrize("requested_mode", [PsuMode.INDEPENDENT, PsuMode.SERIES, PsuMode.PARALLEL])
@pytest.mark.parametrize("failure", ["scpi", "timeout"])
def test_r16b25_connect_detection_preserves_combined_mode(actual_mode, requested_mode, failure):
    class InheritedMode(_StatefulConnect2220Resource):
        def query(self, msg):
            if msg == "*CLS;:OUTP OFF;:SYST:ERR?":
                self.messages.append(msg)
                if failure == "timeout":
                    raise TimeoutError("B25 detection reply lost")
                self._execute("OUTP OFF")
                return '-113,"B25 detection error"'
            return super().query(msg)

    fake = InheritedMode()
    fake.actual_mode = actual_mode
    fake.enabled = {"CH1": True, "CH2": True}
    psu = Keithley2220PSU(combination_mode=requested_mode)
    psu.connect("MOCK::B25", rm=MagicMock(open_resource=lambda *_: fake))
    assert "*CLS;:OUTP OFF;:SYST:ERR?" in fake.messages
    assert psu._output_cmd == "OUTP:ENAB"
    assert fake.actual_mode == actual_mode
    assert not any("OUTP:ENAB OFF" in message for message in fake.messages)
    assert "OUTP OFF;*OPC?" in fake.messages
    assert not fake.output_state and not fake.energized
    assert not psu.output_commanded_on
    assert not _energizing(fake.messages)
