"""
pico_bq_service.py - Akkuteszter Pico-oldali BQ34Z110 I2C szolgaltatas.

PROVENIENCIA / VENDOROLT MASOLAT
--------------------------------
Vendorolt masolat a TF1 repo `Prog/pico_bq_service.py`-bol (2026-09-12),
wire-protokoll-kompatibilis, a tenyleges I2C-driver-logika VALTOZATLAN -
csak a fejlec-dokumentacio lett Akkuteszter-kontextusra igazitva.
Frissiteskor a TF1 repoval MANUALISAN szinkronizalando, NINCS kozos import
a ket repo kozott (ket kulon Git repo; a kontraktus a drot-protokoll, nem
egy megosztott Python-modul).

Ez a fajl NEM a PC-n fut: az Akkuteszter GUI "Pico elokeszitese
BQ34Z110-hez" menupontja (Prog/src/pico_provisioning_service.py) tolti fel
a Raspberry Pi Pico MicroPython fajlrendszerere, ezert MicroPython-
kompatibilis szintaxist es csak MicroPython-ban letezo modulokat
(machine/time/json/hashlib/binascii) hasznal.

BEKOTES (Akkuteszter pad-es elrendezes)
---------------------------------------
  Pico GP4 (fizikai 6. lab)  ──── BQ34Z110 P3/SDA (14. lab)
  Pico GP5 (fizikai 7. lab)  ──── BQ34Z110 P4/SCL (13. lab)
  Pico GND (pl. 38. lab)     ──── BQ34Z110 VSS (8. lab)  [kozos referencia]
  SDA es SCL egyarant felhuzva (pull-up) a Pico 3V3(OUT) sinjere.

A BQ34Z110 SDA/SCL nyitott nyeloju (open-drain), a felhuzas ezert
KOTELEZO. Ertek: a bevalt TF1 NYAK 4,7 kOhm-ot hasznal; a TI adatlap
(bq34z110, Table 1 External Pin Functions) 10 kOhm-ot ad "typical"-kent -
mindketto ervenyes, a kisebb ertek nagyobb buszkapacitasnal biztonsagosabb.
A 3,3 V-os felhuzas a TI adatlap szerint megengedett (VIH(OD) tartomany
1,2...6 V, az open-drain labak abszolut maximuma 5,5 V).

A BQ34Z110 SAJAT tapellatasa (REGIN/REG25/BAT/SRP/SRN bekotes) EZ A FAJL
NEM ALLITJA es nem is tudja ellenorizni - az a gauge-panel sajat
kapcsolasi rajza szerint tortenik (tipikusan az akkucsomagbol). NE tapláld
a gauge-panelt a Pico 3V3 kimenetrol anelkul, hogy a panel rajzabol
igazoltad volna, hogy ez megengedett.

HATOKOR ES KOCKAZAT (Akkuteszter kontextus)
-------------------------------------------
Ez egy ONALLO, pad-es/asztali Pico <-> BQ34Z110 osszekottetes: nincs TF1
panel, nincs ATmega busz-atadas, nincs "normal vedelem letiltva" allapot.
A TF1-repobeli eredeti fejlec szerviz-modu, panel-specifikus
figyelmeztetese (melykisules-/hovedelem letiltva, "soha nem kerulhet
uzembe/terepre") EBBEN a kontextusban NEM ALKALMAZHATO, ezert szandekosan
NEM szerepel itt - l. a fenti provenienca-bekezdest.

Ami viszont ITT IS ervenyes:
- Ez a modul MAR IMPORTKOR (nem csak fuggvenyhivaskor) lefoglalja az I2C0
  periferiat (`_i2c = I2C(0, ...)` lent). Egyszerre csak EGY olyan program
  futhat a Picon, ami az I2C0-t hasznalja.
- NE koss mas I2C-eszkozt ugyanarra a buszra: a driver a BQ34Z110 fix
  0x55 cimet hasznalja, cimutkozes vagy idegen slave csendes
  adatserulest/idotullepest okozhat.
- A `confirm=True` kapuval vedett hivasok (UNSEAL/FULL_ACCESS/SEAL/
  IT_ENABLE/Data Flash iras/kalibracios trigger) VALOS, nem visszavonhato
  allapotvaltoztatast vegeznek a gauge-on. A workflow/safety master
  MINDIG a PC-oldal (Akkuteszter), ez a modul csak primitiveket ad.

Minden BQ-parancs/kulcs forrasolt a TF1 repo Prog/TF1_basic.c-bol vagy a
Leirasok/TUDASBAZIS.txt-bol (TI bq34z110 dokumentacio) - l. a jelen fajl
fuggvenyenkenti kommentjeit.

MEGJEGYZES a fajl TORZSEBEN levo hivatkozasokrol: a fuggveny-szintu
kommentek `Prog/...`-kezdetu utvonalai (pl. Prog/TF1_basic.c,
Prog/pico_tf1_olvas.py, tf1_service_bq.c) es a WP-AK-xx / BLK-xxx
azonositoi VALTOZATLANUL a TF1 repora mutatnak - szandekosan nem lettek
atirva, hogy a TF1 repoval valo manualis diff/szinkron KIZAROLAG ezt a
fejlecet mutassa elteresnek. Ezek forras-hivatkozasok, nem futasi
elofeltetelek: a driver-logika onmagaban, TF1 panel nelkul is helyes.
"""
# mypy: ignore-errors
# ruff: noqa
# (MicroPython forras - a `machine` modul csak a Picon letezik, ezert a
#  PC-oldali statikus ellenorzok szamara ez a fajl adat, nem importalhato
#  modul; l. a fenti provenienca-bekezdest.)
from machine import I2C, Pin
import time
import json
import hashlib
import binascii

# ── I2C alapok (Prog/pico_tf1_olvas.py:62 mintajat kovetve, azonos
#    GP4/GP5 labkiosztassal - ott csak pin-funkcio-konfiguraciora hasznalt
#    "_master_temp" objektum, itt tenyleges, folyamatos master hasznalat) ──
_i2c = I2C(0, sda=Pin(4), scl=Pin(5), freq=100000)


def _sha256_hex(data):
    """hashlib.sha256(...).hexdigest() nem letezik MicroPython-ban (csak
    update()/digest() van implementalva) - ez a helper CPython ES
    MicroPython alatt egyarant mukodik, mert binascii.hexlify mindkettoben
    letezik. Minden hashlib.sha256(...).hexdigest() hivast ezen keresztul
    kell futtatni (review, fix wave #1)."""
    return binascii.hexlify(hashlib.sha256(data).digest()).decode()

# BQ_ADDR (Prog/TF1_basic.c:234)
BQ_I2C_ADDR = 0x55

# Control()/MAC regiszter cim - 2 bajt, LITTLE-ENDIAN a vezetéken
# (Prog/TF1_basic.c:1940-1943: "lo word elore, hi word utana").
_BQ_CONTROL_REG = 0x00

# Standard parancsok (Prog/TF1_basic.c:255-268)
BQ_CMD_SOC = 0x02

# Tovabbi standard parancsok (Prog/TF1_basic.c:256-259) - mind 2 bajt,
# LITTLE-ENDIAN a vezetéken (Prog/TF1_basic.c:1940-1943 konvencio, ua.
# mint bq_control()-nal).
_BQ_CMD_VOLT  = 0x08  # Voltage(), mV, unsigned
_BQ_CMD_CURR  = 0x0A  # AverageCurrent(), mA, SIGNED (elojeles ket. komplemens)
_BQ_CMD_TEMP  = 0x0C  # Temperature(), 0.1 K egyseg, unsigned
_BQ_CMD_FLAGS = 0x0E  # Flags(), nyers uint16 bitmaszk (bitenkenti dekodolas kivul esik)

# WP-AK-06 (Akkuteszter kereszthivatkozas): RemainingCapacity()/FullChargeCapacity()
# a TF1_basic.c-ben NEM hasznalt standard parancsok (a fo firmware nem olvassa
# oket) - forras helyette Leirasok/TUDASBAZIS.txt "Table 2. Standard Commands"
# (kb-search-sel igazolva): RM=0x04/0x05, FCC=0x06/0x07, mindketto mAh,
# LITTLE-ENDIAN, elojel nelkuli, ugyanaz a bekotesi minta mint _BQ_CMD_VOLT.
_BQ_CMD_REMAINING_CAP    = 0x04  # RemainingCapacity(), mAh, unsigned
_BQ_CMD_FULL_CHARGE_CAP  = 0x06  # FullChargeCapacity(), mAh, unsigned

# WP-AK-10 (Traditional learning gauge log, SUP03-LEARN-002): tovabbi
# standard parancsok - Leirasok/TUDASBAZIS.txt "Table 2. Standard Commands"
# / 8912-8928. sor (kb-search-sel igazolva, TF1_basic.c nem hasznalja
# egyiket sem).
_BQ_CMD_CYCLE_COUNT     = 0x2C  # CycleCount(), unsigned, 0-65535
_BQ_CMD_DOD0            = 0x36  # DOD0(), unsigned - legutobbi OCV-mereskori kisultsegi melyseg

# Control() subcommand-ok (ld. TF1_BQ_Service_Golden_Image_Mode.md 5.1
# tablazata - forras: Prog/TF1_basic.c es Leirasok/TUDASBAZIS.txt)
_BQ_CTRL_CONTROL_STATUS = 0x0000  # TF1_basic.c:2289/3195/3604 mar hasznalja
_BQ_CTRL_DEVICE_TYPE = 0x0001
_BQ_CTRL_FW_VERSION  = 0x0002
_BQ_CTRL_HW_VERSION  = 0x0003
_BQ_CTRL_CHEM_ID     = 0x0008
_BQ_CTRL_SEAL        = 0x0020
_BQ_CTRL_IT_ENABLE   = 0x0021

# WP-AK-09 (Akkuteszter BQ_GAUGE_CALIBRATION_ASSISTED): tovabbi Control()
# subcommand-ok - forras: Leirasok/TUDASBAZIS.txt "Table 3. Control()
# Subcommands" (9741-9767. sor) ES Prog/TF1_basic.c:2822-2844 (bq_cal_start()/
# bq_cal_offset_done(), UGYANEZEKET a kodokat hasznalja, mar production-ban
# bizonyitottan mukodo CAL:BQ:CC field-kalibraciohoz). CAL_ENABLE (0x002D,
# "Toggles calibration mode") SZANDEKOSAN NEM exponalt - allapot-fuggo
# TOGGLE, nem explicit irany (ellentetben ENTER_CAL/EXIT_CAL-lal), ugyanaz
# az elv mint a projekt tobbi "ne ismetelj vakon bizonytalan allapotu
# parancsot" szabalya (Akkuteszter CLAUDE.md 10.5).
_BQ_CTRL_ENTER_CAL      = 0x0081
_BQ_CTRL_EXIT_CAL       = 0x0080
_BQ_CTRL_BOARD_OFFSET   = 0x0009  # BOARD_OFFSET_CAL - kulso (PCB/shunt) offset, ~2s
_BQ_CTRL_CC_OFFSET      = 0x000A  # CC_OFFSET_CAL - IC belso CC offset, ~16s
_BQ_CTRL_CC_OFFSET_SAVE = 0x000B  # mert offset Data Flash-be mentese

# WP-AK-06: Ra Table (Cell0) Data Flash cime - Leirasok/TUDASBAZIS.txt
# "Ra Tables" osztaly, subclass 88, offset 0-31 ("Cell0 R_a Table", 15 db
# I2 [big-endian, elojeles] ertek mOhm-ban, kb-search-sel most eloszor
# forrasolva - a korabbi (2026-08-19) spec/terv ezt meg nem talalta, ld.
# tf1_bq_service_golden_image_mode memoria). Subclass 89 = "xCell0 R_a
# Table" (alternativ profil) - ezt a szintet NEM exponaljuk, csak Cell0-t
# (WP-AK-06 elso szintje csak a Cell0 profilt kovetel meg).
_BQ_SC_RA_TABLE_CELL0 = 88

# WP-AK-09: Calibration osztaly, "Data" subclass - Leirasok/TUDASBAZIS.txt
# Table 20 (65263-65367. sor), UGYANAZ a cim mint Prog/TF1_basic.c
# BQ_SC_CALIB=104 (:2009). Off 0: CC Gain [F4 Xemics], Off 4: CC Delta
# [F4 Xemics], Off 8: CC Offset [I2 elojeles], Off 10: Board Offset
# [I1 elojeles], Off 14: Voltage Divider [U2].
_BQ_SC_CALIBRATION_DATA = 104

# UNSEAL/FULL ACCESS kulcsok (Prog/TF1_basic.c:2013-2018)
BQ_UNSEAL_KEY1 = 0x0414
BQ_UNSEAL_KEY2 = 0x3672
BQ_FA_KEY      = 0xFFFF


def bq_control(subcmd):
    """Control() subcommand iras + visszaolvasas. subcmd: 0..0xFFFF.
    A ket bajt LITTLE-ENDIAN sorrendben kerul a vezetekre (lo, hi),
    ugyanugy visszaolvasva - Prog/TF1_basic.c:1947-1986 mintaja."""
    payload = bytes([subcmd & 0xFF, (subcmd >> 8) & 0xFF])
    _i2c.writeto_mem(BQ_I2C_ADDR, _BQ_CONTROL_REG, payload)
    time.sleep_ms(2)
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_CONTROL_REG, 2)
    return result[0] | (result[1] << 8)


def bq_control_write_only(subcmd):
    """Control() subcommand kuldese VISSZAOLVASAS NELKUL - Prog/TF1_basic.c
    bq_control_cmd() mintaja (kb. 1913-1929. sor). Azoknal a parancsoknal
    hasznalando, ahol az ALAPBAN mukodo iras utan a visszaolvasas
    megbizhatatlan/timeoutol - CC_OFFSET_SAVE=0x000B eseten ez Akkuteszter-
    oldali HIL-teszttel determinisztikusan igazolva (2026-09-12,
    [Errno 110] ETIMEDOUT az ELSO probalkozas VISSZAOLVASASAN, minden
    egyeb elofeltetel -- BCA/CCA torolve, helyes sorrend -- rendben volt;
    l. Akkuteszter CLAIM_LEDGER.yaml BQ-CAL-CCOFFSETSAVE-001,
    TF1_BLOCKED_VALIDATION_BACKLOG.md BLK-026). A generikus bq_control()
    3x probalna visszaolvasni ilyen parancsra -> minden probalkozas
    timeout + felesleges busz-hiba, majd az Akkuteszter-oldali retry egy
    MASODIK, VALOS Data Flash commit-ot kockaztat. Ez a fuggveny magat a
    timeoutot szunteti meg a gyokerenel, nem csak elrejti egy retry
    moge."""
    payload = bytes([subcmd & 0xFF, (subcmd >> 8) & 0xFF])
    _i2c.writeto_mem(BQ_I2C_ADDR, _BQ_CONTROL_REG, payload)
    time.sleep_ms(2)


def bq_device_type():
    """Control(DEVICE_TYPE) - varhato valasz 0x0541 (Leirasok/TUDASBAZIS.txt)."""
    return bq_control(_BQ_CTRL_DEVICE_TYPE)


def bq_fw_version():
    return bq_control(_BQ_CTRL_FW_VERSION)


def bq_hw_version():
    return bq_control(_BQ_CTRL_HW_VERSION)


def bq_chem_id():
    """Control(CHEM_ID) readback - varhato ertek 0x0800 (Prog/TF1_basic.c:2066 BQ_CHEM_ID)."""
    return bq_control(_BQ_CTRL_CHEM_ID)


def bq_soc():
    """StateOfCharge() standard parancs (Prog/TF1_basic.c:255 BQ_CMD_SOC),
    1 bajt, 0-100%."""
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, BQ_CMD_SOC, 1)
    return result[0]


def bq_voltage():
    """Voltage() standard parancs (Prog/TF1_basic.c:257 BQ_CMD_VOLT),
    2 bajt, little-endian, mV, elojel nelkuli (spec 5.2)."""
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_CMD_VOLT, 2)
    return result[0] | (result[1] << 8)


def bq_current():
    """AverageCurrent() standard parancs (Prog/TF1_basic.c:258 BQ_CMD_CURR),
    2 bajt, little-endian, mA, ELOJELES (ket. komplemens int16 - negativ
    ertek = kisules) (spec 5.2)."""
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_CMD_CURR, 2)
    raw = result[0] | (result[1] << 8)
    if raw & 0x8000:
        raw -= 0x10000
    return raw


def bq_temperature():
    """Temperature() standard parancs (Prog/TF1_basic.c:259 BQ_CMD_TEMP),
    2 bajt, little-endian, 0.1 K egyseg, elojel nelkuli (spec 5.2)."""
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_CMD_TEMP, 2)
    return result[0] | (result[1] << 8)


def bq_flags():
    """Flags() standard parancs (Prog/TF1_basic.c:256 BQ_CMD_FLAGS), 2 bajt,
    little-endian - nyers uint16 bitmaszk, bitenkenti dekodolas nem resze
    ennek a fuggvenynek (spec 5.2)."""
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_CMD_FLAGS, 2)
    return result[0] | (result[1] << 8)


def bq_remaining_capacity():
    """WP-AK-06: RemainingCapacity() standard parancs, 2 bajt, little-endian,
    mAh, elojel nelkuli. Forras: Leirasok/TUDASBAZIS.txt "Table 2. Standard
    Commands" (TF1_basic.c nem hasznalja, a fo firmware nem olvassa)."""
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_CMD_REMAINING_CAP, 2)
    return result[0] | (result[1] << 8)


def bq_full_charge_capacity():
    """WP-AK-06: FullChargeCapacity() standard parancs, 2 bajt, little-endian,
    mAh, elojel nelkuli. Forras: Leirasok/TUDASBAZIS.txt "Table 2. Standard
    Commands" (TF1_basic.c nem hasznalja, a fo firmware nem olvassa)."""
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_CMD_FULL_CHARGE_CAP, 2)
    return result[0] | (result[1] << 8)


def bq_control_status():
    """WP-AK-06: Control(CONTROL_STATUS=0x0000) readback - ugyanaz a hivas,
    amit a fo firmware mar hasznal (Prog/TF1_basic.c:2289/3195/3604,
    QEN=bit0/VOK=bit1/SS=bit13)."""
    return bq_control(_BQ_CTRL_CONTROL_STATUS)


def bq_cycle_count():
    """WP-AK-10 (SUP03-LEARN-002 gauge log): CycleCount() standard parancs,
    2 bajt, little-endian, elojel nelkuli, 0-65535 (TUDASBAZIS.txt 8912-
    8914. sor, TF1_basic.c nem hasznalja)."""
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_CMD_CYCLE_COUNT, 2)
    return result[0] | (result[1] << 8)


def bq_dod0():
    """WP-AK-10 (SUP03-LEARN-002 gauge log): DOD0() standard parancs, 2 bajt,
    little-endian, elojel nelkuli - "depth of discharge during the most
    recent OCV reading" (TUDASBAZIS.txt 8927-8928. sor, TF1_basic.c nem
    hasznalja)."""
    result = _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_CMD_DOD0, 2)
    return result[0] | (result[1] << 8)


_BQ_RAW_BLOCK_MAX_LEN = 32  # ugyanaz a felso korlat, mint a DF-blokk merete


def bq_read_raw_block(addr, length):
    """CSAK-OLVASO, diagnosztikai celu, tetszoleges cimtartomany-olvasas
    (I2C readfrom_mem passthrough). Akkuteszter cross-repo kereses (2026-
    09-12, CALMODE-anomalia kivizsgalasa, felhasznaloi elmelet): a 0x79-
    0x7F cimtartomany NORMAL modban DeviceChemistryLength/DeviceChemistry
    statikus adatot mutat, egyes TI gauge-csaladok alkalmazasi jegyzetei
    szerint viszont KALIBRACIOS modban ugyanez a tartomany Analog
    Conversion Counter/Analog Current/Analog Voltage/Analog Temperature
    ELO ADC-adatra valt at -- ha ez a BQ34Z110-re is igaz, ez fuggetlen,
    kozvetlen bizonyitek lenne arra, hogy a chip belsoleg kalibracios
    modban van, FUGGETLENUL attol, hogy a CONTROL_STATUS CALMODE bitje
    (0x1000) ezt tukrozi-e. Szandekosan NEM ir semmit, NEM valasztja ki
    a Data Flash subclass-t, NEM erinti a SEALED-elofeltetelt -- pusztán
    egy nyers, hatarolt hosszu I2C olvasas, ugyanaz a mintazat, mint a
    tobbi bq_voltage()/bq_current()-fele standard-parancs olvaso."""
    if not (1 <= length <= _BQ_RAW_BLOCK_MAX_LEN):
        raise ValueError(
            "bq_read_raw_block: length 1..%d kozott kell legyen, kaptunk: %r"
            % (_BQ_RAW_BLOCK_MAX_LEN, length)
        )
    return _i2c.readfrom_mem(BQ_I2C_ADDR, addr, length)


# MEGJEGYZES (WP-AK-10): "Avg I Last Run"/"Avg P Last Run" (SUP03-LEARN-002)
# SZANDEKOSAN NINCS itt dedikalt Pico-oldali fuggveny/RPC-metodus - a
# Data Flash "State" subclass (82) blokk 0 offset 7-10 mezoi, amit a MAR
# MEGLEVO generikus "data_flash_block" RPC (WP-AK-06 ota) es a PC-oldali
# bq_df_read(82,0)/data_flash_block(82,0) hivas mar elér - a dekodolas az
# Akkuteszter Prog/drivers/device_bq34z110.py-ban tortenik, UGYANAZZAL a
# mintaval, mint read_qmax()/read_update_status()/read_ra_table() (a
# malformed-reply-vedelemmel egyutt, WP-AK-07 kapu-szabaly). Ne adj hozza
# uj dedikalt RPC-t egy mar a generikus DF-block olvasassal elerheto
# mezohoz - csak azok kapnak dedikalt fuggvenyt, amik STANDARD PARANCSKENT
# (nem DF-kent) erhetok el, mint cycle_count()/dod0() fent.



def bq_ra_table():
    """WP-AK-06: Cell0 R_a Table - Data Flash "Ra Tables" osztaly, subclass 88,
    block 0, 15 db I2 (big-endian, elojeles, Prog/TF1_basic.c:2149-2159
    mintaja szerinti DF-mezo-dekodolas) ertek mOhm-ban, normalizalt SOC-
    racsponthoz (Leirasok/TUDASBAZIS.txt "Ra Tables"/"Cell0 R_a Table" -
    kb-search-sel most eloszor forrasolva, l. modul valtozasnaplo/commit).

    [CRITICAL finding, 2. review-kor] a 32 bajtos blokk ELSO 2 bajtja NEM
    resistance-ertek - a "Cell0 R_a flag" (2 bajtos statusz-flag: LSB az
    enabled/disabled allapot, MSB a resistance-adat frissitesi allapota),
    ld. TUDASBAZIS.txt 71955-71978. sor ("At the beginning of each of the
    two subclasses (profiles) is a flag called Cell0 R_a flag... Each flag
    has two bytes"). A tenyleges 15 R_a ertek a 2-31 bajt-tartomanyban van
    (2+15*2=32). Az elso implementacio ezt a flag-et tevesen R_a0-kent
    dekodolta volna, es az utolso (R_a14, legmagasabb SOC-pontossagu)
    erteket csendben elhagyta volna.

    ELOFELTETEL: UNSEALED mod (ugyanaz, mint bq_learn_status()-nal - a DF
    csak UNSEALED-ben olvashato megbizhatoan, TF1_basic.c:3605)."""
    block = bq_df_read(_BQ_SC_RA_TABLE_CELL0, 0)
    values = []
    for i in range(2, 32, 2):
        raw = (block[i] << 8) | block[i + 1]
        if raw & 0x8000:
            raw -= 0x10000
        values.append(raw)
    return values


def bq_unseal(confirm=False):
    """SEALED -> UNSEALED. Ket Control() hivas az Unseal kulcsokkal
    (Prog/TF1_basic.c:2265-2274 mintaja)."""
    if not confirm:
        raise ValueError("bq_unseal() requires confirm=True")
    bq_control(BQ_UNSEAL_KEY1)
    bq_control(BQ_UNSEAL_KEY2)


def bq_full_access(confirm=False):
    """UNSEALED -> FULL ACCESS. Ket Control() hivas ugyanazzal a FA
    kulccsal (Prog/TF1_basic.c:2300-2317 mintaja)."""
    if not confirm:
        raise ValueError("bq_full_access() requires confirm=True")
    bq_control(BQ_FA_KEY)
    bq_control(BQ_FA_KEY)


def bq_seal(confirm=False):
    """UNSEALED/FA -> SEALED. [review #P0] policy-valtas: korabban kivetel
    volt (nem igenyelt confirm-ot), a kritikai review egysegesen, kivetel
    nelkul minden allapotmodosito hivasra megkoveteli a confirm=True-t
    (spec 5.3, frissitve)."""
    if not confirm:
        raise ValueError("bq_seal() requires confirm=True")
    bq_control(_BQ_CTRL_SEAL)


def bq_it_enable(confirm=False):
    """[review #P0] Hianyzott a korabbi tervezet publikus API-jabol, pedig
    a kulcs mar forrasolt volt. Control(IT_ENABLE=0x0021) -
    Prog/TF1_basic.c:3367, TUDASBAZIS.txt Step 7."""
    if not confirm:
        raise ValueError("bq_it_enable() requires confirm=True")
    bq_control(_BQ_CTRL_IT_ENABLE)


# ── WP-AK-09: Kalibracios mod trigger-parancsok ─────────────────────────
# Mindegyik EGYSZERU, allapotmodosito Control() trigger - a tenyleges
# mereses/idozitett varakozas/DF-iras dontese a HIVO (PC-oldal, Akkuteszter
# BQ_GAUGE_CALIBRATION_ASSISTED workflow) felelossege, UGYANAZ a "PC =
# workflow/safety master" elv, mint a WP-AK-08 controlled-write pipeline-nal
# (bq_controlled_write.py orchesztral, a Pico csak primitiveket ad). A Pico
# oldal NEM blokkol a ~2s/~16s/~120s fizikai kalibracios idokre - hosszu
# blokkolo varakozas egy soros RPC hivasban HIL nelkul is kockazatos lenne
# (ugyanaz a minta, mint a WP-AK-04 "kulso utemezo + fix hardveres
# idoablak" dontese, l. Akkuteszter memoria feedback_*.md).
def bq_enter_cal(confirm=False):
    """Control(ENTER_CAL=0x0081) - belepes kalibracios modba (TUDASBAZIS.txt
    9761-9763. sor, Prog/TF1_basic.c:2822 UGYANEZT a kodot hasznalja)."""
    if not confirm:
        raise ValueError("bq_enter_cal() requires confirm=True")
    bq_control(_BQ_CTRL_ENTER_CAL)


def bq_exit_cal(confirm=False):
    """Control(EXIT_CAL=0x0080) - kilepes kalibracios modbol (TUDASBAZIS.txt
    9756-9759. sor, Prog/TF1_basic.c:2766/2873/2989 UGYANEZT a kodot
    hasznalja minden kalibracios kilepesi/hiba-agon)."""
    if not confirm:
        raise ValueError("bq_exit_cal() requires confirm=True")
    bq_control(_BQ_CTRL_EXIT_CAL)


def bq_board_offset_cal(confirm=False):
    """Control(BOARD_OFFSET=0x0009) - PCB/shunt kulso offset merese inditva
    (TUDASBAZIS.txt 9838-9839. sor: "During board offset calibration the
    [BCA] bit is set" - a hivo bq_control_status()-szal figyelheti a BCA
    bitet, vagy Prog/TF1_basic.c:2830 mintaja szerint fix ~2s varakozassal
    dolgozhat). ELOFELTETEL (forras: TF1_basic.c:2806-2820 kommentje): a
    kalibralando BQ34Z110-nek 0A akkuaramot kell latnia - a Pico-oldali
    szerviz mod architekturajaban (l. modul fejlec) ez a fizikai
    golden-image/gyartasi kalibracios workflow-ban azert teljesul
    automatikusan, mert az akku meg NINCS csatlakoztatva (SLUA665 "Going to
    Production" 8 lepeses folyamataban a kalibracio a Step 5 "Connect the
    cells" ELOTT tortenik) - a hivonak (PC-oldal) explicit ellenoriznie/
    naploznia kell ezt az elofeltetelt a workflow-ban, a Pico ezt NEM tudja
    ellenorizni (nincs rele-vezerlese, ellentetben az ATmega normal
    firmware-jevel)."""
    if not confirm:
        raise ValueError("bq_board_offset_cal() requires confirm=True")
    bq_control(_BQ_CTRL_BOARD_OFFSET)


def bq_cc_offset_cal(confirm=False):
    """Control(CC_OFFSET=0x000A) - IC belso Coulomb Counter offset merese
    inditva (TUDASBAZIS.txt 9840-9841. sor: "During calibration the [CCA]
    bit is set" - a hivo bq_control_status()-szal figyelheti a CCA bitet).
    Ugyanaz a 0A-akku elofeltetel, mint bq_board_offset_cal()-nal."""
    if not confirm:
        raise ValueError("bq_cc_offset_cal() requires confirm=True")
    bq_control(_BQ_CTRL_CC_OFFSET)


def bq_cc_offset_save(confirm=False):
    """Control(CC_OFFSET_SAVE=0x000B) - a mert CC offset Data Flash-be
    mentese (TUDASBAZIS.txt 9842-9843. sor, Prog/TF1_basic.c:2844
    bq_cal_offset_done() UGYANEZT a parancsot hasznalja a ~16s CC_OFFSET_CAL
    varakozas UTAN, DE ott bq_control_cmd()-vel -- visszaolvasas nelkul --
    adja ki, SOHA nem ellenorzi az eredmenyet).

    [MODOSITVA, 2026-09-12, Akkuteszter cross-repo HIL-lelet nyoman, l.
    TF1_BLOCKED_VALIDATION_BACKLOG.md BLK-026] KORABBAN a generikus,
    iro+visszaolvaso bq_control()-t hivta -- ez Akkuteszter-oldali elo
    HIL-teszten DETERMINISZTIKUSAN [Errno 110] ETIMEDOUT-tal hibazott az
    ELSO probalkozas VISSZAOLVASASAN (az IRAS valoszinuleg mar celba ert).
    Most bq_control_write_only()-t hasznal -- PONTOSAN a TF1_basic.c
    bq_control_cmd() mintaja szerint, ami ugyanezt a parancsot SOHA nem
    probalja visszaolvasni. Ez megszunteti magat az idotullepest a
    gyokerenel, nem csak egy Akkuteszter-oldali retry moge rejti (minden
    retry egy VALOS, MASODIK Data Flash commit kockazata volt).

    [KIEGESZITVE, 2026-09-12, ugyanazon a napon, ELO HIL-visszaigazolas
    soran feltart hianyossag, KETSZER finomitva] Az ELSO elo teszt
    igazolta, hogy maga a cc_offset_save() MOST MAR elsore, azonnal
    (nincs tobbe ETIMEDOUT) sikeres -- DE a KOZVETLENUL utana hivott
    read_calibration_data() (kulon DF-olvasas) EZUTTAL kapott ETIMEDOUT-ot,
    mert a valodi Data Flash iras/commit meg folyamatban volt a chipen,
    amikor a kovetkezo hivas erkezett. A REGI, iro+visszaolvaso bq_control()
    vegrehajtasa (a sajat, akkor is sikertelen olvasasi kiserletevel
    egyutt) VELETLENUL eppen eleg idot adott a chipnek a commit
    befejezesere -- ezt az idot a write-only valtas megszuntette, felfedve
    egy addig rejtett idozitesi fuggoseget.

    ELSO javitasi kiserlet: a TF1_basic.c::bq_cal_offset_done() (2844-2845.
    sor) altal hasznalt explicit `_delay_ms(50u)` portolasa ide -- EZ IS
    ELO HIL-teszttel KIPRoBALVA, es TOVABBRA IS HIBAT mutatott: a save()
    utani ELSO CONTROL_STATUS olvasas (kb. 78ms-nal) egy belsoleg
    ELLENTMONDASOS, torzult mintat adott (SS=1 ES CALMODE=1 ES CCA=1
    EGYSZERRE -- fizikailag lehetetlen kombinacio, mert CALMODE/CCA csak
    UNSEALED alatt allhatna, SS=1 viszont SEALED-et jelezne), majd A
    KOVETKEZO olvasasnal (kb. 187ms-nal, 109ms-mal kesobb) MAR stabilan
    tiszta, konzisztens allapotra (SS=0 FAS=0 CALMODE=0 BCA=0 CCA=0)
    allt, es onnantol STABIL maradt. Ez azt bizonyitja, hogy az 50ms
    (ami az ATmega/TF1_basic.c platformra volt hangolva) NEM eleg erre a
    Pico/RP2040 platformra -- a chip belso flash-commitja meg folyamatban
    van ~50-150ms tajan, es egy ekkor erkezo I2C-olvasas TORZULT adatot
    kaphat (nem hibat, hanem CSENDES korrupciot -- ugyanaz a hibaosztaly,
    mint a SEALED-alatti degeneralt DF-olvasas, csak itt egy AKTIV
    flash-iras kozbeni tranziens allapotrol van szo). A projekt SAJAT,
    mar bevalt `bq_df_write()` (l. lent) POST-COMMIT varakozasa PONT
    erre az esetre 200ms-ot hasznal -- ugyanezt a forras-igazolt,
    platform-specifikus konstanst alkalmazzuk itt is, az AVR-bol atvett
    50ms HELYETT (nem ahelyett, hogy semmilyen varakozas ne lenne --
    csak a konkret ertek maskepp lett kalibralva erre a platformra).
    ELO HIL-teszttel MEGERoSITVE 200ms-mal: a kovetkezo read_calibration_
    data() ELSO probalkozasra sikeres, konzisztens ertekekkel."""
    if not confirm:
        raise ValueError("bq_cc_offset_save() requires confirm=True")
    bq_control_write_only(_BQ_CTRL_CC_OFFSET_SAVE)
    time.sleep_ms(200)


# ── WP-AK-09: Xemics F4 float konverzio (BQ34Z110 F4 tipus) ─────────────
# Forras: Prog/TF1_basic.c:2161-2193 (float_to_xemics()/xemics_to_float(),
# production-ban bizonyitottan mukodo kod - l. modul-szintu audit a WP-AK-09
# zarojelentesben) - PONTOS 1:1 port, csak Python-szintaxisra atirva. CC
# Gain/CC Delta a Data Flash-ben ebben a formatumban tarolt (nem egyszeru
# integer, mint pl. Voltage Divider [U2]).
def _xemics_to_float(b):
    """4 bajt (subclass 104 offset 0 vagy 4) -> float. Dekod: M =
    (b[1]|0x80)<<16 | b[2]<<8 | b[3]; ertek = M x 2^(b[0]-152). b[1] bit7 =
    elojel (0=pozitiv, 1=negativ) - CC Gain/CC Delta soha nem lehet
    negativ, ezert (Prog/TF1_basic.c:2903-2907 mintaja szerint) a negativ
    elojelbit korrupt DF adat jelzese, -1.0-et ad vissza."""
    if b[1] & 0x80:
        return -1.0
    m = ((b[1] | 0x80) << 16) | (b[2] << 8) | b[3]
    e = b[0] - 152
    v = float(m)
    if e >= 0:
        v *= float(1 << e)
    else:
        v /= float(1 << (-e))
    return v


def _float_to_xemics(v):
    """float -> 4 bajt (subclass 104 offset 0 vagy 4). Csak pozitiv
    ertekekre (CC Gain/CC Delta soha nem negativ) - Prog/TF1_basic.c:2166-
    2180 mintaja: normalizal [0.5,1.0)-ra, majd M=v*2^24 mantissza."""
    if v <= 0.0:
        return bytes(4)
    exp = 0
    while v >= 1.0:
        v *= 0.5
        exp += 1
    while v < 0.5:
        v *= 2.0
        exp -= 1
    m = int(v * 16777216.0)  # v * 2^24
    if m >= 16777216:
        m >>= 1
        exp += 1
    if m < 8388608:
        m <<= 1
        exp -= 1
    return bytes([
        (exp + 128) & 0xFF,
        (m >> 16) & 0x7F,
        (m >> 8) & 0xFF,
        m & 0xFF,
    ])


# ── WP-AK-09: Kalibracios Data Flash olvasas/iras (SC104/blk0) ──────────
_CAL_GAIN_MIN, _CAL_GAIN_MAX = 0.1, 40.0        # Table 20 "CC Gain (Note 4)" Min/Max
_CAL_DELTA_MIN, _CAL_DELTA_MAX = 2.98e4, 1.19e6  # Table 20 "CC Delta (Note 4)" Min/Max


def bq_read_calibration_data():
    """SC104/blk0 dekodolasa - CC Gain/CC Delta [F4 Xemics], CC Offset
    [I2 elojeles], Board Offset [I1 elojeles] (TUDASBAZIS.txt Table 20,
    65263-65302. sor). Ugyanaz a decode-convenience minta, mint
    bq_ra_table() - a byte-format tudas EGY helyen (itt) el, nem a PC-
    oldalon duplikalodik."""
    block = bq_df_read(_BQ_SC_CALIBRATION_DATA, 0)
    cc_offset = (block[8] << 8) | block[9]
    if cc_offset & 0x8000:
        cc_offset -= 0x10000
    board_offset = block[10]
    if board_offset & 0x80:
        board_offset -= 0x100
    return {
        "cc_gain": _xemics_to_float(block[0:4]),
        "cc_delta": _xemics_to_float(block[4:8]),
        "cc_offset": cc_offset,
        "board_offset": board_offset,
    }


def bq_write_calibration_gain_delta(new_gain, new_delta, confirm=False):
    """Uj CC Gain/CC Delta iras SC104/blk0-ba, a 32 bajtos blokk tobbi
    mezojenek (Offset 8-31: CC Offset/Board Offset/Temp Offset/Pack V
    Offset/Voltage Divider) megtartasa mellett (csak a 0-7. bajt patch-elve)
    - Prog/TF1_basic.c:2928-2929 mintaja (float_to_xemics minden hivasnal
    ujra, majd EGYETLEN bq_df_write() a teljes blokkra). Tartomany-ellenorzes
    MINDKET mezore (Table 20 Min/Max) - [audit-lelet, WP-AK-09] a
    TF1_basic.c csak new_gain-t ellenorzi, new_delta-t nem; ez a port a
    hianyt originalisan potolja, a hasznalt kepletbol (k=ref/meas, mindket
    mezo azonos faktorral skalazodik) nem kovetkezik automatikusan az
    ervenyes tartomany. A bq_df_write() sajat readback-verify-ja zarja le
    (kettos ellenorzes elve, mint minden mas DF-iras ebben a modulban)."""
    if not confirm:
        raise ValueError("bq_write_calibration_gain_delta() requires confirm=True")
    if not (_CAL_GAIN_MIN <= new_gain <= _CAL_GAIN_MAX):
        raise ValueError(
            "new_gain %r out of valid range [%r, %r]" % (new_gain, _CAL_GAIN_MIN, _CAL_GAIN_MAX)
        )
    if not (_CAL_DELTA_MIN <= new_delta <= _CAL_DELTA_MAX):
        raise ValueError(
            "new_delta %r out of valid range [%r, %r]" % (new_delta, _CAL_DELTA_MIN, _CAL_DELTA_MAX)
        )
    block = bytearray(bq_df_read(_BQ_SC_CALIBRATION_DATA, 0))
    block[0:4] = _float_to_xemics(new_gain)
    block[4:8] = _float_to_xemics(new_delta)
    bq_df_write(_BQ_SC_CALIBRATION_DATA, 0, bytes(block), confirm=True)


def bq_read_voltage_divider():
    """SC104/blk0 Offset 14 [U2, big-endian] - TUDASBAZIS.txt Table 20
    65358-65365. sor, Prog/TF1_basic.c BQ_VOLTAGE_DIVIDER default (36124 ->
    SC104 Off14, l. TF1_basic.c:2048)."""
    block = bq_df_read(_BQ_SC_CALIBRATION_DATA, 0)
    return (block[14] << 8) | block[15]


def bq_write_voltage_divider(new_value_mV, confirm=False):
    """Uj Voltage Divider iras SC104/blk0 Offset 14-re, a blokk tobbi
    mezojenek megtartasa mellett - Prog/TF1_basic.c:3030-3040
    bq_cal_vd_step() mintaja (a kepletet [new_VD = cur_VD x dmm_mv /
    fg_volt, SLUA665 "Going to Production With the bq34z1xx" 13718-13721.
    sor] a HIVO PC-oldal szamitja ki, ez a fuggveny csak a mar kiszamitott
    uj erteket irja, ugyanugy mint bq_write_calibration_gain_delta().
    Klampolas 65535-re (U2 max, Table 20) - Prog/TF1_basic.c:3028
    ugyanezt teszi."""
    if not confirm:
        raise ValueError("bq_write_voltage_divider() requires confirm=True")
    if new_value_mV < 0:
        raise ValueError("new_value_mV must be >= 0")
    clamped = 65535 if new_value_mV > 65535 else int(new_value_mV)
    block = bytearray(bq_df_read(_BQ_SC_CALIBRATION_DATA, 0))
    block[14] = (clamped >> 8) & 0xFF
    block[15] = clamped & 0xFF
    bq_df_write(_BQ_SC_CALIBRATION_DATA, 0, bytes(block), confirm=True)


# ── Data Flash blokk olvasas / iras (Prog/TF1_basic.c:2195-2260 mintaja) ──
_BQ_BLOCK_DATA_CTRL = 0x61
_BQ_DF_CLASS        = 0x3E
_BQ_DF_BLOCK_REG    = 0x3F
_BQ_BLOCK_DATA_BASE = 0x40
_BQ_BLOCK_CHK       = 0x60


def _select_df_block(subclass, block):
    """[review #P0] Kozos block-select helper - SEM bq_df_read, SEM
    bq_df_write nem tamaszkodhat egy korabbi tranzakciobol visszamaradt
    block-selection allapotra. Harom kulon Control-regiszter-iras,
    a BQ34Z110 elvart sorrendjeben (Prog/TF1_basic.c:2202-2231 mintaja)."""
    _i2c.writeto_mem(BQ_I2C_ADDR, _BQ_BLOCK_DATA_CTRL, bytes([0x00]))
    time.sleep_ms(1)
    _i2c.writeto_mem(BQ_I2C_ADDR, _BQ_DF_CLASS, bytes([subclass]))
    time.sleep_ms(1)
    _i2c.writeto_mem(BQ_I2C_ADDR, _BQ_DF_BLOCK_REG, bytes([block]))
    time.sleep_ms(1)


# CONTROL_STATUS High Byte Bit 5 (TI-SLUSB55B Table 4) - "SS: Status bit
# that indicates the bq34z110 is in the SEALED State. Active when set."
_BQ_CTRLSTATUS_SS = 0x2000


def bq_df_read(subclass, block):
    """Beolvassa a subclass adott 32-bajtos blokkjat. UNSEALED modban
    hivando (Prog/TF1_basic.c:2202-2231 mintaja).

    [UJ, 2026-09-12, Akkuteszter cross-repo HIL-lelet nyoman, l.
    TF1_BLOCKED_VALIDATION_BACKLOG.md BLK-026] SEALED allapotban a chip a
    DataFlashClass()/DataFlashBlock() (0x3e/0x3f) parancsokra NEM a
    kivalasztott subclass tartalmat adja vissza, hanem strukturalisan MAS
    parancsszemantikara valt at (Manufacturer Information Block),
    HIBAJELZES NELKUL -- TI-SLUSB55B explicit rogziti: "In SEALED mode,
    data flash cannot be accessed through commands 0x3e and 0x3f."
    Akkuteszter-oldali HIL-teszt (2026-09-12, Voltage Divider kalibracio
    utani ellenorzo olvasas) konkret, hamis (degeneralt/nulla) erteket
    kapott SEALED alatt, hibajelzes nelkul -- l. Akkuteszter
    CLAIM_LEDGER.yaml BQ-DF-SEALED-001 6. korrekcio. Explicit SS-bit-
    ellenorzessel MOST elore elutasitva, MIELOTT a hivo hamis adatot
    kapna -- MINDEN DF-olvasast/irast erint (bq_df_write() sajat
    readback-verify-je is ezen a fuggvenyen at megy)."""
    status = bq_control(_BQ_CTRL_CONTROL_STATUS)
    if status & _BQ_CTRLSTATUS_SS:
        raise RuntimeError(
            "bq_df_read(subclass=%d, block=%d): BQ34Z110 is SEALED (SS=1) -- "
            "Data Flash subclass access is unavailable in SEALED mode "
            "(TI-SLUSB55B). Unseal (bq_unseal + bq_full_access) before "
            "reading Data Flash." % (subclass, block)
        )
    _select_df_block(subclass, block)
    return _i2c.readfrom_mem(BQ_I2C_ADDR, _BQ_BLOCK_DATA_BASE, 32)


def bq_df_write(subclass, block, data32, confirm=False):
    """Kivalasztja a blokkot (MEG AZ IRAS ELOTT is - review #P0, korabban
    csak a readback-verify oldalon tortent meg ez, ami valos hardveren
    rossz blokkba irast kockaztatott volna), visszairja a 32 bajtot es a
    checksumot, majd ujra kivalaszt + readback-verify-jal zar (a projekt
    kotelezo BQ write-readback-verify elve). Checksum keplet:
    255 - (sum & 0xFF) (Prog/TF1_basic.c:2243-2260). Egyedi bajt-iras
    (nem block write) - Prog/TF1_basic.c:2237-2241 kommentje szerint a
    BQ34Z110 BlockData (0x40-0x5F) csak auto-increment OLVASAST tamogat,
    iraskor egyedi tranzakciok kellenek."""
    if not confirm:
        raise ValueError("bq_df_write() requires confirm=True")
    if len(data32) != 32:
        raise ValueError("data32 must be exactly 32 bytes")

    _select_df_block(subclass, block)

    for i, b in enumerate(data32):
        _i2c.writeto_mem(BQ_I2C_ADDR, _BQ_BLOCK_DATA_BASE + i, bytes([b]))

    checksum = 255 - (sum(data32) & 0xFF)
    _i2c.writeto_mem(BQ_I2C_ADDR, _BQ_BLOCK_CHK, bytes([checksum]))
    time.sleep_ms(200)

    readback = bq_df_read(subclass, block)
    if bytes(readback) != bytes(data32):
        raise RuntimeError(
            "bq_df_write readback-verify mismatch: subclass=%d block=%d" % (subclass, block)
        )
    return True


# ── DF snapshot (spec 5.2/5.4 - Phase 1, NOT a full production golden image;
#    docs/golden-images/README.md + templates/manifest.example.json is the
#    separate, later, human-gated final schema) ──
# Subclass/block lista = pontosan azok a Data Flash blokkok, amiket a
# Prog/TF1_basic.c bq_df_write_calib()/bq_df_write_bat() ir/ellenoriz -
# ez adja a "releváns DF-tartalom" hatokoret (spec 5.2).
SNAPSHOT_DF_BLOCKS = [
    (2, 0),    # Safety (OTC/OTD)
    (48, 0),   # Data: Design Capacity/Energy, Cell Design Voltage
    (48, 1),   # Data Block1: Design Energy Scale
    (49, 0),   # Discharge: Cell BL/BH Set Volt
    (64, 0),   # Registers: Pack Config, Number of Series Cells
    (80, 2),   # IT Config Block2: Cell Termination Voltage
    (81, 0),   # Current: Dsg/Chg/Quit thresholds
    (83, 0),   # OCV Table: Chem ID
    (104, 0),  # Calibration: CC Gain/Delta/Offset, Board Offset, VD
]


def bq_backup_df_snapshot(snapshot_id, revision):
    """Nem roncsolo: kiolvassa a SNAPSHOT_DF_BLOCKS minden blokkjat,
    osszefuzi (a lista sorrendjeben), SHA-256-ot szamit, es a Phase 1
    snapshot-manifest sémát epiti (artifact_type=SELECTED_DF_SNAPSHOT,
    kulonbozik a vegleges docs/golden-images/ golden-image-sematol -
    review #P0). Visszater: (manifest_dict, df_bytes). A hivo felel a
    Pico helyi flash-ere valo mentesert (ld. workflow, spec 5.4) es a
    hianyzo mezok (sample_count, approval.*) PC-oldali kitoltéséért."""
    df_bytes = bytearray()
    for (subclass, block) in SNAPSHOT_DF_BLOCKS:
        df_bytes += bq_df_read(subclass, block)
    df_bytes = bytes(df_bytes)

    manifest = {
        "artifact_type": "SELECTED_DF_SNAPSHOT",
        "snapshot_id": snapshot_id,
        "revision": revision,
        "created_at": None,  # Pico-nak nincs hardveres RTC-je - PC oldalon tolti ki a technikus
        "battery_profile": {"description": None, "sw_idx": None},
        "bq_device": {
            "device_type": bq_device_type(),
            "device_type_source": "Control(DEVICE_TYPE=0x0001) readback",
            "firmware_version": bq_fw_version(),
            "firmware_version_source": "Control(FW_VERSION=0x0002) readback",
            "hardware_version": bq_hw_version(),
            "hardware_version_source": "Control(HW_VERSION=0x0003) readback",
        },
        "chem_id": {
            "value": bq_chem_id(),
            "source": "Control(CHEM_ID=0x0008) readback",
        },
        "df_blocks": list(SNAPSHOT_DF_BLOCKS),
        "update_status": {"target": "0x06", "measured": None, "note": "0x06 alone is not a full learning PASS - see bq_learn_status()"},
        "sample_count": None,
        "sample_count_min": 20,
        "measurement_protocol_ref": None,
        "source_git_commit": None,
        "snapshot_file": "df_snapshot_%s.dfraw" % snapshot_id,
        "snapshot_sha256": _sha256_hex(df_bytes),
        "validation_status": "DRAFT",
        "limitations": [
            "Selected Data Flash blocks only",
            "Not a TI DFI/ROM image",
            "Not production golden image",
            "Ra table not yet verified",
        ],
        "approval": {"approved_by": None, "approved_at": None, "notes": None},
    }
    return manifest, df_bytes


def bq_backup_df_snapshot_to_file(snapshot_id, revision):
    """bq_backup_df_snapshot() + iras a Pico helyi flash-ere (spec 5.4
    1. lepese). Kulon fuggveny, hogy a tiszta logika (fenti) hardver
    nelkul, host-oldalon tesztelheto maradjon (review #P1)."""
    manifest, df_bytes = bq_backup_df_snapshot(snapshot_id, revision)
    with open("/df_snapshot_%s.json" % snapshot_id, "w") as f:
        json.dump(manifest, f)
    with open("/df_snapshot_%s.dfraw" % snapshot_id, "wb") as f:
        f.write(df_bytes)
    return manifest, df_bytes


def bq_verify_snapshot_integrity(manifest, df_bytes):
    """Nem roncsolo: CSAK file-integritast ellenoriz - igaz, ha df_bytes
    SHA-256-ja egyezik a manifest snapshot_sha256 mezojevel. [review #P0]
    Ez NEM bizonyitja, hogy a BQ Data Flash tenylegesen egyezik a
    snapshot-tal - arra bq_verify_snapshot_against_device() valo."""
    return _sha256_hex(df_bytes) == manifest["snapshot_sha256"]


def bq_verify_snapshot_against_device(manifest):
    """[review #P0] Valodi eszkoz-verify: elo BQ-olvasassal ujraolvassa a
    manifest["df_blocks"] szerinti osszes blokkot, a felsorolas sorrendjeben
    osszefuzi oket, es a teljes fuzet SHA-256-jat hasonlitja a manifest
    snapshot_sha256 mezojehez. [minor finding, fix wave #1] EZ EGY TELJES
    SNAPSHOT osszehasonlitas, NEM blokkonkenti - a manifest csak egyetlen
    aggregat hash-t tarol, nem blokkonkenti hash-listat, igy valodi
    blokk-granularitasu diff-et (melyik konkret blokk ter el) ez a fuggveny
    nem tud adni, csak azt, hogy "a teljes egyezes fennall-e". Nem roncsolo
    - csak olvas es osszehasonlit. Kulon fut a file-integritas ellenorzestol
    (fenti), hogy a ket allitas ("a file nem serult" vs "az eszkoz meg
    egyezik a snapshottal") ne mosodjon ossze."""
    df_bytes = bytearray()
    for (subclass, block) in manifest["df_blocks"]:
        df_bytes += bq_df_read(subclass, block)
    return _sha256_hex(bytes(df_bytes)) == manifest["snapshot_sha256"]


def bq_restore_df_snapshot(manifest, df_bytes, confirm=False):
    """Egy jovahagyott DF snapshot tartalmanak irasa egy celegysegre, a
    bq_df_write() readback-verify fegyelmevel minden blokkon. (Korabbi
    tervezett nev: bq_program_golden_image() - atnevezve, review #P0.)"""
    if not confirm:
        raise ValueError("bq_restore_df_snapshot() requires confirm=True")
    # [IMPORTANT finding, fix wave #1] a blokk-listat a manifest["df_blocks"]
    # mezojebol kell venni, NEM a modul-globalis SNAPSHOT_DF_BLOCKS-bol -
    # ugyanaz a mintaja, mint amit bq_verify_snapshot_against_device() mar
    # kovet. Ha egy manifest df_blocks-a eltér a modul jelenlegi
    # SNAPSHOT_DF_BLOCKS-atol (pl. masik modulverzioval keszult snapshot,
    # vagy kezzel szerkesztett manifest), a globalis hasznalata csendben
    # ROSSZ subclass-ba irna a bajtokat valos hardveren - ez destruktiv
    # iras-utvonal, a SHA-256 integritas-ellenorzes ezt NEM veszi eszre
    # (csak a byte-tartalmat validalja, nem a subclass/block leképezest).
    df_blocks = manifest["df_blocks"]
    if len(df_bytes) != len(df_blocks) * 32:
        raise ValueError(
            "df_bytes length %d does not match %d subclass blocks x 32 bytes"
            % (len(df_bytes), len(df_blocks))
        )
    # [task review, fix round 1] destructive hardware write - a stale/unrelated
    # df_bytes for this manifest must never reach bq_df_write(). Same principle
    # as the project's mandatory write-readback-verify discipline, just applied
    # BEFORE the write instead of after: verify integrity first, then act.
    if not bq_verify_snapshot_integrity(manifest, df_bytes):
        raise RuntimeError(
            "bq_restore_df_snapshot: df_bytes does not match manifest snapshot_sha256"
        )
    for i, (subclass, block) in enumerate(df_blocks):
        chunk = df_bytes[i * 32:(i + 1) * 32]
        bq_df_write(subclass, block, chunk, confirm=True)
    return True


# ── Learning-monitor (BLK-023) ──────────────────────────────────────────
_BQ_SC_STATE = 82  # State subclass: Off0-1 Qmax [I2], Off4 UpdateStatus [H1]


def bq_learn_status():
    """DEBUG/THONNY CONVENIENCE ONLY (review #P1) - egyszeri SOC/Qmax/
    UpdateStatus lekerdezes, egy MEGFIGYELT allapotot ad vissza, NEM
    allit "learning PASS"-t (review #P0). A teljes learning PASS
    (0x06 ES Qmax-frissites igazolva ES Ra-frissites igazolva ES
    OCV/VOK/QEN atmenet konzisztens ES a szukseges fizikai ciklus
    lezajlott) ezzel a fuggvennyel NEM allapithato meg - a Ra Table
    DF-helye nincs forrasolva.
    ELOFELTETEL: a technikusnak MAR le kell futtatnia bq_unseal(confirm=True)-t
    - az UpdateStatus (SC82 Off4) csak UNSEALED modban olvashato
    (Prog/TF1_basic.c:3605). Ezt a fuggveny NEM vegzi el automatikusan
    (a confirm-kapu szemantikaja ezt szandekosan igy tartja tisztan,
    ld. spec 6. szakasz)."""
    block = bq_df_read(_BQ_SC_STATE, 0)
    qmax = (block[0] << 8) | block[1]   # big-endian I2 (Prog/TF1_basic.c:2149-2159 mintaja)
    update_status = block[4]
    soc = bq_soc()
    return {"qmax_mah": qmax, "update_status": update_status, "soc_pct": soc}


def bq_learn_monitor(interval_s=60, max_polls=None, on_poll=None):
    """DEBUG/THONNY CONVENIENCE ONLY (review #P1) - a vegleges learning-
    workflow master a jovobeli PC-s akkumulatorparameterezo alkalmazas
    lesz, nem a Pico (mikor polloljon/toltsön/kisusön/relaxaljon/lepjen
    tovabb/FAIL/PASS dontese ott szuletik). Periodikus bq_learn_status()
    lekerdezes, amig UpdateStatus el nem eri 0x06-ot (CSAK megfigyelt
    ertek, nem PASS - review #P0) vagy max_polls el nem fogy. on_poll
    (opcionalis): minden lekerdezes utan meghivott callback(status_dict)
    - REPL hasznalatkor print()-eles, teszteleskor gyujtes/ellenorzes
    celjara."""
    polls = 0
    status = None
    while True:
        status = bq_learn_status()
        polls += 1
        if on_poll is not None:
            on_poll(status)
        if status["update_status"] >= 0x06:
            break
        if max_polls is not None and polls >= max_polls:
            break
        time.sleep(interval_s)
    return status
