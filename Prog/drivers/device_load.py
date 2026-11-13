"""
Keithley2380Load SCPI driver.
[N9] input_commanded_on nyilvántartás.
[ELLENŐRIZENDŐ] INPUT ON vs INP ON firmware — _detect_input_command() önellenőrzés.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from Prog.src.exceptions import (
    InstrumentCommandError,
    InstrumentInvalidReading,
    InstrumentTimeoutError,
    check_activation_stop,
)

# CLAUDE.md §10.4: aktív kisütés alatt a Load áram/feszültség/teljesítmény-
# readback NaN/Inf/overload esetét sem szabad csendben, feltétel nélküli
# float()-tal átengedni — Python float("nan")/float("inf") NEM dob kivételt,
# egy NaN érték csendben megmérgezné az Ah-integrációt (minden >
# összehasonlítás False-t ad NaN-nal, a MAX_DISCHARGE_AH limit észrevétlenül
# kikapcsolna). Ugyanaz az elv, mint a device_dmm.py::_validate_voltage()-ben.
_OVERLOAD_THRESHOLD = 1e30

# R16-B10/B10b: közös 1 s-os projektpolicy; nem gyártói garancia (HIL_PENDING).
INPUT_ACTIVATION_ACK_TIMEOUT_S = 1.0


def _validate_reading(value: float, label: str) -> float:
    if math.isnan(value) or math.isinf(value):
        raise InstrumentInvalidReading(f"Load {label}: NaN vagy Inf érték: {value}")
    if abs(value) > _OVERLOAD_THRESHOLD:
        raise InstrumentInvalidReading(f"Load {label}: overload érték: {value:.3e}")
    return value


# Review-kör 5, Hardver/Műszervezérlés HW4-01 (P3): a Keithley 2380
# STATus:QUEStionable:CONDition? regiszter (NR1, csatorna-specifikáció
# nélküli) bitjeinek elnevezése -- VERIFIED_PRIMARY forrás:
# 2380-120-60-900-01_A_Nov_2015.txt 3415-3486. sor. A kézikönyv e regisztert
# egy helyen "unlatched"-nek nevezi (4762-4765. sor), DE a lent escalált 7
# bitre (VF/OC/OP/OT/RRV/LRV/PS) UGYANAZON kézikönyv bitenkénti latch-
# szöveget ad (l. SCF-015, SOURCE_CONFLICT) -- a kód ma KIZÁRÓLAG olvas,
# PROT:CLE-t nem küld, tehát ez a konfliktus a mai (fail-safe irányú)
# viselkedést nem érinti. A hiányzó bitek (5, 6,
# 7 [RUN], 8 [EPU, "not used"]) szándékosan nincsenek benne -- 7 (RUN, list
# futás jelző) sem "protection"-jellegű, itt nem releváns diagnosztika.
_QUESTIONABLE_STATUS_BITS: dict[int, str] = {
    0: "VF",    # Voltage Fault (OV vagy reverse voltage)
    1: "OC",    # Over Current
    2: "RS",    # Remote Sense connected
    3: "OP",    # Overpower
    4: "OT",    # Over Temperature
    9: "RRV",   # Remote Reverse Voltage
    10: "UNR",  # Unregulated
    11: "LRV",  # Local Reverse Voltage
    12: "OV",   # Over Voltage
    13: "PS",   # Protection Shutdown (a tényleges "input kikapcsolva" trip)
    14: "VON",  # Voltage-on (sink current on, Von level felett)
    15: "TBF",  # Trace Buffer Full
}

# [regresszió, review-kör 5, HW-R5-A (P1), a HW4-01 (78100ab) közvetlen
# következménye] a fenti tábla összekeverte a VÉDELMI/trip biteket az
# INFORMATÍV állapotbitekkel -- a hívó (instrument_manager.py::
# _poll_load_questionable_status()) MINDEN aktív bitet a kritikus
# device-error csatornára küldött, ahol test_runner.py::is_critical_
# scpi_error() (üres NON_CRITICAL_SCPI_ERROR_SUBSTRINGS whitelist miatt)
# MINDEN nem-üres stringet kritikusnak minősít -> FAULT. Valós hardveren
# a RS (négyvezetékes sense bekötésnél tartósan igaz), VON (default Von
# szint MINimum = 0 V, egy rákötött akku ezt szinte mindig átlépi -- l.
# 2380-120-60-900-01_A_Nov_2015.txt 6455-6476. sor,
# [SOURce:]VOLTage[:LEVel]:ON) és TBF (sikeres trace-capture normál
# végállapota) bitek NEM hiba-jelzők, hanem állandó/normál üzemi
# állapotjelzők -- ezekkel MINDEN kisütési/töltési session az első
# poll_device_errors() tick-nél hamis FAULT-tal állt volna le. UNR
# (Unregulated) is ide tartozik, amíg HIL nem igazolja, hogy bemenet-OFF
# állapotban NEM tartósan igaz (HIL_PENDING). Csak a ténylegesen
# véd(elm)i/trip-jellegű bitek (2380 txt 3405-3514. sor: "fault"/
# "condition has occurred"/PS-t is beállító OC/OP/OT) escalálnak.
#
# Kör 21 F17 (ARC21-6 instance 1, DFAM-010, átnevezve a vezető
# aláhúzásról): ezt a maszkot `instrument_manager.py` réteghatáron át
# importálja -- ez maga a HELYES minta (egyetlen forrás egy safety-
# releváns Load protection-bit maszkra, CLAUDE.md §8), csak a korábbi
# vezető aláhúzásos ("privát") elnevezés volt téves, mivel a szimbólum
# SZÁNDÉKOSAN publikus API a rétegek között.
QUESTIONABLE_STATUS_PROTECTION_BITS: frozenset[str] = frozenset(
    {"VF", "OC", "OP", "OT", "RRV", "LRV", "OV", "PS"}
)


def _clear_resource(resource: Any) -> None:
    """Teljes kódbázis review 2. kör (2026-08-28) + 3. kör (2026-08-29) P1
    kiegészítés — l. device_dmm.py azonos segédfüggvény docstringje."""
    try:
        resource.clear()
    except Exception:
        pass


def _parse_input_state_response(resp: str) -> bool:
    """Azonos parser az állapotpollhoz és a bekapcsolási nyugtához."""
    resp = resp.strip().upper()
    if resp in ("1", "ON"):
        return True
    if resp in ("0", "OFF"):
        return False
    try:
        numeric = float(resp)
    except ValueError:
        pass
    else:
        if numeric == 0.0:
            return False
        if numeric == 1.0:
            return True
    raise InstrumentInvalidReading(f"Load bemeneti állapot: értelmezhetetlen válasz: {resp!r}")


@dataclass
class Keithley2380Load:
    timeout_ms: int = 5000
    max_retries: int = 3

    _resource: Any = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _last_cmd: str = field(default="", init=False, repr=False)
    _input_cmd: str = field(default="INP", init=False, repr=False)
    input_commanded_on: bool = field(default=False, init=False)
    input_on_commanded_at: float | None = field(default=None, init=False)

    # ------------------------------------------------------------------ #
    # Kapcsolat                                                            #
    # ------------------------------------------------------------------ #

    def connect(self, resource_string: str, rm=None) -> None:
        if rm is None:
            import pyvisa
            rm = pyvisa.ResourceManager()
        self._resource = rm.open_resource(resource_string)
        self._resource.timeout = self.timeout_ms
        self._connected = True
        # Kör 20 Batch E3 (HW20-01, DFAM-039 5. előfordulás) -- a mechanizmus
        # és az indoklás l. device_dmm.py::connect() azonos javítását.
        # 2380-oldali forrás (2380-120-60-900-01_A_Nov_2015.txt):
        # 4294-4303. sor (`*CLS` törli az Error Queue-t), 3818-3820. sor
        # ("Including Common Commands ... you may insert them anywhere in
        # the message"), 3906-3912. sor ("Commands execute in the order that
        # they are presented in the program message"), 4045-4048. sor
        # (`*OPC?` + a válasz kiolvasásának kötelezettsége).
        #
        # BEST-EFFORT, A KIVÉTEL NEM PROPAGÁL -- ez itt NEM stílus, hanem
        # safety-követelmény (CLAUDE.md §6.1, SAFE-001/INS-002): a `*CLS`
        # KÖZVETLENÜL a safety-kritikus `input_off()` ELŐTT áll. Ha egy
        # `_query()` timeout innen kiszökne, a bemenet-kikapcsolás ezen a
        # hívási úton EL SEM INDULNA. A GUI-úton az instrument_manager.py::
        # connect_all() rollback-ága ma is hív safe_off()-ot, DE a
        # `Prog/tools/hil_*_method_only.py` szkriptek a `connect()`-et a
        # try-blokkon KÍVÜL hívják -- ott nincs ilyen háló, és a driver
        # amúgy sem támaszkodhat a hívójára.
        # A `*CLS` KUDARCA nem rosszabb a fix ELŐTTI állapotnál: a hibasor
        # ugyanúgy töröletlen marad, amit a meglévő `poll_device_errors()`
        # csatorna felszínre hoz -- ugyanaz a best-effort minta és indoklás,
        # mint a lenti `VOLT:ON 0` / `SOUR:FUNC:MODE FIX` hívásoknál.
        # SORREND-INVARIÁNS: a `*CLS` marad az ELSŐ SCPI üzenet; az
        # `input_off()` UTÁNRA tolni tilos (kitörölné egy elutasított
        # "INP OFF" hibabejegyzését a hibasorból).
        try:
            self._query("*CLS;*OPC?")
        except Exception:
            pass
        self._detect_input_command()
        # SAFE-001/INS-002: energiaszegény alapállapot csatlakozáskor — a bemenet
        # explicit OFF-ra állítva, nem feltételezzük az előző (front panel/session)
        # állapotot.
        self.input_off()
        # R2-B2 (R2-HW-01): öröklött INP:SHOR / INP:TIM semlegesítése,
        # FAIL-CLOSED -- l. _neutralize_inherited_short_and_timer().
        self._neutralize_inherited_short_and_timer()
        # Review-kör 5, Hardver/Műszervezérlés HW-R5-I (P3, felhasználói
        # döntés 2026-09-10): a Von latch funkció (2380 txt 2324-2339. sor)
        # alapértelmezésben ON (VOLT:LATC default, 2380 txt 6492-6494. sor),
        # és ha egy KORÁBBI session/front panel egy nem-nulla Von szintet
        # (VOLT:ON) hagyott, a manual explicit dokumentálja a kockázatot:
        # "Set Von value and enable Von latch function may cause failure of
        # turning on the load... If the load cannot be turned on, check
        # whether the Von latch function is enabled. If yes, set Von value
        # to 0 to clear the fault." Enélkül input_on() SCPI-szinten
        # sikeresnek látszik (INP?=1), de a load fizikailag NEM terhel, ha a
        # tényleges akkufeszültség a korábban hagyott Von alatt van --
        # csendben, diagnosztizálhatatlanul (csak a MÁR MEGLÉVŐ
        # check_load_current_setpoint() WARNING-ja jelzi a tünetet, az okot
        # nem). Von=0-nál a latch funkció definíció szerint hatástalan
        # (bármilyen nem-negatív feszültség "Von fölött" van) -- ez a gyári
        # MINimum alapértékhez (VOLT:ON default) tér vissza, tehát nem új,
        # a driver-életciklustól idegen állapotot vezet be.
        #
        # Best-effort, NEM safety-invariáns (l. device_psu.py::
        # _disable_output_timer() azonos indoklása, HW-R5-C): egy
        # kommunikációs hiba itt nem rosszabb, mint a fix ELŐTTI állapot
        # (a régi Von-érték marad, ugyanaz a kockázat, ami eddig is
        # fennállt) -- nem szabad, hogy meghiúsítsa a teljes connect()-et.
        try:
            self._write("VOLT:ON 0")
        except Exception:
            pass
        # Kör 19 D9 (HW19-03, DFAM-032 review_search-tengely, "inherited/
        # assumed instrument state vs. what the driver actually establishes
        # at connect"): a `[SOURce:]FUNCtion:MODE {FIXed|LIST}` (VERIFIED_
        # PRIMARY, Keithley 2380 kézikönyv) korábban SOSEM lett explicit
        # beállítva/lekérdezve -- ha egy KORÁBBI session vagy front-panel
        # művelet a loadot LIST módban hagyta, a SOUR:FUNC CURR/CURR:RANG/
        # SOUR:CURR parancsok többé nem kormányozzák a süllyesztőáramot
        # (a tünet megkülönböztethetetlen lenne a már javított Von-latch
        # osztálytól, l. fent). A LIST-módú load tényleges viselkedése
        # INP ON alatt SOURCE_REQUIRED/HIL_PENDING -- ez a komment nem
        # állít ennél erősebb következményt. Best-effort, ugyanaz a minta,
        # mint a fenti VOLT:ON 0 írás: egy kommunikációs hiba itt nem
        # rosszabb, mint a fix ELŐTTI állapot, nem szabad meghiúsítania a
        # teljes connect()-et.
        try:
            self._write("SOUR:FUNC:MODE FIX")
        except Exception:
            pass

    def _neutralize_inherited_short_and_timer(self) -> None:
        """R2-B2 (R2-HW-01, DFAM-032 inherited-state axis; DFAM-039): a
        `[SOURce:]INPut:SHORt[:STATe]` és a `[SOURce:]INPut:TIMer[:STATe]`
        sticky állapot, előlapról/korábbi sessionből (SAV0 power-on, 2380
        txt 1305-1320) öröklődhet (Default OFF, Returns 0|1; 2380 txt
        5814-5858). SHORT ON mellett CC/CP/CR módban a load a tartomány
        110%-át süllyesztheti (1280-1288); a TRAN előtt a kézikönyv szerint
        SHORT OFF-ot kell beállítani ("should", 5938-5942). A timer lejárati szemantikája SOURCE_REQUIRED/
        HIL_PENDING; a 0|1 válaszformátum a valós firmware-en HIL_PENDING.

        Egyetlen szinkronizált üzenet (`_apply_config_atomic`), majd
        szigorú readback: csak a pontos "0" fogadható el. Timeout,
        "1" (az OFF-parancs után is aktív -> InstrumentCommandError) vagy
        bármely más válasz (-> InstrumentInvalidReading) kivételként
        propagál, a connect() bukik.

        A FAIL-CLOSED garancia HATÓKÖRE a manager/GUI út: ott az
        `InstrumentManager.connect_all()` rollbackje (safe_off() +
        disconnect() minden megkísérelt műszeren, a PSU-t is beleértve)
        fut, a `_InstrumentConnectWorker` `connect_failed` jelet ad, runner
        nem épül, így ezen az úton input_on() nem érhető el. A driver
        ÖNMAGÁBAN NEM reteszel: a connect() elején már `_connected=True`,
        és egy hívó, amely a kivétel után mégis közvetlenül input_on()-t
        hívna, kiadná a bekapcsoló parancsot (a connect()-et try-blokkon
        kívül hívó Prog/tools szkriptek a saját felelősségük).

        A bemenetről ez a metódus NEM állít végleges OFF-állapotot: az
        elsődleges `INP OFF;*OPC?` (input_off()) előtte csak OFF-
        KÍSÉRLET; ha a műszer az OFF-parancsot elutasítaná, a bemenet
        bekapcsolva maradhat (meglévő korlát). HA a connect() emiatt vagy a
        semlegesítés hibája miatt bukik, a connect_all() rollback
        safe_off()-ja újra megkísérli az OFF-ot, a worker hibaüzenete pedig
        "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" figyelmeztetést ad (a connect_all()
        kivétele nem hordoz safe_off_attempted jelölést). HA viszont a
        semlegesítés sikeres, egy elutasított INP OFF-ot a connect() NEM
        észlel; azt a session-poll (SYST:ERR? / commanded-actual INP?)
        jelzi."""
        self._apply_config_atomic("INP:SHOR OFF", "INP:TIM OFF")
        for query in ("INP:SHOR?", "INP:TIM?"):
            resp = self._query(query).strip()
            if resp == "1":
                raise InstrumentCommandError(
                    f"Load {query} OFF parancs után is aktív (readback {resp!r})"
                )
            if resp != "0":
                raise InstrumentInvalidReading(
                    f"Load {query}: értelmezhetetlen válasz: {resp!r}"
                )

    def _detect_input_command(self) -> None:
        """
        [IGAZOLVA] Keithley 2380 firmware: INP OFF (nem INPUT OFF).
        INP 0 és INP OFF egyaránt elfogadott a műszer által.
        """
        self._input_cmd = "INP"

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

    def check_error(self, clear_first: bool = True) -> list[str]:
        # Teljes kódbázis review 1. kör (2026-08-26), P3 lelet: l.
        # device_dmm.py::check_error() ugyanezen javítás indoklása.
        #
        # Teljes kódbázis review 3. kör (2026-08-29), Hardver/Műszervezérlés
        # P3 lelet: l. device_dmm.py::check_error() `_last_cmd`
        # diagnosztikai kontextus kiegészítésének indoklása.
        #
        # `clear_first` (review-kör 2, HW2-08 P1-1 follow-up, 2026-09-08):
        # l. `_write()` docstringje -- `pulse_measurement_service.py` a
        # trace-capture elindítása (`configure_trace()`) UTÁN, MÉG a
        # `fetch_trace()` előtt hívja ezt is, `clear_first=False`-szal.
        #
        # Kör 17 MAX-effort review, R17-18 (l. device_psu.py::check_error()
        # azonos hivatkozású kommentje): most hogy a _query() is frissíti
        # self._last_cmd-et, a SAJÁT "SYST:ERR?" queryNK ELŐTT kell
        # elmentenünk a diagnosztikai célú értéket ("mi okozta a hibát"),
        # különben önmagunkat írnánk felül ("[last: SYST:ERR?]",
        # értelmetlen) -- ÉS instrument_manager.py a hibasort akár
        # TÖBBSZÖR (max 32x/eszköz/tick) üríti egy tickben, tehát a
        # visszaállítás nélkül a 2. hívástól kezdve már "SYST:ERR?"-t
        # mutatna, elveszítve az EREDETI, hibát okozó parancsot minden
        # további ürítési körben.
        last_cmd_before_check = self._last_cmd
        resp = self._query("SYST:ERR?", clear_first=clear_first)
        self._last_cmd = last_cmd_before_check
        stripped = resp.strip()
        if stripped == "0" or resp.startswith("+0") or resp.startswith("0,"):
            return []
        return [f"{stripped} [last: {last_cmd_before_check}]"]

    def safe_off(self) -> bool:
        """True, ha az INPUT OFF parancs ténylegesen elment (nem dobott
        kivételt) — a hívó (InstrumentManager.safe_all_off()) ez alapján
        dönt bizonytalan-OFF-állapot jelzéséről (CLAUDE.md §10.4).
        input_commanded_on mindig False-ra áll, mert ez a PARANCSOLT
        (szoftver-oldali) állapotot tükrözi, nem a tényleges hardveres
        visszaigazolást — ugyanaz az elv, mint device_psu.py::safe_off()-nál.
        Teljes kódbázis review 8. kör (2026-08-30), Hardver/Műszervezérlés
        P1 lelet: korábban ha input_off() a KRITIKUS "INP OFF" write-on
        (nem a flag-frissítés utáni, már dokumentáltan nem-kritikus TRAN 0
        hívásán) hibázott, a kivétel a flag frissítése ELŐTT szakította meg
        a metódust — a flag a hívás előtti (jellemzően True) értéken ragadt."""
        try:
            self.input_off()
            success = True
        except Exception:
            success = False
        self.input_commanded_on = False
        return success

    # ------------------------------------------------------------------ #
    # Terhelés vezérlés                                                    #
    # ------------------------------------------------------------------ #

    def set_mode_cc(self) -> None:
        self._write("SOUR:FUNC CURR")

    def set_current(self, i: float) -> None:
        # Review-kör 5, Hardver/Műszervezérlés HW-R5-E (P2, felhasználói
        # döntés 2026-09-10): a 2380-120-60 két áramtartományt támogat (Low
        # 0-6A, High 0-60A, 2380 txt 3042-3043. sor); a driver korábban
        # egyiket sem állította be expliciten -- a tartomány a session
        # előtti, öröklött állapotból eredt (nem determinisztikus), és a
        # gyári alapérték MAXimum (high range, 2380 txt 6031-6033. sor)
        # rontja a pontosságot kis áramoknál (High range-en ±(0,05%+0,1%FS)
        # ≈ ±60mA, tipikus 2-6A-es C10/C20 kisütésnél ez 1-3%-os
        # szisztematikus hiba a mért kapacitásban).
        #
        # A numerikus CURR:RANG a következő szintírásokat is korlátozhatja.
        # R16-K1 HIL: SN 802436052777870003, fw 1.00-1.00, CC/FIX,
        # INPUT OFF mellett a plafon fölötti Immediate/ALEV írás 120-as
        # elutasítás, a régi szint megmarad. A plafon csökkentése viszont
        # az Immediate/ALEV/BLEV szinteket csendben, hibasorbejegyzés
        # nélkül vágta az új numerikus plafonra. Ez két külön hibamód.
        # Forrás: 2380 kézikönyv p103–106, p139 és
        # Mérések/HIL/load_range/20261107T231940_622929Z_LOAD_RANGE_K0.json.
        # A readback nem fizikai LOW/HIGH-azonosító; a fenti konfigurációs
        # HIL nem bizonyít pontosságot vagy más példány/fw viselkedését.
        # A CC-sorrend marad range -> Immediate: a végleges plafon UTÁN
        # írjuk a kívánt áramot. A régi tranziens szintek levágódhatnak;
        # configure_transient_pulse() a saját plafonját és A/B szintjeit
        # külön rendezi, OFF-előfeltétellel.
        # 2026-09-15, R2-09 (DFAM-039 sibling): a két setpont-írás korábban
        # KÜLÖN _write() volt -- ha a második hívás Device Clear-je
        # eldobta az elsőt, a load a KORÁBBI (a CURR:RANG által esetleg
        # már levágott) áramszinten terhelt volna, csendben. L.
        # _apply_config_atomic().
        self._apply_config_atomic(f"CURR:RANG {i:.6g}", f"SOUR:CURR {i:.6g}")

    def input_on(
        self, clear_first: bool = True, *, wait_for_ack: bool = True,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        """`clear_first` -- l. `_write()` docstringje (HW2-08 P1-1
        follow-up): `pulse_measurement_service.py` a trace-capture
        elindítása után, MÉG a `fire_transient_pulse()`/`fetch_trace()`
        előtt hívja ezt, `clear_first=False`-szal.

        Teljes kódbázis review 2. kör (2026-09-16, R2-08-F1 testvér-lelete,
        P2, DFAM-032): a flag-beállítás korábban a write UTÁN, GUARD NÉLKÜL
        állt -- ha a `_write()` a max_retries kimerülése után dobott,
        miközben a parancs fizikailag már átment, a flag False-on ragadt egy
        ÉLŐ bemenet mellett, és a hamis False a VESZÉLYES irány (safety.py::
        check_concurrent_psu_load() egy élő Load mellett engedné a PSU-t --
        rules/10-safety.md 2. pont). Pontosan az a minta, amit ugyanebben a
        fájlban az `input_off()` (l. ott, CF-13/HW-R1-04) és a
        device_psu.py::output_on()/all_outputs_off() MÁR követ. A kivétel
        VÁLTOZATLANUL propagál (mind a hívóhely try/except-ben van és
        FAULT-ra eszkalál); a `;*OPC?` szinkronizáció bevezetése
        SZÁNDÉKOSAN NEM része ennek a fixnek (KEEP_AS_IS: `_query()`-re
        váltás a `clear_first=False` trace-capture úttal interferálna).

        R1 B2 (2026-10-25, F-03, rules/10-safety.md 6. pont): az "INP ON"
        EGYLÖVÉSES (`single_attempt=True`) -- egy sikertelen write után a
        parancs kimenetele bizonytalan (a műszer megkaphatta), ezért a
        driver NEM küldi újra vakon (`clear_first=False` mellett a régi
        retry ráadásul a 2. kísérlettől Device Clear-t is küldött a
        trace-capture ablakon belül). A kivétel a hívóhoz propagál, a flag
        konzervatívan True. A garancia a Python-oldali transzportkivételre
        szól; `;*OPC?` itt továbbra sincs (l. fent), tehát egy SCPI-szintű
        elutasítás nem kivétel. A `max_retries`, az `input_off()` és a
        többi írás retry-ja VÁLTOZATLAN.

        R16-B10: normál ON után állapotnyugtára várunk. A hardveres capture
        (clear_first=False) és a szoftveres pulse (wait_for_ack=False)
        időzítése KEEP_AS_IS: ott nincs új olvasás vagy várakozás."""
        # R16-B10b: az ablak a parancskísérlettől indul, nem az első polltól.
        commanded_at = time.monotonic()
        try:
            self._write(f"{self._input_cmd} ON", clear_first=clear_first, single_attempt=True)
        finally:
            self.input_commanded_on = True
            self.input_on_commanded_at = commanded_at
        if clear_first and wait_for_ack:
            self._wait_input_activation(stop_requested)
            # Igazolt nyugta után egy új OFF már nem bekapcsolási késés.
            self.input_on_commanded_at = None

    def _wait_input_activation(self, stop_requested: Callable[[], bool] | None = None) -> None:
        """Egyszeri ON után korlátos, clear és I/O-retry nélküli nyugta."""
        # R16-B10: az 1 s a B14-gyel azonos projektpolicy, nem gyártói garancia.
        # HIL_PENDING: a valós nyugtaidő-eloszlás még nincs validálva.
        deadline = time.monotonic() + INPUT_ACTIVATION_ACK_TIMEOUT_S
        message = "Load: bekapcsolás nem igazolt (nyugtaablak lejárt: 1 s)"
        resource = self._resource
        while True:
            check_activation_stop(stop_requested)
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise InstrumentInvalidReading(message)
            original_timeout = resource.timeout
            cmd = f"{self._input_cmd}?"
            try:
                resource.timeout = min(
                    self.timeout_ms if original_timeout is None else original_timeout,
                    max(1, int(remaining * 1000)),
                )
                self._last_cmd = cmd
                try:
                    response = resource.query(cmd)
                except Exception as exc:
                    raise InstrumentTimeoutError(
                        f"Load bekapcsolási nyugta olvasási hiba: {cmd!r}"
                    ) from exc
            finally:
                resource.timeout = original_timeout
            actual = _parse_input_state_response(response)
            check_activation_stop(stop_requested)
            remaining = deadline - time.monotonic()
            if actual and remaining >= 0.0:
                return
            if remaining <= 0.0:
                raise InstrumentInvalidReading(message)
            time.sleep(min(0.05, remaining))

    def input_off(self) -> None:
        # A kritikus OFF-parancs *OPC?-nyugtát kér; hiba esetén is törlődik
        # a parancsolt ON-jelző. Ez nem fizikai OFF-readback: a bizonytalan
        # állapotot a hívó safe-off/emergency útja kezeli.
        #
        # Kör 19 Batch D2 (HW19-02, DFAM-001+DFAM-039): a `safe_off()`
        # (fent) korábban a puszta `_write()` sikeréből ("a hívás nem dobott
        # kivételt") olvasta ki a sikert -- ez SEMMIT nem bizonyít arról,
        # hogy az "INP OFF" ténylegesen célba ért/végrehajtódott. A
        # STRUKTURÁLISAN AZONOS PSU-oldali safe-off út (device_psu.py
        # `output_on()`-hoz tartozó "OUTP OFF" ág) már `_query(...;*OPC?)`-
        # re lett cserélve pontosan emiatt (l. ott a DFAM-039/F7 kommentet).
        # A Load-oldal sosem kapta meg ezt a testvér-javítást, holott
        # `InstrumentManager.safe_all_off()` a Load és a PSU booleanjait
        # AZONOS bizonyítékerővel kezeli (LOAD_INPUT_STATE_UNCERTAIN,
        # checkpoint last_safe_off_result, report.json). `_query()` a
        # meglévő `try`/`finally` szemantikát megtartja -- egy timeout
        # esetén a flag ugyanúgy False-ra áll, a kivétel ugyanúgy
        # propagál. A `clear_first=False` trace-capture-interferencia
        # KEEP_AS_IS indoka (fent, input_on()) NEM vonatkozik erre a
        # metódusra -- annak nincs `clear_first` paramétere.
        # HIL_PENDING (MAX verifier P2-1, 2026-09-16): a "<cmd>;*OPC?"
        # összetett üzenetforma a valós Keithley 2380-on ezen a konkrét
        # hívóhelyen (input_off()) MÉG NINCS HIL-lel gyakorolva -- a
        # kézikönyv alapján VERIFIED_PRIMARY (SCPI az üzenet-egységeket
        # sorrendben hajtja végre, tehát "INP OFF" a *OPC? előtt fut le
        # akkor is, ha a *OPC? maga hibázna -- fail-safe irány, csak
        # hamis BIZONYTALAN, nem hamis BIZTOS eredményt adhatna), de
        # fizikai megfigyelés még nem történt. L.
        # CLAIM_LEDGER.yaml INSTR-LOAD-CONFIG-ATOMIC-001.
        try:
            self._query(f"{self._input_cmd} OFF;*OPC?")
        finally:
            self.input_commanded_on = False
            self.input_on_commanded_at = None
        # Release-audit 2026-08-23 (2.15): a biztonságkritikus INP OFF
        # (fentebb) és az input_commanded_on flag frissítése között korábban
        # egy NEM-kritikus write (TRAN 0) állt — ha az elhasalt (pl. a load
        # épp SUP01-pulse tranziens módban volt, kommunikációs hiccup), a
        # kivétel megszakította a metódust a flag frissítése ELŐTT. Az összes
        # emergency_stop()/safe-off hívó try/except Exception: pass-be
        # csomagolja ezt a hívást, tehát a kivétel elnyelődött, DE a flag
        # stuck True maradt, holott a load fizikailag már biztonságosan OFF
        # volt — ez egy KÉSŐBBI, legitim PSU-bekapcsolást hamis
        # CONCURRENT_PSU_LOAD_ON FAULT-tal blokkolhatott (SAFE-006 egy
        # konkrét kiterjesztése). A flag frissítése most közvetlenül a
        # biztonságkritikus write UTÁN, a nem-kritikus TRAN 0 ELŐTT történik.
        # [SUP01-PULSE HIL 2026-08-23 lelet] a tranziens generátor ([SOURce:]
        # TRANsient[:STATe], alapérték OFF, kezicsvegyv "TRAN 0"/"TRAN 1")
        # sticky allapot - ha configure_transient_pulse() bekapcsolta, input_off()
        # utan is ON maradna, meglepetest okozva a kovetkezo, nem-tranziens
        # hivasnak. SCPI igazolva: 2380-120-60-900-01_A_Nov_2015.txt 5938-5960. sor.
        # Review-kör 5, Hardver/Műszervezérlés HW5-06 (P3, a release-audit
        # 2.15 döntés természetes folytatása): korábban ha EZ a NEM-kritikus
        # write hibázott, a kivétel VÁLTOZATLANUL kiszivárgott innen — mind
        # a 11 hívóhely (charge/discharge/chemid/golden_gg/traditional_
        # learning/ocv_soc/iec_dc_resistance_controller.py + pulse_
        # measurement_service.py) egy generikus `except Exception` mögött
        # UGYANAZZAL a "LOAD_OFF_FAILED" jelzéssel klasszifikálta, mint a
        # KRITIKUS "INP OFF" hibáját -- pedig a bemenet fizikailag MÁR
        # biztonságosan OFF (a flag fent, a try/finally-ban, MÁR frissült).
        # A hívó/operátor emiatt egy valós biztonsági hibát színlelő
        # jelzést kapott egy pusztán kozmetikus, a tranziens generátor
        # KÖVETKEZŐ pulzus-módú használatát érintő write-hibáért. Elnyelve,
        # ugyanazzal az idiómával, mint _clear_resource() -- a bemenet
        # OFF-állapotát ez nem érinti, az instrumentum SAJÁT hibasorát
        # (SYST:ERR?) a hívók a check_error()-on keresztül továbbra is
        # elérik, ha a diagnosztika mégis kellene.
        try:
            self._write("TRAN 0")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # SUP01-PULSE-002/004, SUP01-SRC-010: hardveres tranziens PULSE mód   #
    # + trace buffer — a jövőbeli PulseMeasurementService (SUP01-ARCH-001)#
    # építőeleme. Additív, NEM hívja semmilyen meglévő workflow — a       #
    # OcvSocController pulse-mérése továbbra is tick-alapú marad, amíg    #
    # ez HIL-validálva és bekötve nincs (l. Riportok/sup01_*.md).         #
    # SCPI igazolva: 2380-120-60-900-01_A_Nov_2015.txt 5460-5522,         #
    # 6241-6339, 4128-4149, 5606-5622. sor.                                #
    # ------------------------------------------------------------------ #

    def configure_transient_pulse(
        self, pulse_A: float, baseline_A: float, pulse_width_s: float
    ) -> None:
        """
        Egyetlen impulzus triggerre (CURR:TRAN:MODE PULS) — a 30 s-os pulse
        idozitését a műszer adja, nem Python sleep vagy a fő 2 s runner tick.
        PULSE módban az AWIDTH a tényleges impulzusszélesség (kézikönyv 6313-6315).

        [SUP01-PULSE HIL 2026-08-23 lelet, elso valos hardveres futtataskor
        talalva] a korabbi verzio nem kuldte el a [SOURce:]TRANsient[:STATe]
        ("TRAN 1") parancsot, ami magat a tranziens generatort kapcsolja be -
        alapertelmezese OFF (kezikonyv 5938-5960. sor: "This command turns
        the transient generator on or off... Default Value: OFF"). Enelkul
        a FORCe:TRIGger hatastalan volt, a load a sima (nem-tranziens)
        aramszinten maradt - valos HIL-teszttel derult ki (a trace 20
        szegmensre bontva egysegesen lapos volt, nem mutatott lepcsot).

        R16-K1: konfiguráció csak visszaolvasott INPUT OFF mellett.
        A végleges numerikus plafon max(pulse_A, baseline_A), és minden
        A/B szintírás ezután, egy root-specifieres, szinkron üzenetben fut.
        SN 802436052777870003, fw 1.00-1.00, CC/FIX, INPUT OFF HIL:
        plafon fölötti Immediate/ALEV írás -> 120, a régi szint megmarad;
        plafoncsökkentés -> Immediate/ALEV/BLEV csendes vágása, hibasor 0.
        Forrás: 2380 kézikönyv p103–106, p139 és
        Mérések/HIL/load_range/20261107T231940_622929Z_LOAD_RANGE_K0.json.
        Aktiválás előtt a hívó hibasort és alkalmazott A/B szinteket
        ellenőriz; az OPC önmagában nem igazol konfigurációs helyességet.
        Az aktív impulzus és a trace fizikai validálása HIL_PENDING.
        """
        if self.read_input_state():
            raise InstrumentCommandError(
                "Load tranziens konfiguráció: INPUT OFF előfeltétel sérült"
            )
        self._apply_config_atomic(
            f"CURR:RANG {max(pulse_A, baseline_A):.6g}",
            "CURR:TRAN:MODE PULS",
            f"CURR:TRAN:ALEV {pulse_A:.6g}",
            f"CURR:TRAN:BLEV {baseline_A:.6g}",
            f"CURR:TRAN:AWID {pulse_width_s:.6g}",
            "TRIG:SOUR BUS",
            "TRAN 1",
        )

    def read_transient_current_levels(self) -> tuple[float, float]:
        """Alkalmazott ALEV/BLEV; csak a trace-capture indítása előtt hívható."""
        pulse_A = _validate_reading(float(self._query("CURR:TRAN:ALEV?")), "ALEV")
        baseline_A = _validate_reading(float(self._query("CURR:TRAN:BLEV?")), "BLEV")
        return pulse_A, baseline_A

    def fire_transient_pulse(self, clear_first: bool = True) -> None:
        """A konfigurált tranziens impulzus szoftveres elsütése (FORCe:TRIGger).

        `clear_first` -- l. `_write()` docstringje (HW2-08 P1-1 follow-up):
        `pulse_measurement_service.py` `clear_first=False`-szal hívja, mert
        ez a trace-capture indítása (`configure_trace()`) és a lekérdezése
        (`fetch_trace()`) KÖZÖTTI utolsó write a Load-on."""
        self._write("FORC:TRIG", clear_first=clear_first)

    def configure_trace(
        self, feed: str = "CURR", interval_s: float = 0.001, points: int = 1000
    ) -> None:
        """Trace buffer a pulse alatti mért áram/feszültség gyűjtéséhez.
        feed: "CURR" | "VOLT" | "TWO". Buffer max 1000 minta (kézikönyv szerint)."""
        if feed not in ("CURR", "VOLT", "TWO"):
            raise ValueError(f"feed={feed!r} — CURR, VOLT vagy TWO lehet")
        # R15-B2: sticky NEXT mellett POIN -> -221; elobb szinkron NEV.
        # Az osszetett NEV--POIN;*OPC? uzenet HIL_PENDING.
        self._apply_config_atomic(
            "TRAC:FEED:CONT NEV",
            "TRAC:CLE",
            f"TRAC:FEED {feed}",
            f"TRAC:TIM {interval_s:.6g}",
            f"TRAC:POIN {points}",
        )
        self._write("TRAC:FEED:CONT NEXT")

    def fetch_trace(self) -> str:
        """Nyers trace adat lekérdezése — a válasz parszolása a hívó felelőssége.

        Review-kör 2 (2026-09-08), HW2-08: `clear_first=False` -- l. `_query()`
        docstringje. `fire_transient_pulse()` UTÁN közvetlenül hívva, amíg a
        trace-buffer feltöltése még folyamatban lehet."""
        return self._query("TRAC:DATA?", clear_first=False)

    # ------------------------------------------------------------------ #
    # Mérések                                                              #
    # ------------------------------------------------------------------ #

    def read_input_state(self) -> bool:
        """Review-kör 5, Hardver/Műszervezérlés HW5-03 (P3): TISZTÁN
        olvasás-only diagnosztika -- a tényleges (nem parancsolt) bemeneti
        állapotot kérdezi le, `input_commanded_on`-tól FÜGGETLENÜL. Nem hív
        semmilyen safety-döntést; a hívó felelőssége eldönteni, mit kezd
        egy esetleges commanded/actual eltéréssel (l. instrument_manager.py
        ::poll_device_errors() kiterjesztése).

        A Load egyetlen bemenetű műszer (nincs CH1/CH2 mint a PSU-nál) --
        `[SOURce:]INPut[:STATe]?` "Returns 0|1" (2380 txt 5793-5810. sor),
        egyetlen lekérdezés elég, csatorna-választás nélkül.

        Teljes kódbázis review 2. kör (2026-09-15, R2-03, P3, PREVENTÍV/
        PROJECT_POLICY, HIL_PENDING): a `read_questionable_status()` a
        KÓDBAN toleráns egy firmware által esetleg lebegőpontos alakban
        ("1.0") visszaadott NR1-re, DE ez maga is forrás nélküli,
        kódból megfigyelt (CODE_OBSERVED) védekező minta, nem gyártói
        dokumentum vagy HIL-mérés -- ez a fix kizárólag konzisztencia-célú
        elővigyázatosság ezzel a testvér-mintával, NEM egy megfigyelt hiba
        javítása. A numerikus ág `float()`-tal is próbálkozik, DE
        KIZÁRÓLAG pontos 0.0/1.0 értéket fogad el -- egy dokumentálatlan
        "2" vagy NaN/Inf válasz továbbra is explicit hibát dob."""
        actual = _parse_input_state_response(self._query(f"{self._input_cmd}?"))
        if actual and self.input_commanded_on:
            # R16-B10b: az első pozitív readback lezárja az ON-nyugtaablakot.
            self.input_on_commanded_at = None
        return actual

    def read_questionable_status(self) -> dict[str, bool]:
        """Review-kör 5, Hardver/Műszervezérlés HW4-01 (P3): TISZTÁN
        olvasás-only diagnosztika -- a `STATus:QUEStionable:CONDition?`
        (2380 txt 4760-4776. sor: "read-only register that holds the
        real-time (unlatched) questionable status ... not channel
        specific") regiszter aktuális bitjeit adja vissza, névvel indexelt
        bool-szótárként (l. `_QUESTIONABLE_STATUS_BITS`). A "unlatched"
        állítás UGYANEZEN kézikönyv bitenkénti latch-szövegével áll
        ellentmondásban 7 a 8 escalált bit közül (SCF-015, SOURCE_CONFLICT)
        -- e metódus olvasás-only jellege miatt ez a mai kódot nem érinti.
        NEM hív semmilyen
        safety-döntést, NEM küld `PROT:CLE`-t -- a tényleges latch-
        feloldás HIL-jóváhagyás-köteles, ezen metódus kizárólag a jelenlegi
        állapot lekérdezése. A hívó felelőssége eldönteni, mit kezd egy
        aktív bittel (l. instrument_manager.py::poll_device_errors()
        kiterjesztése)."""
        resp = self._query("STAT:QUES:COND?").strip()
        try:
            value = int(resp)
        except ValueError:
            # Néhány firmware lebegőpontos formátumban adja vissza az NR1
            # regisztert (pl. "13.0") -- a manual NR1-et dokumentál, de a
            # driver a többi readbacknél (measure_*) is toleráns a
            # float-string alakra.
            value = int(float(resp))
        return {name: bool(value & (1 << bit)) for bit, name in _QUESTIONABLE_STATUS_BITS.items()}

    def measure_voltage(self) -> float:
        return _validate_reading(float(self._query("MEAS:VOLT?")), "feszültség")

    def measure_current(self) -> float:
        return _validate_reading(float(self._query("MEAS:CURR?")), "áram")

    def measure_power(self) -> float:
        return _validate_reading(float(self._query("MEAS:POW?")), "teljesítmény")

    # ------------------------------------------------------------------ #
    # Belső segédek                                                        #
    # ------------------------------------------------------------------ #

    def _apply_config_atomic(self, *commands: str) -> None:
        """Teljes kódbázis review 2. kör (2026-09-15, R2-09, DFAM-039
        sibling, a device_psu.py::_apply_config_atomic()/device_dmm.py
        azonos helperjének 3. példánya -- UGYANAZ a minta, új viselkedési
        tér nélkül): egy TÖBBPARANCSOS load-konfigurációt EGYETLEN,
        szinkronizált SCPI program message-ként küld ki (`a;:b;*OPC?`),
        külön `_write()`-ok sorozata HELYETT.

        Miért kell: a `_write()`/`_query()` minden kísérlet ELŐTT
        feltétel nélkül `_clear_resource()`-t (VISA/IEEE-488 Device
        Clear) küld, ami eldobhatja a szekvencia korábbi, még fel nem
        dolgozott tagját -- csendben, a hibasorba semmit nem írva (l.
        CLAIM_LEDGER.yaml INSTR-DMM-CONFIG-ATOMIC-001: ez a mechanizmus
        MÁS eszközön MÁR HIL-lel megfigyelt; a 2380-on MEGELŐZŐ/elméleti,
        HIL_PENDING).

        A ";:" (root specifier) NEM stílus, hanem a 2380 KÉZIKÖNYV
        követelménye: a message 2. és további tagjaira a műszer a "head
        path"-ot előre biggyeszti, tehát `CURR:RANG 2.5;SOUR:CURR 2.5`
        `CURR:SOUR:CURR 2.5`-ként ÉRTELMEZŐDNE (érvénytelen). A vezető
        kettőspont törli a head path-ot. A záró `*OPC?` válaszára várva a
        szekvencia bizonyítottan végrehajtódott, mielőtt bármely későbbi
        hívás Device Clear-t küldhetne.
        Forrás (VERIFIED_PRIMARY, 2380-120-60-900-01_A_Nov_2015.txt):
        3788. sor (pontosvessző-szeparátor), 3789-3812. sor (head path +
        root specifier), 3818-3820. sor (common command a message-ben),
        3888-3889. sor (message unit separator), 3906-3912. sor
        (végrehajtási sorrend), 4045-4048. sor (*OPC? és a válasz
        kiolvasásának kötelezettsége).

        Readback-verifikáció SZÁNDÉKOSAN NINCS hozzáadva: az atomikus
        üzenet MAGA zárja ki a rést, és a TÉNYLEGES áramot a
        controllerek MÁR függetlenül mérik (measure_current() +
        check_load_current_setpoint()) -- ugyanaz az indoklás, mint a
        device_psu.py azonos helperjében."""
        self._query(";:".join(commands) + ";*OPC?")

    def _write(self, cmd: str, clear_first: bool = True, *, single_attempt: bool = False) -> None:
        """`clear_first` -- l. `_query()` docstringje, ugyanaz az elv.
        MAX verifier follow-up (2026-09-08, HW2-08 P1-1): a trace-capture
        MÁR a `TRAC:FEED:CONT NEXT` write-tal elindul (kézikönyv p.97:
        "When NEXT is selected, the storage process starts, fills the
        buffer and then stops"), NEM a `FORC:TRIG`-gel -- tehát minden,
        a capture ELINDÍTÁSA és a `fetch_trace()` közötti write-nak
        (`check_error()`, `input_on()`, `fire_transient_pulse()`) is
        `clear_first=False`-t kell kapnia, nem csak magának a
        `fetch_trace()`-nek.

        `single_attempt=True` (R1 B2 F-03, 2026-10-25): pontosan EGY
        kísérlet, retry nélkül (a `clear_first` szerinti első-kísérlet
        előtti clear változatlan) -- az energizáló "INP ON"-hoz (l.
        `input_on()`), ahol vak ismétlés tilos (rules/10-safety.md 6.
        pont). Alapértelmezésben a viselkedés VÁLTOZATLAN (max_retries)."""
        self._last_cmd = cmd
        # Teljes kódbázis review 3. kör (2026-08-29), Hardver/Műszervezérlés
        # P2 lelet — l. device_dmm.py azonos ellenőrzés docstringje.
        if self._resource is None:
            raise InstrumentTimeoutError(
                f"Load: nincs aktív kapcsolat (connect() nem hívva): {cmd!r}"
            )
        last_err: Optional[Exception] = None
        attempts = 1 if single_attempt else self.max_retries
        for attempt in range(attempts):
            if clear_first or attempt > 0:
                _clear_resource(self._resource)
            try:
                self._resource.write(cmd)
                return
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"Load write failed after {attempts} retries: {cmd!r}"
        ) from last_err

    def _query(self, cmd: str, clear_first: bool = True) -> str:
        """
        clear_first=False (review-kör 2, 2026-09-08, HW2-08 -- a
        device_dmm.py CF-03/HW-R1-01/INSTR-DMM-CLEAR-001 testvér-hibája):
        `pulse_measurement_service.py::_step_hardware_pulse()`
        `configure_trace()`-szal ELINDÍTJA a trace-capture-t (a
        kézikönyv szerint, printed p.97: "When NEXT is selected, the
        storage process starts, fills the buffer and then stops" --
        tehát a capture MÁR a `TRAC:FEED:CONT NEXT` write-tal aktív, NEM
        csak a `FORC:TRIG`-gel), utána `check_error()`, `input_on()`,
        `fire_transient_pulse()`, végül `fetch_trace()` (`TRAC:DATA?`)
        következik -- MIND A NÉGY hívás a capture-ablakon BELÜL fut, és
        mindegyik `clear_first=False`-t kap ettől a ponttól (l.
        `check_error()`/`input_on()`/`fire_transient_pulse()` saját
        docstringje). MAX verifier follow-up (2026-09-08, P1-1): az
        eredeti HW2-08 fix TÉVESEN csak a `fetch_trace()` hívást
        javította, mert a capture kezdetét hibásan `FORC:TRIG`-hez
        kötötte -- a köztes 3 hívás (mindegyik SAJÁT, akkor még
        változatlan clear-first-viselkedéssel) így ÉRINTETLENÜL hagyta a
        tényleges kockázatot.
        Forrás (2380-120-60-900-01): "Using Device Clear" (p.66/77:
        "abort a SCPI command that may be hanging up the GPIB
        interface"); GPIB interfész-függvény tábla, "Device Clear"
        bejegyzés (printed p.50: "command the electronic load to clear
        any activity that would prevent it from receiving and executing
        a new command"); `*WAI` leírás (printed p.75: "trigger actions
        are overlapped with subsequent commands... *WAI can be aborted
        only by sending the electronic load a GPIB DCL"). MAX verifier
        follow-up (P2-1): az eredeti HW2-08 fix tévesen azt állította,
        hogy a kézikönyv NEM mondja ki explicit módon a trace-specifikus
        abort-hatást -- valójában a fenti p.50/p.75 idézetek ERŐSEBBEN
        alátámasztják a kockázatot, mint amit az eredeti fix idézett
        (a citáció volt hiányos, nem a kézikönyv). Ennek ellenére a
        PONTOS, trace-specifikus szemantika (vs. "csak konfigurációs
        állapot marad meg, az 'activity' bizonytalan hatókörű") HIL
        nélkül nem dönthető el -- l. CLAIM_LEDGER.yaml
        INSTR-LOAD-CLEAR-001, SOURCE_REQUIRED/HIL_PENDING marad.
        A védekező irány (ne küldj preventív Device Clear-t egy éppen
        elindított/folyamatban lévő capture-ablakban) kockázatmentes:
        az ELSŐ kísérlet sikeres esetben ugyanúgy működik clear nélkül
        is, RETRY (2. kísérlettől) továbbra is clear-rel indul, ha az
        első kísérlet más okból hibázna, a resync-viselkedés megmarad.
        Minden más hívó (a capture-ablakon KÍVÜLI check_error()/
        input_on()/fire_transient_pulse() hívások) a default
        (clear_first=True) változatlan viselkedését kapja.
        """
        # Kör 17 MAX-effort review, R17-18 (DFAM-001/DFAM-015, 3.
        # testvér egy kétszer már javított mintának -- l. device_dmm.py
        # és device_psu.py azonos hivatkozású _query() kommentje): a
        # _write() mindig frissítette self._last_cmd-et, a _query() itt
        # korábban NEM -- check_error() '[last: ...]' diagnosztikai
        # annotációja emiatt mindig az utolsó ÍRÁST mutatta, sosem egy
        # _query()-n keresztül kiadott parancsot.
        self._last_cmd = cmd
        if self._resource is None:
            raise InstrumentTimeoutError(
                f"Load: nincs aktív kapcsolat (connect() nem hívva): {cmd!r}"
            )
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            if clear_first or attempt > 0:
                _clear_resource(self._resource)
            try:
                return self._resource.query(cmd)
            except Exception as e:
                last_err = e
        raise InstrumentTimeoutError(
            f"Load query failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err
