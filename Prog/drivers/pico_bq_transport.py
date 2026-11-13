"""
PicoBQTransport — WP-AK-06/AK-08 "PC <-> Pico" réteg: JSON Lines RPC kliens,
ami a `Bq34z110Transport` Protocol-t (l. Prog/drivers/device_bq34z110.py)
valósítja meg egy valós (vagy tesztben fake) soros porton keresztül.

Kötelező absztrakció (WP-AK-06): workflow -> BQDevice -> PicoBQTransport.
A `Bq34z110ReadOnlyDriver` tölti be a "BQDevice" szerepet (a workflow ezen
keresztül olvas, sosem közvetlenül a transporton/soros protokollon) — ez a
modul a legalsó, ténylegesen soros protokollt beszélő réteg.

Drótprotokoll (Kör 21 F1/ARC21-3: EZ a szerződés forrása MOSTANTÓL
`Prog/pico_firmware/pico_bq_rpc.py` + `pico_bq_service.py` -- az auto-
provisioning [commit 3b9bbef, `Prog/src/pico_provisioning_service.py`]
óta a GUI MAGA tölti fel ezt az in-repo másolatot a Picóra, tehát ez NEM
egy külső, a repón kívül élő szerződés, hanem egy in-repo kliens/szerver
pár, amit a `Prog/tests/test_source_citation_consistency.py` [Kör 21 F9]
mellett egy metódusnév-paritási meta-teszt is véd -- l.
`Prog/tests/test_pico_bq_transport.py::TestNegotiateWriteMethodParity` és
`ROUND21_DEFECT_FAMILY_MINING_ARCHIVE.md` §6.1 a strukturális megjegyzésért.
A korábbi "KÜLÖN repo, nem közös import" állítás azért maradt sokáig
észrevétlenül hamis, mert egy olvasó, aki elhiszi, nem is nyitja meg a
`Prog/pico_firmware/` fát -- ez volt a gyökere annak, hogy egy korábbi,
Kör 21 Hardver-reviewer által talált "P1" finding [HW21-01] valójában már
javítva volt a firmware oldalán, csak senki nem nézte meg):
  Keres:  {"id": <int>, "method": "<nev>", "params": {...}}\\n
  Valasz: {"id": <int>, "ok": true, "result": <any>}\\n
       vagy {"id": <int>, "ok": false, "error": {"code": "...", "message": "..."}}\\n

Capability discovery: `negotiate()` a "capabilities" metodust hivja elsokent,
ellenorzi a protocol_version-t, mielott barmilyen tenyleges BQ-olvasast
engedne — igy egy protokoll-inkompatibilis (regi/uj) Pico-firmware azonnal,
egyertelmu hibaval jelentkezik, nem csendes felre-ertelmezett valasszal.
Kör 21 F1/ARC21-3(b): a `negotiate()` opcionalis `required_write_methods`
parametere azt is ellenorzi, hogy a csatlakoztatott firmware a HIVO altal
tenylegesen hasznalando allapotmodosito primitiveket exponalja-e -- igy egy
regi (pl. WP-AK-06-kori, csak-olvaso) vagy protocol_version-t nem valtott,
de a szukseges write-metodusokat nem hordozo firmware MAR a kapcsolodaskor,
NEM az elso valodi write-nal (energizalt/bekototott allapotban) bukik el.

HATOKOR (WP-AK-08 kiegeszitese a WP-AK-06 read-only retegehez kepest):
az OLVASO metodusok (Bq34z110Transport Protocol) mellett MOSTANTOL az
allapotmodosito primitivek (unseal/full_access/seal/it_enable/
write_data_flash_block/restore_df_snapshot) is elerhetok - mindegyik
EXPLICIT `confirm: bool` parametert igenyel (nincs alapertelmezett True),
ami a TF1-oldali `pico_bq_rpc.py`-n keresztul VALTOZATLANUL a
`pico_bq_service.py` sajat confirm=True kapujahoz jut el. Ez a modul MAGA
NEM dont semmilyen write-sorrendrol/pipeline-rol (CONFIRM->IDENTIFY->VERIFY
DEVICE/FW->BACKUP->HASH->WRITE->READBACK->COMPARE->CHECKSUM->STATUS VERIFY->
PASS/FAIL) - az a workflow-reteg (Prog/src/bq_controlled_write.py) feladata,
ami ezt a transportot alacsony szintu primitivkent hasznalja (architekturalis
hatar: driver nem hoz workflow-dontest, l. CLAUDE.md 9. szakasz).

HIL-STATUS (2026-08-23 frissitve): a `negotiate()` + osszes olvaso metodus
(Bq34z110Transport Protocol) ELSOKENT sikeresen validalva valos Pico/BQ34Z110
hardveren, a TF1 repo `pico_bq_rpc.py` `serve_forever()`-en keresztul futo
JSON-Lines szerverevel szemben (COM10). Minden ertek belsoleg konzisztens
(pl. remaining/full_charge_capacity aranya = SOC%, control_status/chem_id/
device_type egyezik a korabbi kozvetlen pico_bq_service.py mearesekkel).
Korabban csak fake soros port ellen volt tesztelve, l.
Riportok/wp_ak_06_implementacios_zarojelentes.md. A WP-AK-08 write-
metodusok (unseal/full_access/seal/it_enable/write_data_flash_block/
restore_df_snapshot) MEG NINCSENEK HIL-validalva ezen a retegen keresztul
(csak a TF1-oldali pico_bq_service.py-n kozvetlenul, l. TF1 repo
TF1_BQ_Service_Golden_Image_Mode.md) - EXTRA HIL-kockazatot hordoznak
(elsokent irnak valos BQ Data Flash-be RPC-n keresztul) - l.
Riportok/wp_ak_08_implementacios_zarojelentes.md.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.exceptions import InstrumentCommandError, InstrumentTimeoutError

PROTOCOL_VERSION = "1.0"

# Kör 21 F1/ARC21-3(b): a firmware `_capabilities()`-je (pico_bq_rpc.py)
# `write_methods` mezőjének két, a hívó SZÁNDÉKA szerinti névvel ellátott
# részhalmaza -- a `negotiate(required_write_methods=...)` ezekkel hívható,
# hogy a kapcsolódás pillanatában, MIELŐTT bármilyen tényleges write
# megtörténne, kiderüljön, ha a csatlakoztatott firmware nem támogatja a
# session által tényleg használt primitíveket. A névsor itt a KLIENS saját,
# a `_call()` hívásaiban szó szerint használt metódusneveinek listája (nem
# a firmware `_WRITE_METHOD_NAMES`-jének importja) -- a modul docstring
# szerint a firmware MOSTANTÓL in-repo (`Prog/pico_firmware/pico_bq_rpc.py`),
# tehát az import technikailag lehetséges volna (a modul csak `sys`/`json`-t
# importál top-level-ben), de a két oldal szándékosan KÜLÖN listaként él:
# ez a kliens saját hívás-inventáriuma, a firmware-é a szerver saját
# kontraktusa, és a kettő szinkronban-tartását egy paritási teszt védi
# (l. TestNegotiateWriteMethodParity), NEM egy futásidejű import-függőség.
LEARNING_SESSION_WRITE_METHODS = frozenset({
    "unseal", "full_access", "seal", "it_enable",
    "write_data_flash_block", "restore_df_snapshot",
})
# Kör 21 F1/ARC21-3(b) MAX-verifier B3 lelet: a kalibrációs szekvencia
# ELSŐ két és UTOLSÓ write-hívása is unseal()/full_access()/seal() (l.
# bq_gauge_calibration.py:439-445 unseal+full_access enter_cal() előtt,
# :674 stb. seal() cleanupként) -- ezek NÉLKÜL a gate a kalibrációs úton
# éppen azt a hibaosztályt nem fogta volna el, amit ki kellett volna:
# egy unseal/full_access/seal-t nem exponáló firmware csak az ELSŐ valódi
# write-nál (energizált chip mellett) bukott volna el, nem a kapcsolódáskor.
CALIBRATION_WRITE_METHODS = frozenset({
    "unseal", "full_access", "seal",
    "enter_cal", "exit_cal", "board_offset_cal", "cc_offset_cal",
    "cc_offset_save", "write_calibration_gain_delta", "write_voltage_divider",
})


class SerialLike(Protocol):
    """Minimál soros-port interfész — valós hardveren `pyserial.Serial`
    (natívan implementálja mindhárom metódust), teszthez `FakePicoSerial`
    (l. Prog/tests/mock_drivers/fake_pico_serial.py). `reset_input_buffer()`
    KÖTELEZŐ — a keret-szinkron-vesztés elleni védelem (l. `PicoBQTransport.
    _call()` docstring) erre támaszkodik."""

    def write(self, data: bytes) -> int: ...
    def readline(self) -> bytes: ...
    def reset_input_buffer(self) -> None: ...
    # Review 2026-09-15 (C-02, teljes kódbázis review): a Protocol korábban
    # NEM deklarálta a `close()`-t, holott mind a `disconnect()`, mind a
    # `_reopen_serial()` MÁR hívta -- ez 2, pre-existing mypy hibát okozott
    # itt, és egy HARMADIKAT main_window.py-ban, amikor a session-owned
    # bq_transport SAJÁT `.serial`-ját kezdte közvetlenül zárni (C-02 fix).
    #
    # KORREKCIÓ (MAX verifier F1 lelete, 2026-09-15): egy korábbi verzió
    # itt tévesen állította, hogy a teszt `FakePicoSerial`-nek MÁR VOLT
    # close()-ja -- ez HAMIS volt (a mock ekkor még nem deklarálta), ezért
    # ez a Protocol-bővítés ÖNMAGÁBAN +7 UJ mypy strukturális-tipizálási
    # hibát vezetett volna be (minden `PicoBQTransport(serial=
    # FakePicoSerial(...))` hívóhelyen) -- NETTÓ ROSSZABB állapotot adva
    # (26->31), nem a beígért javulást. JAVÍTVA: a `FakePicoSerial` MOST
    # kapott egy valódi `close()`-t (l. fake_pico_serial.py `closed` mező +
    # metódus) -- ezzel a valós `pyserial.Serial` ÉS a teszt mock egyaránt
    # ténylegesen implementálja a Protocolt, nem csak állítva.
    def close(self) -> None: ...


class PicoBQTransportError(InstrumentCommandError):
    """RPC szintű hiba (ismeretlen metódus, protokoll-eltérés, Pico-oldali
    METHOD_ERROR válasz) — nem soros I/O timeout (arra InstrumentTimeoutError)."""


# WP-AK-06 review-lelet, valós hiba (nem csak elméleti kockázat) — l.
# device_bq34z110.py docstring "KÉT KÜLÖN CÍMTÉR" szakasza: a standard
# command regiszterek (0x00-0x1F) ÉS a Control() alparancsok (0x0000-0xFFFF)
# Python int-ként AZONOS ÉRTÉKRE eshetnek (STD_CMD_VOLTAGE=0x08 == MAC_CHEM_ID
# =0x0008 == 8). Az első verzió egyetlen dict-be tette mindkettőt — ez
# csendben felülírta a "voltage_mV" bejegyzést "chem_id"-vel, és a
# `test_driver_reads_full_telemetry_set_through_transport` integrációs teszt
# ezt AZONNAL el is buktatta (12500 helyett 2048-at olvasott vissza). Ezért
# KÉT külön táblázat, a Bq34z110Transport két külön metódusának megfelelően.
_STANDARD_COMMAND_METHOD: dict[int, str] = {
    Bq34z110ReadOnlyDriver.STD_CMD_STATE_OF_CHARGE: "soc_percent",
    Bq34z110ReadOnlyDriver.STD_CMD_REMAINING_CAPACITY: "remaining_capacity_mAh",
    Bq34z110ReadOnlyDriver.STD_CMD_FULL_CHARGE_CAPACITY: "full_charge_capacity_mAh",
    Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE: "voltage_mV",
    Bq34z110ReadOnlyDriver.STD_CMD_AVERAGE_CURRENT: "average_current_mA",
    Bq34z110ReadOnlyDriver.STD_CMD_TEMPERATURE: "temperature_01K",
    Bq34z110ReadOnlyDriver.STD_CMD_FLAGS: "flags",
    # WP-AK-10 (SUP03-LEARN-002 gauge log)
    Bq34z110ReadOnlyDriver.STD_CMD_CYCLE_COUNT: "cycle_count",
    Bq34z110ReadOnlyDriver.STD_CMD_DOD0: "dod0",
}

_CONTROL_SUBCOMMAND_METHOD: dict[int, str] = {
    Bq34z110ReadOnlyDriver.MAC_CHEM_ID: "chem_id",
    Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS: "control_status",
    # WP-AK-07 (BQ Diagnostics GUI "BQ device" mező) - a TF1 repo
    # pico_bq_rpc.py-ban ADDITÍV, granularis metódusokként hozzáadva
    # (a már meglévő kombinált "identify" RPC-metódus mellett, azt nem
    # érintve) - l. TF1 repo Prog/pico_bq_rpc.py build_methods() docstring.
    Bq34z110ReadOnlyDriver.MAC_DEVICE_TYPE: "device_type",
    Bq34z110ReadOnlyDriver.MAC_FW_VERSION: "firmware_version",
    Bq34z110ReadOnlyDriver.MAC_HW_VERSION: "hardware_version",
}


# Teljes kódbázis review 2. kör (2026-08-28), Hardver/Műszervezérlés P1
# lelet: a Data Flash blokk-olvasásoknál (read_data_flash_block()) a
# `bytes.fromhex()` legalább a hex-string FORMÁTUMÁT ellenőrzi (l. review-kör
# 2, HW2-06, 2026-09-08: a végeredmény BÁJT-hossza akkor még NEM volt
# ellenőrizve — ez ott, a saját metódusnál javítva) — a standard command/
# Control() szó-olvasásoknál eddig még a formátum-ellenőrzés is hiányzott.
# A `_call()` a `response.get("result")`-ot típusellenőrzés
# nélkül adja tovább — mivel Python `json.loads()` alapértelmezetten
# elfogadja a nem szabványos NaN/Infinity JSON-tokeneket (kivétel nélkül!),
# egy Pico-oldali hiba (pl. 0-val osztás egy származtatott mezőnél) vagy egy
# `null` eredmény (nem implementált/hibázó Pico-metódus) kivétel nélkül
# átfolyt volna — a hívó (`Bq34z110ReadOnlyDriver`) `int`-ként deklarálta,
# de futásidőben nem kényszerítette. Ez megsértette a `bq_diagnostics.py::
# poll_bq_diagnostics()` saját, dokumentált field-isolation tervezési célját:
# a `decode_flags()`/`decode_control_status()` bitwise `&` egy NaN (float)
# vagy None `word`-ön el nem kapott TypeError-t dobott volna, ami a TELJES
# `poll_bq_diagnostics()`-t elszállította volna, nem csak azt az egy mezőt.
def _validate_word_result(value: Any, method: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PicoBQTransportError(
            f"PicoBQTransport: érvénytelen (nem int) eredmény method={method!r}: "
            f"{value!r} ({type(value).__name__})"
        )
    return value


# Teljes kódbázis review 4. kör (2026-08-29), Hardver/Műszervezérlés P1
# lelet: ugyanaz a hibaosztály, mint _validate_word_result() fent, de a
# read_calibration_data()/read_voltage_divider() kimaradt belőle — a
# downstream bq_gauge_calibration.py közvetlenül aritmetikában/indexelésben
# használja az eredményt (data["cc_offset"]/["board_offset"]/["cc_gain"]/
# ["cc_delta"], round(old_vd * ...)), ami egy malformed Pico-válasz (None/
# NaN) esetén TypeError-t/ValueError-t dobott volna — ezt a `_guarded()`
# (bq_gauge_calibration.py) KIZÁRÓLAG InstrumentError-ra szűkített
# except-ága NEM kapja el, tehát a kivétel elkapatlanul kiszáll a state
# machine-ből, MIELŐTT a _fail() meghívná az exit_cal()/seal() takarítást —
# a BQ34Z110 UNSEALED + FULL ACCESS (+ a gain-ágon CALMODE) állapotban
# maradhat, dokumentálatlanul.
_CALIBRATION_DATA_NUMERIC_FIELDS = ("cc_offset", "board_offset", "cc_gain", "cc_delta")


def _validate_calibration_data_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PicoBQTransportError(
            f"PicoBQTransport: érvénytelen (nem dict) eredmény "
            f"method='read_calibration_data': {value!r} ({type(value).__name__})"
        )
    for key in _CALIBRATION_DATA_NUMERIC_FIELDS:
        # Teljes kódbázis review 10. kör (2026-08-31), Hardver/Műszervezérlés
        # P1 lelet: korábban `if key not in value: continue` — egy TELJESEN
        # HIÁNYZÓ mező (nem NaN/típushiba, hanem a kulcs teljes hiánya, pl.
        # egy csonka Pico-válasz) csendben átment ezen a validáción, a dict
        # változatlanul visszaadva. A downstream bq_gauge_calibration.py
        # `data["cc_offset"]`-je ilyenkor bare KeyError-t dob — ami NEM
        # InstrumentError, tehát a _guarded() (kizárólag InstrumentError-ra
        # szűkített except-ág) NEM kapja el: a kivétel elkapatlanul kiszáll,
        # a _fail()->exit_cal()/seal() takarítás sosem fut le, a chip
        # UNSEALED+FULL_ACCESS(+CALMODE) állapotban maradhat, és — mivel a
        # _save_offset()-ben a state-átállítás a hibás olvasás UTÁN történne
        # — a következő 500ms-es timer-tick ugyanazt a cc_offset_save(
        # confirm=True) parancsot ismételheti a valós hardveren, korlátlanul
        # (CLAUDE.md §10.5: bizonytalan előző eredményű parancsot nem szabad
        # vakon ismételni).
        if key not in value:
            raise PicoBQTransportError(
                f"PicoBQTransport: hiányzó {key!r} mező read_calibration_data "
                f"válaszban: {value!r}"
            )
        field_value = value[key]
        if isinstance(field_value, bool) or not isinstance(field_value, (int, float)):
            raise PicoBQTransportError(
                f"PicoBQTransport: érvénytelen (nem szám) {key!r} mező "
                f"read_calibration_data válaszban: {field_value!r} "
                f"({type(field_value).__name__})"
            )
        if isinstance(field_value, float) and (math.isnan(field_value) or math.isinf(field_value)):
            raise PicoBQTransportError(
                f"PicoBQTransport: NaN/Inf {key!r} mező read_calibration_data "
                f"válaszban: {field_value!r}"
            )
    return value


@dataclass
class PicoBQTransport:
    """`Bq34z110Transport` Protocol implementáció JSON Lines RPC felett."""

    serial: SerialLike | None = None
    max_retries: int = 3

    _next_id: int = field(default=1, init=False, repr=False)
    # HIL-D5 (2026-09-13, l. _reopen_serial() docstring): a connect()-nek
    # átadott paramétereket el kell tárolni, hogy egy soros I/O hiba után a
    # port UGYANAZZAL a paraméterezéssel újranyitható legyen.
    _port: str | None = field(default=None, init=False, repr=False)
    _baudrate: int = field(default=115200, init=False, repr=False)
    _timeout: float = field(default=1.0, init=False, repr=False)

    # ------------------------------------------------------------------ #
    # Kapcsolat — Teljes kódbázis review 2. kör (2026-08-28), Hardver/    #
    # Műszervezérlés P2 lelet: CLAUDE.md §9 minden driverhez saját        #
    # connect()/disconnect()/is_connected() metódust ír elő, ugyanúgy,    #
    # mint a PSU/Load/DMM drivereknél — korábban ez a réteg hiányzott, a  #
    # soros port életciklus-kezelése (nyitás/zárás) a GUI-rétegben        #
    # duplikálódott (main_window.py két helyen, bq_calibration_dialog.py).#
    # ------------------------------------------------------------------ #
    def connect(self, port: str, baudrate: int = 115200, timeout: float = 1.0) -> None:
        import serial as pyserial  # type: ignore[import-untyped]  # lazy import, csak tényleges csatlakozáskor kell
        self.serial = pyserial.Serial(port, baudrate=baudrate, timeout=timeout)
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout

    def disconnect(self) -> None:
        if self.serial is not None:
            try:
                self.serial.close()
            except Exception:
                pass
            self.serial = None
        # MAX verifier 2. kör (2026-09-14, NEW-1 lelet): self._port-ot IS
        # törölni kell itt -- kulonben egy SZANDEKOS disconnect() utan a
        # KOVETKEZO _call() a self._port alapjan (self.serial is None, de
        # self._port NEM None) csendben UJRANYITNA a portot, visszavonva a
        # hivo explicit disconnect-szandekat. connect() ujra beallitja, ha
        # szukseges -- self._port-nak nincs mas olvasoja.
        self._port = None

    def is_connected(self) -> bool:
        return self.serial is not None

    def _reopen_serial(self) -> None:
        """HIL-D5 (2026-09-13, Folyamatok/tervek/HIL_TESZTTERV_2026-09-11_
        24V_7200mAh.md 7. szakasz, CLAIM_LEDGER.yaml PICO-RECONNECT-001):
        valós COM10 Pico+BQ34Z110 egységen igazolva, hogy egy ~10s-es USB-
        kábel-kihúzás UTÁN a MÁR MEGNYITOTT `pyserial.Serial` handle SOHA
        nem állt helyre önmagától — minden további `_call()`-próbálkozás
        (a meglévő `max_retries`-on belül is) azonnali, NEM-timeout-jellegű
        OS-szintű kivétellel hibázott (Windows: `PermissionError`/"WriteFile
        failed"), FÜGGETLENÜL attól, hogy a fizikai eszköz közben
        visszacsatlakozott és az OS már újra látta a COM portot. `connect()`
        csak EGYSZER nyitja meg a portot, session indulásakor (mint minden
        másik instrumentum-drivernél, l. device_psu.py/device_load.py/
        device_dmm.py — ugyanaz a sibling-kockázat azokon is fennáll,
        dokumentálva DEFECT_FAMILY_REGISTRY.yaml DFAM-040-ben, de HIL-
        bizonyíték nélkül ott NEM javítva, CLAUDE.md §10 "ne módosíts
        működő testvér-útvonalat bizonyított szükség nélkül" szerint).

        Ez a helper egy soros I/O kivétel UTÁN próbálja újranyitni UGYANAZT
        a portot, a connect()-nek átadott paraméterekkel — ha a fizikai
        eszköz addigra valóban visszatért, a KÖVETKEZŐ `_call()`-próbálkozás
        (akár a jelenlegi retry-cikluson belül, akár egy KÉSŐBBI, független
        hívás, pl. a következő periodikus diagnosztikai poll) már sikeres
        lehet, anélkül hogy a teljes session-t újra kellene indítani.
        `connect()` sosem hívott eset (pl. teszt direkt `serial=...`
        injektálva) no-op — nincs port-paraméter, amivel újra lehetne
        nyitni."""
        if self._port is None:
            return
        import serial as pyserial  # type: ignore[import-untyped]
        if self.serial is not None:
            try:
                self.serial.close()
            except Exception:
                pass
        try:
            self.serial = pyserial.Serial(self._port, baudrate=self._baudrate, timeout=self._timeout)
        except Exception:
            # Az újranyitás maga is hibázhat (az eszköz MÉG nincs vissza) —
            # ez nem szabad, hogy egy megtévesztő kivételt dobjon a hívó
            # _call() retry-ciklusa felé; self.serial None-ra állítva a
            # KÖVETKEZŐ ciklus-iteráció (vagy egy KÉSŐBBI hívás) az explicit
            # "nincs aktív soros kapcsolat" hibát adja, nem egy újabb,
            # csendben-sikertelen write()-et.
            self.serial = None

    # ------------------------------------------------------------------ #
    def negotiate(
        self, required_write_methods: frozenset[str] = frozenset()
    ) -> dict[str, Any]:
        """Capability discovery — protocol_version ellenőrzés MIELŐTT
        bármilyen tényleges BQ-olvasás megtörténne. A hívó felelőssége ezt
        egyszer, kapcsolódáskor meghívni (l. BQDevice/workflow réteg).

        Teljes kódbázis review 11. kör (2026-08-31), Hardver/Műszervezérlés
        P1 lelet: korábban a "capabilities" RPC-válasz típusa validálatlan
        volt (ellentétben a _validate_word_result()/_validate_calibration_
        data_result() mintával) — egy nem-dict eredmény (pl. None, csonka
        Pico-válasz) esetén a `.get("protocol_version")` bare AttributeError-t
        dobott volna, ami NEM PicoBQTransportError, tehát mindhárom hívóhely
        (bq_calibration_dialog.py::_BqCalibrationConnectWorker.run(),
        main_window.py::_BqConnectWorker.run()/_construct_and_connect_
        instruments()) szűk `except PicoBQTransportError` ága nem kapja el —
        a BqCalibrationDialog MODÁLIS, "Csatlakozás…" állapotban véglegesen
        beragadhat, blokkolva a MainWindow Emergency Stop gombját egy
        párhuzamosan futó fizikai teszt alatt (CLAUDE.md §15).

        Kör 21 F1/ARC21-3(b): `required_write_methods` (l. modul-szintű
        LEARNING_SESSION_WRITE_METHODS/CALIBRATION_WRITE_METHODS konstansok)
        -- ha nem üres, és a firmware `write_methods` listája NEM tartalmazza
        MINDEGYIKÜKET, `negotiate()` MOST explicit hibával áll meg, MIELŐTT
        a hívó bármelyik write-primitívet meghívná. Korábban a firmware a
        `capabilities`-ben MÁR visszaadta ezt az információt (`read_only`/
        `write_methods`), de SEMMILYEN hívó nem nézte meg -- egy régi
        (pl. WP-AK-06-kori, csak-olvasó) vagy egyébként nem-teljes firmware
        MEGVÁLTOZATLAN `protocol_version`-nel sikeresen "negotiate"-elt volna,
        és csak az ELSŐ valódi write-nál bukott volna el, energizált/
        bekötött akku mellett (l. ROUND21_DEFECT_FAMILY_MINING_ARCHIVE.md
        ARC21-3). Alapértelmezetten üres halmaz -- a meglévő, csak-olvasó
        hívók (pl. _BqConnectWorker, pico_provisioning_service.py
        verify_service_running()) viselkedése változatlan."""
        capabilities = self._call("capabilities", {})
        if not isinstance(capabilities, dict):
            raise PicoBQTransportError(
                f"PicoBQTransport: érvénytelen (nem dict) eredmény "
                f"method='capabilities': {capabilities!r} ({type(capabilities).__name__})"
            )
        remote_version = capabilities.get("protocol_version")
        if remote_version != PROTOCOL_VERSION:
            raise PicoBQTransportError(
                f"PicoBQTransport: protocol_version eltérés — Pico {remote_version!r}, "
                f"PC kliens {PROTOCOL_VERSION!r}. Frissítsd az egyik oldalt."
            )
        if required_write_methods:
            available = capabilities.get("write_methods")
            available_set = set(available) if isinstance(available, list) else set()
            missing = required_write_methods - available_set
            if missing:
                raise PicoBQTransportError(
                    f"PicoBQTransport: a csatlakoztatott firmware NEM támogatja "
                    f"a session által igényelt write-metódusokat: "
                    f"{sorted(missing)!r} (firmware write_methods="
                    f"{sorted(available_set)!r}). Ellenőrizd/frissítsd a Pico "
                    f"firmware-t (Pico előkészítése menü)."
                )
        return capabilities

    # ------------------------------------------------------------------ #
    # Bq34z110Transport Protocol                                          #
    # ------------------------------------------------------------------ #
    def read_standard_command_word(self, command: int) -> int:
        method = _STANDARD_COMMAND_METHOD.get(command)
        if method is None:
            raise PicoBQTransportError(
                f"PicoBQTransport: nincs RPC-metódus rendelve a 0x{command:02X} "
                f"standard command-hoz"
            )
        return _validate_word_result(self._call(method, {}), method)

    def read_control_subcommand_word(self, subcommand: int) -> int:
        method = _CONTROL_SUBCOMMAND_METHOD.get(subcommand)
        if method is None:
            raise PicoBQTransportError(
                f"PicoBQTransport: nincs RPC-metódus rendelve a 0x{subcommand:04X} "
                f"Control() alparancshoz"
            )
        return _validate_word_result(self._call(method, {}), method)

    def read_data_flash_block(self, subclass: int, block: int) -> bytes:
        hex_str = self._call("data_flash_block", {"subclass": subclass, "block": block})
        try:
            result = bytes.fromhex(hex_str)
        except (TypeError, ValueError) as exc:
            raise PicoBQTransportError(
                f"PicoBQTransport: érvénytelen hex válasz data_flash_block-hoz: {hex_str!r}"
            ) from exc
        # Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-06 lelet: a
        # 116. sor körüli komment tévesen állította, hogy itt MÁR van
        # explicit hosszellenőrzés — valójában `bytes.fromhex()` csak a
        # hex-STRING formátumát validálja, a végeredmény BÁJT-hosszát nem.
        # SLUSB55B (rev B, ACCESSING DATA FLASH, nyomtatott p.13-14):
        # "Reading and writing subclass data are block operations 32 bytes
        # in length." -- MAX verifier follow-up (P3-1, 2026-09-08): a
        # rövidebb blokk lehetősége ugyanott KIZÁRÓLAG az ÍRÁS irányra
        # vonatkozik ("Data can be written in shorter block sizes,
        # however."), az OLVASÁS (ez a metódus) mindig a teljes 32 bájtot
        # adja vissza -- ugyanezt a 32 bájtot kényszeríti ki
        # `write_data_flash_block()` az írás oldalán is (l. lent). Egy
        # rövidebb/hosszabb, hex-formátumban egyébként érvényes válasz (pl.
        # Pico-oldali csonkítás) explicit hiba nélkül folyt volna tovább az
        # offset-alapú mezőkinyeréshez (bq_data_flash_service.py), ahol egy
        # IndexError vagy csendes rossz-offset olvasás messze a valódi
        # hibaforrástól jelentkezett volna.
        if len(result) != 32:
            raise PicoBQTransportError(
                f"PicoBQTransport: data_flash_block válasz hossza {len(result)} bájt, "
                f"várt 32 (subclass={subclass}, block={block})"
            )
        return result

    def read_raw_block(self, addr: int, length: int) -> bytes:
        """CSAK-OLVASÓ, diagnosztikai célú, tetszőleges I2C-cím-tartomány
        olvasása — 2026-09-12, CALMODE-anomália kivizsgálása (l.
        CLAIM_LEDGER.yaml BQ-CTRLSTATUS-CALMODE-001). NEM Data Flash
        subclass-alapú, NEM érinti a SEALED-előfeltételt — ugyanaz a nyers
        I2C `readfrom_mem` mintázat, mint a standard parancsok (Voltage()/
        AverageCurrent() stb.) olvasása, csak tetszőleges címre."""
        hex_str = self._call("read_raw_block", {"addr": addr, "length": length})
        try:
            result = bytes.fromhex(hex_str)
        except (TypeError, ValueError) as exc:
            raise PicoBQTransportError(
                f"PicoBQTransport: érvénytelen hex válasz read_raw_block-hoz: {hex_str!r}"
            ) from exc
        if len(result) != length:
            raise PicoBQTransportError(
                f"PicoBQTransport: read_raw_block válasz hossza {len(result)} bájt, "
                f"várt {length} (addr=0x{addr:02X})"
            )
        return result

    # ------------------------------------------------------------------ #
    # WP-AK-08: állapotmódosító primitívek — mindegyik EXPLICIT               #
    # `confirm: bool` paramétert vár, alapértelmezés NÉLKÜL (a hívónak       #
    # mindig tudatosan kell megadnia), a `pico_bq_rpc.py`-n keresztül        #
    # változatlanul a `pico_bq_service.py` saját confirm=True kapujáig       #
    # jut el — ez a réteg csak a wire-hívást végzi, semmilyen pipeline-      #
    # döntést nem hoz (l. modul docstring, "PC = workflow master" határ).   #
    # ------------------------------------------------------------------ #
    def unseal(self, *, confirm: bool) -> None:
        self._call("unseal", {"confirm": confirm})

    def full_access(self, *, confirm: bool) -> None:
        self._call("full_access", {"confirm": confirm})

    def seal(self, *, confirm: bool) -> None:
        self._call("seal", {"confirm": confirm})

    def it_enable(self, *, confirm: bool) -> None:
        self._call("it_enable", {"confirm": confirm})

    def write_data_flash_block(
        self, subclass: int, block: int, data32: bytes, *, confirm: bool
    ) -> None:
        if len(data32) != 32:
            raise ValueError(
                f"PicoBQTransport.write_data_flash_block: data32 must be exactly "
                f"32 bytes, got {len(data32)}"
            )
        self._call("write_data_flash_block", {
            "subclass": subclass, "block": block,
            "data_hex": data32.hex(), "confirm": confirm,
        })

    def restore_df_snapshot(
        self, manifest: dict[str, Any], df_bytes: bytes, *, confirm: bool
    ) -> None:
        self._call("restore_df_snapshot", {
            "manifest": manifest, "df_bytes_hex": df_bytes.hex(), "confirm": confirm,
        })

    # ------------------------------------------------------------------ #
    # WP-AK-09 (BQ_GAUGE_CALIBRATION_ASSISTED): kalibrációs mód trigger-       #
    # parancsok + kalibrációs Data Flash olvasás/írás. Ugyanaz az elv, mint   #
    # a WP-AK-08 write-primitíveknél — ez a réteg NEM dönt semmilyen          #
    # időzítésről/sorrendről (ENTER_CAL~azonnal, BOARD_OFFSET~2s,             #
    # CC_OFFSET~16s, gain-stabilizálás~120s várakozás) — az a                #
    # `Prog/src/bq_gauge_calibration.py` workflow-réteg feladata.            #
    # ------------------------------------------------------------------ #
    def enter_cal(self, *, confirm: bool) -> None:
        self._call("enter_cal", {"confirm": confirm})

    def exit_cal(self, *, confirm: bool) -> None:
        self._call("exit_cal", {"confirm": confirm})

    def board_offset_cal(self, *, confirm: bool) -> None:
        self._call("board_offset_cal", {"confirm": confirm})

    def cc_offset_cal(self, *, confirm: bool) -> None:
        self._call("cc_offset_cal", {"confirm": confirm})

    def cc_offset_save(self, *, confirm: bool) -> None:
        self._call("cc_offset_save", {"confirm": confirm})

    def read_calibration_data(self) -> dict[str, Any]:
        return _validate_calibration_data_result(self._call("read_calibration_data", {}))

    def write_calibration_gain_delta(
        self, new_gain: float, new_delta: float, *, confirm: bool
    ) -> None:
        self._call("write_calibration_gain_delta", {
            "new_gain": new_gain, "new_delta": new_delta, "confirm": confirm,
        })

    def read_voltage_divider(self) -> int:
        return _validate_word_result(self._call("read_voltage_divider", {}), "read_voltage_divider")

    def write_voltage_divider(self, new_value_mV: int, *, confirm: bool) -> None:
        self._call("write_voltage_divider", {
            "new_value_mV": new_value_mV, "confirm": confirm,
        })

    # ------------------------------------------------------------------ #
    def _call(self, method: str, params: dict[str, Any]) -> Any:
        """[Important finding, 2. review-kör] KERET-SZINKRON-VESZTÉS ELLENI
        VÉDELEM: minden ÚJ kérés írása előtt eldobjuk a soros port bejövő
        pufferében esetlegesen ott ragadt, korábbi (már feladott/lejárt)
        kéréshez tartozó, KÉSVE megérkezett választ (`reset_input_buffer()`).
        Enélkül egy id-eltérés/timeout utáni retry úgy küldött volna új
        kérést, hogy a KÖVETKEZŐ olvasás egy régi, "egy lépéssel lemaradt"
        választ kaphatott volna — ami megint id-eltérést okoz, megint
        retry-t indít, és soha nem áll helyre (végtelen "egy lépéssel
        lemaradva" sodródás, amíg a max_retries el nem fogy — utána viszont
        a KÖVETKEZŐ, teljesen más `_call()` hívást is megmérgezte volna a
        pufferben maradt válasz). Fennmaradó, dokumentált korlát: ha egy
        késve érkező válasz PONT a reset és az új írás közötti pillanatban
        fut be, ez az elmélet szerint még mindig nem 100%-ban zárja ki az
        egyszeri versenyhelyzetet — valós hardveri időzítési adatok (HIL)
        nélkül ennél erősebb garancia itt nem adható."""
        if self.serial is None and self._port is None:
            # connect() sosem hívva -- ez NEM reconnect-helyzet, nincs
            # port-parameter, amivel próbálkozni lehetne.
            raise PicoBQTransportError(
                f"PicoBQTransport: nincs aktív soros kapcsolat (connect() nem "
                f"hívva) method={method!r}"
            )
        last_exc: Exception | None = None
        for _ in range(max(1, self.max_retries)):
            if self.serial is None:
                # MAX verifier 1. kör (2026-09-14, V1 lelet): korábban ez az
                # ág CSAK last_exc-et állított, sosem hívta MEG ÚJRA
                # _reopen_serial()-t -- egyetlen sikertelen reopen-kísérlet
                # után a transport VÉGLEGESEN "nincs aktív kapcsolat"
                # hibával állt, minden KÖVETKEZŐ, független _call()-hívásnál
                # is, mert a ciklus ELSŐ iterációja is csak ezt az ágat érte
                # el -- pontosan azt a viselkedést reprodukálva, amit a fix
                # megszüntetni hivatott volt (l. DEFECT_FAMILY_REGISTRY.yaml
                # DFAM-040). Most MINDEN iteráció (ciklus elején is, retry
                # közben is) újra megpróbálja a reopen-t, ha van port-info.
                self._reopen_serial()
            if self.serial is None:
                # MAX verifier 2. kör (2026-09-14, V3 lelet): az EREDETI
                # I/O-kivétel szövege (pl. a valós "WriteFile failed
                # (PermissionError...)" ok) az ELŐZŐ iteráció last_exc-ében
                # állt, de ez a sor FELÜLÍRTA volna azt egy generikus
                # "reconnect sikertelen" üzenettel -- a bq_diagnostics.csv
                # <mező>_error oszlopába (str(exc)-ként, nem __cause__-on
                # keresztül) emiatt sosem jutott volna el az eredeti ok. Az
                # előző last_exc szövegét most explicit belefűzzük.
                prior = f"; utolsó I/O hiba: {last_exc}" if last_exc is not None else ""
                last_exc = PicoBQTransportError(
                    f"PicoBQTransport: nincs aktív soros kapcsolat (reconnect "
                    f"sikertelen) method={method!r}{prior}"
                )
                continue
            req_id = self._next_id
            self._next_id += 1
            request_line = json.dumps({"id": req_id, "method": method, "params": params})
            try:
                self.serial.reset_input_buffer()
                self.serial.write((request_line + "\n").encode("utf-8"))
                raw = self.serial.readline()
            except Exception as exc:  # soros I/O hiba - retry-olható
                # MAX verifier 1. kör (V3 lelet, 2. körben pontosítva --
                # a round-1-es "explicit kivétel-lánc (from exc)" indoklás
                # TÉVES volt, l. lent a tényleges mechanizmust): az eredeti
                # OS-szintű kivétel szövege (pl. "WriteFile failed
                # (PermissionError...)") itt a last_exc szövegébe kerül. Ha
                # a KÖVETKEZŐ iteráció reopen-je is hibázna, a "reconnect
                # sikertelen" last_exc FELÜLÍRNÁ ezt a szöveget -- ez a fenti
                # "if self.serial is None" ágban most explicit bele van
                # fűzve a KÖVETKEZŐ üzenetbe (l. ott), NEM a __cause__-on
                # keresztül (a bq_diagnostics.csv-be kerülő str(exc) sosem
                # mutatná a __cause__-t). A __cause__ beállítása alább
                # MELLÉKES (hasznos, ha ez a last_exc végül nem íródik felül
                # és valós traceback-ben jelenik meg), nem a fő mechanizmus.
                last_exc = InstrumentTimeoutError(
                    f"PicoBQTransport: soros I/O hiba method={method!r}: {exc}"
                )
                last_exc.__cause__ = exc
                self._reopen_serial()
                continue

            if not raw:
                last_exc = InstrumentTimeoutError(
                    f"PicoBQTransport: nincs válasz (timeout) method={method!r}"
                )
                continue

            try:
                response = json.loads(raw.decode("utf-8").strip())
            except (ValueError, UnicodeDecodeError) as exc:
                last_exc = PicoBQTransportError(
                    f"PicoBQTransport: érvénytelen JSON válasz method={method!r}: {raw!r} ({exc})"
                )
                continue

            if not isinstance(response, dict) or response.get("id") != req_id:
                # SZÁNDÉKOSAN szigorú: egy id-eltérés keret-szinkron-vesztést
                # jelezhet - inkább hibázzon explicit, mint hogy egy másik
                # kéréshez tartozó (rossz) eredményt fogadjon el csendben.
                last_exc = PicoBQTransportError(
                    f"PicoBQTransport: válasz id-eltérés method={method!r}: "
                    f"kért={req_id}, kapott={response.get('id') if isinstance(response, dict) else response!r}"
                )
                continue

            if "ok" not in response:
                # Kör 20 HW20-08: egy hiányzó "ok" kulcs NEM egyenértékű egy
                # explicit ok=false-szal -- ez utóbbi a Pico RPC szerver
                # tényleges, feldolgozott hibaválasza (nem retry-olható,
                # azonnal raise). A hiányzó kulcs viszont épp azt az állapotot
                # jelzi, amikor a Pico az idle (friendly, ">>> " promptos)
                # REPL-ben van és NINCS RPC szerver futva -- a nyers
                # kérés-sort visszhangozza, ezért kaphat a hívó egy
                # id-egyező, de "ok" nélküli dict-et. (NEM a raw REPL --
                # az `OK<stdout>\x04<stderr>\x04` alakú választ ad, ami nem
                # dict-té parse-olódna.) MAX verifier (Kör 20 Batch E15)
                # empirikusan igazolta a valódi pico_bq_rpc.py firmware
                # ellen: az RPC szerver MINDEN handle_line() return-ágán
                # jelen van az "ok" kulcs, ez az ág kizárólag REPL-echo
                # esetén érhető el, ahol semmilyen RPC metódus nem futott --
                # ez a LEGINKÁBB retry-olható eset (a
                # felhasználó/session időközben elindíthatja a szervert),
                # ezért last_exc-et állítunk és continue-olunk, ugyanúgy, mint
                # a fenti JSON-parse/id-eltérés ágakon.
                last_exc = PicoBQTransportError(
                    f"PicoBQTransport: RPC válasz hiányzó 'ok' mező "
                    f"method={method!r}: {response!r}"
                )
                continue

            if not response.get("ok"):
                error = response.get("error")
                if not isinstance(error, dict):
                    error = {}
                raise PicoBQTransportError(
                    f"PicoBQTransport: Pico RPC hiba method={method!r} "
                    f"[{error.get('code')}]: {error.get('message')}"
                )

            return response.get("result")

        assert last_exc is not None
        raise last_exc
