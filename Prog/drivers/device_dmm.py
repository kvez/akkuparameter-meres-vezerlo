"""
Keysight34465ADMM SCPI driver.
DCV mód: DMM1 (feszültségmérés)
TEMP mód: DMM2 (4-wire PT100, FRTD)
[N8] NaN / Inf / overload (9.9E37) validáció.
"""
from __future__ import annotations
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from threading import RLock
from typing import Any, Concatenate, Optional, ParamSpec, TypeVar

from Prog.src.exceptions import (
    InstrumentError,
    InstrumentTimeoutError,
    InstrumentInvalidReading,
)

# Kör 21 F17 (ARC21-6, DFAM-010 sibling, F16 saját maga hozta létre ezt az
# instance-t): a Kör 21 F16 (HW21-05(b)) egy hívóhelye (Prog/tools/
# hil_dmm_nplc_samptim_instrument_only_check.py) réteghatáron át
# importálta ezt a konstanst -- ugyanaz a minta, mint az ARC21-6
# instance 1 (QUESTIONABLE_STATUS_PROTECTION_BITS): az import a HELYES
# design (egyetlen forrás, CLAUDE.md §8), csak a vezető aláhúzásos
# ("privát") elnevezés téves egy szándékosan réteghatáron át exportált
# szimbólumra. Átnevezve, importálva NEM módosítva. NEM azonos a
# device_load.py/device_psu.py/pulse_measurement_service.py SAJÁT,
# FÜGGETLENÜL definiált (nem importált) `_OVERLOAD_THRESHOLD` másolataival
# -- azok a DFAM-017 (config/const-duplikáció) hatókörébe tartoznak, nem
# ebbe a rétegimport-hibaosztályba, és NEM érintettek e batch által.
OVERLOAD_THRESHOLD = 1e30   # 9.9E37 overload kódhoz


def _clear_resource(resource: Any) -> None:
    """Teljes kódbázis review 2. kör (2026-08-28), Hardver/Műszervezérlés
    P2 lelet (HIL-lel nem igazolt, elővigyázatossági): VISA device-clear-t
    küld minden `_write`/`_query` hívás ELSŐ kísérlete előtt IS, nem csak
    retry előtt (3. kör, 2026-08-29, P1 kiegészítés: a védelem korábban csak
    egy híváson BELÜLI retry előtt futott — a hívások HATÁRÁN, egy előző
    hívás retry-kimerülése UTÁN a KÖVETKEZŐ, más hívás ELSŐ kísérlete előtt
    nem tisztított, ami éppen a `pico_bq_transport.py`-ban dokumentáltan
    kijavított puffer-szinkron-vesztés mintáját hagyta nyitva ezen a
    ponton). Feltétel nélküli hívással most pontosan a `pico_bq_transport.
    py::_call()` mintáját követi. Hibatűrően becsomagolva — ha a resource
    nem támogatja a clear()-t, vagy az maga is hibázik, ez nem
    akadályozhatja a hívást."""
    try:
        resource.clear()
    except Exception:
        pass


class DMMMeasurementBusy(InstrumentError):
    """R16-B5a: a művelet ütközik a DMM mérési tulajdonosával/tranzakciójával."""


_P = ParamSpec("_P")
_T = TypeVar("_T")


def _transaction(
    *, require_unowned: bool = False, blocking: bool = True, voltage_read: bool = False,
) -> Callable[
    [Callable[Concatenate[Keysight34465ADMM, _P], _T]],
    Callable[Concatenate[Keysight34465ADMM, _P], _T],
]:
    # A teljes művelet védett: I/O, validálás és állapotfrissítés együtt.
    def decorate(
        method: Callable[Concatenate[Keysight34465ADMM, _P], _T],
    ) -> Callable[Concatenate[Keysight34465ADMM, _P], _T]:
        @wraps(method)
        def locked(
            self: Keysight34465ADMM, *args: _P.args, **kwargs: _P.kwargs,
        ) -> _T:
            def check_access() -> None:
                if require_unowned and self._measurement_owner is not None:
                    raise DMMMeasurementBusy("DMM: aktív mérési tulajdonos")
                if voltage_read and self._measurement_owner is not kwargs.get("owner"):
                    raise DMMMeasurementBusy("DMM: eltérő mérési tulajdonos")

            # A korai ellenőrzés csak gyors elutasítás. A zár ALATT ismétlünk
            # (TOCTOU); tulajdonváltásnál sem várunk egy teljes VISA-timeoutot.
            check_access()
            if not self._transaction_lock.acquire(blocking=False):
                if not blocking and kwargs.get("owner") is None:
                    raise DMMMeasurementBusy("DMM: folyamatban lévő tranzakció")
                # B21b: a saját tulajdonos rövid helyi lockütközése nem acquisition-hiba.
                # Az owner nélküli poll továbbra sem vár; idegen token már fent kiesett.
                while not self._transaction_lock.acquire(timeout=0.01):
                    check_access()
            try:
                check_access()
                return method(self, *args, **kwargs)
            finally:
                self._transaction_lock.release()
        return locked
    return decorate


@dataclass
class Keysight34465ADMM:
    timeout_ms: int = 10000
    max_retries: int = 3
    max_voltage_jump_V: float = 2.0     # [N8] max megengedett ugrás mintánként

    _resource: Any = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _last_cmd: str = field(default="", init=False, repr=False)
    _last_valid_voltage: Optional[float] = field(default=None, init=False, repr=False)
    _temp_mode: str = field(default="RTD_4W_PT100", init=False, repr=False)
    _thermistor_r25_ohm: float = field(default=10000.0, init=False, repr=False)
    _thermistor_beta_K: float = field(default=3950.0, init=False, repr=False)
    _transaction_lock: RLock = field(
        default_factory=RLock, init=False, repr=False, compare=False,
    )
    _measurement_owner: object | None = field(
        default=None, init=False, repr=False, compare=False,
    )

    @property
    def measurement_owned(self) -> bool:
        """Nem blokkoló belépési jelzés; az I/O-kapu továbbra is a zár alatt él."""
        return self._measurement_owner is not None

    @_transaction(require_unowned=True)
    def acquire_measurement(self, owner: object) -> None:
        """Egyedi, nem None token foglal; azonos tokennel sincs dupla foglalás."""
        if owner is None:
            raise ValueError("DMM: a mérési tulajdonos nem lehet None")
        if self._measurement_owner is not None:
            raise DMMMeasurementBusy("DMM: aktív mérési tulajdonos")
        self._measurement_owner = owner

    @_transaction()
    def release_measurement(self, owner: object) -> None:
        """Csak a tulajdonos oldhat fel, a folyamatban lévő művelet vége után."""
        if owner is None or self._measurement_owner is not owner:
            raise DMMMeasurementBusy("DMM: eltérő mérési tulajdonos")
        self._measurement_owner = None

    # ------------------------------------------------------------------ #
    # Kapcsolat                                                            #
    # ------------------------------------------------------------------ #

    @_transaction(require_unowned=True)
    def connect(self, resource_string: str, rm=None) -> None:
        if rm is None:
            import pyvisa
            rm = pyvisa.ResourceManager()
        self._resource = rm.open_resource(resource_string)
        self._resource.timeout = self.timeout_ms
        self._connected = True
        # Kör 20 Batch E3 (HW20-01, DFAM-039 4. előfordulás; a PSU-oldali
        # testvér-javítás Kör 19 D9/HW19-04-ben már megtörtént, l.
        # device_psu.py::_detect_output_command()). A `*CLS` korábban
        # csupasz `_write()` volt. A `_write()`/`_query()` MINDEN kísérlet
        # ELŐTT feltétel nélkül Device Clear-t küld (l. `_clear_resource()`),
        # a kézikönyv szerint pedig (34460-70-Manual.txt 5391-5397. sor,
        # "Using Device Clear": "The instrument's input and output buffers
        # are cleared") a KÖVETKEZŐ driver-hívás Device Clear-je a még fel
        # nem dolgozott `*CLS`-t csendben eldobhatja. Következmény: az előző
        # processz hibasor-maradéka bennmarad, és az ELSŐ
        # `poll_device_errors()` tick hamis, session-indító FAULT-ot adna.
        # A záró `*OPC?` válaszára várva a `*CLS` bizonyítottan
        # végrehajtódott, mielőtt bármely későbbi hívás clear-t küldhetne.
        # A `*CLS;*OPC?` forma a kézikönyv SAJÁT példája (uo. 5338. sor:
        # `*RST; *CLS; *ESE 32; *OPC?`) -- VERIFIED_PRIMARY.
        # SORREND-INVARIÁNS: ez marad a connect() ELSŐ SCPI üzenete;
        # későbbre tolva a SAJÁT connect-parancsaink hibáit törölné ki.
        # Itt NINCS try/except (ellentétben device_load.py azonos
        # javításával): a connect() után NEM következik safety-kritikus
        # lépés, és a kivételt a hívó (instrument_manager.py::connect_all()
        # rollback-ága + main_window bring-up hibaút) MÁR kezeli.
        self._query("*CLS;*OPC?")

    @_transaction(require_unowned=True)
    def disconnect(self) -> None:
        if self._resource is not None:
            # 2026-09-12, INSTR-DMM-ABORT-001: a session ELHAGYÁSAKOR is
            # vissza kell vinni a trigger-rendszert "idle"-be, különben egy
            # függőben lévő (INIT-tel armed, de FETC?-csel soha be nem
            # fejezett) mérés a KÖVETKEZŐ processz elején is aktív marad --
            # pontosan ez a 2026-09-12-i HIL-megfigyelés 3. futása, ahol az
            # első sima READ? két, vesszővel elválasztott értéket adott
            # vissza (az előző futás SAMP:COUN-ja még élt).
            # Forrás: 34460-70-Manual.txt 5452-5453. sor (nyomtatott 201. o.,
            # "ABORt -- Aborts a measurement in progress, returning the
            # instrument to the trigger idle state") és 5398. sor ("The ABORt
            # command is the recommended method to terminate a measurement").
            # Szándékosan NEM a `_write()` (nincs 3x retry + Device Clear
            # ciklus egy teardown úton): best-effort, egyetlen kísérlet, a
            # hiba nem akadályozhatja a close()-t (rules/10-safety.md 9.).
            try:
                self._resource.write("ABOR")
            except Exception:
                pass
            try:
                self._resource.close()
            except Exception:
                pass
        self._resource = None
        self._connected = False

    @_transaction(require_unowned=True)
    def set_io_timeout_ms(self, timeout_ms: int) -> None:
        """Review-kör 5, Hardver/Műszervezérlés HW5-04 (P3): ideiglenesen
        felülírja a VISA I/O timeoutot a fix, connect()-kori `timeout_ms`
        alapértékhez képest -- a fő hívóhely a `pulse_measurement_
        service.py::_capture_recovery_if_configured()`, ahol egy 10s-nél
        (a jelenlegi alapértelmezett `timeout_ms`) hosszabb, EGYETLEN,
        chunkolatlan `fetch_timed_acquisition()` (FETC?) garantáltan
        timeoutolna, mielőtt a műszer befejezné a kért mintaszám
        felvételét. NEM módosítja a `self.timeout_ms` mezőt (ami a
        "normál üzemi" alapértéket jelöli) -- a hívó felelőssége
        `finally`-ben visszaállítani (l. a fenti hívóhely mintáját)."""
        if self._resource is not None:
            self._resource.timeout = timeout_ms

    @_transaction()
    def is_connected(self) -> bool:
        return self._connected and self._resource is not None

    # ------------------------------------------------------------------ #
    # Alapvető SCPI                                                        #
    # ------------------------------------------------------------------ #

    @_transaction(require_unowned=True)
    def idn(self) -> str:
        return self._query("*IDN?")

    @_transaction(require_unowned=True)
    def reset(self) -> None:
        self._write("*RST")

    @_transaction(require_unowned=True)
    def clear_status(self) -> None:
        self._write("*CLS")

    @_transaction(voltage_read=True, blocking=False)
    def check_error(self, *, owner: object | None = None) -> list[str]:
        # Teljes kódbázis review 1. kör (2026-08-26), P3 lelet: a bare "0"
        # válasz felismerése korábban csak device_psu.py-ban volt kezelve —
        # itt (és device_load.py-ban) hiányzott, inkonzisztencia a 3 SCPI
        # driver között (a téves irány itt "fail-safe": hamis pozitív hiba,
        # nem elnyelt valós hiba, de tesztetlen/dokumentálatlan eltérés volt).
        #
        # Teljes kódbázis review 3. kör (2026-08-29), Hardver/Műszervezérlés
        # P3 lelet: device_psu.py::check_error() a hibaüzenethez hozzáfűzi
        # a legutóbb kiküldött SCPI parancsot (`[last: {self._last_cmd}]`)
        # diagnosztikai kontextusként — itt korábban hiányzott ugyanez,
        # ami inkonzisztenssé tette a 3 driver hibaüzenetét, és megnehezíti
        # egy SCPI hiba forrásának visszakövetését DMM-en.
        # 2026-09-12: a `_query()` fix ota mindig frissiti a `self._last_cmd`-
        # et -- a diagnosztikai erteket ("mi okozta a hibat") a SAJAT
        # "SYST:ERR?" queryNK ELOTT kell elmenteni, l. device_psu.py azonos
        # javitasa.
        # 2026-09-12, fuggetlen verifier P3: instrument_manager.py a
        # hibasort tobbszor uriti egy tickben -- self._last_cmd
        # visszaallitasa nelkul a 2. hivastol "SYST:ERR?"-t mutatna.
        last_cmd_before_check = self._last_cmd
        resp = self._query("SYST:ERR?")
        self._last_cmd = last_cmd_before_check
        stripped = resp.strip()
        if stripped == "0" or resp.startswith("+0") or resp.startswith("0,"):
            return []
        return [f"{stripped} [last: {last_cmd_before_check}]"]

    def safe_off(self) -> bool:
        return True  # DMM-nek nincs output — safe_off no-op, mindig sikeres

    # ------------------------------------------------------------------ #
    # DCV konfiguráció (DMM1 — feszültségmérés)                           #
    # ------------------------------------------------------------------ #

    @_transaction(voltage_read=True)
    def _apply_config_atomic(self, *commands: str, owner: object | None = None) -> None:
        """2026-09-12, INSTR-DMM-CONFIG-ATOMIC-001 — egy TÖBBPARANCSOS
        műszerkonfigurációt EGYETLEN, szinkronizált SCPI program message-ként
        küld ki (`a;:b;:c;*OPC?`), külön `_write()`-ok sorozata HELYETT.

        Miért normatív, nem stílus (két, egymástól független ok):

        1) DEVICE CLEAR A PARANCSOK KÖZÖTT. A `_write()`/`_query()` minden
           kísérlet előtt `_clear_resource()`-t (VISA/IEEE-488 Device Clear)
           küld (l. `_clear_resource()` docstringje). A kézikönyv szerint
           (34460-70-Manual.txt 5391-5395. sor, nyomtatott 199. o., "Using
           Device Clear"): "Device Clear performs the following actions: ...
           l The instrument's input and output buffers are cleared." Vagyis
           egy, a műszer INPUT PUFFERÉBEN még nem feldolgozott parancsot a
           KÖVETKEZŐ hívás Device Clear-je ELDOBHAT — csendben, a hibasorba
           semmit nem írva. Ez pontosan a 2026-09-12-i HIL-megfigyelések
           mintázata: a `reset_to_single_sample()` KÉT írásából a MÁSODIK
           (`SAMP:SOUR IMM`) érvényesült (`SAMP:SOUR? -> IMM`), az ELSŐ
           (`SAMP:COUN 1`) NEM (`SAMP:COUN? -> +2`) -- a különbség éppen az,
           hogy az elsőt egy rákövetkező Device Clear érte. Ugyanez magyarázza
           a 2. futás `SAMP:TIM 0.41` írásának csendes elvesztését is: a
           `configure_timed_acquisition()` UTÁN azonnal `check_error()`
           (`SYST:ERR?`) fut, ami szintén Device Clear-rel indul.
           A több parancs EGY üzenetben azt jelenti, hogy a Device Clear
           mindig a TELJES szekvencia ELŐTT történik, sosem közötte.

        2) SZINKRONIZÁCIÓ A KÖVETKEZŐ HÍVÁS DEVICE CLEAR-JE ELLEN. A záró
           `*OPC?` válaszára VÁRUNK, tehát a konfiguráció bizonyítottan
           végrehajtódott, mielőtt bármely későbbi hívás Device Clear-t
           küldhetne. (IEEE 488.2 Common Commands, 34460-70-Manual.txt
           5415. sor; a SAMPle/TRIGger/CONFigure parancsok szekvenciálisak,
           nem overlapped -- egyedül az INIT overlapped, uo. 5516-5518. sor.)

        Megjegyzés a hibakezelésről: ha a szekvencia egy PARANCSHIBÁT
        (-100...) okozna, a műszer a program message hátralévő részét
        eldobja, a `*OPC?` nem válaszol, és a `_query()` hangosan
        `InstrumentTimeoutError`-t dob -- nem csendes félrekonfigurálás.
        VÉGREHAJTÁSI hiba (pl. -221 "Settings conflict", 14760. sor) esetén
        a végrehajtás folytatódik, a `*OPC?` válaszol, és a hiba a hibasorban
        marad a hívó `check_error()`-jának (l. pulse_measurement_service.py
        CF-04 kapuja) -- a meglévő védelem változatlan.

        FONTOS PONTOSÍTÁS (independent verifier, 2026-09-12): a fenti (1)
        Device Clear-mechanizmus IDÉZETE forrás-hű (5391-5395. sor), DE a
        kézikönyv NEM mondja ki expliciten, hogy ez egy MÁR KIKÜLDÖTT, MÉG
        FEL NEM DOLGOZOTT parancsot eldobna -- ez plauzibilis következtetés
        az "input buffer" törléséből, HIL-lel nem közvetlenül megfigyelt
        (SOURCE_REQUIRED, l. CLAIM_LEDGER.yaml INSTR-DMM-CONFIG-ATOMIC-001).
        EMELLETT ez a Device Clear-alapú magyarázat (B2) és az INSTR-DMM-
        ABORT-001 "processzhatáron átnyúló armed INIT" magyarázata (B1)
        LOGIKAILAG KIZÁRJÁK EGYMÁST a 3. futás konkrét esetére: ha a
        `connect()`-kori `*CLS` Device Clear-je valóban DCL, az a
        kézikönyv szerint (5392-5393. sor) MAGA IS lezárja a folyamatban lévő
        mérést -- tehát a 3. futás `configure_dcv()`-je elé nem érkezhetett
        volna armed állapotban (B1 nem magyarázná a tünetet); ha viszont a
        `_clear_resource()` NEM valódi DCL (l. `_clear_resource()` HIL_PENDING
        jelzése), akkor input-puffer-törlés sincs (B2 lehetetlen). A fix
        EZÉRT NEM egy egyetlen, bizonyított gyökérokra épül, hanem MINDKÉT
        lehetséges mechanizmust lefedi egyszerre (atomikus üzenet = nincs
        Device Clear a parancsok között; ABORt = nincs armed állapot) --
        emiatt robusztus, függetlenül attól, melyik magyarázat a helyes.
        Élő HIL-verifikáció (HIL_PENDING) szükséges a tényleges hatékonyság
        megerősítéséhez.
        """
        self._query(";:".join(commands) + ";*OPC?")

    @_transaction(require_unowned=True)
    def abort_measurement(self) -> None:
        """ABORt — a folyamatban lévő mérést megszakítja, a trigger-rendszert
        "idle" állapotba viszi.

        Forrás: 34460-70-Manual.txt 5452-5461. sor (nyomtatott 201. o.):
        "ABORt -- Aborts a measurement in progress, returning the instrument
        to the trigger idle state. ... Use this to abort a measurement when
        the instrument is waiting for a trigger, or for aborting a long
        measurement or series of measurements."; 5527. sor (az INITiate
        leírásában): "Use ABORt to return to idle."; 5398. sor: "The ABORt
        command is the recommended method to terminate a measurement."

        Miért kell: az `arm_timed_acquisition()` (INIT) OVERLAPPED parancs
        (uo. 5516-5518. sor) — ha a hozzá tartozó `fetch_timed_acquisition()`
        (FETC?) egy hibaágon SOHA nem fut le, a műszer "wait-for-trigger"/
        mérés-folyamatban állapotban MARAD, és ezt sem a `SAMP:COUN 1`, sem a
        `SAMP:SOUR IMM` írás NEM zárja le (egyik sem trigger-parancs)."""
        self._write("ABOR")

    @_transaction(require_unowned=True)
    def configure_dcv(self, range_V: float, nplc: float) -> None:
        """Teljes, EXPLICIT DCV mérési + trigger üzemmód-definíció egyetlen,
        szinkronizált SCPI üzenetben (l. `_apply_config_atomic()`).

        2026-09-12, INSTR-DMM-ABORT-001/INSTR-DMM-CONFIG-ATOMIC-001: korábban
        4 külön `_write()` volt, és a metódus KIZÁRÓLAG a CONFigure
        dokumentált mellékhatásaira TÁMASZKODOTT a trigger-/mintavételi
        állapot alapállapotba hozásában (34460-70-Manual.txt 6669-6688. sor,
        "Default Settings for the CONFigure Command": "Samples per Trigger 1
        sample / Trigger Count 1 trigger / Trigger Source Immediate"). A
        2026-09-12-i HIL-megfigyelés 3. futása MEGCÁFOLTA, hogy erre
        támaszkodni lehet: a `configure_dcv()` UTÁN kiadott, sima
        `read_voltage()` (READ?) KÉT értéket adott vissza
        ('+1.29388309E+01,+1.29398106E+01'), tehát a SAMPle:COUNt az előző
        futás 2-es értékén maradt — a `CONF:VOLT:DC` írás maga sem
        érvényesült (a következő `_write()` Device Clear-je eldobta, l.
        `_apply_config_atomic()` 1. pontja).

        A javítás ezért (a) `ABOR`-ral kezd, hogy egy KORÁBBI processzből
        maradt, függőben lévő INIT ne befolyásolja a konfigurálást
        (5452-5453./5527. sor), (b) EXPLICIT beállítja a trigger- és
        mintavételi paramétereket is a gyári alapértékükre a CONFigure
        mellékhatására hagyatkozás HELYETT (`TRIG:SOUR IMM`, `TRIG:COUN 1`,
        `SAMP:COUN 1`, `SAMP:SOUR IMM` -- a gyári alapértékek forrása
        34460-70-Manual.txt 15161-15168. és 15174-15176. sor, nyomtatott
        475. o., "Triggering Operations ... Trigger Count 1 / Trigger Source
        Immediate / Sample Count 1 / ... Sample Source Immediate"), (c) mindezt EGY
        üzenetben, `*OPC?`-vel szinkronizálva. Új konfigurációs szabadságot
        NEM vezet be: minden hozzáadott érték a kézikönyvben dokumentált
        gyári alapérték, amire a kód eddig is épített, csak kimondatlanul."""
        self._apply_config_atomic(
            "ABOR",
            f"CONF:VOLT:DC {range_V:.6g}",
            f"VOLT:DC:NPLC {nplc:.6g}",
            f"VOLT:DC:RANG {range_V:.6g}",
            "VOLT:DC:RANG:AUTO OFF",
            "TRIG:SOUR IMM",
            "TRIG:COUN 1",
            "SAMP:COUN 1",
            "SAMP:SOUR IMM",
        )
        self._last_valid_voltage = None  # reset jump detector

    @_transaction(require_unowned=True)
    def set_nplc(self, nplc: float) -> None:
        # Kör 19 Batch D2 (HW19-01, DFAM-039 új testvér-előfordulás): korábban
        # egyetlen, szinkronizálatlan `_write()` volt -- kitéve annak a
        # mechanizmusnak, amit a DFAM-039 család EZEN a műszeren (34465A)
        # már a TÜNET-OSZTÁLY és a JAVÍTÁS HATÁSA szintjén HIL-igazolt (l.
        # CLAIM_LEDGER.yaml INSTR-DMM-CONFIG-ATOMIC-001) -- a PONTOS
        # alacsony szintű DCL-mechanizmus önmagában továbbra is
        # SOURCE_REQUIRED (nem közvetlenül megfigyelt, l.
        # _apply_config_atomic() fenti docstringje). Egy Device Clear a
        # KÖVETKEZŐ hívás elején
        # (pl. _configure_dmm_for_step()-öt közvetlenül követő
        # _poll_device_errors()/READ? hívás) elveszejtheti a feldolgozatlan
        # NPLC-parancsot. A hívó (test_runner.py) a PARANCSOLT értéket
        # látenseli (`_last_configured_nplc`), visszaolvasás nélkül -- egy
        # csendben eldobott write a relax-fázisbeli NPLC-emelésnél a gyors
        # integrációs időt hagyhatná érvényben, miközben a cache 10-et jelez. A
        # `target_nplc == self._last_configured_nplc` early-return pedig
        # elnyomja a további próbálkozást. `;*OPC?`-re váltva -- a
        # `_apply_config_atomic()`-ban már bevált, egyetlen szinkronizált
        # üzenet mintáját követve -- a parancs nyugtázott a következő SCPI előtt.
        # R10-B3: ez nem NPLC-readback vagy műszeroldali HIL-igazolás.
        self._query(f"VOLT:DC:NPLC {nplc:.6g};*OPC?")

    @_transaction(require_unowned=True)
    def enable_autozero(self) -> None:
        """Q-6 PROJECT_POLICY: AZ ON, nyugtázva és visszaolvasva.

        VERIFIED_PRIMARY: 34460-90901 Ed.7, 385. o., {OFF|ON|ONCE}.
        HIL_VERIFIED: Q-6 rev4, 2026-11-08, ON forma.
        Az ONCE nem folyamatos autozero.
        """
        self._query("VOLT:DC:ZERO:AUTO ON;*OPC?")
        actual = self._parse_float(self._query("VOLT:DC:ZERO:AUTO?"), context="autozero")
        if actual != 1.0:
            raise InstrumentError(f"DMM_AUTOZERO_READBACK_REJECTED: {actual!r}")

    @_transaction(require_unowned=True)
    def get_nplc(self) -> float:
        """2026-09-12, FAST_TRANSIENT NPLC-javítás: a MŰSZERBŐL visszaolvasott
        aktuális DCV integrációs idő (PLC-ben).

        Forrás: 34460-70-Manual.txt 11420-11421. sor (nyomtatott 382. o.),
        `[SENSe:]VOLTage[:DC]:NPLC? [{MIN|MAX|DEF}]`, tipikus válasz
        `+1.00000000E+01`.

        Azért kell, mert a DCV NPLC-nek a session alatt TÖBB, egymástól
        független írója van (main_window.py::_bring_up() konfigurációs
        `configure_dcv(..., nplc=10)`, test_runner.py::_configure_dmm_for_
        step()/_reconfigure_dmm_nplc_for_relax_substate() lépés- és
        alállapot-függő `set_nplc()`-hívásai) — egy ideiglenes, mérés-
        specifikus NPLC-váltás ezért NEM állítható vissza egy beégetett
        konstansra, csak a váltás ELŐTT ténylegesen érvényes, visszaolvasott
        értékre (l. pulse_measurement_service.py::_apply_acquisition_nplc()).
        Ugyanaz a "commanded state helyett readback" elv, mint a
        .claude/rules/40-instruments.md előírásában."""
        return self._parse_float(self._query("VOLT:DC:NPLC?"), context="nplc")

    @_transaction(voltage_read=True)
    def get_sample_timer_s(self, *, owner: object | None = None) -> float:
        """2026-09-12, musszer-only HIL-javitas: a JELENLEG ténylegesen
        beállított SAMPle:TIMer (nem a MIN, l. `query_sample_timer_
        capability()`) — a `configure_timed_acquisition()` által KÉRT
        érték MŰSZERI READBACK-je.

        Miért kell: musszer-only HIL-méréssel (2026-09-12, valós Keysight
        34465A MY60016569, NPLC=10, 0,2 s SAMP:TIM kérés) igazolva, hogy a
        műszer a kért intervallumot CSENDBEN, A HIBASORBA SEMMIT NEM ÍRVA
        is figyelmen kívül hagyhatja (a `SAMP:TIM` az előző értékén marad)
        — ez SÚLYOSABB a kézikönyv dokumentált -221 "changed to MIN"
        viselkedésénél, és a `SYST:ERR?`-alapú ellenőrzés (l. `check_
        error()` hívói) nem fedi fel. Csak egy tényleges readback
        (`SAMPle:TIMer?`) mutatja meg a ténylegesen alkalmazott értéket.
        Ugyanaz a "commanded state helyett readback" elv, mint `get_nplc()`
        -nél és a .claude/rules/40-instruments.md előírásában. L.
        Folyamatok/canonical/CLAIM_LEDGER.yaml INSTR-DMM-SAMPTIM-001
        UPDATE 2026-09-12 a teljes SCPI-sweep-eredményért."""
        return self._parse_float(self._query("SAMP:TIM?"), context="sample_timer")

    @_transaction(require_unowned=True)
    def autorange_off(self) -> None:
        self._write("VOLT:DC:RANG:AUTO OFF")

    @_transaction(voltage_read=True)
    def read_voltage(self, *, owner: object | None = None) -> float:
        """
        [N8] Validált DCV olvasás.
        Kivétel: InstrumentInvalidReading — NaN, Inf, overload, nagy ugrás.
        """
        # A felszabadított, régi tokennel indított olvasás is ütközés.
        if self._measurement_owner is not owner:
            raise DMMMeasurementBusy("DMM: eltérő mérési tulajdonos")
        raw = self._query("READ?")
        v = self._parse_float(raw, context="voltage")
        self._validate_voltage(v)
        self._last_valid_voltage = v
        return v

    # ------------------------------------------------------------------ #
    # SUP01-PULSE-003/SRC-009: determinisztikus, időzített mintavétel —   #
    # a jövőbeli PulseMeasurementService (SUP01-ARCH-001) építőeleme.     #
    # Additív, NEM hívja semmilyen meglévő workflow. Előfeltétel:         #
    # configure_dcv() már lefutott (fix range, autorange off).           #
    # SCPI igazolva: 34460-70-Manual.txt 8959-9009. sor                   #
    # ("SAMP:SOUR TIM" / "SAMP:TIM" / "INIT" munkafolyamat).              #
    # ------------------------------------------------------------------ #

    # R16-B21a: a teljes timed chunk ugyanazzal a B5a-tokennel foglalható.
    @_transaction(voltage_read=True)
    def configure_timed_acquisition(
        self, sample_count: int, interval_s: float, *, owner: object | None = None,
    ) -> None:
        """SAMPle:COUNt + SAMPle:SOURce TIMer + SAMPle:TIMer beállítása.

        2026-09-12, INSTR-DMM-ABORT-001/INSTR-DMM-CONFIG-ATOMIC-001: a három
        írás EGYETLEN, `*OPC?`-vel szinkronizált üzenetté vonva, `ABOR`
        előtaggal (l. `_apply_config_atomic()` és `abort_measurement()`
        docstringjeit). Enélkül (a) a hívó `check_error()`-jának Device
        Clear-je eldobhatta az utolsó, még fel nem dolgozott `SAMP:TIM`
        írást — ez a 2026-09-12-i HIL 2. futásának mintázata (kért 0,41 s,
        visszaolvasott 1,0 s = a gyári alapérték, 34460-70-Manual.txt
        9090-9091. sor, ÜRES hibasor mellett) —, és (b) egy előző chunk
        félbeszakadt, INIT-tel armed állapota érintetlenül maradt volna.
        A parancssorrend (COUNt -> SOURce TIMer -> TIMer) változatlan, a
        kézikönyv saját példáját követi (uo. 8973-8975. és 9040-9042. sor)."""
        self._apply_config_atomic(
            "ABOR",
            f"SAMP:COUN {sample_count}",
            "SAMP:SOUR TIM",
            f"SAMP:TIM {interval_s:.6g}",
            owner=owner,
        )

    @_transaction(voltage_read=True)
    def arm_timed_acquisition(self, *, owner: object | None = None) -> None:
        """INITiate — a műszert 'wait-for-trigger' állapotba állítja."""
        self._write("INIT")

    @_transaction(voltage_read=True)
    def fetch_timed_acquisition(self, *, owner: object | None = None) -> str:
        """FETCh? — a kész, időzített minták nyers lekérdezése.

        Governance-v2 review-kör 1 (CF-03/HW-R1-01/INSTR-DMM-CLEAR-001): az
        ELSŐ kísérlet NEM küld előtte VISA/IEEE-488 Device Clear-t (l.
        `_query(clear_first=False)`). Egy `arm_timed_acquisition()` (INIT)
        által armed, még folyamatban lévő mérést a Device Clear ABORTÁL
        (34460-70-Manual.txt "Using Device Clear", nyomtatott 199. o.) — a
        korábbi feltétel nélküli clear ezért minden FETC?-öt már az első
        kísérletnél elvitt. Ismert, nem teljesen lezárt reziduális kockázat
        (DFAM-007): ha az első kísérlet mégis hibázik és egy retry (ami
        clear-t küld) sikeres FETC?-öt ad, az elméletileg egy KORÁBBI
        (stale) mérés reading-memory-ban maradt adatát is visszaadhatja —
        ehhez explicit stale-minősítés a hívó oldalon (pulse_measurement_
        service.py) még nincs bekötve, l. MASTER_CONSOLIDATED.md CF-03,
        6. szakasz #3 kockázat."""
        return self._query("FETC?", clear_first=False)

    @_transaction(require_unowned=True)
    def query_sample_timer_capability(self) -> str:
        """SUP01-PULSE-003: SAMPle:TIMer? MIN — képességellenőrzés a beállítás után.

        2026-09-12, FAST_TRANSIENT NPLC-javítás — a hívási SORREND normatív,
        nem stílus kérdése (34460-70-Manual.txt 9053-9055. sor, nyomtatott
        311. o.): "Since the minimum value changes depending on configuration,
        a command order dependency exists. You must completely configure the
        measurement before setting the sample timer to MIN, or you may
        generate an error." A visszaadott MIN a JELENLEGI konfigurációra
        érvényes ("Execute SAMPle:TIMer? MIN to determine the recommended
        interval for the current measurement configuration", uo. 9043-9047.
        sor), tehát a kért NPLC/apertúra BEÁLLÍTÁSA UTÁN kell lekérdezni.
        Fix (nem auto) range mellett — amit a `configure_dcv()` beállít
        (`VOLT:DC:RANG:AUTO OFF`) — a MIN a tényleges alsó korlát; autorange
        bekapcsolva a kézikönyv szerint csak "recommended value" (uo.
        9056-9060. sor)."""
        return self._query("SAMP:TIM? MIN")

    @_transaction(voltage_read=True)
    def reset_to_single_sample(self, *, owner: object | None = None) -> None:
        """A configure_timed_acquisition() által beállított SAMPle:COUNt/
        SAMPle:SOURce TARTÓS (sticky) állapotát visszaállítja az alapértelmezett
        egyetlen-mintás, azonnali módra — enélkül minden ezt követő sima
        read_voltage() (READ?) a chunk mérete szerinti CSV-t adna vissza egyetlen
        float helyett, ami InstrumentInvalidReading-et okozna (WP-AK-04 review-lelet).
        SCPI igazolva: 34460-70-Manual.txt 8864-8969. sor — SAMPle:COUNt alapérték 1,
        34465A/70A sor (8873. sor; a 8872. sor a 34460A/61A modellre vonatkozik),
        SAMPle:SOURce alapérték IMMediate (8968. sor).

        2026-09-12, INSTR-DMM-ABORT-001/INSTR-DMM-CONFIG-ATOMIC-001 — KÉT,
        élő hardveren igazolt hiányosság javítva:

        (1) HIÁNYZÓ `ABORt`. Ezt a metódust MINDEN időzített mintavételi
            hívóhely a `finally` ágából hívja (pulse_measurement_service.py::
            _step_instrument_timed / _step_hardware_pulse /
            _capture_baseline_window / _capture_recovery_if_configured), tehát
            AKKOR is, ha az `arm_timed_acquisition()` (INIT) MÁR lefutott, de a
            `fetch_timed_acquisition()` (FETC?) egy hibaágon már NEM. A
            `SAMP:COUN`/`SAMP:SOUR` írás azonban NEM trigger-parancs: a
            kézikönyv szerint a függőben lévő mérést kizárólag az `ABORt`
            zárja le ("Use ABORt to return to idle", 34460-70-Manual.txt
            5527. sor; "Aborts a measurement in progress, returning the
            instrument to the trigger idle state", uo. 5452-5453. sor). A
            `_step_hardware_pulse()` ága ezt különösen élesen mutatja: ott az
            INIT és a FETC? KÖZÖTT `load.input_on()` és
            `load.fire_transient_pulse()` fut — bármelyikük kivétele armed
            állapotban hagyta a műszert.

        (2) DEVICE CLEAR A KÉT ÍRÁS KÖZÖTT. A 2026-09-12-i HIL-diagnosztika
            szerint a metódus UTÁN `SAMP:SOUR? -> IMM` (a MÁSODIK írás
            érvényesült), de `SAMP:COUN? -> +2` (az ELSŐ nem) — pontosan az
            a mintázat, amit a második `_write()` Device Clear-je okoz
            (l. `_apply_config_atomic()` 1. pontja). Most egyetlen,
            `*OPC?`-vel szinkronizált üzenet."""
        self._apply_config_atomic("ABOR", "SAMP:COUN 1", "SAMP:SOUR IMM", owner=owner)

    # ------------------------------------------------------------------ #
    # TEMP konfiguráció (DMM2 — 4-wire PT100, FRTD)                       #
    # SCPI igazolva: 34460-70-Manual.txt sor 6988                         #
    # ------------------------------------------------------------------ #

    @_transaction(require_unowned=True)
    def configure_temp_4wire_pt100(self, nplc: float = 10.0) -> None:
        """2026-09-12, INSTR-DMM-CONFIG-ATOMIC-001 sibling-javítás (rules/
        50-review.md: "Ismétlődő family esetén ne javíts csak egy példányt"):
        ugyanaz a több-`_write()`-os konfigurációs minta, tehát ugyanaz a
        Device-Clear-eldobási kockázat, mint a `configure_dcv()`-ben. Itt a
        csendes elvesztés METROLÓGIAILAG közvetlenül káros: ha a
        `SENS:TEMP:TRAN:FRTD:RES 100` írás vész el, a műszer a gyári
        alapértelmezett R0-val számol, és a °C-érték csendben hibás lesz.
        Parancsok és sorrend VÁLTOZATLAN, csak egyetlen, `*OPC?`-vel
        szinkronizált üzenetté vonva (l. `_apply_config_atomic()`)."""
        self._apply_config_atomic(
            "ABOR",
            "CONF:TEMP FRTD,85",          # 85 = IEC 751 alfa=0.00385 kód
            "SENS:TEMP:TRAN:FRTD:RES 100",  # PT100: R0=100 Ω
            f"SENS:TEMP:NPLC {nplc:.6g}",
            "UNIT:TEMP C",
        )
        self._temp_mode = "RTD_4W_PT100"

    # ------------------------------------------------------------------ #
    # TEMP konfiguráció (DMM2 pótlás — 2-wire NTC termisztor)             #
    # A műszer beépített termisztor-konverziója (CONF:TEMP THER,5000)     #
    # KIZÁRÓLAG az 5 kΩ@25°C "44007"-típusú NTC-hez van kalibrálva —      #
    # SCPI igazolva: 34460-70-Manual.txt 6982-6983/8216/11061. sor        #
    # ("5000 (only available choice)"), a beégetett Steinhart-Hart        #
    # együtthatók (A/B/C) is csak erre a görbére érvényesek (2467. sor).  #
    # A 10 kΩ@25°C/B=3950K pótszonda ELTÉR ettől, ezért a műszert sima    #
    # 2-eres ellenállásmérésre állítjuk (CONF:RES, igazolva: 6934-6968/   #
    # 10448-10462. sor), a °C-ra váltást a Beta-egyenlettel a driver      #
    # végzi a szonda SAJÁT adatlap-paramétereivel.                       #
    #                                                                     #
    # Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-01 lelet: a   #
    # korábbi kód a FIX tartományt a NÉVLEGES R25-ből számolta            #
    # (`RES:RANG {r25_ohm}` = 10 kΩ) — egy NTC ellenállása viszont        #
    # HŐMÉRSÉKLET-CSÖKKENÉSSEL NŐ (negatív hőfoktényező), így már 20°C    #
    # alatt is meghaladja a 10 kΩ fix tartományt (pl. B=3950K mellett     #
    # 0°C-on ~33,6 kΩ), és a műszer OVERLOAD-ot ("9.9E37") jelez a        #
    # gyári kézikönyv szerint (34460-70-Manual.txt 10464-10466. sor:      #
    # "Selecting a fixed range disables autoranging"/"If the input       #
    # signal is greater than can be measured on the specified manual     #
    # range, the instrument displays the word Overload"). MAX verifier    #
    # follow-up (P3-1): a GOLDEN_GG 0°C-os log ma NEM fut le (a           #
    # kondicionálás/0°C ciklus tervezetten kihagyva, l. golden_gg_export. #
    # py + DECISION_REGISTER.md GOV-018) — a ténylegesen ELÉRHETŐ hiba a  #
    # FIAMM nominál 15-30°C labor-hőmérsékleti ablak (safety.py) ALSÓ     #
    # felén jelentkezik: kb. 21°C alatt már túllépi a 10 kΩ fix tartomány #
    # 120%-os overload-küszöbét (B=3950K/R25=10kΩ mellett 20°C-on ~12,5   #
    # kΩ), tehát egy hétköznapi 15-20°C-os laborban is faultolt volna,    #
    # nem csak a (ma nem futó) 0°C ponton. A javítás a gyári kézikönyv    #
    # SAJÁT ajánlott mintáját követi (uo. 10485-10488.                    #
    # sor: `CONF:RES` + `RES:RANG:AUTO ONCE`) — itt folytonos             #
    # autoranging-ot (`RES:RANG:AUTO ON`, nem `ONCE`) használva, mert a   #
    # hőmérséklet egy session alatt több decade-határon átlépve is       #
    # változhat, nem csak egyszeri beállításkor.                         #
    # ------------------------------------------------------------------ #

    @_transaction(require_unowned=True)
    def configure_temp_2wire_ntc_thermistor(
        self, r25_ohm: float = 10000.0, beta_K: float = 3950.0, nplc: float = 10.0
    ) -> None:
        # 2026-09-12, INSTR-DMM-CONFIG-ATOMIC-001 sibling-javítás — l.
        # configure_temp_4wire_pt100() azonos indoklását. Itt a `RES:RANG:
        # AUTO ON` csendes elvesztése a fenti HW2-01 lelet overload-hibáját
        # hozná vissza. Parancsok és sorrend változatlan.
        self._apply_config_atomic(
            "ABOR", "CONF:RES", "RES:RANG:AUTO ON", f"RES:NPLC {nplc:.6g}"
        )
        self._thermistor_r25_ohm = r25_ohm
        self._thermistor_beta_K = beta_K
        self._temp_mode = "NTC_2W_BETA"

    @_transaction(require_unowned=True)
    def read_temperature(self) -> float:
        raw = self._query("READ?")
        val = self._parse_float(raw, context="temperature")
        if math.isnan(val) or math.isinf(val) or abs(val) > OVERLOAD_THRESHOLD:
            raise InstrumentInvalidReading(f"DMM temp: overload {val:.3e}")
        if self._temp_mode == "NTC_2W_BETA":
            t = self._ntc_beta_resistance_to_celsius(val)
        else:
            t = val
        if not (-80.0 <= t <= 150.0):
            raise InstrumentInvalidReading(f"DMM temp: tartományon kívül {t:.1f}°C")
        return t

    def _ntc_beta_resistance_to_celsius(self, r_ohm: float) -> float:
        """Beta-egyenlet: 1/T = 1/T25 + (1/B)*ln(R/R25). Mérnöki feltételezés
        (nem gyári Steinhart-Hart 3 együtthatós modell) — a szonda adatlapja
        csak R25/B-t ad meg, ez a szabványos, e paraméterekkel elérhető
        legpontosabb átszámítás."""
        if r_ohm <= 0.0:
            raise InstrumentInvalidReading(
                f"DMM temp (NTC): érvénytelen ellenállás {r_ohm:.3f} Ω"
            )
        t25_K = 298.15
        inv_t_K = 1.0 / t25_K + (1.0 / self._thermistor_beta_K) * math.log(
            r_ohm / self._thermistor_r25_ohm
        )
        return (1.0 / inv_t_K) - 273.15

    # ------------------------------------------------------------------ #
    # Belső segédek                                                        #
    # ------------------------------------------------------------------ #

    def _validate_voltage(self, v: float) -> None:
        """[N8] NaN / Inf / overload / jump ellenőrzés."""
        if math.isnan(v) or math.isinf(v):
            raise InstrumentInvalidReading(f"DMM: NaN vagy Inf feszültség: {v}")
        if abs(v) > OVERLOAD_THRESHOLD:
            raise InstrumentInvalidReading(f"DMM: overload érték: {v:.3e}")
        if self._last_valid_voltage is not None:
            jump = abs(v - self._last_valid_voltage)
            if jump > self.max_voltage_jump_V:
                raise InstrumentInvalidReading(
                    f"DMM: feszültségugrás {jump:.3f}V > limit {self.max_voltage_jump_V}V "
                    f"(volt: {self._last_valid_voltage:.3f}V, most: {v:.3f}V)"
                )

    @staticmethod
    def _parse_float(raw: str, context: str = "") -> float:
        raw = raw.strip()
        if raw.upper() in ("NAN", "+NAN", "-NAN", "NAN\n"):
            raise InstrumentInvalidReading(f"DMM {context}: NaN válasz: {raw!r}")
        if raw.upper() in ("INF", "+INF", "-INF", "INFINITY"):
            raise InstrumentInvalidReading(f"DMM {context}: Inf válasz: {raw!r}")
        try:
            return float(raw)
        except ValueError as e:
            raise InstrumentInvalidReading(
                f"DMM {context}: nem értelmezhető válasz: {raw!r}"
            ) from e

    @_transaction()
    def _write(self, cmd: str) -> None:
        self._last_cmd = cmd
        # Teljes kódbázis review 3. kör (2026-08-29), Hardver/Műszervezérlés
        # P2 lelet: explicit ellenőrzés, ha connect() sosem történt meg vagy
        # disconnect() után hívják — enélkül self._resource.write(cmd) egy
        # AttributeError-t dobott volna, amit a lenti except Exception
        # elkapott, DE emiatt 3× feleslegesen újrapróbálkozott, majd egy
        # félrevezető "write failed after 3 retries" üzenetet adott
        # kommunikációs időtúllépést sugallva, holott a valós ok "nincs
        # kapcsolat". L. pico_bq_transport.py::_call() azonos mintája.
        if self._resource is None:
            raise InstrumentTimeoutError(
                f"DMM: nincs aktív kapcsolat (connect() nem hívva): {cmd!r}"
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
            f"DMM write failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err

    @_transaction()
    def _query(self, cmd: str, clear_first: bool = True) -> str:
        """
        clear_first=False (governance-v2 review-kör 1, CF-03): az első
        kísérlet előtt NEM küld Device Clear-t — kizárólag
        `fetch_timed_acquisition()` (FETC? egy armed mérésre) használja,
        mert a clear a valós műszeren abortálná a still-pending overlapped
        mérést. Minden RETRY (2. kísérlettől) továbbra is clear-rel indul —
        ha az első kísérlet más okból hibázna, a resync-viselkedés
        megmarad. Minden más hívó a default (clear_first=True) változatlan
        viselkedését kapja.

        2026-09-12, independent verifier F2 follow-up: a `_write()` mindig
        frissítette a `self._last_cmd`-et, a `_query()` korábban NEM — az
        `_apply_config_atomic()` bevezetése óta TÖBB konfigurációs metódus
        `_query()`-n megy, tehát `check_error()` `[last: ...]` diagnosztikai
        annotációja elavult parancsot mutatott volna ezeken az utakon.
        """
        self._last_cmd = cmd
        if self._resource is None:
            raise InstrumentTimeoutError(
                f"DMM: nincs aktív kapcsolat (connect() nem hívva): {cmd!r}"
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
            f"DMM query failed after {self.max_retries} retries: {cmd!r}"
        ) from last_err
