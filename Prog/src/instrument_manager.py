"""
InstrumentManager — négy műszer egységes kezelése.
connect_all(), safe_all_off(), is_all_connected().
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time

from Prog.drivers.device_dmm import DMMMeasurementBusy
from Prog.drivers.device_load import (
    INPUT_ACTIVATION_ACK_TIMEOUT_S,
    QUESTIONABLE_STATUS_PROTECTION_BITS,
)
from Prog.src.exceptions import InstrumentTimeoutError

# R1 F-01/F-05 (B1B2_fixplan_FINAL.md B1/1): típusos jelölés a
# poll_device_errors()-bejegyzéseken -- a PSU/Load energia-állapota nem
# parancsolt (commanded=OFF, actual=ON) vagy ebben a pollban nem igazolt
# (állapotolvasás hibázott, vagy InstrumentTimeoutError miatt kimaradt).
_ENERGY_STATE_UNCERTAIN = "energy_state_uncertain"
_ENERGY_DEVICES = ("PSU", "Load")


def is_energy_state_uncertain(entry: dict) -> bool:
    """R1 F-01/F-05: True, ha a bejegyzést a producer (poll_device_errors())
    típusosan energia-állapot-bizonytalannak jelölte. Nem szövegparse; a
    hívó (test_runner.py) StepKind-től függetlenül kritikusnak veszi."""
    return entry.get(_ENERGY_STATE_UNCERTAIN) is True


def is_error_queue_poll_deferred(entry: dict) -> bool:
    """R16-B5a: a halasztás nem sikeres hibasorolvasás és nem műszerhiba."""
    return entry.get("status") == "HALASZTVA"


def is_input_activation_pending(entry: dict) -> bool:
    """R16-B10b: a függő ON-nyugta nem mismatch és nem műszerhiba."""
    return entry.get("status") == "ON_ACK_PENDING"


@dataclass
class InstrumentConfig:
    psu_resource: str = "PLACEHOLDER"
    load_resource: str = "PLACEHOLDER"
    dmm_voltage_resource: str = "PLACEHOLDER"
    dmm_temperature_resource: str = "PLACEHOLDER"


class InstrumentManager:
    def __init__(self, psu, load, dmm_voltage, dmm_temperature):
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature

    @property
    def psu(self):
        return self._psu

    @property
    def load(self):
        return self._load

    @property
    def dmm_voltage(self):
        return self._dmm_v

    @property
    def dmm_temperature(self):
        return self._dmm_t

    def connect_all(self, config: InstrumentConfig) -> None:
        instruments = [
            (self._psu,   config.psu_resource),
            (self._load,  config.load_resource),
            (self._dmm_v, config.dmm_voltage_resource),
            (self._dmm_t, config.dmm_temperature_resource),
        ]
        # Teljes kódbázis review 5. kör (2026-08-29), Hardver/Műszervezérlés
        # P2 lelet: `connected` korábban csak a SIKERES inst.connect(resource)
        # visszatérése UTÁN bővült — a valós driverek (device_psu.py/
        # device_load.py/device_dmm.py) viszont a _resource-t és
        # _connected=True-t a connect() ELEJÉN állítják be, a
        # biztonságkritikus all_outputs_off()/input_off()/*CLS-t pedig a
        # VÉGÉN hívják. Ha EZ hibázik, a kivétel a connect()-ből száll ki,
        # de mivel connected.append(inst) korábban csak a sikeres
        # visszatéréskor futott, a rollback except-ág SOHA nem hívta meg
        # erre a disconnect()-et — nyitva maradt VISA _resource,
        # _connected=True stuck. Az `attempted` lista most a hívás
        # MEGKÍSÉRLÉSEKOR bővül, hogy a rollback minden ténylegesen
        # megérintett (potenciálisan resource-ot nyitó) műszeren
        # megkísérelje a disconnect()-et.
        attempted: list = []
        # R6-B1 (R5-B1-N3, DFAM-016 x DFAM-050): a PSU vagy a Load connect()-
        # hibája után a MÁSIK energiaműszer connect()-je is megkísérlődik --
        # a saját connect()-beli OFF-ja (PSU all_outputs_off() / Load INP OFF)
        # csak így fut le (CLAUDE.md §6). Hiba esetén a DMM-ek connect()-je
        # kimarad. A kapuk a driverben maradnak; az ELSŐ kivétel propagál.
        # Időzítés: halott második energiaműszernél ez a worker-szálon
        # timeoutokkal nyújtja a hibautat. A GUI eseményhurka kezelheti a
        # vészleállítás-kérést, de kapcsolódás közben az csak
        # `_connect_abort_requested`-et állít; a blokkoló I/O-t nem szakítja
        # meg, a lekapcsolás befejezése továbbra is kivárhatja a timeoutokat.
        first_exc: Exception | None = None
        try:
            for inst, resource in instruments[:2]:
                attempted.append(inst)
                try:
                    inst.connect(resource)
                # Szándékosan széles (mint a lenti rollback-ág): bármely
                # connect()-hiba után a másik energiaműszer OFF-ja is fusson.
                except Exception as exc:  # noqa: BLE001 -- l. fenti indoklás
                    if first_exc is None:
                        first_exc = exc
            if first_exc is not None:
                raise first_exc
            for inst, resource in instruments[2:]:
                attempted.append(inst)
                inst.connect(resource)
        except Exception:
            # MAX verifier follow-up (2026-09-08, HW2-04 F1, DFAM-016
            # RECURRING sibling): main_window.py 3 bring-up hibaútja
            # (_construct_and_connect_instruments/abort-ág/_build_runner()
            # hiba-ága) mind safe_all_off()-ot hív disconnect_all() előtt --
            # ez a rollback-ág itt VOLT az egyetlen, ahol ez hiányzott, ÉS
            # az itt legkockázatosabb pont: a `connect()` minden egyes
            # driver saját, belső all_outputs_off()/input_off() hívásával
            # zárul (l. fenti "5. kör" komment) -- az az instrument, amelyiken
            # a `connect()` kivételt dobott, éppen EBBEN a saját belső
            # safety-off lépésben hibázhatott, vagy azt még el sem érte. A
            # korábban SIKERESEN csatlakozott instrumenteken a safe_off()
            # csak ártalmatlan újra-megerősítés (safe_off() sosem dob
            # kivételt, l. saját docstringje), az utolsóként megkísérelten
            # viszont ez az EGYETLEN hely, ahol egyáltalán megpróbáljuk.
            for inst in reversed(attempted):
                try:
                    inst.safe_off()
                except Exception:
                    pass
                try:
                    inst.disconnect()
                except Exception:
                    pass
            raise

    def disconnect_all(self) -> None:
        """Fordított connect sorrend: DMM_T → DMM_V → Load → PSU.

        Foglalt DMM erőforrása nyitva marad. A többi eszköz lezárási
        kísérlete után az utolsó DMMMeasurementBusy kivételt továbbdobja;
        az egyéb lezárási hibákat elnyeli.
        """
        busy: DMMMeasurementBusy | None = None
        for inst in (self._dmm_t, self._dmm_v, self._load, self._psu):
            try:
                inst.disconnect()
            except DMMMeasurementBusy as exc:
                busy = exc
            except Exception:
                pass
        if busy is not None:
            raise busy

    def safe_all_off(self) -> dict[str, bool]:
        """Sorrend: LOAD OFF → PSU OFF → DMM-ek (no-op). [R1] Relay nincs.

        Minden eszközön MEGKÍSÉRLI a safe_off()-ot egymástól függetlenül (egyik
        hibája nem akadályozza a másik megkísérlését, CLAUDE.md §10.3), és
        per-eszköz bool eredményt ad vissza — a hívó (TestRunner) ez alapján
        dönt WarningCode.PSU_OUTPUT_STATE_UNCERTAIN/LOAD_INPUT_STATE_UNCERTAIN
        kiadásáról, hogy egy bizonytalan OFF-állapot sose maradjon csendben
        (CLAUDE.md §10.4)."""
        result: dict[str, bool] = {}
        for name, instrument in (
            ("Load", self._load),
            ("PSU", self._psu),
            ("DMM_V", self._dmm_v),
            ("DMM_T", self._dmm_t),
        ):
            try:
                result[name] = bool(instrument.safe_off())
            except Exception:
                result[name] = False
        return result

    def identify_all(self, expected_models: "dict[str, str] | None" = None) -> dict[str, str]:
        """WP-AK-02 (checkpoint "instrument identities" mező) / INS-001: minden
        eszköz *IDN?-jét lekérdezi és névvel visszaadja. A meglévő `idn()`
        metódus eddig csak a kézi `connection_test.py`/`command_test.py`
        eszközökben volt használva — ez az első hívóhely az éles session-
        útvonalon. Csak `connect_all()` UTÁN hívható.

        Governance-v2 review-kör 1 (CF-38/HW-R1-06, DFAM-003): korábban a
        lekérdezett identitásokat semmi nem vetette össze az elvárt
        modellel — egy felcserélt VISA resource-string esetén a PSU-driver
        a load-ra küldött volna parancsot és fordítva, csak a session
        futása közben, SCPI error-queue-ból (vagy egyáltalán nem) derülve
        ki. Az opcionális `expected_models` — {szerepnév: elvárt *IDN?
        almintázat} — megadása esetén minden identitást ellenőriz, és az
        ELSŐ eltérésnél `ConfigurationError`-t dob. A minta a hívott
        driver OSZTÁLYÁHOZ kötött modellcsalád (nem egyedi sorozatszám),
        a hívó adja át — itt nincs hardkódolva. `None` (default):
        visszamenőlegesen kompatibilis, nincs validáció."""
        identities = {
            "PSU": self._psu.idn(),
            "Load": self._load.idn(),
            "DMM_V": self._dmm_v.idn(),
            "DMM_T": self._dmm_t.idn(),
        }
        # Review-kör 5, Hardver/Műszervezérlés HW5-05 (P3): az *IDN? válasz
        # tartalmazza a műszer egyedi sorozatszámát, tehát két KÜLÖNBÖZŐ
        # fizikai eszköz sosem adhat vissza AZONOS *IDN?-t. Ha mégis
        # (jellemzően a DMM_V/DMM_T szerepnél reális hiba: ugyanaz a
        # fizikai DMM kötve mindkét VISA resource-string mögé), a két
        # csatorna CSENDBEN ugyanazt a műszert olvasná -- az egyik mérés
        # (feszültség VAGY hőmérséklet) a másikat felülíró/versengő
        # panel-átváltásokkal torzulna, csak a session futása közben,
        # magyarázat nélkül derülne ki. Ez a lenti `expected_models`
        # ellenőrzéstől FÜGGETLEN, mindig fut (nem igényel elvárt modell-
        # mintát), mert önmagában, bármilyen konfigurációban érvényes.
        idn_to_roles: dict[str, list[str]] = {}
        for role, idn in identities.items():
            if idn:
                idn_to_roles.setdefault(idn, []).append(role)
        for idn, roles in idn_to_roles.items():
            if len(roles) > 1:
                from Prog.src.exceptions import ConfigurationError

                raise ConfigurationError(
                    f"Műszer-azonosítás sikertelen: ugyanaz a fizikai "
                    f"eszköz ({idn!r}) van bekötve több szerepre is "
                    f"({roles!r}) — felcserélt/duplikált VISA "
                    f"resource-string gyanús."
                )
        if expected_models:
            from Prog.src.exceptions import ConfigurationError

            for name, expected_substr in expected_models.items():
                actual = identities.get(name, "")
                if expected_substr not in actual:
                    raise ConfigurationError(
                        f"Műszer-azonosítás sikertelen: a(z) {name!r} szerepre "
                        f"csatlakozott eszköz *IDN? válasza ({actual!r}) nem "
                        f"tartalmazza a várt {expected_substr!r} mintát — "
                        f"felcserélt VISA resource-string gyanús."
                    )
        return identities

    def is_all_connected(self) -> bool:
        return all(
            i.is_connected()
            for i in (self._psu, self._load, self._dmm_v, self._dmm_t)
        )

    def poll_device_errors(
        self, *, skip_load: bool = False,
        continue_poll: Callable[[list[dict]], bool] | None = None,
    ) -> list[dict]:
        """Minden eszköz SYST:ERR? queue-ját leolvassa (destruktív FIFO).
        Addig hívja check_error()-t, amíg az üres listát nem ad vissza.
        Visszatér: [{"device": name, "error": error_str}, ...]
        R16-B5a: foglalt DMM esetén status="HALASZTVA" bejegyzést ad;
        ez nem sikeres hibasorolvasás. A fogyasztó külön eseményként kezeli.

        Kör 21 F6 (METRO-R21-1 + open_findings[70], DFAM-027): a
        `skip_load=True` KIHAGYJA a Load SYST:ERR?/kérdéses-státusz/
        bemenet-állapot lekérdezéseit -- 1:1 a `read_all()` már bevált
        `skip_load_voltage`/`skip_load_current` mintáját követve. A hívó
        (`test_runner.py::_poll_device_errors()`) ezt a controller SAJÁT,
        adott tickben már megállapított `load_current_readback_valid is
        False` jelzéséből számolja -- ugyanaz a "MÁR bizonyítottan halott
        Load" jelzés, mint amit `_build_sample()` a mérési oldalon már
        kihasznál (l. `skip_load_voltage=ctrl_i_load_valid is False`).
        NEM egy ÚJ, önálló "dead device" állapot-tárolás -- a MEGLÉVŐ,
        per-tick controller-jelzést használja fel, hogy egy MÁR halottnak
        bizonyult Loadnak ne küldjünk TOVÁBBI SCPI-kérdéseket egy safe-off
        UTÁNI, kényszerített hiba-lefolyáson.

        R1 F-05 (DFAM-027): ha egy PSU/Load lekérdezés (SYST:ERR?, INP?)
        InstrumentTimeoutError-ral bukik, az adott eszköz további
        lekérdezései UGYANEBBEN a pollban kimaradnak (a driver retry-
        budgetje már kimerült). Eszközönkénti; nem-timeout kivételnél és
        DMM-nél nincs rövidzár. A poll késleltetését csökkenti, a teljes
        OFF-időre nem garancia. R1 F-01: a kimaradt vagy hibás PSU/Load
        állapotolvasást és a commanded=OFF/actual=ON eltérést a bejegyzés
        `is_energy_state_uncertain()`-nel olvasható jelölése hordozza."""
        result: list[dict] = []
        timed_out: set[str] = set()
        for name, device in [
            ("PSU",   self._psu),
            ("DMM_V", self._dmm_v),
            ("DMM_T", self._dmm_t),
        ] if skip_load else [
            ("PSU",   self._psu),
            ("Load",  self._load),
            ("DMM_V", self._dmm_v),
            ("DMM_T", self._dmm_t),
        ]:
            for _ in range(32):  # max 32 hiba/eszköz/tick
                # A kapu a driver try/except-jén KÍVÜL van: az OFF hibája nem pollhiba.
                if continue_poll is not None and not continue_poll(result):
                    return result
                try:
                    errors = device.check_error()
                except DMMMeasurementBusy:
                    result.append({
                        "device": name,
                        "error": "ERROR_QUEUE_POLL_DEFERRED",
                        "status": "HALASZTVA",
                    })
                    break
                except Exception as exc:
                    # Review 2026-09-15 (C-11/DFAM-018+DFAM-012, teljes kódbázis
                    # review): korábban ez a kivétel csendben elnyelődött -- egy
                    # TARTÓS kommunikációs hiba (pl. VISA timeout/USB-kiesés az
                    # eszköz felé) emiatt a device-oldali SCPI hibasor sosem
                    # jutott el az INS-003 kritikus-SCPI-hiba→FAULT kapuig
                    # (test_runner.py::_run_step()), ami "nincs hiba"-ként
                    # olvasta a "nem tudom lekérdezni"-t (DFAM-018 klasszikus
                    # mintája). Mostantól a poll-hiba MAGA explicit bejegyzésként
                    # kerül a listába -- ugyanazon a csatornán fut tovább (logger/
                    # on_device_error/DeviceErrorPanel/_device_error_total_count),
                    # mint a valós SCPI hibák, és az is_critical_scpi_error()
                    # alapértelmezetten kritikusnak minősíti (nincs whitelistelve
                    # benignusként), tehát aktív energiaátadás alatt MÁR AZ
                    # ELSŐ előforduláskor helyesen FAULT-ot vált ki -- nincs
                    # "tartós ismétlődés"-höz kötött türelmi ablak (MAX
                    # verifier F1 pontosítása, l. CLAIM_LEDGER.yaml
                    # INSTRUMENT-ERROR-AGGREGATION-001 a teljes indoklásért,
                    # miért ez a helyes/szándékos viselkedés).
                    entry: dict = {
                        "device": name,
                        "error": f"ERROR_QUEUE_POLL_FAILED: {exc}",
                    }
                    if name in _ENERGY_DEVICES and isinstance(exc, InstrumentTimeoutError):
                        # R1 F-05: az eszköz állapotolvasása ebben a pollban
                        # kimarad -> az energia-állapot nem igazolt (F-01).
                        timed_out.add(name)
                        entry[_ENERGY_STATE_UNCERTAIN] = True
                    result.append(entry)
                    break
                if not errors:
                    break
                result.extend({"device": name, "error": err} for err in errors)
        def continue_with_partial(partial: list[dict]) -> bool:
            return continue_poll is None or continue_poll(result + partial)

        result.extend(
            self._poll_output_state_mismatches(skip_load=skip_load, timed_out=timed_out)
            if continue_poll is None else self._poll_output_state_mismatches(
                skip_load=skip_load, timed_out=timed_out, continue_poll=continue_with_partial,
            )
        )
        if not skip_load and "Load" not in timed_out:
            result.extend(
                self._poll_load_questionable_status()
                if continue_poll is None else self._poll_load_questionable_status(
                    continue_poll=continue_with_partial,
                )
            )
        if continue_poll is not None:
            continue_poll(result)
        return result

    def _poll_load_questionable_status(
        self, *, continue_poll: Callable[[list[dict]], bool] | None = None,
    ) -> list[dict]:
        """Review-kör 5, Hardver/Műszervezérlés HW4-01 (P3): TISZTÁN
        olvasás-only diagnosztika -- a Load `STATus:QUEStionable:
        CONDition?` regiszterének aktív VÉDELMI (nem informatív) bitjeit
        jelzi a MEGLÉVŐ eszköz-hibanapló csatornán, ugyanazt a mintát
        követve, mint `_poll_output_state_mismatches()` (DFAM-026
        elkerülése -- nincs önálló, teszteletlen fogyasztó-felület). NEM
        küld `PROT:CLE`-t és NEM hív semmilyen safety-döntést -- a
        tényleges latch-feloldás HIL-jóváhagyás-köteles, ezen a lépésen
        kívül esik. Bit-tábla forrás: 2380 txt 3415-3486. sor (l.
        device_load.py::_QUESTIONABLE_STATUS_BITS). A regiszter "unlatched"
        jellegére vonatkozó belső kézikönyv-ellentmondás: l. SCF-015
        (SOURCE_CONFLICT, SOURCE_CONFLICTS.md) -- a mai olvasás-only
        viselkedést nem érinti.

        PONTOSÍTÁS (C-11/DFAM-018+DFAM-012, 2026-09-15, MAX verifier F1
        lelete): a fenti "NEM hív semmilyen safety-döntést" arra
        vonatkozik, hogy ez a metódus MAGA nem ad ki semmilyen műszer-
        parancsot és nem módosít semmilyen MEGLÉVŐ safety-logikát (pl.
        `check_concurrent_psu_load()` továbbra is KIZÁRÓLAG a commanded
        flaget nézi, változatlanul). EZ NEM jelenti azt, hogy a visszaadott
        bejegyzés ne váltana ki FAULT-ot -- ahogy a lenti HW-R5-A
        bekezdés is leírja, a hívó (`test_runner.py`) MINDEN nem-üres
        `poll_device_errors()`-bejegyzést (a sikeres olvasás utáni
        `QUESTIONABLE_STATUS:`-ot ÉPPÚGY, mint az itt hozzáadott
        `QUESTIONABLE_STATUS_POLL_FAILED`-et) EGYFORMÁN kritikusnak
        kezeli az `is_critical_scpi_error()` alapértelmezett (üres
        whitelist -> minden nem-üres üzenet kritikus) szabálya szerint --
        MINDKETTŐ AZONNAL, MÁR AZ ELSŐ ELŐFORDULÁSKOR FAULT-ot vált ki
        aktív energiaátadás alatt, nincs "tartós ismétlődés"-höz kötött
        türelmi ablak. Ez SZÁNDÉKOS: egy kommunikáció-kiesés a Loaddal
        saját jogán safety-releváns, függetlenül attól, hogy milyen
        konkrét lekérdezés bukott el rajta -- l. `CLAIM_LEDGER.yaml`
        INSTRUMENT-ERROR-AGGREGATION-001 a türelmi-ablak (miért NINCS
        k-egymást-követő-hiba-küszöb itt, szemben pl. a BQ_TRANSPORT_
        PERSISTENTLY_UNREACHABLE/psu_voltage_readback_lost_after_n_checks
        mintával) teljes mérnöki indoklásáért.

        [regresszió, HW-R5-A (P1), a HW4-01 (78100ab) közvetlen
        következménye] a hívó (`test_runner.py`) MINDEN nem-üres
        `poll_device_errors()`-bejegyzést kritikus SCPI hibaként kezel
        (`scpi_error_policy.py` üres whitelist -> `is_critical_scpi_
        error()` mindig True) -> FAULT. Az EREDETI kód a teljes
        `read_questionable_status()` szótárt (mind a 12 bit, informatív
        RS/UNR/VON/TBF-fel együtt) továbbadta ide -- valós hardveren a
        RS (négyvezetékes sense bekötésnél tartósan igaz) és VON (Von
        alapszint MINimum=0V, egy rákötött akku szinte mindig átlépi, l.
        2380-120-60-900-01_A_Nov_2015.txt 6455-6476. sor,
        [SOURce:]VOLTage[:LEVel]:ON) bit szinte MINDIG aktív normál üzemben,
        tehát ez MINDEN session-t hamis FAULT-tal állított volna le az
        első poll-tickre. Csak a ténylegesen véd(elm)i/trip-jellegű
        bitek (`QUESTIONABLE_STATUS_PROTECTION_BITS`) escalálnak a
        kritikus csatornára -- az informatív bitek (RS/UNR/VON/TBF)
        SZÁNDÉKOSAN nem jutnak el ide (a teljes, szűretlen állapot
        továbbra is elérhető közvetlenül `read_questionable_status()`-
        szal, ha valaha kellene)."""
        result: list[dict] = []
        if continue_poll is not None and not continue_poll(result):
            return result
        try:
            status = self._load.read_questionable_status()
            active = [
                name for name, is_set in status.items()
                if is_set and name in QUESTIONABLE_STATUS_PROTECTION_BITS
            ]
            if active:
                result.append({
                    "device": "Load",
                    "error": f"QUESTIONABLE_STATUS: {','.join(active)}",
                })
        except Exception as exc:
            # Review 2026-09-15 (C-11/DFAM-018+DFAM-012) -- l. poll_device_
            # errors() azonos hivatkozású kommentje: korábban ez is
            # csendben elnyelte a kivételt, "nincs védelmi bit aktív"-ra
            # fordítva egy TÉNYLEGES kommunikációs hibát a Loaddal.
            result.append({
                "device": "Load",
                "error": f"QUESTIONABLE_STATUS_POLL_FAILED: {exc}",
            })
        return result

    def _poll_output_state_mismatches(
        self, *, skip_load: bool = False, timed_out: set[str] | None = None,
        continue_poll: Callable[[list[dict]], bool] | None = None,
    ) -> list[dict]:
        """Review-kör 5, Hardver/Műszervezérlés HW5-02/HW5-03 (P3): TISZTÁN
        olvasás-only diagnosztika -- a driver `*_commanded_on` (szoftver-
        oldali szándék) és a ténylegesen lekérdezett hardverállapot közötti
        eltérést jelzi a meglévő eszköz-hibanapló csatornán (ugyanaz a
        poll_device_errors()-fogyasztói lánc, mint a SCPI error-queue
        bejegyzéseknél: logger/on_device_error/DeviceErrorPanel), így
        elkerülve egy önálló, még nem tesztelt fogyasztó-felület bevezetését
        (DFAM-026). NEM változtat semmilyen meglévő safety-döntést -- pl.
        check_concurrent_psu_load() továbbra is KIZÁRÓLAG a commanded
        flaget nézi, változatlanul -- pusztán operátori figyelemfelhívás egy
        bizonytalan állapotra (CLAUDE.md §6.2: "bizonytalan állapot is
        hiba"). read_output_state()/read_input_state() forrás: 2220-30-1/prog.txt
        1553-1567. és 1477-1495. sor (PSU elsődleges/fallback mód), 2380 txt
        5793-5810. sor (Load).

        PONTOSÍTÁS (C-11/DFAM-018+DFAM-012, 2026-09-15, MAX verifier F1
        lelete) -- l. _poll_load_questionable_status() azonos hivatkozású
        pontosítása a teljes indoklásért. A "NEM változtat safety-döntést"
        arra vonatkozik, hogy ez a metódus MAGA nem ad ki parancsot/nem
        módosít MEGLÉVŐ logikát -- de a visszaadott bejegyzés (akár egy
        sikeres olvasás utáni MISMATCH, akár az itt hozzáadott OUTPUT_
        STATE_POLL_FAILED/INPUT_STATE_POLL_FAILED poll-hiba) EGYFORMÁN,
        MÁR AZ ELSŐ ELŐFORDULÁSKOR FAULT-ot vált ki a közös
        is_critical_scpi_error() kapun át aktív energiaátadás alatt --
        nincs "tartós ismétlődés"-höz kötött türelmi ablak. Ez SZÁNDÉKOS
        -- l. CLAIM_LEDGER.yaml INSTRUMENT-ERROR-AGGREGATION-001 a teljes
        mérnöki indoklásért, miért nincs itt k-egymást-követő-hiba-
        küszöb.

        R1 F-01/F-05 (B1B2_fixplan_FINAL.md B1/1, B1/4): a commanded=OFF/
        actual=ON eltérés és a *_STATE_POLL_FAILED bejegyzés típusos
        energia-állapot-bizonytalan jelölést kap (`is_energy_state_
        uncertain()`); ezt a hívó StepKind-től függetlenül, RELAX-ban is
        FAULT-nak veszi. `timed_out`: a poll_device_errors() közös,
        pollon belüli halmaza -- a benne lévő eszköz állapotolvasása
        kimarad; a Load INP? InstrumentTimeoutError-ja ide kerül, így a
        hívó ugyanabban a pollban a QUES-lekérdezést is kihagyja.

        R16-B10b kivétel: még nem nyugtázott Load ON után, a parancstól
        számított 1 s-on belüli OFF ON_ACK_PENDING állapot, nem hiba.
        A nyugta vagy OFF-parancs lezárja az ablakot; OFF/ON eltérésre,
        hibás readbackre és a hibasorra nincs türelmi ablak (HIL_PENDING)."""
        if timed_out is None:
            timed_out = set()
        result: list[dict] = []
        if "PSU" not in timed_out:
            if continue_poll is not None and not continue_poll(result):
                return result
            try:
                actual = self._psu.read_output_state()
                commanded = self._psu.output_commanded_on
                if actual != commanded:
                    entry: dict = {
                        "device": "PSU",
                        "error": (
                            f"OUTPUT_STATE_MISMATCH: commanded="
                            f"{'ON' if commanded else 'OFF'} actual="
                            f"{'ON' if actual else 'OFF'}"
                        ),
                    }
                    if actual and not commanded:
                        entry[_ENERGY_STATE_UNCERTAIN] = True
                    result.append(entry)
            except Exception as exc:
                # Review 2026-09-15 (C-11/DFAM-018+DFAM-012) -- l. poll_device_
                # errors() azonos hivatkozású kommentje.
                result.append({
                    "device": "PSU",
                    "error": f"OUTPUT_STATE_POLL_FAILED: {exc}",
                    _ENERGY_STATE_UNCERTAIN: True,
                })
        if not skip_load and "Load" not in timed_out:
            if continue_poll is not None and not continue_poll(result):
                return result
            try:
                actual = self._load.read_input_state()
                commanded = self._load.input_commanded_on
                commanded_at = getattr(self._load, "input_on_commanded_at", None)
                # R16-B10b: kizárólag az ON-parancshoz kötött, monoton ablak.
                # OFF/ON, olvasási hiba és a hibasor továbbra is azonnali hiba.
                pending = (
                    commanded and not actual and commanded_at is not None
                    and 0.0 <= time.monotonic() - commanded_at < INPUT_ACTIVATION_ACK_TIMEOUT_S
                )
                if pending:
                    result.append({
                        "device": "Load",
                        "error": "INPUT_ACTIVATION_PENDING: commanded=ON actual=OFF",
                        "status": "ON_ACK_PENDING",
                    })
                elif actual != commanded:
                    entry = {
                        "device": "Load",
                        "error": (
                            f"INPUT_STATE_MISMATCH: commanded="
                            f"{'ON' if commanded else 'OFF'} actual="
                            f"{'ON' if actual else 'OFF'}"
                        ),
                    }
                    if actual and not commanded:
                        entry[_ENERGY_STATE_UNCERTAIN] = True
                    result.append(entry)
            except Exception as exc:
                # Review 2026-09-15 (C-11/DFAM-018+DFAM-012) -- l. poll_device_
                # errors() azonos hivatkozású kommentje.
                if isinstance(exc, InstrumentTimeoutError):
                    timed_out.add("Load")
                result.append({
                    "device": "Load",
                    "error": f"INPUT_STATE_POLL_FAILED: {exc}",
                    _ENERGY_STATE_UNCERTAIN: True,
                })
        return result

    def read_all(
        self,
        skip_battery_voltage: bool = False,
        skip_battery_temperature: bool = False,
        skip_psu_voltage: bool = False,
        skip_psu_current: bool = False,
        skip_load_current: bool = False,
        skip_load_voltage: bool = False,
    ) -> dict:
        """Egyszerre olvas minden csatornából; a foglalt DMM-et kihagyja.

        DMMMeasurementBusy: None + OWNED_DEFERRED quality + skipped=True.
        Más olvasási hiba: None, skipped=False. A többi csatorna folytatódik.

        skip_battery_voltage=True: a DMM-feszültség csatornát kihagyja —
        ilyenkor a hívó (TestRunner._build_sample()) a napló mezőjét az
        aktív controller MÁR ugyanabban a tick-ben kiolvasott, tick-
        konzisztens u_batt_V property-jéből tölti fel egy második, független
        DMM-lekérdezés helyett. Ok: 2026-08-23 traditional_learning OV
        incidens elemzés — a korábbi, minden tick-ben lefutó második
        read_voltage() hívás egy, a controller safety-check-je által
        ténylegesen kiértékelt olvasástól ELTÉRŐ (később mért, gyors dV/dt
        mellett jelentősen eltérő) értéket naplózott, ami megtévesztő volt a
        hibaelemzésnél (CLAUDE.md §9: egy DMM-et egy tickben egy komponens
        olvasson).

        skip_battery_temperature=True: ugyanez az elv a hőmérséklet-DMM
        csatornára — minden step-controller saját, tick-konzisztens
        battery_temperature_C-t olvas/exponál, e nélkül a paraméter nélkül a
        DMM_T-t korábban MINDEN tick-ben másodszor is lekérdezte a
        _build_sample(), StepKind-tól függetlenül.

        C-11/DFAM-018 (2026-09-15): a `battery_voltage_V`/`battery_
        temperature_C` mező None-ja korábban MEGKÜLÖNBÖZTETHETETLEN volt
        aközött, hogy (a) a hívó SZÁNDÉKOSAN kihagyatta a csatornát
        (`skip_*=True`), vagy (b) a csatornát megpróbáltuk olvasni, de a
        hívás HIBÁZOTT. A jelenlegi EGYETLEN hívó (`TestRunner._build_
        sample()`) ezt a kétértelműséget elkerüli (a skip=True ágon a
        controller SAJÁT u_batt_V/battery_temperature_C property-jét
        használja, sosem ezt a None-t), de egy jövőbeli hívó számára a
        kétértelműség LATENS hiba lenne. Mostantól a visszaadott dict
        explicit `<mező>_skipped` kulcsot is hordoz minden csatornára --
        `True`, ha a hívó kérte a kihagyást vagy a DMM foglalt; `False` egyébként (a `None`
        `False` mellett tehát MINDIG valódi olvasási hibát jelent).

        Kör 20 / SW20-1 (2026-09-18): a fenti "minden csatornára" állítás
        a bevezetésétől 2026-09-18-ig HAMIS volt -- a
        `load_readback_voltage_V` volt az EGYETLEN csatorna, amelyik nem
        kapott `_skipped` kulcsot (DFAM-043: egy komment sosem bizonyíték
        önmagára). A hat kulcs mostantól hiánytalan, és tételesen
        ellenőrizhető: `battery_voltage_V_skipped`,
        `battery_temperature_C_skipped`, `psu_readback_voltage_V_skipped`,
        `psu_readback_current_A_skipped`, `load_readback_current_A_
        skipped`, `load_readback_voltage_V_skipped` -- l. a metódus végi
        kulcs-blokkot és a `TestReadAllSkippedKeyParity` őrtesztet.

        skip_psu_voltage/skip_psu_current=True: C-08b (2026-09-15),
        ugyanaz az elv, mint a battery_voltage/temperature csatornákon --
        a beágyazott ChargeController a saját `_read_psu_voltage()`/
        `_read_psu_current()` hívásával MÁR elvégezte ugyanazt a fizikai
        olvasást a jelen ticknél (l. `u_psu_measured_V`/`i_psu_measured_A`
        property), a hívó (`TestRunner._build_sample()`) ekkor NEM
        indít egy MÁSODIK, önálló PSU-lekérdezést -- ugyanaz a CLAUDE.md
        §9 "egy DMM-et egy tickben egy komponens olvasson" elv, kiterjesztve
        a PSU-csatornára. A `<mező>_skipped` kulcs itt is mindig jelen
        van, ugyanazzal a szemantikával.

        skip_load_current=True: teljes kódbázis review 2. kör (2026-09-15,
        R2-06, 12. writer-kör) -- ugyanaz az elv, kiterjesztve a Load-áram
        csatornára: a hívó (jelenleg 6 controller: DischargeController és
        5 beágyazott-DischargeController-t nem használó composite) MÁR
        elvégezte a fizikai áram-olvasást ugyanabban a tickben (l. az
        egyes controllerek `i_load_measured_A`/`load_current_readback_
        valid` property-jét). A Load-FESZÜLTSÉG csatornára ez a DUPLA-
        OLVASÁS elleni indok SZÁNDÉKOSAN NEM vonatkozik: egyetlen
        controller sem olvas Load-feszültséget önállóan, tehát nincs
        védendő controller-oldali epoch, és egy `skip_load_voltage=
        <validity> is not None` alakú, a skip_load_current-et TÜKRÖZŐ
        paraméter egy ma is naplózott, legitim mérést törölne
        indokolatlanul. Ez az indoklás TOVÁBBRA IS ÉRVÉNYES -- az alábbi
        skip_load_voltage szerződése SZŰKEBB, és MÁS indokon áll.

        skip_load_voltage=True: Kör 20 / SW20-1 (2026-09-18). Nem a
        fizikai dupla-olvasás elkerülése a cél (l. fent), hanem a
        `.claude/rules/10-safety.md` #3 ("emergency stop után további
        aktív parancs tiltása"): ha a controller EBBEN a tickben
        POZITÍVAN megállapította, hogy a Load halott
        (`load_current_readback_valid is False` -- a 6 controller mind a
        7 hívóhelyén ez azt jelenti, hogy az `emergency_stop("LOAD_COMM_
        LOST")` és vele a Load OFF / PSU ALL OFF safe-off MÁR lefutott),
        akkor ez a napló-célú `MEAS:VOLT?` egy BIZONYÍTOTTAN halott
        eszközre menne: `device_load.py` szerint max_retries=3 x
        timeout_ms=5000, azaz akár ~15 s blokkoló I/O a terminális
        útvonalra lépés ELŐTT (DFAM-027). A mért érték ilyenkor amúgy sem
        használható: a safe-off UTÁN mérve már nem a terhelt, hanem a
        lekapcsolt állapot feszültsége lenne, azaz intra-tick epochot
        keverne (ugyanaz az érv, mint R2-07-nél a PSU két csatornáján).
        A hívó (`test_runner.py::_build_sample()`) ezért KIZÁRÓLAG az
        `is False` ágon adja True-ra; `None` (a controller ezen a ticken
        nem olvasott Load-áramot) és `True` (sikeres olvasás, az eszköz
        bizonyítottan él) esetén a csatorna VÁLTOZATLANUL olvasódik."""
        result = {}
        channels = []
        if not skip_load_voltage:
            channels.append(("load_readback_voltage_V", self._load, "measure_voltage"))
        if not skip_load_current:
            channels.append(("load_readback_current_A", self._load, "measure_current"))
        if not skip_psu_current:
            channels.insert(0, ("psu_readback_current_A", self._psu, "measure_output_current"))
        if not skip_psu_voltage:
            channels.insert(0, ("psu_readback_voltage_V", self._psu, "measure_output_voltage"))
        if not skip_battery_temperature:
            channels.insert(0, ("battery_temperature_C", self._dmm_t, "read_temperature"))
        if not skip_battery_voltage:
            channels.insert(0, ("battery_voltage_V", self._dmm_v, "read_voltage"))
        for name, instrument, method in channels:
            try:
                result[name] = getattr(instrument, method)()
            except DMMMeasurementBusy:
                # A worker tulajdonlása explicit kihagyás, nem DMM-hiba.
                result[name] = None
                result[f"{name}_quality"] = "OWNED_DEFERRED"
            except Exception:
                result[name] = None
        result["battery_voltage_V_skipped"] = skip_battery_voltage or result.get("battery_voltage_V_quality") == "OWNED_DEFERRED"
        result["battery_temperature_C_skipped"] = skip_battery_temperature or result.get("battery_temperature_C_quality") == "OWNED_DEFERRED"
        result["psu_readback_voltage_V_skipped"] = skip_psu_voltage
        result["psu_readback_current_A_skipped"] = skip_psu_current
        result["load_readback_current_A_skipped"] = skip_load_current
        result["load_readback_voltage_V_skipped"] = skip_load_voltage
        if skip_battery_voltage:
            result["battery_voltage_V"] = None
        if skip_battery_temperature:
            result["battery_temperature_C"] = None
        if skip_psu_voltage:
            result["psu_readback_voltage_V"] = None
        if skip_psu_current:
            result["psu_readback_current_A"] = None
        if skip_load_current:
            result["load_readback_current_A"] = None
        if skip_load_voltage:
            result["load_readback_voltage_V"] = None
        return result
