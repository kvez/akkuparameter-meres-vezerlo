"""
Keithley2220PSU SCPI driver.
Mód-agnosztikus output API [N7] — controller réteg CH1/CH2-t nem lát.
[N9] output_commanded_on nyilvántartás.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from Prog.src.exceptions import (
    InstrumentInvalidReading,
    InstrumentTimeoutError,
    check_activation_stop,
)
from Prog.src.safety import PsuMode

# CLAUDE.md §10.4: aktív töltés alatt a PSU áram/feszültség-readback NaN/Inf/
# overload esetét sem szabad csendben, feltétel nélküli float()-tal átengedni
# — Python float("nan")/float("inf") NEM dob kivételt, egy NaN érték csendben
# megmérgezné az Ah-integrációt (minden > összehasonlítás False-t ad NaN-nal,
# az MAX_CHARGE_AH_REACHED limit észrevétlenül kikapcsolna). Ugyanaz az elv,
# mint a device_dmm.py::_validate_voltage()-ben.
_OVERLOAD_THRESHOLD = 1e30


def _validate_reading(value: float, label: str) -> float:
    if math.isnan(value) or math.isinf(value):
        raise InstrumentInvalidReading(f"PSU {label}: NaN vagy Inf érték: {value}")
    if abs(value) > _OVERLOAD_THRESHOLD:
        raise InstrumentInvalidReading(f"PSU {label}: overload érték: {value:.3e}")
    return value


def _parse_bool_response(resp: str, label: str) -> bool:
    """Review-kör 5, Hardver/Műszervezérlés HW5-02 (P3): a bare OUTP?
    (2220-30-1/prog.txt 1553-1568. sor -- Kör 20 E18/SRC-F-9: korábban
    helytelenül "2220 Prog PDF") "Returns 1|0" formátumot ad, az OUTP:ENAB?
    (fallback ág, uo. 1477-1495. sor) viszont dokumentáltan "Returns
    Disabled|Enabled" szöveget — a driver a csatlakozáskor detektált
    dialektustól (`_output_cmd`) függően MINDKÉT válaszformátumot kaphatja,
    ezért mindkettőt kezelni kell, nem csak a numerikusat. Ismeretlen
    válasz esetén CLAUDE.md §6.4 szerint tilos csendben hamis
    (pl. mindig-OFF) értéket helyettesíteni — explicit hibát dob.

    Teljes kódbázis review 2. kör (2026-09-15, R2-03, P3, PREVENTÍV/
    PROJECT_POLICY, HIL_PENDING): a manual NR1 formátumot dokumentál a
    numerikus ágra; a driver más readbackjei (`read_regulation_state()`,
    `read_questionable_status()`) a KÓDBAN ugyanígy toleránsak egy
    firmware által esetleg lebegőpontos alakban ("1.0"/"+0") visszaadott
    NR1-re, DE ez maga is forrás nélküli, kódból megfigyelt (CODE_OBSERVED)
    védekező minta, nem gyártói dokumentum vagy HIL-mérés — a repó saját
    HIL-logja (Mérések/nyers_logok/hil_scf010_20260911.md) ezen a konkrét
    PSU-n mindig egész "1"/"0" választ rögzített. Ez a fix tehát KIZÁRÓLAG
    konzisztencia-célú elővigyázatosság a testvér readbackekkel, NEM egy
    megfigyelt hiba javítása — ha egy firmware valaha lebegőpontos NR1-et
    adna, a numerikus ág `float()`-tal próbálkozik, DE KIZÁRÓLAG pontos
    0.0/1.0 értéket fogad el (nem `bool(int(float(token)))`-ot, ami
    hibásan igazra értelmezne egy "2" vagy "-1" választ is — az továbbra
    is explicit hibát dob, mert nem tudjuk, milyen fizikai állapotot
    jelent egy dokumentálatlan érték)."""
    token = resp.strip().upper()
    if token in ("1", "ON", "ENABLED", "ENABLE"):
        return True
    if token in ("0", "OFF", "DISABLED", "DISABLE"):
        return False
    try:
        numeric = float(token)
    except ValueError:
        pass
    else:
        if numeric == 0.0:
            return False
        if numeric == 1.0:
            return True
    raise InstrumentInvalidReading(f"{label}: értelmezhetetlen állapotválasz: {resp!r}")


def _clear_resource(resource: Any) -> None:
    """Teljes kódbázis review 2. kör (2026-08-28) + 3. kör (2026-08-29) P1
    kiegészítés — l. device_dmm.py azonos segédfüggvény docstringje."""
    try:
        resource.clear()
    except Exception:
        pass


@dataclass
class Keithley2220PSU:
    combination_mode: PsuMode = PsuMode.INDEPENDENT
    timeout_ms: int = 5000
    max_retries: int = 3

    _resource: Any = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _output_cmd: str = field(default="OUTP", init=False, repr=False)
    _last_cmd: str = field(default="", init=False, repr=False)
    output_commanded_on: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ #
    # Kapcsolat                                                            #
    # ------------------------------------------------------------------ #

    def connect(self, resource_string: str, rm=None) -> None:
        """PON is not checked (R13-UD-PSU-PON-REMOVE).
        The lab 2220-30-1 fw 1.15-1.05 does not support the command.
        Operator responsibility: Menu -> User Settings -> Output Recall = Off;
        On risks unsupervised output reactivation after a mains interruption.
        """
        if rm is None:
            import pyvisa
            rm = pyvisa.ResourceManager()
        self._resource = rm.open_resource(resource_string)
        self._resource.timeout = self.timeout_ms
        self._connected = True
        self._detect_output_command()
        # Teljes kódbázis review 2. kör (2026-08-28), P0 lelet: a
        # _detect_output_command() korábbi fallback (OUTP:ENAB) próba-írása
        # csak az épp kiválasztott csatornát tiltotta (l. all_outputs_off()
        # kommentje) — SAF-001/CLAUDE.md §10.1 "csatlakozáskor PSU ALL OUTPUTS
        # OFF" invariánsa csak akkor teljesül garantáltan, ha itt explicit
        # all_outputs_off()-ot hívunk mindkét csatornára, nem a próba-írás
        # mellékhatására támaszkodva.
        self.all_outputs_off()
        # Review-kör 5, Hardver/Műszervezérlés HW-R5-C (P2, felhasználói döntés
        # 2026-09-10): a PSU Output Timer funkciója (2220-30-1/prog.txt 1577-1642.
        # sor) csatorna-scope-olt, sticky állapot -- ha egy KORÁBBI session/
        # front panel bekapcsolva hagyta egy nem-alapértelmezett időtartammal,
        # egy több órás CHARGE lépés a szoftver tudta nélkül, magától
        # lekapcsolhat ("the specified output... will turn off automatically if
        # left on longer than the specified duration"). A driver ma semmilyen
        # sticky-védelmi állapotot nem kérdez le/állít vissza connect()-kor
        # (.claude/rules/40-instruments.md: "mock modellezze a sessionön át
        # megmaradó hardverállapotot is" -- ugyanez elve a valós drivernél:
        # a driver maga felelős a session-előtti örökölt állapot kiiktatásáért).
        # Explicit felhasználói jóváhagyással (nem OUTPUT ON/INPUT ON parancs,
        # csak egy védelmi IDŐZÍTŐ letiltása) mindkét csatornán letiltjuk.
        self._disable_output_timer()

    def _disable_output_timer(self) -> None:
        """HW-R5-C: [SOURce:]OUTPut:TIMer[:STATe] csatorna-scope-olt
        (2220-30-1/prog.txt 1616-1642. sor -- Kör 20 E18/SRC-F-9: korábban
        helytelenül "2220 Prog PDF") -- mindkét csatornán explicit OFF-ra
        állítjuk, hogy egy korábbi session/front panel beállítása ne
        kapcsolhassa le váratlanul a kimenetet egy hosszú CHARGE lépés
        közben.

        Best-effort, NEM safety-invariáns: ellentétben all_outputs_off()-fal
        (aminek a sikere maga a §6.1 safe-alapállapot garanciája), ennek a
        hívásnak a kudarca NEM veszélyesebb, mint a fix ELŐTTI állapot --
        legrosszabb esetben a régi, nem-letiltott timer marad érvényben
        (ugyanaz a kockázat, ami eddig is fennállt), NEM egy új, ON-irányú
        hibaosztály. Ezért egy kommunikációs hiba itt nem szabad, hogy
        meghiúsítsa a teljes connect()-et -- csatornánként izolált, elnyelt
        kivétel, a device_load.py::input_off() TRAN 0 utáni, hasonló
        indoklású mintáját (HW5-06) követve.

        [regresszió, independent-verifier lelet, DFAM: csatorna-kiválasztás
        hívás-utáni mellékhatása -- MÁSODIK előfordulás, l. HW-R5-G
        (`read_output_state()` ugyanezen mintája) -- CLAUDE.md §8 szerint
        STRUCTURAL_PREVENTION_REVIEW kötelező, de itt a minimális, a
        HW-R5-G-ben már bevált fix (`finally: self._select_ch1()`) elegendő,
        önálló refaktor nem indokolt]: a fenti ciklus CH2-n hagyta a
        kiválasztást connect() után -- a bare "OUTP" (elsődleges) firmware-
        ágon ez egy ÚJ, a fix ELŐTT nem létező post-connect() állapot volt
        (all_outputs_off() ezen az ágon nem küld INST:SEL-t). Minden valós
        hívó (`set_output_voltage()`/`set_output_current()`/`output_on()`)
        maga is `_select_ch1()`-gyel kezd, tehát élő hibát ez nem okozott --
        de a HW-R5-G tanulsága szerint (ugyanez a hibaosztály MÁR egyszer
        előfordult) a `finally` itt is helyes."""
        try:
            for ch in ("CH1", "CH2"):
                try:
                    # 2026-09-12, DFAM-039/F7: atomikus kuldes (l.
                    # _apply_config_atomic()) -- best-effort, ugyanaz a
                    # csatorna-cimzesi mintazat, mint all_outputs_off()-ban.
                    self._apply_config_atomic(f"INST:SEL {ch}", "OUTP:TIM:STAT OFF")
                except Exception:
                    pass
        finally:
            self._select_ch1()

    def _detect_output_command(self) -> None:
        """
        [ELLENŐRIZENDŐ] OUTP OFF vs OUTP:ENAB OFF firmware különbség.
        Próbálja OUTP OFF-fal; ha kivétel VAGY a műszer csendben (SCPI
        error-queue-ban, kivétel nélkül) elutasítja, OUTP:ENAB dialektust választ.
        A detektálás nem ír ENAB OFF-ot: örökölt SERIES módot bontana
        (R16-HIL-PSU-SERIES-ENAB-001). A connect következő all_outputs_off()
        hívása globális OFF-fal kezd, és csak igazolt INDEPENDENT módban tilt csatornát.

        Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: korábban
        KIZÁRÓLAG a kivétel-ág váltott fallback-re — egy VISA-szinten
        sikeres, de SCPI-szinten elutasított parancs (ismeretlen parancs egy
        eltérő firmware-en) sosem lett volna észlelve, mert a `_write()` csak
        kommunikációs hibára dob kivételt, nem SCPI-elutasításra
        (`SYST:ERR?` szükséges hozzá — ugyanaz a hibaosztály, mint a
        korábban HIL-lel felfedezett hiányzó TRAN 1 parancs)."""
        # 2026-09-12, DFAM-039/F7 (V5, fuggetlen verifier): a proba-iras
        # ("OUTP OFF") es a ra kovetkezo hibasor-ellenorzes korabban KULON
        # _write()/check_error() volt -- ez SZO SZERINT a DMM-en HIL-lel
        # megfigyelt tunetalak (write, majd kozvetlenul utana egy kulon
        # SYST:ERR? query, aminek Device Clear-je elvben elvethetne az
        # elso, meg fel nem dolgozott parancsot). Atomikus lekerdezes:
        # az "OUTP OFF" es a "SYST:ERR?" EGYETLEN uzenetben megy ki.
        #
        # Kör 19 D9 (HW19-04, DFAM-039 ugyanaz a csalad, mint HW19-01/02):
        # a connect()-ben korabban KULON self._write("*CLS") hivas elozte
        # meg ezt a metodust -- egy kozbeekelodo Device Clear a *CLS ES az
        # OUTP OFF/SYST:ERR? paros KOZOTT egyetlen, veletlenul benne maradt
        # sor-hiba-uzenetet "kiszivargathatott" volna a has_error kiertekelesbe,
        # ami csendben az OUTP:ENAB dialektusra zarna a sessiont (l. lent
        # output_on() R2-08 F2 -- a fallback dialektus egy sikertelen OUTP ON-t
        # is elnyelhetett; R1 B2 F-03 ota NEM, a hiba propagal). A *CLS
        # mostantol az EGYETLEN atomikus uzenet resze --
        # ez a metodusnak MA az EGYETLEN hivohelye (connect()), tehat a *CLS
        # ide-mozgatasa nem valtoztat semmit azon, MIKOR fut le, csak azon,
        # hogy egyetlen, oszthatatlan uzenetben megy-e ki.
        try:
            resp = self._apply_atomic_query("*CLS", "OUTP OFF", "SYST:ERR?")
            stripped = resp.strip()
            has_error = not (stripped == "0" or resp.startswith("+0") or resp.startswith("0,"))
            if has_error:
                self._output_cmd = "OUTP:ENAB"
            else:
                self._output_cmd = "OUTP"
        except Exception:
            # R16-B14: a detektálási hiba (timeout is) továbbra is fallbacket
            # választ; ez nem bizonyítja a globális OUTP támogatásának hiányát.
            # Kombinált aktiválást ezért az output_on() külön tiltja.
            self._output_cmd = "OUTP:ENAB"

    def disconnect(self) -> None:
        if self._resource is not None:
            try:
                self._resource.close()
            except Exception:
                pass
        self._resource = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._resource is not None

    # ------------------------------------------------------------------ #
    # Alapvető SCPI                                                        #
    # ------------------------------------------------------------------ #

    def idn(self) -> str:
        return self._query("*IDN?")

    def reset(self) -> None:
        self._write("*RST")

    def clear_status(self) -> None:
        self._write("*CLS")

    def check_error(self) -> list[str]:
        # 2026-09-12: a `_query()` DFAM-039/F2 fix ota mindig frissiti a
        # `self._last_cmd`-et -- a diagnosztikai celu erteket ("mi okozta a
        # hibat") a SAJAT "SYST:ERR?" queryNK ELOTT kell elmenteni, kulonben
        # onmagunkat irnank felul ("[last: SYST:ERR?]", ertelmetlen).
        # 2026-09-12, fuggetlen verifier P3: instrument_manager.py a
        # hibasort AKAR TOBBSZOR (max 32x/eszkoz/tick) uriti egy tickben --
        # a self._last_cmd VISSZAALLITASA NELKUL a 2. hivastol kezdve mar
        # "SYST:ERR?"-t mutatna (az 1. hivas SAJAT querye altal beallitva),
        # elveszitve az EREDETI, hibat okozo parancsot minden tovabbi
        # ureesi korben.
        last_cmd_before_check = self._last_cmd
        resp = self._query("SYST:ERR?")
        self._last_cmd = last_cmd_before_check
        stripped = resp.strip()
        if stripped == "0" or resp.startswith("+0") or resp.startswith("0,"):
            return []
        return [f"{stripped} [last: {last_cmd_before_check}]"]

    def safe_off(self) -> bool:
        """True, ha az ALL OUTPUTS OFF parancs ténylegesen elment (nem dobott
        kivételt) — a hívó (InstrumentManager.safe_all_off()) ez alapján
        dönt bizonytalan-OFF-állapot jelzéséről (CLAUDE.md §10.4).
        output_commanded_on mindig False-ra áll, mert ez a PARANCSOLT
        (szoftver-oldali) állapotot tükrözi, nem a tényleges hardveres
        visszaigazolást — a kettő szándékosan külön fogalom."""
        try:
            self.all_outputs_off()
            success = True
        except Exception:
            success = False
        self.output_commanded_on = False
        return success

    # ------------------------------------------------------------------ #
    # Mód-agnosztikus output API [N7]                                     #
    # Controller réteg CSAK ezeket hívja — CH1/CH2 nem látható felfelé.  #
    # ------------------------------------------------------------------ #

    def set_output_voltage(self, v: float) -> None:
        # 2026-09-12, DFAM-039/F7: atomikus kuldes -- l. _apply_config_atomic()
        # docstringje (csatorna-valasztas + setpont KULON _write()-kent egy
        # Device Clear altal elvben szetvalaszthato volt).
        self._apply_config_atomic("INST:SEL CH1", f"SOUR:VOLT {v:.6g}")

    def set_output_current(self, i: float) -> None:
        self._apply_config_atomic("INST:SEL CH1", f"SOUR:CURR {i:.6g}")

    def output_on(self, *, stop_requested: Callable[[], bool] | None = None) -> None:
        # Teljes kódbázis review 2. kör (2026-09-16, R2-08-F1, P2, DFAM-032
        # 3. előfordulásának LEZÁRÁSA -- a 2026-09-08 óta nyitott, lentebb
        # ("ISMERT, NYITVA HAGYOTT RESIDUUM") dokumentált eset): a metódus
        # EGÉSZE egyetlen try/finally-be került, tehát a commanded-flag a
        # KORÁBBAN unguarded CH1/CH2 engedélyező blokkok hibája esetén IS
        # konzervatívan True-ra áll. A FALLBACK ("OUTP:ENAB") dialektuson EZ
        # a blokk MAGA a fizikai energia-bekapcsolás, tehát egy itteni
        # kivétel után a flag eddig False-on ragadt egy esetleg MÁR
        # energizált kimenet mellett -- a hamis False a VESZÉLYES irány
        # (safety.py::check_concurrent_psu_load() engedné a Loadot,
        # rules/10-safety.md 2. pont). A kivétel-propagáció VÁLTOZATLAN (az
        # egyetlen hívó, charge_controller.py::_run_psu_preset(), try/
        # except-ben van és FAULT-ra eszkalál), új viselkedési tér NINCS: a
        # lenti, R2-08-ban bevezetett `finally` szemantikája terjed ki az
        # egész metódusra, NEM új fallback. A DFAM-032 család negyedik
        # előfordulása ezzel szerkezetileg kizárt -- közös helper-refaktor
        # (a family `prevention` mezőjének felvetése) NEM indokolt
        # (CLAUDE.md §5/§10).
        #
        # R1 B2 (2026-10-25, F-03, rules/10-safety.md 6. pont): a metódus
        # MINDEN ON-t hordozó üzenete (CH1 OUTP:ENAB ON, SER/PAR CH2
        # OUTP:ENAB ON, bare OUTP ON) EGYLÖVÉSES (`single_attempt=True`):
        # egy elveszett *OPC? válasz után a parancs kimenetele bizonytalan,
        # ezért NEM küldjük újra vakon -- a kivétel a hívóhoz propagál
        # (emergency_stop), a flag a `finally`-ben konzervatívan True. A
        # `max_retries` és az OFF-/olvasási utak retry-ja VÁLTOZATLAN.
        try:
            # R16-B14: a tárolt mód nem hardveres bizonyíték. Fallbackben
            # kombinált aktiválás nem igazolt; nincs automatikus módjavítás.
            if self._output_cmd != "OUTP" and self.combination_mode != PsuMode.INDEPENDENT:
                raise InstrumentInvalidReading("PSU: kombinált aktiválás fallback dialektusban tiltott")
            self._verify_combination_mode()
            # R1 B2 (2026-10-25, F-04): INDEPENDENT módban a CH2 OUTP:ENAB
            # egy korábbi SER/PAR session (vagy az előlapi "Channel Enable"
            # menü) után sticky ON maradhat (HIL-SCF010), a lenti bare OUTP
            # ON pedig "turns all of the enabled output channels on"
            # (2220-30-1/prog.txt, [SOURce:]OUTPut[:STATe][:ALL]) -- ezért
            # a try ELSŐ műveleteként, MINDEN ON-t hordozó üzenet ELŐTT a CH2
            # explicit tiltása. OFF-típusú, idempotens üzenet -> NORMÁL
            # retry (nem single_attempt). A "hiba után nem megy ki ON"
            # garancia KIZÁRÓLAG transzportkivételre (InstrumentTimeoutError)
            # szól: egy firmware-oldali SCPI-elutasítás nem szükségképpen
            # Python-kivétel (`_apply_config_atomic()` nem olvassa a
            # hibasort). A fizikai elfogadás (CH2 ENAB?=0, CH2 kapocs ~0 V)
            # HIL_PENDING.
            if self.combination_mode == PsuMode.INDEPENDENT:
                self._apply_config_atomic("INST:SEL CH2", "OUTP:ENAB OFF", "INST:SEL CH1")
                self._verify_combination_mode()
            # OUTP:ENAB ON: csatorna-szintű engedélyezés — szükséges, ha CH1 korábban
            # front panel "Channel Enable" menüből volt tiltva (OUTP ON önmagában nem elég).
            # 2026-09-12, DFAM-039/F7: atomikus kuldes (l. _apply_config_atomic()).
            self._apply_config_atomic("INST:SEL CH1", "OUTP:ENAB ON", single_attempt=True)
            # Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-02 lelet: a
            # fenti OUTP:ENAB ON kizárólag a kiválasztott CH1-et engedélyezi —
            # csatorna-scope-olt parancs (l. all_outputs_off() kommentje, gyári
            # kézikönyv 2220-30-1/prog.txt 1477-1496. sor). all_outputs_off()
            # viszont MINDKÉT csatorna OUTP:ENAB-ját explicit letiltja (fallback
            # ágon), ezért egy korábbi all_outputs_off()/safe_off()/connect()
            # után CH2 engedélye OFF marad, amíg valami vissza nem kapcsolja —
            # output_on() eddig SOSEM tette ezt meg. SERIES/PARALLEL kombinációs
            # módban ez csendben (readback-ellenőrzés nélkül) fél feszültséget/
            # áramot eredményezett volna, mert csak CH1 ad tényleges kimenetet.
            # INDEPENDENT módban CH2-t szándékosan nem használja a driver (l.
            # set_output_voltage()/set_output_current() mindig CH1-et választ) —
            # ott CH2 visszakapcsolása egy korábbi, más kombinációs módból
            # maradt setpontot energizálna, ezért a CH2-ENGEDÉLYEZÉS KIZÁRÓLAG
            # SER/PAR módra szól, INDEPENDENT-re nem. [R1 B2 F-04, 2026-10-25]
            # INDEPENDENT módban a driver a CH2-t SOSEM engedélyezi, hanem a
            # metódus elején EXPLICIT TILTJA (l. a try első műveletét) --
            # a korábbi "nem nyúl a CH2-höz" viselkedés egy sticky CH2
            # OUTP:ENAB ON mellett a bare OUTP ON-nal a CH2-t is energizálta
            # volna.
            #
            # MAX verifier follow-up (2026-09-08, P1-1): ezt a blokkot a bare
            # "OUTP ON" ELÉ kell tenni, nem utána — a gyári kézikönyv (prog.txt
            # 1553-1568. sor) szerint a bare OUTP "minden ENGEDÉLYEZETT
            # csatornára" vonatkozik; ha OUTP ON akkor megy ki, amikor CH2 ENAB-ja
            # MÉG OFF, egyik kézikönyv sem dokumentálja, hogy egy KÉSŐBBI
            # OUTP:ENAB ON hatással van-e egy már bekapcsolt OUTP állapotra —
            # tehát a korábbi sorrend a fix hatását éppen a fallback-firmware-en
            # (ahol OUTP:ENAB a tényleges be/ki-kapcsoló) érvénytelenítette volna.
            #
            # R13-B2: measurement semantics follow R13-UD-PSU-SERIES-MEAS.
            # The write-side tracking observation remains separate:
            # INSTR-PSU-COMB-002 / hil_instr_psu_comb_002_20260912.md records
            # CH2 setpoint tracking CH1 in SERIES mode; it does not justify
            # summing voltage measurements.
            if self.combination_mode in (PsuMode.SERIES, PsuMode.PARALLEL):
                # 2026-09-12, DFAM-039/F7: atomikus kuldes -- korabban 3 kulon
                # _write() volt (CH2 kivalasztas, engedelyezes, vissza CH1-re),
                # ahol egy kozbeekelodo Device Clear elvben a CH2-valasztast
                # vagy az engedelyezest is elvethette volna.
                self._verify_combination_mode()
                self._apply_config_atomic(
                    "INST:SEL CH2", "OUTP:ENAB ON", "INST:SEL CH1", single_attempt=True,
                )
            # Governance-v2 review-kör 1 (CF-12/HW-R1-02): a korábbi
            # `f"{self._output_cmd} ON"` fallback módban (`_output_cmd ==
            # "OUTP:ENAB"`) MÉGEGYSZER "OUTP:ENAB ON"-t küldött ki — a csupasz
            # "OUTP ON" SOHA nem ment ki, a kimenet fizikailag OFF maradt,
            # miközben output_commanded_on=True lett. A bare "OUTP ON" mostantól
            # MINDIG kimegy. [R1 B2 F-03, 2026-10-25: a korábbi "best-effort"
            # szemantika -- fallback dialektuson a hiba elnyelése -- MEGSZŰNT,
            # l. lent.] HIL_PENDING a pontos OUTP vs OUTP:ENAB
            # szemantikára (l. CLAIM_LEDGER.yaml INSTR-PSU-COMB-001).
            #
            # 2026-09-12, DFAM-039/F7 (V6, fuggetlen verifier): ez az AKTIV
            # ("OUTP" dialektus, HIL-lel megerositve a laborban -- l.
            # Meresek/nyers_logok/hil_scf010_20260911.md) uton az EGYETLEN
            # energia-BEkapcsolo parancs, korabban szinkronizalatlan
            # `_write()`-kent ment ki. `_query(...;*OPC?)`-re valtva a hivas
            # bizonyitottan vegrehajtodik, mielott barmi mas SCPI menne.
            #
            # Teljes kodbazis review 2. kor (2026-09-15, R2-08, P2, DFAM-032
            # -- "commanded_state_flag_desync_via_unguarded_write_between_
            # physical_action_and_flag_set", l. DEFECT_FAMILY_REGISTRY.yaml;
            # NEM DFAM-039, ami a tobb-parancsos ATOMIZALAS csaladja -- ezt a
            # MAX verifier F1 lelete pontositotta): a fenti V6-fix a
            # szinkronizaciot bevezette, de a kovetkezo `except Exception:
            # pass` MEGTARTOTTA a csendes hiba-elnyeleset -- a primer ("OUTP")
            # dialektuson ez az EGYETLEN energia-bekapcsolo parancs, tehat egy
            # SCPI/transzport-hiba utan a driver BIZONYITEK NELKUL allitotta
            # True-ra a flaget, es az EGYETLEN hivo (charge_controller.py::
            # _run_psu_preset(), MAR try/except-ben, l.
            # test_charge_controller.py::test_output_on_failure_escalates_to_
            # fault) sosem kapott FAULT-ot (CLAUDE.md paragrafus 6; rules/
            # 10-safety.md 4. pont; a 6. pontot -- "ne ismetelj vakon" -- az
            # R2-08 fix meg nem erintette, l. MAX verifier F5; azt az R1 B2
            # F-03 zarja le, l. lent). [TORTENETI, R1 B2 F-03 ota NEM
            # ervenyes:] a FALLBACK ("OUTP:ENAB") dialektuson a
            # best-effort szemantika (hiba-elnyeles) megmaradt -- DE (MAX
            # verifier F2 lelete) `_output_cmd == "OUTP:ENAB"` NEM KIZAROLAG
            # akkor all be, ha a bare "OUTP" ténylegesen elutasitott parancs
            # -- `_detect_output_command()` BÁRMELY `_query`-kivetelre (pl.
            # tiszta olvasasi timeout is) fallback-re valt, tehat egy
            # felredetektalt sessionben az R2-08 defekt a primer firmware-en
            # is megmaradt (R2-08 F2 reziduum).
            #
            # R1 B2 (2026-10-25, F-03): LEZARVA -- az elnyeles megszunt, a
            # bare OUTP ON MINDKET dialektuson egylovéses
            # (`single_attempt=True`), a kivetel MINDIG propagal (a flag a
            # `finally`-ben konzervativan True). Valodi fallback-firmware-en
            # (ahol a bare OUTP elutasitott parancs) egy SCPI-elutasitas
            # `;*OPC?` mellett nem szuksegkeppen Python-kivetel, hanem a
            # hibasorba kerul (aktiv CHARGE-lepesben a TestRunner
            # hibasor-pollja SCPI_CRITICAL_ERROR FAULT-ra vezeti -- ures
            # whitelist, scpi_error_policy.py; CODE_OBSERVED, nem HIL);
            # ez a driver-garancia KIZAROLAG transzportkivetelre szol. Az
            # `*OPC?` pontos
            # viselkedese elutasitott parancs utan osszetett uzenetben
            # SOURCE_REQUIRED.
            #
            # LEZÁRVA (2026-09-16, R2-08-F1): a korábban itt dokumentált
            # "ISMERT, NYITVA HAGYOTT RESIDUUM" (a fenti CH1/CH2 engedélyező
            # blokkok try/except NÉLKÜLI voltára) a metódus tetején lévő
            # komment szerint MOST már lezárva -- l. ott a teljes indoklást.
            self._verify_combination_mode()
            self._query("OUTP ON;*OPC?", single_attempt=True)
            # Az OPC-nyugta nem állapot-readback; SERIES-ben HIL_PENDING.
            self._wait_output_activation(stop_requested)
        finally:
            # A flag KONZERVATIVAN True marad akkor is, ha a parancs hibat
            # dobott: egy elveszett *OPC? valasz NEM bizonyitja, hogy a
            # kimenet OFF, es egy hamis False lenne a VESZELYES irany
            # (check_concurrent_psu_load() egy esetleg elo kimenet mellett
            # engedne Load-ot -- rules/10-safety.md 2. pont: "bizonytalan
            # state is safety esemeny"). Ugyanaz a try/finally mintazat,
            # mint all_outputs_off() primer aganak flag-kezelese.
            self.output_commanded_on = True

    def output_off(self) -> None:
        # Governance-v2 review-kör 1 (CF-12/HW-R1-02): a korábbi kód nem
        # hívott _select_ch1()-et és nem cikluson keresztül tiltotta le a
        # csatornákat fallback módban, tehát csak az UTOLJÁRA kiválasztott
        # csatornát kapcsolta le — SERIES/PARALLEL módban ez a másik
        # csatornát energizálva hagyhatta a sikeres CHARGE_DONE úton (az
        # EGYETLEN output_off() hívóhely a kódbázisban). A mód-agnosztikus
        # API [N7] szerint output_off()-nak minden esetben a teljes kimenetet
        # kell kikapcsolnia — ez pontosan az all_outputs_off() szemantikája,
        # ami már try/finally-ben, csatorna-hibatűrően kezeli mindkét ágat.
        self.all_outputs_off()

    def all_outputs_off(self) -> None:
        # Release-audit 2026-08-23 (2.5): a _detect_output_command() fallback-je
        # (OUTP:ENAB) csatorna-scope-olt parancs a gyári kézikönyv szerint
        # (2220-30-1/prog.txt 1477-1496. sor) — szemben a bare OUTP-vel, ami
        # "minden engedélyezett csatornára" vonatkozik (uo. 1553-1567. sor). Ha
        # a fallback egy tranziens (nem tényleges firmware-inkompatibilitási)
        # hiba miatt aktiválódott, a régi kód itt csak az aktuálisan kiválasztott
        # csatornát tiltotta volna le — gyengítve a CLAUDE.md #10.1 "induláskori/
        # befejezéskori PSU ALL OUTPUTS OFF" invariánst. Fallback módban ezért
        # explicit módon mindkét csatornát letiltjuk.
        if self._output_cmd == "OUTP":
            # Teljes kódbázis review 8. kör (2026-08-30), Hardver/Műszervezérlés
            # P1 lelet: a fallback-ág (lentebb) és safe_off() (l. ott a
            # docstringet) MÁR explicit, dokumentáltan mindig False-ra állítja
            # a flaget, függetlenül a write sikerétől — ez az elsődleges ("OUTP")
            # ág korábban NEM követte ezt a saját maga kimondott elvet: ha a
            # write InstrumentTimeoutError-t dobott (max_retries kimerülve), a
            # self.output_commanded_on = False sor sosem futott le, a flag a
            # hívás előtti (jellemzően True) értéken ragadt. emergency_stop()
            # mind a 6 controllerben KÖZVETLENÜL ezt az ágat hívja (a
            # gyakoribb kódút), és check_concurrent_psu_load() az EGYETLEN
            # forrásként olvassa ezt a flaget — kommunikációs hiba után egy
            # soron következő jogos PSU/Load-bekapcsolást hamis
            # CONCURRENT_PSU_LOAD_ON FAULT blokkolhatott volna.
            #
            # 2026-09-12, DFAM-039/F7 (V6, fuggetlen verifier): ez a
            # SAFETY-KRITIKUS safe-off ut a valos labor-egysegen TENYLEGESEN
            # futo ("OUTP") dialektus -- a fallback-agnal (lentebb) MAR
            # megtortent atomizalas ide nem ert el, holott ez a
            # ténylegesen elerheto ag. `_query(...;*OPC?)`-re valtva a
            # kikapcsolas bizonyitottan vegrehajtodik, mielott barmi mas
            # SCPI menne -- a try/finally szemantika valtozatlan.
            try:
                self._query("OUTP OFF;*OPC?")
            finally:
                self.output_commanded_on = False
            return
        # Teljes kódbázis review 3. kör (2026-08-29), Hardver/Műszervezérlés
        # P0 lelet: korábban ha a CH1 write hibázott (max_retries kimerülve,
        # InstrumentTimeoutError), a kivétel megszakította a ciklust — a CH2
        # OFF-parancsa SOHA nem ment ki, és az output_commanded_on flag sem
        # frissült (stale maradt). CLAUDE.md §10.3 "az egyik OFF hiba nem
        # akadályozhatja a másik OFF megkísérlését" — csatornánként izolált
        # try/except, a flag feltétel nélküli frissítésével, mielőtt a hibát
        # újra dobnánk (a hívó, pl. safe_off()/InstrumentManager.safe_all_off(),
        # már helyesen kezeli a bizonytalan-OFF állapot jelzését).
        # 2026-09-12, DFAM-039/F7: csatornankent ATOMIKUS kuldes (l.
        # _apply_config_atomic()) -- ez a SAFETY-KRITIKUS all-off ut: ha egy
        # kozbeekelodo Device Clear korabban elvethette volna a csatorna-
        # valasztast, az OFF-parancs a ROSSZ (korabban kivalasztott)
        # csatornara mehetett volna, a szandekolt csatornat BEKAPCSOLVA
        # hagyva. A csatornankenti try/except izolacio VALTOZATLAN (egyik
        # csatorna hibaja nem akadalyozhatja a masikat).
        # R16-B14: SERIES-ben az ENAB OFF bontja a módot, a tiltás pedig
        # elmarad (HIL_VERIFIED: SN9204604; gyártói szerződés SOURCE_REQUIRED).
        # Ismeretlen vagy kombinált tényleges/kért módban csak globális OFF;
        # a nem támogatott parancs OPC-nyugtája önmagában nem OFF-igazolás.
        # R16-B14F2: a vészleállítást nem késleltetheti retry-s módolvasás.
        # A globális OFF hibája csak igazolt INDEPENDENT csatornatiltással
        # váltható ki; kombinált/ismeretlen módban továbbra is propagál.
        global_off_exc: InstrumentTimeoutError | None = None
        try:
            self._query("OUTP OFF;*OPC?")
        except InstrumentTimeoutError as exc:
            global_off_exc = exc
        try:
            actual_mode: str | None = self.query_combination_mode()
        except (InstrumentTimeoutError, InstrumentInvalidReading):
            actual_mode = None
        if self.combination_mode != PsuMode.INDEPENDENT or actual_mode != "OFF":
            try:
                if global_off_exc is not None:
                    raise global_off_exc
                # R16-B14F3: egyszeri állapotellenőrzés, pollablak nélkül.
                # Az OFF-readback fizikai késése HIL_PENDING.
                if _parse_bool_response(self._query("OUTP?"), "PSU fallback globális OFF"):
                    raise InstrumentInvalidReading("PSU: fallback globális OFF nem igazolt")
            finally:
                self.output_commanded_on = False
            return
        last_exc: Exception | None = None
        for ch in ("CH1", "CH2"):
            try:
                self._apply_config_atomic(f"INST:SEL {ch}", f"{self._output_cmd} OFF")
            except Exception as e:
                last_exc = e
        self.output_commanded_on = False
        if last_exc is not None:
            raise last_exc

    def read_output_state(self) -> bool:
        """Review-kör 5, Hardver/Műszervezérlés HW5-02 (P3): TISZTÁN
        olvasás-only diagnosztika -- a tényleges (nem parancsolt)
        kimeneti állapotot kérdezi le, `output_commanded_on`-tól
        FÜGGETLENÜL. Nem hív semmilyen safety-döntést; a hívó felelőssége
        eldönteni, mit kezd egy esetleges commanded/actual eltéréssel
        (l. instrument_manager.py::poll_device_errors() kiterjesztése).

        Elsődleges ("OUTP") módban a bare `[SOURce:]OUTPut[:STATe][:ALL]?`
        MINDEN engedélyezett csatornára vonatkozik (2220-30-1/prog.txt
        1553-1567. sor) -- egyetlen, csatorna-választás nélküli lekérdezés
        elég. Fallback ("OUTP:ENAB") dialektusban csak igazolt INDEPENDENT
        tényleges és kért módnál használjuk a csatornák enable-bitjeinek
        OR-ját (uo. 1477-1495. sor). Kombinált módban az enable nem a
        globális kimeneti állapot: az OUTP? readback szükséges; hibája
        explicit bizonytalan állapotot jelent, kivétellel.

        [regresszió, review-kör 5, HW-R5-G (P3)] a fallback ág a
        lekérdezéshez SZÜKSÉGSZERŰEN csatornát VÁLT (`INST:SEL CH1`/
        `CH2`), tehát ez a "TISZTÁN olvasás-only" jelleg a hívó
        szempontjából csak addig igaz, amíg a metódus a hívás UTÁN is
        determinisztikus csatorna-állapotban hagyja a műszert. A hívás
        előtt korábban CSAK a bare "OUTP" módban volt garantált, hogy a
        metódus nem hagy csatorna-választást maga után (mert ott nincs
        INST:SEL write); a fallback ág korábban CH2-n hagyta a
        kiválasztást. Ma ez a `poll_device_errors()` mintáin keresztül
        periodikusan, AKTÍV energiaátadás közben is lefut -- a `finally`
        biztosítja, hogy minden hívó (jelenlegi és jövőbeli) ugyanazt a
        CH1-re-visszaálló konvenciót kapja, mint a többi driver-metódus
        (`set_output_voltage()`/`set_output_current()`/`output_on()`
        mind `_select_ch1()`-gyel kezd)."""
        if self._output_cmd == "OUTP":
            return _parse_bool_response(self._query("OUTP?"), "PSU kimeneti állapot")
        # Kombinált módban az enable nem a globális output állapota.
        # Ha OUTP? sem támogatott, a kivétel explicit bizonytalan állapot.
        if self.combination_mode != PsuMode.INDEPENDENT or self.query_combination_mode() != "OFF":
            return _parse_bool_response(self._query("OUTP?"), "PSU fallback globális állapot")
        any_enabled = False
        try:
            for ch in ("CH1", "CH2"):
                # 2026-09-12, DFAM-039/F7 (V4, fuggetlen verifier): a
                # csatorna-valasztas + allapot-lekerdezes korabban KULON
                # write()/query() volt -- szerkezetileg AZONOS a mar
                # javitott parokkal (csatorna-cimzesi kockazat egy aktiv
                # energiaatadas alatt periodikusan futo diagnosztikai
                # uton). _apply_atomic_query() egyetlen uzenetben kuldi.
                resp = self._apply_atomic_query(f"INST:SEL {ch}", "OUTP:ENAB?")
                if _parse_bool_response(resp, f"PSU {ch} kimeneti állapot"):
                    any_enabled = True
        finally:
            self._select_ch1()
        return any_enabled

    def read_regulation_state(self) -> dict[str, bool]:
        """Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2, felhasználói
        döntés 2026-09-10): TISZTÁN olvasás-only diagnosztika -- a PSU
        CH1 regulációs státuszát (Constant Voltage / Constant Current)
        és a kimenet be/ki bitjét kérdezi le a `STATus:OPERation:
        INSTrument:ISUMmary1:CONDition?` (2220-30-1/prog.txt 1955-1972. sor,
        "the operation condition register of a channel", valós idejű,
        NEM latched -- ugyanez az állítás a Load `STAT:QUES:COND?`-jánál
        SOURCE_CONFLICT alá esik, l. SCF-015; e metódus TISZTÁN olvasás-
        only, safety-döntést nem hoz, tehát a konfliktus nem érinti)
        regiszterből, a
        driver saját CH1-addressing konvencióját követve (ugyanaz a
        csatorna, mint `measure_output_voltage()`/`measure_output_current()`).
        Bit-tábla forrás (2220-30-1/prog.txt 2560-2584. sor, Table 3-4 "OSR bit
        functions"): bit0=CV, bit1=CC, bit3=ON (bit2=CAL, bit4-7 nem
        használt -- ezeket szándékosan nem dekódoljuk, nem regulációs
        állapot). A kézikönyv saját, EVENt-lekérdezésre adott példája
        (2017-2018. sor) megerősíti a bit-dekódot: "...might return 9,
        which indicates that channel 1 is turned on and in constant
        voltage mode" (9 = bit3+bit0) -- formálisan az ISUMmary<x>
        [:EVENt]? parancshoz tartozik, nem a driver által ténylegesen
        használt :CONDition?-hez, DE a kézikönyv (661-664. sor) explicit
        kimondja, hogy a CONDition-regiszter bitváltozása az EVENt-
        regiszterben is megjelenik -- azaz mindkét lekérdezés UGYANAZT az
        OSR bit-táblát (Table 3-4) osztja, a példa érvényes korroboráció.

        FONTOS, a HW-R5-A tanulságát követő tervezési döntés: ez a
        metódus SZÁNDÉKOSAN NEM megy a `poll_device_errors()` kritikus
        escalation csatornájára -- a CV bit egy tipikus AGM/VRLA töltés
        abszorpciós/float fázisában SZINTE MINDIG aktív (ez a
        rendeltetése, nem hibaállapot), ugyanaz a hibaosztály, ami a
        Load RS/VON/TBF bitjeit tévesen escalálta HW4-01-ben. A hívó
        (`TestRunner._poll_psu_regulation_state()`) egy KÜLÖN, NEM-
        escaláló GUI-only csatornára teszi közzé (l. ott a docstringje),
        SOSEM a device-error-loghoz vagy a session CSV/riporthoz."""
        resp = self._query("STAT:OPER:INST:ISUM1:COND?").strip()
        try:
            value = int(resp)
        except ValueError:
            value = int(float(resp))
        # R14-B2: SERIES-ben az ISUM nem különbözteti meg igazoltan a CV/CC-t.
        if self.combination_mode == PsuMode.SERIES:
            return {"CVCC_VALID": False, "ON": bool(value & 8)}
        return {"CV": bool(value & 1), "CC": bool(value & 2), "ON": bool(value & 8)}

    def measure_output_voltage(self) -> float:
        """R13-B2 / R13-UD-PSU-SERIES-MEAS (2026-11-05): CH1 reports
        total output voltage in the loaded SERIES and PARALLEL HIL on
        SN 9204604, fw 1.15-1.05, with 47 ohm load and DMM reference.
        See measure_output_current() for the four raw loaded-HIL files
        and the scope of this evidence. Selected-CH2 voltage and ALL
        readings mirror the total; do not sum them. Parameterized CH2
        queries time out in combined modes.
        This agrees with 2220S-900-01 Rev. C, users.txt 1047-1051.
        The user decision supersedes the September SCF-010 half-voltage
        interpretation; its discrepancy remains unexplained.
        INDEPENDENT keeps the existing CH1 voltage readback.
        """
        return _validate_reading(float(self._query("MEAS:VOLT? CH1")), "feszültség (CH1)")

    def measure_output_current(self) -> float:
        """R13-B2: CH1 reports total output current AND voltage in the
        loaded SERIES and PARALLEL HIL (2026-11-05, SN 9204604,
        fw 1.15-1.05): 47 ohm / 3 W resistor, DMM voltage reference
        across the combined output, no battery, Load input open.
        SERIES CC test: 10.03 mA; CV test: 21.76 mA.
        PARALLEL CV test: 22.59 mA; CC test: 10.04 mA, matching the
        10 mA current setpoint rather than twice that setpoint.
        Selected-CH2 voltage and ALL voltage/current readings mirror
        the total; never sum CH1/CH2 or the duplicate ALL readings.
        Raw files under Mérések/HIL/scpi_inventory/:
        psu_series_energized_20261105T203730Z.json (CC),
        psu_series_energized_20261105T203919Z.json (CV),
        psu_parallel_energized_20261105T204610Z.json (CV),
        psu_parallel_energized_20261105T204628Z.json (CC).
        Evidence is limited to these 1 V, 0.01/0.05 A setpoint tests;
        it does not establish behavior at higher voltages or currents.
        INDEPENDENT keeps the existing CH1 current readback.
        """
        resp = self._query("MEAS:CURR? CH1")
        return _validate_reading(float(resp), "áram")

    # ------------------------------------------------------------------ #
    # Kombinációs mód                                                      #
    # ------------------------------------------------------------------ #

    def set_mode_independent(self) -> None:
        # Fw 1.15-1.05: INST:COMB:OFF → error 170. Helyes: SOUR:OUTP:SER/PAR OFF/ON.
        # 2026-09-12, DFAM-039/F7 (V3, fuggetlen verifier -- ez a sibling-hely
        # NEV SZERINT szerepelt a DEFECT_FAMILY_REGISTRY.yaml sibling_search_
        # scope-jaban, de az elozo korben kimaradt): atomikus kuldes, ugyanaz
        # a mintazat, mint set_output_voltage()-nel.
        self._apply_config_atomic("SOUR:OUTP:SER OFF", "SOUR:OUTP:PAR OFF")
        self.combination_mode = PsuMode.INDEPENDENT

    def set_mode_series(self) -> None:
        self._apply_config_atomic("SOUR:OUTP:PAR OFF", "SOUR:OUTP:SER ON")
        self.combination_mode = PsuMode.SERIES

    def set_mode_parallel(self) -> None:
        self._apply_config_atomic("SOUR:OUTP:SER OFF", "SOUR:OUTP:PAR ON")
        self.combination_mode = PsuMode.PARALLEL

    def query_combination_mode(self) -> str:
        """Teljes kódbázis review 2. kör (2026-09-16, R2-03-F1, P2, DFAM-004
        silent_catchall_dispatch új előfordulása ebben a fájlban): a két
        boolean-readback korábban `== "1"` szigorú összehasonlítással dőlt el,
        és MINDEN más válasz (szó-forma, lebegőpontos NR1, csonka/elcsúszott
        válasz) CSENDBEN a "OFF" ágra esett. A gyártói kézikönyv (prog.txt
        1538-1552. és 1497-1510. sor) e két lekérdezéshez NEM dokumentál
        "Returns" szakaszt (csak a SET oldal Arguments-ét: 0|1|OFF|ON) --
        SOURCE_REQUIRED a pontos válaszformátumra --, míg a KÖZVETLENÜL
        fölötte dokumentált testvér-query (OUTP:ENAB?, uo. 1494-1495. sor)
        SZÓ-formát ad ("Disabled"/"Enabled"), az INST:COMB? (uo. 1022-1024.
        sor) szintén ("Series"/"Parallel"/"NONE"). Ezen a konkrét egységen a
        numerikus "0"/"1" HIL-igazolt (Folyamatok/jegyzőkönyvek/
        scpi_command_test_20260606_183715.csv 48-49/53/59/65-66. sor), tehát
        ez NEM megfigyelt hiba -- a javítás a CSENDES ÖSSZEOMLÁST szünteti
        meg: INDEPENDENT módban a hívó (main_window.py::_construct_and_
        connect_instruments()) éppen az "OFF" választ FOGADJA EL, tehát egy
        értelmezhetetlen válasz a kaput ÁTENGEDTE volna, miközben a PSU
        fizikailag SER/PAR módban lehet, így eltérő tényleges móddal
        indulhatna a session. R13-B2: a régi fél-pack mérési értelmezést
        az R13-UD-PSU-SERIES-MEAS döntés felülírta.
        `_parse_bool_response()` ugyanazt a tolerancia + fail-closed mintát
        adja, amit az R2-03 kör MÁR bevezetett ebben a fájlban
        (read_output_state()); ismeretlen válaszra InstrumentInvalidReading,
        amire a hívó safe_all_off()+disconnect_all()-tal reagál, és a session
        NEM indul el.

        KEEP_AS_IS: a SER-elsőbbségű precedencia (ha egy firmware valaha
        MINDKETTŐRE True-t adna) VÁLTOZATLAN -- fizikailag lehetetlen
        állapot, amit a hívó bármelyik ágon eltérésként kezel; külön ág
        nyitása új, forrás nélküli hibaosztályt vezetne be."""
        ser = _parse_bool_response(
            self._query("SOUR:OUTP:SER?"), "PSU SERIES kombinációs mód"
        )
        par = _parse_bool_response(
            self._query("SOUR:OUTP:PAR?"), "PSU PARALLEL kombinációs mód"
        )
        if ser:
            return "SER"
        if par:
            return "PAR"
        return "OFF"

    # ------------------------------------------------------------------ #
    # Belső segédek                                                        #
    # ------------------------------------------------------------------ #

    def _verify_combination_mode(self) -> None:
        """Friss mód-readback minden ON előtt; eltéréskor a hívó safe-offol."""
        expected = {
            PsuMode.INDEPENDENT: "OFF",
            PsuMode.SERIES: "SER",
            PsuMode.PARALLEL: "PAR",
        }[self.combination_mode]
        actual = self.query_combination_mode()
        if actual != expected:
            raise InstrumentInvalidReading(
                f"PSU kombinációs mód eltérés: kért={expected}, tényleges={actual}"
            )

    def _wait_output_activation(self, stop_requested: Callable[[], bool] | None = None) -> None:
        """Közös monoton nyugtaablak a kimenetre és a kombinációs módra."""
        # R16-B14F1: az 1 s projektpolicy, nem gyártói válaszidő-garancia.
        # Nincs újabb aktiválás vagy Device Clear a függő nyugta alatt.
        deadline = time.monotonic() + 1.0
        expected = {
            PsuMode.INDEPENDENT: "OFF",
            PsuMode.SERIES: "SER",
            PsuMode.PARALLEL: "PAR",
        }[self.combination_mode]
        last_error = "PSU: bekapcsolás nem igazolt"

        def read(cmd: str) -> str:
            check_activation_stop(stop_requested)
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise InstrumentInvalidReading(last_error + " (nyugtaablak lejárt: 1 s)")
            # Egyetlen olvasás, a teljes ablakból fennmaradó timeouttal.
            # A session eredeti timeoutja minden terminális úton visszaáll.
            resource = self._resource
            original_timeout = resource.timeout
            try:
                resource.timeout = min(
                    self.timeout_ms if original_timeout is None else original_timeout,
                    max(1, int(remaining * 1000)),
                )
                self._last_cmd = cmd
                return resource.query(cmd)
            except Exception as exc:
                raise InstrumentTimeoutError(
                    f"PSU bekapcsolási nyugta olvasási hiba: {cmd!r}"
                ) from exc
            finally:
                resource.timeout = original_timeout

        while True:
            output = _parse_bool_response(read("OUTP?"), "PSU bekapcsolás utáni kimeneti állapot")
            ser = _parse_bool_response(read("SOUR:OUTP:SER?"), "PSU SERIES kombinációs mód")
            par = _parse_bool_response(read("SOUR:OUTP:PAR?"), "PSU PARALLEL kombinációs mód")
            check_activation_stop(stop_requested)
            actual = "SER" if ser else "PAR" if par else "OFF"
            last_error = (
                f"PSU kombinációs mód eltérés: kért={expected}, tényleges={actual}"
                if actual != expected else "PSU: bekapcsolás nem igazolt"
            )
            remaining = deadline - time.monotonic()
            if output and actual == expected and remaining >= 0.0:
                return
            if remaining <= 0.0:
                raise InstrumentInvalidReading(last_error + " (nyugtaablak lejárt: 1 s)")
            time.sleep(min(0.05, remaining))

    def _select_ch1(self) -> None:
        self._write("INST:SEL CH1")

    def _apply_config_atomic(self, *commands: str, single_attempt: bool = False) -> None:
        """2026-09-12, DFAM-039 sibling-javitas (F7, l. device_dmm.py::
        _apply_config_atomic() azonos indoklasa/mintaja) — egy TOBBPARANCSOS
        PSU-konfiguraciot (jellemzoen csatorna-kivalasztas + setpont/allapot-
        iras) EGYETLEN, szinkronizalt SCPI program message-kent kuld ki
        (`a;:b;*OPC?`), kulon `_write()`-ok sorozata HELYETT.

        Miert kell: a `_write()`/`_query()` minden kiserlet ELOTT feltetel
        nelkul `_clear_resource()`-t (VISA/IEEE-488 Device Clear) kuld — ha
        egy csatorna-kivalasztas (`INST:SEL CH1`) es az utana kovetkezo
        setpont-iras (`SOUR:VOLT ...`) KULON `_write()` hivaskent megy ki, a
        MASODIK hivas Device Clear-je elmeletileg eldobhatja az ELSOT, meg
        FEL NEM DOLGOZOTT allapotban — csendben, a hibasorba semmit nem irva
        (l. Folyamatok/canonical/CLAIM_LEDGER.yaml INSTR-DMM-CONFIG-ATOMIC-001,
        ugyanez a mechanizmus, MAS transzporton/eszkozon MAR HIL-lel
        megfigyelve a DMM-en). Ha ez itt bekovetkezne, a setpont a KORABBAN
        kivalasztott csatornara irodna, NEM CH1-re — csendes, safety-releváns
        csatorna-cimzesi hiba. A zaro `*OPC?`-re varva (l. `_query()` hivas
        lent) a szekvencia bizonyitottan vegrehajtodott, mielott barmely
        kesobbi hivas Device Clear-t kuldhetne.

        Readback-verifikacio (pl. "melyik csatorna van kivalasztva most")
        SZANDEKOSAN NINCS hozzaadva: az atomikus uzenet MAGA zarja ki a
        rest (nincs Python-oldali also ablak, ahol egy masik hivas
        kozbeekelodhetne egy tobbparancsos szekvencia KOZEPEBE), ellentetben
        a DMM SAMP:TIM esetevel, ahol a muszer MAGA csendben mas erteket
        alkalmazhat a kertnel egyetlen parancson BELUL is (ott a readback a
        MUSZER dontesét ellenorzi, itt a Python-oldali hivas-sorrendet).

        FORRAS-STATUS (fuggetlen verifier, 2026-09-12): a Keithley 2220
        SAJAT programozoi kezikonyveben (prog.txt) NINCS "Using Device
        Clear" szakasz es nincs explicit allitas arra, hogy a Device Clear
        egy mar kikuldott, meg fel nem dolgozott parancsot eldobna -- a
        mechanizmus (B2, l. device_dmm.py azonos jelolese) itt MEGELOZO/
        elmeleti, NEM HIL-lel vagy gyartoi forrassal kozvetlenul igazolt
        (a 2026-09-11/12-i SOROS HIL-meresek soran elejtett INST:SEL-t
        soha nem figyeltek meg -- l. DEFECT_FAMILY_REGISTRY.yaml DFAM-039).
        A;:b;*OPC? szintaxis MAGA VERIFIED_PRIMARY (prog.txt 515-521/
        1196-1211/2764-2767. sor). HIL_PENDING: elo hardveren meg nem
        igazolt, hogy ez a konkret mechanizmus valaha ELOFORDULT volna a
        PSU-n -- a fix indokolt es biztonsagos (nem novel viselkedesi
        teret), de NEM tekintheto HIL-lel lezart hibaosztalynak.

        `single_attempt` -- l. `_query()` (R1 B2 F-03)."""
        self._query(";:".join(commands) + ";*OPC?", single_attempt=single_attempt)

    def _apply_atomic_query(self, *commands: str) -> str:
        """Mint `_apply_config_atomic()`, de az UTOLSO parancs egy
        LEKERDEZES, es a valaszat adja vissza -- nincs kulon `;*OPC?`, mert
        maga a lekerdezes-valasz kiolvasasa a szinkronizacio (2220-30-1/prog.txt
        2764-2767. sor: "the controller must read a query response before
        it sends the next command"). Hasznalat: csatorna-valasztas +
        allapot-lekerdezes egyetlen atomikus uzenetben (pl. read_output_
        state()/_detect_output_command()), hogy egy kozbeekelodo Device
        Clear ne valaszthassa szet a kettot."""
        return self._query(";:".join(commands))

    def _write(self, cmd: str) -> None:
        self._last_cmd = cmd
        # Teljes kódbázis review 3. kör (2026-08-29), Hardver/Műszervezérlés
        # P2 lelet — l. device_dmm.py azonos ellenőrzés docstringje.
        if self._resource is None:
            raise InstrumentTimeoutError(
                f"PSU: nincs aktív kapcsolat (connect() nem hívva): {cmd!r}"
            )
        last_err: Optional[Exception] = None
        for _ in range(self.max_retries):
            _clear_resource(self._resource)
            try:
                self._resource.write(cmd)
                return
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"PSU write failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err

    def _query(self, cmd: str, *, single_attempt: bool = False) -> str:
        # R1 B2 (2026-10-25, F-03, rules/10-safety.md 6. pont):
        # `single_attempt=True` -> pontosan EGY kiserlet (az elotte kuldott
        # Device Clear megmarad), retry NINCS -- az ON-t hordozo
        # uzenetekhez (output_on()), ahol egy elveszett valasz utan a
        # parancs kimenetele bizonytalan, es vak ismetles tilos. Az
        # alapertelmezes (max_retries kiserlet) az OFF- es olvasasi
        # utakon VALTOZATLAN.
        #
        # 2026-09-12, DFAM-039/F2 (a device_dmm.py fixre irt fuggetlen
        # verifier azonos kovetkezetlensege PSU-n): a _write() mindig
        # frissitette a self._last_cmd-et, a _query() korabban NEM -- az
        # _apply_config_atomic() bevezetese ota TOBB konfiguracios metodus
        # _query()-n megy, tehat check_error() '[last: ...]' diagnosztikai
        # annotacioja elavult parancsot mutatott volna ezeken az utakon.
        self._last_cmd = cmd
        if self._resource is None:
            raise InstrumentTimeoutError(
                f"PSU: nincs aktív kapcsolat (connect() nem hívva): {cmd!r}"
            )
        last_err: Optional[Exception] = None
        attempts = 1 if single_attempt else self.max_retries
        for _ in range(attempts):
            _clear_resource(self._resource)
            try:
                return self._resource.query(cmd)
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"PSU query failed after {attempts} retries: {cmd!r}"
        ) from last_err
