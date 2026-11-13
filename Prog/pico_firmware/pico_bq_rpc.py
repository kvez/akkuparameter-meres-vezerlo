"""
pico_bq_rpc.py - Akkuteszter Pico-oldali JSON Lines RPC szerver, ami a
pico_bq_service.py funkcioit teszi elerhetove a PC-oldali kliensnek
(Prog/drivers/pico_bq_transport.py :: PicoBQTransport) a Pico USB CDC
soros portjan keresztul.

PROVENIENCIA / VENDOROLT MASOLAT
--------------------------------
Vendorolt masolat a TF1 repo `Prog/pico_bq_rpc.py`-bol (2026-09-12),
wire-protokoll-kompatibilis, a tenyleges RPC-dispatcher/metodustabla
logika VALTOZATLAN - csak a fejlec-dokumentacio lett Akkuteszter-
kontextusra igazitva (a TF1-panel-specifikus `pico_tf1_olvas.py`
mailbox-uzemmod es a Thonny-hasznalati mod emlitese ide nem alkalmazhato).
Frissiteskor a TF1 repoval MANUALISAN szinkronizalando, NINCS kozos import
a ket repo kozott.

Ez a fajl NEM a PC-n fut: az Akkuteszter GUI "Pico elokeszitese
BQ34Z110-hez" menupontja (Prog/src/pico_provisioning_service.py) tolti fel
a Pico MicroPython fajlrendszerere, a `pico_bq_service.py` es a `main.py`
melle. A `main.py` epiti fel a dispatchert es hivja a `serve_forever()`-t,
igy a szolgaltatas minden bekapcsolas/reset utan onalloan elindul.

ONALLO, ADDITIV modul - a pico_bq_service.py-t VALTOZATLANUL hagyja, es
semmilyen kozvetlen `import pico_bq_service`-t nem tartalmaz (a metodus-
tablat a `build_methods(bq)` parameterkent kapott modulra epiti). Egyszerre
csak EGY olyan program hasznalhatja a Pico USB CDC portjat, ami ezt a
protokollt beszeli - amig a `serve_forever()` fut, a port nem hasznalhato
mas celra (pl. interaktiv REPL-re); a provisioning-reteg ezert Ctrl-C-vel
szakitja meg a szervert, mielott ujra feltoltene a fajlokat.

Protokoll:
  Egy JSON objektum soronkent, mindket iranyban.
  Keres:  {"id": <int>, "method": "<nev>", "params": {...}}\\n
  Valasz (siker):  {"id": <int>, "ok": true, "result": <any>}\\n
  Valasz (hiba):   {"id": <int>, "ok": false,
                     "error": {"code": "<str>", "message": "<str>"}}\\n

Capability discovery: a "capabilities" metodus parameter nelkul hivhato,
visszaadja a protokoll-verziot, az elerheto metodusok teljes listajat, es
kulon a "write_methods" reszhalmazt (allapotmodosito muveletek) - a PC-oldali
kliens ezzel deríti fel, mit tamogat a csatlakoztatott Pico-firmware verzio,
mielott barmit hivna.

HATOKOR (WP-AK-08 "Controlled BQ write reteg" kiegeszitese a WP-AK-06 "Elso
szint - read-only" retegehez kepest): az allapotmodosito pico_bq_service
muveletek (UNSEAL/FULL_ACCESS/SEAL/IT_ENABLE/DF write/DF snapshot restore)
MOSTANTOL bekotve vannak, DE mindegyik VALTOZATLANUL a pico_bq_service.py
sajat confirm=True kapujat hasznalja - az RPC-reteg csupan tovabbitja a
hivo altal expliciten kuldott "confirm" parametert, NEM ad hozza sajat
alapertelmezest (a hianyzo "confirm" mezo params.get(...)-tal False-ra esik,
tehat egy hianyos/elgepett keres ugyanugy ValueError-t kap, mint egy
kozvetlen, confirm nelkuli pico_bq_service hivas). A PC-oldali "kotelezo
write pipeline" (CONFIRM->IDENTIFY->VERIFY DEVICE/FW->BACKUP->HASH->WRITE->
READBACK->COMPARE->CHECKSUM->STATUS VERIFY->PASS/FAIL) az Akkuteszter repo
Prog/src/bq_controlled_write.py-jaban el, NEM ezen a retegen - ez a modul
csak a dronton levo primitiveket exponalja, workflow-dontest nem hoz
("PC = workflow/safety master" architekturalis hatar).

I2C hardver-fuggetlen: a tenyleges pico_bq_service.py fuggvenyeket
parameterkent kapja (methods dict), igy host-oldalon (fake I2C-vel) is
teljesen tesztelheto, valos Pico/BQ HIL nelkul is. A write-metodusok
(UNSEAL/SEAL/DF-iras/kalibracios triggerek) EXTRA HIL-kockazatot hordoznak,
mert valos BQ Data Flash-be irnak, nem csak olvasnak.

Kalibracios kiegeszites: tovabbi allapotmodosito trigger-parancsok
(enter_cal/exit_cal/board_offset_cal/cc_offset_cal/cc_offset_save) es
kalibracios DF-iras metodusok (write_calibration_gain_delta/
write_voltage_divider) - mind a pico_bq_service.py sajat confirm=True
kapujat es sajat tartomany-ellenorzeset hasznaljak. Az idozitett varakozas
(BOARD_OFFSET ~2s, CC_OFFSET ~16s, gain-stabilizalas ~120s) NEM ezen a
retegen tortenik - a PC-oldali Akkuteszter workflow (Prog/src/
bq_gauge_calibration.py) orchesztralja.
"""
# mypy: ignore-errors
# ruff: noqa
# (MicroPython forras - a PC-oldali statikus ellenorzok szamara ez a fajl
#  adat, nem importalhato modul; l. a fenti provenienca-bekezdest.)
import sys
import json

PROTOCOL_VERSION = "1.0"


def _hex(data):
    """bytes/bytearray -> hex string (JSON nem tud nyers bajtot hordozni)."""
    return "".join("%02x" % b for b in bytearray(data))


def build_methods(bq):
    """A pico_bq_service modulra (vagy egy azt hordozo objektumra/fake-re)
    epiti a metodus-tablat - kulon fuggveny, hogy a dispatch logika ne
    tartalmazzon kozvetlen `import pico_bq_service`-t (host-tesztelhetoseg,
    ugyanaz a mintaja mint tests/host/test_pico_bq_service.py fake I2C-je)."""
    return {
        "identify": lambda params: {
            "device_type": bq.bq_device_type(),
            "firmware_version": bq.bq_fw_version(),
            "hardware_version": bq.bq_hw_version(),
        },
        # WP-AK-07 (Akkuteszter BQ Diagnostics GUI): a fenti "identify" egy
        # kombinalt, 3-mezos valaszt ad - a PicoBQTransport tobbi Control()
        # alparancsa (chem_id/control_status) viszont egyetlen erteket ad
        # vissza egy hivasra, ugyanabba a _CONTROL_SUBCOMMAND_METHOD
        # tablazatba illesztve (l. Akkuteszter Prog/drivers/pico_bq_transport.py).
        # Ez a harom uj, granularis metodus UGYANAZT a mar letezo
        # bq_device_type()/bq_fw_version()/bq_hw_version() fuggvenyt hivja,
        # csak egyenkent - additiv, az "identify" metodus valtozatlanul marad.
        "device_type": lambda params: bq.bq_device_type(),
        "firmware_version": lambda params: bq.bq_fw_version(),
        "hardware_version": lambda params: bq.bq_hw_version(),
        "chem_id": lambda params: bq.bq_chem_id(),
        "voltage_mV": lambda params: bq.bq_voltage(),
        "average_current_mA": lambda params: bq.bq_current(),
        "temperature_01K": lambda params: bq.bq_temperature(),
        "soc_percent": lambda params: bq.bq_soc(),
        "remaining_capacity_mAh": lambda params: bq.bq_remaining_capacity(),
        "full_charge_capacity_mAh": lambda params: bq.bq_full_charge_capacity(),
        "flags": lambda params: bq.bq_flags(),
        "control_status": lambda params: bq.bq_control_status(),
        "qmax_and_update_status": lambda params: bq.bq_learn_status(),
        # WP-AK-10 (SUP03-LEARN-002 gauge log) - "Avg I/P Last Run" NEM itt:
        # a mar meglevo generikus "data_flash_block" RPC-vel (SC82/blk0)
        # erheto el, PC-oldali dekodolassal (l. pico_bq_service.py bq_dod0()
        # utani megjegyzes).
        "cycle_count": lambda params: bq.bq_cycle_count(),
        "dod0": lambda params: bq.bq_dod0(),
        "ra_table_mohm": lambda params: bq.bq_ra_table(),
        "data_flash_block": lambda params: _hex(
            bq.bq_df_read(params["subclass"], params["block"])
        ),
        # 2026-09-12, CALMODE-anomalia kivizsgalasa (Akkuteszter cross-repo
        # kereses, felhasznaloi elmelet) - csak-olvaso, nem allapotmodosito
        # diagnosztikai eszkoz, l. pico_bq_service.py::bq_read_raw_block()
        # docstringje.
        "read_raw_block": lambda params: _hex(
            bq.bq_read_raw_block(params["addr"], params["length"])
        ),
        "selected_df_snapshot": lambda params: _selected_df_snapshot(bq, params),
        # WP-AK-08 (Controlled BQ write reteg) - allapotmodosito muveletek.
        # Mindegyik VALTOZATLANUL a pico_bq_service.py sajat confirm=True
        # kapujat hasznalja, az RPC-reteg csak tovabbitja a "confirm"
        # parametert (hianyzo mezo -> False, ugyanugy elutasitva, mint egy
        # confirm nelkuli Thonny-hivas). Roncsolo/allapotmodosito sorrendben
        # felsorolva, ahogy a Konszolidalt terv WP-AK-08 szakasza is teszi.
        "unseal": lambda params: _write_call(bq.bq_unseal, params),
        "full_access": lambda params: _write_call(bq.bq_full_access, params),
        "seal": lambda params: _write_call(bq.bq_seal, params),
        "it_enable": lambda params: _write_call(bq.bq_it_enable, params),
        "write_data_flash_block": lambda params: _write_data_flash_block(bq, params),
        "restore_df_snapshot": lambda params: _restore_df_snapshot(bq, params),
        # WP-AK-09 (BQ_GAUGE_CALIBRATION_ASSISTED) - kalibracios mod
        # trigger-parancsok, mind a pico_bq_service.py sajat confirm=True
        # kapujat hasznaljak, ugyanaz a minta mint fentebb.
        "enter_cal": lambda params: _write_call(bq.bq_enter_cal, params),
        "exit_cal": lambda params: _write_call(bq.bq_exit_cal, params),
        "board_offset_cal": lambda params: _write_call(bq.bq_board_offset_cal, params),
        "cc_offset_cal": lambda params: _write_call(bq.bq_cc_offset_cal, params),
        "cc_offset_save": lambda params: _write_call(bq.bq_cc_offset_save, params),
        "read_calibration_data": lambda params: bq.bq_read_calibration_data(),
        "write_calibration_gain_delta": lambda params: _write_calibration_gain_delta(bq, params),
        "read_voltage_divider": lambda params: bq.bq_read_voltage_divider(),
        "write_voltage_divider": lambda params: _write_voltage_divider(bq, params),
    }


def _selected_df_snapshot(bq, params):
    manifest, df_bytes = bq.bq_backup_df_snapshot(
        params["snapshot_id"], params["revision"]
    )
    return {"manifest": manifest, "df_bytes_hex": _hex(df_bytes)}


def _write_call(fn, params):
    """Kozos wrapper a parameter nelkuli, csak confirm-et igenylo
    allapotmodosito hivasokhoz (unseal/full_access/seal/it_enable). A
    hianyzo "confirm" mezo params.get(...)-tal explicit False-ra esik - ez
    UGYANAZT a ValueError-t valtja ki, mint egy confirm nelkuli kozvetlen
    pico_bq_service hivas (l. modul fejlec-komment)."""
    fn(confirm=params.get("confirm", False))
    return {"status": "ok"}


def _write_data_flash_block(bq, params):
    """params: subclass, block, data_hex (32 bajt hex-kodolva), confirm.
    A bq.bq_df_write() maga vegzi a block-select-et ES a beepitett
    readback-verify-t (kritikai review #P0, l. TF1_BQ_Service_Golden_Image_
    Mode.md 5.2) - ez a wrapper csak a hex<->bytes atalakitast vegzi."""
    data32 = bytes.fromhex(params["data_hex"])
    bq.bq_df_write(
        params["subclass"], params["block"], data32,
        confirm=params.get("confirm", False),
    )
    return {"status": "ok"}


def _write_calibration_gain_delta(bq, params):
    """params: new_gain, new_delta (float), confirm. A bq.
    bq_write_calibration_gain_delta() maga vegzi a tartomany-ellenorzest
    (Table 20 Min/Max) ES a bq_df_write() beepitett readback-verify-jat -
    ez a wrapper semmilyen sajat dontest nem hoz."""
    bq.bq_write_calibration_gain_delta(
        params["new_gain"], params["new_delta"],
        confirm=params.get("confirm", False),
    )
    return {"status": "ok"}


def _write_voltage_divider(bq, params):
    """params: new_value_mV (int), confirm."""
    bq.bq_write_voltage_divider(
        params["new_value_mV"], confirm=params.get("confirm", False),
    )
    return {"status": "ok"}


def _restore_df_snapshot(bq, params):
    """params: manifest (dict), df_bytes_hex, confirm. A bq.
    bq_restore_df_snapshot() maga ellenorzi a manifest["df_blocks"]<->
    df_bytes hossz-egyezest ES a manifest snapshot_sha256-ot IRAS ELOTT
    (l. pico_bq_service.py bq_restore_df_snapshot docstring, "Important
    finding, fix wave #1") - ez a wrapper csak a hex<->bytes atalakitast
    vegzi, semmilyen sajat dontest nem hoz."""
    df_bytes = bytes.fromhex(params["df_bytes_hex"])
    bq.bq_restore_df_snapshot(
        params["manifest"], df_bytes,
        confirm=params.get("confirm", False),
    )
    return {"status": "ok"}


# WP-AK-08: az allapotmodosito ("write") metodusok neve - a capabilities()
# ezt hasznalja a "write_methods" reszhalmaz jelzesere, hogy a PC-oldali
# kliens negotiate()-kor fel tudja merni, tamogatja-e a csatlakoztatott
# Pico-firmware a kontrollalt write-pipeline-t, mielott barmit hivna.
_WRITE_METHOD_NAMES = frozenset({
    "unseal", "full_access", "seal", "it_enable",
    "write_data_flash_block", "restore_df_snapshot",
    # WP-AK-09
    "enter_cal", "exit_cal", "board_offset_cal", "cc_offset_cal",
    "cc_offset_save", "write_calibration_gain_delta", "write_voltage_divider",
})


class RpcDispatcher:
    """Tiszta (I/O-mentes) keres->valasz logika - kulon a soros-port hurok-tol,
    hogy host-oldalon egyszeruen, string be/ki parokkal tesztelheto legyen."""

    def __init__(self, methods):
        self._methods = dict(methods)
        self._methods.setdefault("capabilities", self._capabilities)

    def _capabilities(self, params):
        write_methods = sorted(
            name for name in self._methods if name in _WRITE_METHOD_NAMES
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "methods": sorted(self._methods.keys()),
            # WP-AK-08: a modul mostantol allapotmodosito metodusokat is
            # exponal - "read_only" ezert dinamikusan szamitott (nem a
            # regi, WP-AK-06-os fix True), a "write_methods" pedig explicit
            # listazza, MELYIK metodusok igenyelnek confirm=True-t, hogy a
            # PC-oldali kliens negotiate()-kor eldonthesse, tamogatja-e a
            # csatlakoztatott firmware a kontrollalt write-pipeline-t.
            "read_only": len(write_methods) == 0,
            "write_methods": write_methods,
        }

    def handle_line(self, line):
        """Egy nyers JSON-sort dolgoz fel, egy nyers JSON-valasz-sort ad
        vissza (uresen vegzodo, \\n nelkul - a hivo felelossege a sortores)."""
        try:
            request = json.loads(line)
        except ValueError as exc:
            return json.dumps({
                "id": None, "ok": False,
                "error": {"code": "INVALID_JSON", "message": str(exc)},
            })

        req_id = request.get("id") if isinstance(request, dict) else None
        method_name = request.get("method") if isinstance(request, dict) else None
        params = request.get("params", {}) if isinstance(request, dict) else {}

        if method_name is None:
            return json.dumps({
                "id": req_id, "ok": False,
                "error": {"code": "MISSING_METHOD", "message": "request has no 'method'"},
            })

        handler = self._methods.get(method_name)
        if handler is None:
            return json.dumps({
                "id": req_id, "ok": False,
                "error": {
                    "code": "UNKNOWN_METHOD",
                    "message": "unknown method: %s" % method_name,
                },
            })

        try:
            result = handler(params)
        except Exception as exc:  # noqa: BLE001 - RPC hibavalasszá kell alakitani, nem elszallni
            return json.dumps({
                "id": req_id, "ok": False,
                "error": {"code": "METHOD_ERROR", "message": str(exc)},
            })

        return json.dumps({"id": req_id, "ok": True, "result": result})


def _default_write(text):
    sys.stdout.write(text)
    sys.stdout.write("\n")


def serve_forever(dispatcher, readline=None, write_line=None):
    """Blokkolo fo ciklus: soronkent olvas, dispatch-el, soronkent valaszol.
    readline/write_line injektalhato (alapertelmezes sys.stdin.readline/
    sys.stdout) - host-teszthez konnyen helyettesitheto, valos Picon a USB
    CDC stdin/stdout parost hasznalja (ugyanaz a csatorna-mintaja, mint
    Prog/pico_tf1_olvas.py:391-406 mailbox-hurokja)."""
    if readline is None:
        readline = sys.stdin.readline
    if write_line is None:
        write_line = _default_write

    while True:
        raw_line = readline()
        if raw_line == "":
            # Ugyanaz a konvencio, mint Python fajl-objektumok readline()-ja:
            # ures string = folyam vege (EOF) - a hurok leall. Valos Picon
            # ez a gyakorlatban sosem tortenik meg (a USB CDC stream nem zar
            # be), csak a host-tesztekben ad determinisztikus leallitasi
            # pontot egy veges bemeneti sorozathoz.
            break
        line = raw_line.strip()
        if not line:
            continue
        write_line(dispatcher.handle_line(line))
