"""
Bq34z110ReadOnlyDriver — SUP03-APP-002/003: BQ34Z110 gauge READ-ONLY interfész.

FONTOS KORLÁTOZÁS (szándékos, a jelen fázisban):
  - NINCS Data Flash ÍRÁS;
  - NINCS IT_ENABLE (0x0021) parancs;
  - NINCS seal/unseal/full-access (0x0020) parancs;
  - NINCS ManufacturerAccess WRITE — csak olvasás.

Ezek a golden-image gyártási/tanulási írási műveletek explicit felhasználói
HIL-jóváhagyás nélkül NEM adhatók ki (l. CLAUDE.md 8.8, SUP03 cél-utasítás).
Amíg nincs ilyen jóváhagyás és fizikai validálás, minden BQ-oldali "tanulási"
lépés kézi checkpoint marad (SUP03-APP-003) — ez a driver csak azt a réteget
adja, ami a KÉSŐBBI, HIL-validált automatizáláshoz kell.

WP-AK-06 (2026-08-21): a `transport` paraméter mostantól ténylegesen bekötött
egy valós PC↔Pico implementációval is (`Prog/drivers/pico_bq_transport.py::
PicoBQTransport`) — a `Bq34z110Transport` Protocol ("BQDevice" réteg alatti
"PicoBQTransport" réteg a WP-AK-06 terminológiája szerint) továbbra is
műszer-független interfész marad, a fake/mock transport (tesztekhez) és a
valós Pico-RPC transport (élesben) egyaránt implementálja.

REGISZTER-CÍM KERESZT-ELLENŐRZÉS (WP-AK-06, WP-AK-00 óta nyitott tétel,
LEZÁRVA): a korábbi "FELTÉTELEZÉS" jelöléssel jelölt STD_CMD_* címek TÉVESEK
voltak — a `kb-search` skill-lel `Leírások/TI_BQ/BQ34Z110PWR.pdf` (SLUSB55B)
"Table 2. Standard Commands" táblázata ÉS a TF1 repo `Prog/pico_bq_service.py`
(saját forrása: `Prog/TF1_basic.c`) egybehangzóan a jelen fájlban most már
használt címeket igazolja — csak `Control()`/`Voltage()`/`DEVICE_TYPE`/
`CHEM_ID` egyezett véletlenül a korábbi feltételezéssel. Forrás: TF1 repo
`TF1_BQ_Service_Golden_Image_Mode.md` 10. szakasz (WP-AK-06 kiegészítés).
[Review-kör 5, Gyári forrás F5-1 (2026-09-10), DFAM-016 6. előfordulás: a
WP-AK-06-kori idézet eredetileg egy akkori, egyben TXT-be exportált
TUDÁSBÁZIS.txt-re hivatkozott konkrét sorszámmal — a fájl azóta (2026-05-31)
tisztán INDEX-szé alakult (593 sor, l. Leírások/TUDÁSBÁZIS.txt fejléce), a
régi sorhivatkozások feloldhatatlanok. A tábla NEVE/TARTALMA a saját
pdftotext-ellenőrzéssel (BQ34Z110PWR.pdf, SLUSB55B - JUNE 2012 - REVISED MAY
2013) megerősítve, az alábbi citálás a jelenlegi, tényleges forrásra mutat.]

A Data Flash mezők (UpdateStatus/Qmax/Ra Table) NEM egyetlen "subcommand"-dal
olvashatók (a korábbi feltételezett `read_block(subcommand)` modell téves
volt) — a BQ34Z110 subclass+block+offset címzést használ (ugyanaz a modell,
mint a `pico_bq_service.py::bq_df_read(subclass, block)` primitívje). A
`Bq34z110Transport.read_data_flash_block(subclass, block)` egy teljes,
32 bájtos nyers blokkot ad vissza, a driver metódusai ebből vágják ki a
releváns offsetet/mezőt.

KÉT KÜLÖN CÍMTÉR, KÉT KÜLÖN METÓDUS (WP-AK-06, `PicoBQTransport` építése
közben derült ki, valós hibaként — nem csak elméleti kockázat): a BQ34Z110-nél
a közvetlen standard command regiszterek (0x00-0x1F, pl. `STD_CMD_VOLTAGE=0x08`)
ÉS a `Control()` alparancsok (0x0000-0xFFFF, pl. `MAC_CHEM_ID=0x0008`) KÉT
FÜGGETLEN címteret alkotnak, amik Python int-ként AZONOS ÉRTÉKRE eshetnek
(`STD_CMD_VOLTAGE == MAC_CHEM_ID == 8`, `STD_CMD_CONTROL == MAC_CONTROL_STATUS
== 0`). Egy korábbi tervezet egyetlen `read_word(command: int)` metódusba
akarta összevonni mindkettőt — ez egy VALÓS (nem csak elméleti) hibát okozott
a `PicoBQTransport` implementálásakor: egy dict-alapú "melyik RPC-metódust
hívjam" leképezés csendben felülírta a `voltage_mV` bejegyzést a `chem_id`
bejegyzéssel (mindkettő 8-as kulcs). Ezért a Protocol KÉT külön metódusra
bontva: `read_standard_command_word()` (közvetlen regiszter) és
`read_control_subcommand_word()` (Control() alparancs) — a hívó (driver)
mindig tudja, melyik címtérből olvas, a transport-nak nem kell kitalálnia.

WP-AK-07 (BQ Diagnostics GUI) KAPU-JAVÍTÁS: `read_qmax()`/`read_update_status()`
korábban egy túl rövid (malformed) Data Flash blokkra csendesen 0-t adott
vissza — ez a WP-AK-07 Konszolidált terv explicit kapujával ("BQ disconnect
/ stale data / malformed reply nem válhat csendesen 0 értékké") ütközött:
egy 0 UpdateStatus/Qmax egy VALÓS, hihető értékkel összetéveszthető. Most
mindkettő `InstrumentInvalidReading`-et dob helyette (ugyanaz az osztály,
mint amit `device_dmm.py` már NaN/Inf/overload DMM-válaszokra használ — nem
új absztrakció, a meglévő konvenció követése).

[Független review I-1 lelet, javítva] Az eredeti WP-AK-07 kör `read_ra_table()`-t
tudatosan kihagyta ebből a szigorításból (azzal az indoklással, hogy egy
rövid blokk a visszaadott lista HOSSZÁVAL már önmagában jelzi a
hiányosságot) — a független review ezt bizonyítottan MEGSÉRTETTE a
kapu-szabályt: egy TELJESEN hiányzó/üres blokkra `read_ra_table()` egy üres
listát adott, `BqDiagnosticsSnapshot.ra_table_mohm.valid=True`-val (a
`bq_diagnostics.py::poll_bq_diagnostics()` `_read()` wrappere csak
KIVÉTELRE állítja `valid=False`-ra, egy sikeresen visszatérő üres listára
nem) — ugyanarra a hiányzó blokkra a testvér `qmax`/`update_status` mezők
helyesen `valid=False`-ot kaptak, inkonzisztensen. Ez pontosan az a
"malformed reply csendesen NEM 0, de csendesen ÉRVÉNYESNEK TŰNIK" mintázat,
amit a kapu-szabály tiltani szándékozott. Javítva: `read_ra_table()` most
is `InstrumentInvalidReading`-et dob, ha a blokk nem tartalmazza mind a 15
értéket (l. `_RA_TABLE_EXPECTED_LEN`).

Új, a WP-AK-07 diagnosztikai GUI-hoz sourced Control() alparancsok:
`MAC_FW_VERSION=0x0002`, `MAC_HW_VERSION=0x0003` — forrás: `Leírások/
TI_BQ/BQ34Z110PWR.pdf` (SLUSB55B) "Table 3. Control() Subcommands" +
az azt követő "FW_VERSION: 0x0002" / "HW_VERSION: 0x0003" leíró szöveg
(ugyanaz a táblázat-környezet, mint a WP-AK-06-ban már forrásolt
`MAC_DEVICE_TYPE`; l. F5-1 fenti kommentje a forráshivatkozás
frissítésének okáról).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from Prog.src.exceptions import InstrumentInvalidReading

# [Kör 21 F18, HW21-01] A HIL-igazolt, SEALED-módú degenerált Data Flash
# blokk-minta (21×0x00 + 11×0xFF), forrás: CLAIM_LEDGER.yaml BQ-DF-SEALED-001
# (valós COM10 egységen, subclass 36/48/49/64/82/88 block=0 MIND byte-azonos
# mintát adott ugyanabban a dumpban), SOURCE_CONFLICTS.md SCF-011.
#
# MODUL-SZINTŰ és PUBLIKUS (vezető aláhúzás NÉLKÜL, ARC21-6 konvenció, l.
# Kör 21 F17): a `Bq34z110DataFlashService` (Prog/src/bq_data_flash_service.py)
# egy MÁSODIK, ettől a drivertől függetlenül karbantartott olvasási út, amely
# HW21-01 óta ugyanezt a mintát utasítja el — a minta tehát réteghatáron át
# megosztott szimbólum. SZÁNDÉKOSAN EGYETLEN definíció a PRODUKCIÓS kódban:
# egy duplikált másolat csendben szétsodródhatna a másiktól egy jövőbeli
# korrekciónál, egy safety-kapu bájtmintájánál ez elfogadhatatlan.
#
# KIVÉTEL, amit NE "egységesíts" (Kör 21 F18 MAX verifier N2 lelete): a
# `Prog/tests/test_device_bq34z110.py:315` `_DEGENERATE_BLOCK` FÜGGETLEN
# literál-másolata SZÁNDÉKOS DRIFT-HORGONY, NE cseréld erre az importra.
# A verifier mutációval igazolta: ha ez a konstans elsodródik, a teljes
# suite-ban EZ AZ EGYETLEN teszt, ami elbukik -- minden más degenerált-
# blokk teszt (köztük a service-réteg mind a 10 F18-as tesztje) ugyanezt
# a konstanst importálja, tehát a bájtmintára nézve tautologikus fixture
# lenne (rules/60-testing.md: a fixture ne ugyanabból az ellenőrizetlen
# feltevésből származzon, mint a kód).
#
# A minta ALKALMAZÁSI KÖRÉNEK korlátairól (mely subclass/block párokra
# HIL-evidenciázott, és melyekre NEM) l. a lenti
# `_reject_degenerate_sealed_df_block()` feletti részletes kommentet.
DEGENERATE_SEALED_DF_BLOCK = b"\x00" * 21 + b"\xff" * 11


class Bq34z110Transport(Protocol):
    """Alacsony szintű I2C/HDQ transport interfész — a driver ezen keresztül
    olvas, a tényleges buszkommunikációt egy külön modul valósítja meg
    (fake teszthez, `PicoBQTransport` élesben). Két külön szó-olvasó metódus
    — l. modul docstring "KÉT KÜLÖN CÍMTÉR" szakasza."""

    def read_standard_command_word(self, command: int) -> int: ...
    def read_control_subcommand_word(self, subcommand: int) -> int: ...
    def read_data_flash_block(self, subclass: int, block: int) -> bytes: ...


@dataclass
class Bq34z110ReadOnlyDriver:
    """READ-ONLY BQ34Z110 gauge driver — l. modul docstring a korlátozásokról."""

    transport: Bq34z110Transport

    # Standard command regiszterek — forrás: Leírások/TI_BQ/BQ34Z110PWR.pdf
    # (SLUSB55B) "Table 2. Standard Commands" + Prog/pico_bq_service.py
    # (TF1 repo, Prog/TF1_basic.c-ből sourced) — l. modul docstring.
    STD_CMD_CONTROL = 0x00
    STD_CMD_STATE_OF_CHARGE = 0x02
    STD_CMD_REMAINING_CAPACITY = 0x04
    STD_CMD_FULL_CHARGE_CAPACITY = 0x06
    STD_CMD_VOLTAGE = 0x08
    STD_CMD_AVERAGE_CURRENT = 0x0A
    STD_CMD_TEMPERATURE = 0x0C
    STD_CMD_FLAGS = 0x0E
    # WP-AK-10 (SUP03-LEARN-002 gauge log) - forrás: Leírások/TI_BQ/
    # BQ34Z110PWR.pdf (SLUSB55B) "Table 19. Extended Commands" (CycleCount()
    # 0x2c/0x2d, DOD0() 0x36/0x37) — l. F5-1 fenti kommentje.
    STD_CMD_CYCLE_COUNT = 0x2C
    STD_CMD_DOD0 = 0x36

    # ManufacturerAccess (Control()) alparancsok.
    MAC_CONTROL_STATUS = 0x0000
    MAC_DEVICE_TYPE = 0x0001
    MAC_FW_VERSION = 0x0002
    MAC_HW_VERSION = 0x0003
    MAC_CHEM_ID = 0x0008

    # Data Flash subclass/block címek (NEM "subcommand" — l. modul docstring).
    # State (subclass 82, block 0) — MINDKÉT offset VERIFIED_PRIMARY. Forrás:
    # Leírások/TI_BQ/BQ34Z110PWR.pdf (SLUSB55B, rev. B) "Table 20. Data Flash
    # Summary", nyomtatott 41. oldal (torzulásmentes kinyerés:
    # `pdftotext -f 41 -l 41 -table` — a `-layout` mód EZT a táblázatot
    # szétcsúsztatja, l. DECISION_REGISTER.md GOV-020 és CLAIM_LEDGER.yaml
    # BQ-QMAX-001):
    #   "Gas Gauging | 82 | State | 0 | Qmax Cell 0   | I2 | 0..32767 | mAh"
    #   "Gas Gauging | 82 | State | 4 | Update Status | H1 | 0x00..0x06 | --"
    # Update Status ÉRTÉKEI (0x02/0x04/0x05/0x06) külön forrásolva: "Table
    # 11. Update Status Definitions", nyomtatott 18. oldal (l. BQ-STATUS-001
    # — az ott forrásolt ÉRTÉKEK, nem a cím/offset).
    # HIL-keresztellenőrzés (2026-09-08 és 2026-09-14, valós COM10 egység,
    # UNSEALED, két független dump, bájtazonos): a nyers subclass 82/block 0
    # blokk offset 0-1 = 0x1EAB (7851 mAh, egyezik read_qmax()-szal), offset
    # 4 = 0x05 (érvényes Update Status kód), offset 7-8 = -299 és offset
    # 9-10 = -1131 — PONTOSAN a Table 20 "Avg I Last Run"/"Avg P Last Run"
    # (offset 7/9) alapértékével egyezik, tehát a blokk-elrendezés fizikailag
    # is megerősített, nem csak a datasheet-táblázat (Mérések/nyers_logok/
    # bq_df_dump_20260908_182202.csv, bq_df_dump_20260914_222846.csv).
    # [SRC-F-7, Kör 18] A korábbi kommentverzió ezt a TF1 repo
    # Prog/pico_bq_service.py::bq_learn_status()-ára (TF1_basic.c:3374)
    # hivatkozva forrásolta — az a forráshierarchia legalsó szintje
    # (.claude/rules/00-source-authority.md), az implementáció nem igazolja
    # önmagát; a fenti primer datasheet-sor + HIL-bizonyíték a tényleges alap.
    DF_SC_STATE = 82
    DF_BLOCK_STATE = 0
    DF_OFFSET_QMAX = 0
    DF_OFFSET_UPDATE_STATUS = 4
    # Ra Table (Cell0), subclass 88, block 0 — a FORRÁSSZINTEK KÜLÖNVÁLASZTVA
    # [SRC-F-4, Kör 18: a korábbi kommentverzió a "15×I2 big-endian mOhm"
    # BELSŐ elrendezést is SLUSB55B-nek tulajdonította, over-attribution]:
    # - Subclass 88 ("Cell0 R_a Table") / 89 ("xCell0 R_a Table")
    #   azonosítás: VERIFIED_PRIMARY — Leírások/TI_BQ/BQ34Z110PWR.pdf
    #   (SLUSB55B, rev. B) "Table 20. Data Flash Summary" (nyomtatott 41.
    #   oldal) "Ra Tables" osztálysorai ("88 | Cell0 R_a Table | See Note
    #   1", "89 | xCell0 R_a Table | See Note 1") + "The Ra Tables" szakasz
    #   (nyomtatott 19. oldal — Kör 20 E18/SRC-F-1, Kör 18 SRC-F-4 SAJÁT
    #   korrekciója regresszió volt, 19→18-ra "pontosítva": önálló
    #   `pdftotext -f 18 -l 19` kinyeréssel megerősítve, a "18 Submit
    #   Documentation Feedback" lábjegyzet UTÁN, a "Submit Documentation
    #   Feedback 19" lábjegyzet ELŐTT áll, tehát nyomtatott 19. oldal):
    #   "This data is automatically updated during
    #   device operation... Profiles have format Cell0 R_a M, where M is
    #   the number that indicates the state of charge to which the value
    #   corresponds." L. CLAIM_LEDGER.yaml BQ-RA-001.
    # - A 32 bájtos OLVASÁSI blokkméret: VERIFIED_PRIMARY, de NEM a Table
    #   20-ból, hanem az "ACCESSING DATA FLASH" szakaszból (nyomtatott
    #   13-14. oldal — Kör 20 E18/SRC-F-7: a szakasz-cím a 13. oldalon
    #   kezdődik, DE a konkrét idézett mondat a 14. oldalon áll, önálló
    #   `pdftotext -f 13 -l 14` kinyeréssel megerősítve):
    #   "Reading and writing subclass data are block operations 32
    #   bytes in length." L. CLAIM_LEDGER.yaml BQ-DF-BLOCKSIZE-001 — ez
    #   szűkebb és erősebben forrásolt, mint a BQ-RA-001 blokkméret-része.
    # - A blokk BELSŐ elrendezése (2 bájt "Cell0 R_a flag" + 15 × I2
    #   big-endian, mOhm egység, előjel-tilalom) SLUSB55B-ből NEM ismert:
    #   Table 20 a subclass 88/89 soroknál Data Type/Min/Max oszlop helyett
    #   csak "See Note 1"-et ad — SOURCE_REQUIRED. Forrása kizárólag a
    #   cross-family SLUA664 (bq34z100 app note) — l. a lenti kommentet és
    #   SOURCE_CONFLICTS.md SCF-008 (REVIEW STOP az Ra-tábla belső
    #   elrendezésének módosítására). A "flag + 15 elem" KÜLSŐ váz
    #   device-specifikusan is megerősített (Leírások/TI_BQ/BQ34Z110 eval/
    #   2_5.png, TI "bq34z110 v0.04" EVSW, Ra Table fül: "Cell0 R_a flag" +
    #   "Cell0 R_a 0..14"), DE ugyanez a TI-eszköz az elemek Unit oszlopát
    #   "num"-nak írja, NEM mOhm-nak — az mOhm mértékegység, az I2 típus és
    #   az előjel-konvenció változatlanul csak SLUA664-ből ismert,
    #   háttérforrásként.
    # Subclass 89 ("xCell0 R_a Table", alternatív profil) NEM exponált — a
    # WP-AK-06 első szintje csak a Cell0 profilt követeli meg.
    DF_SC_RA_TABLE_CELL0 = 88
    DF_BLOCK_RA_TABLE = 0
    # [CRITICAL finding, 2. review-kör] a 32 bájtos blokk ELSŐ 2 bájtja NEM
    # resistance-érték, hanem a "Cell0 R_a flag" (2 bájtos státusz-flag):
    # "At the beginning of each of the two subclasses (profiles) is a flag
    # called Cell0 R_a flag... Each flag has two bytes". Forrás: Leírások/
    # TI_BQ/BQ34Z110 leírások/slua664.pdf (SLUA664, "Configuring the
    # bq34z100 Data Flash") — ez bq34z100 app note, TEHÁT BQ34Z110-re csak
    # HÁTTÉRFORRÁS (.claude/rules/30-bq34z110.md device-specific gate), az
    # ekvivalencia NINCS igazolva. L. Folyamatok/canonical/CLAIM_LEDGER.yaml
    # BQ-RA-001 (status: SOURCE_REQUIRED, REVIEW STOP a Ra-tábla belső
    # elrendezésének további módosítására) — a 32 bájtos blokk-méret és a
    # subclass 88/89 azonosítás MAGÁBAN a BQ34Z110 datasheetben (Table 20)
    # is megerősített, csak a flag+15-elemes BELSŐ elrendezés forrása
    # cross-family. A 15 tényleges R_a érték a 2-31 bájt-tartományban van.
    DF_RA_TABLE_FLAG_BYTES = 2
    # WP-AK-10 (SUP03-LEARN-002 gauge log): "Avg I Last Run"/"Avg P Last Run"
    # - UGYANAZ a State/blokk0, mint Qmax/UpdateStatus (subclass 82) — forrás:
    # Leírások/TI_BQ/BQ34Z110PWR.pdf (SLUSB55B) "Table 20. Data Flash
    # Summary" "Avg I Last Run"/"Avg P Last Run" sorai (kb-search-sel
    # forrásolva; l. F5-1 fenti kommentje). Mindkettő I2 előjeles, big-endian.
    DF_OFFSET_AVG_I_LAST_RUN = 7
    DF_OFFSET_AVG_P_LAST_RUN = 9

    # [SCF-011 újratervezés, 2026-09-11, felhasználói döntés -- a DEFEKTUS
    # (a lenti bájtminta SEALED chipen) HIL_VERIFIED, a JELEN FIX maga
    # HIL_PENDING, l. CLAIM_LEDGER.yaml BQ-DF-SEALED-001]
    # Defense-in-depth, DRIVER-SZINTŰ védelem BQ-DF-SEALED-001 ellen -- l.
    # CLAIM_LEDGER.yaml BQ-DF-SEALED-001: egy valós, COM10-en futó BQ34Z110-
    # en SEALED állapotban a subclass 82 (State) ÉS 88 (Ra Table Cell0)
    # data_flash_block()-olvasás (a driver akkori két DF-olvasási területe,
    # l. DF_SC_STATE/DF_SC_RA_TABLE_CELL0 fent) BYTE-AZONOS,
    # 32 bájtos, degenerált mintát ad vissza (21×0x00 + 11×0xFF) -- a
    # TI SLUSB55B szerint SEALED módban a DataFlashBlock() (0x3f) a
    # subclass-alapú címzés helyett egy Manufacturer Information Block
    # (A/B/C) kiválasztására áll át, tehát ez NEM hiba, hanem más parancs-
    # szemantika, aminek a válasza (üres/törölt flash mintázat) félrevezetően
    # ÉPP a driver eddigi "túl rövid blokk" kapuit kerüli meg (a blokk
    # PONTOSAN 32 bájt, tehát a hosszalapú kapuk nem fogják ki). Ez a
    # kontroll SZÁNDÉKOSAN az UNSEAL-állapottól FÜGGETLENÜL, minden
    # DF-state/ra_table olvasásnál fut (nem csak SEALED-ben) -- a hívó
    # (pl. GoldenGgController/TraditionalLearningController PRECOND-ja)
    # session-szintű, VISSZAELLENŐRZÖTT unseal()-je a domináns megelőzés,
    # ez a réteg a MÁSODIK, független biztosíték egy elfelejtett/
    # sikertelen unseal, vagy egy köztes, váratlan re-sealing esetére.
    #
    # MAX verifier P2-5 (2026-09-11) PONTOSÍTÁS: ez EXACT-MATCH ellenőrzés
    # a KONKRÉT, subclass 82/88/block 0-n HIL-en megfigyelt bájtmintára --
    # NEM egy általános "bármilyen SEALED-válasz" detektor. A HIL-dump
    # más subclassra (pl. subclass 80) egy MÁSIK, ismeretlen, szintén nem
    # valós mintát adott vissza (a SEALED DataFlashBlock() a chip melyik
    # Manufacturer Information Block-ját (A/B/C) választja, ismeretlen/
    # chip-specifikus lehet) -- ezt a kontroll NEM fogja el, és NE
    # dokumentáld/terjeszd ki úgy, mintha az lenne. KORREKCIÓ (Kör 21
    # Batch F2, HW21-01 dokumentációs javítás): az alábbi lista korábban
    # összemosta a MÁR HIL-evidenciázott és a NEM evidenciázott
    # subclass-okat. A subclass 36/48/49/64 (block 0) UGYANEBBEN a HIL-
    # dumpban byte-azonos degenerált mintát adott, mint a fent kezelt
    # 82/88 (l. CLAIM_LEDGER.yaml BQ-DF-SEALED-001) -- ezekre a
    # kiterjesztés HELYE/módja egy PROJECT_POLICY döntés (SOURCE_CONFLICTS.md
    # SCF-011), NEM új HIL-bizonyíték kérdése. Kiterjesztés a subclass
    # 80-ra (a HIL-dumpban MÁS, nem ezzel a mintával egyező, ismeretlen
    # választ adott) VAGY bármely subclass block≠0-ra TOVÁBBRA IS
    # SOURCE_REQUIRED/HIL_PENDING, valódi új bizonyítékot igényel.
    # A bájtminta MAGA modul-szinten, publikus néven él (l. fent,
    # `DEGENERATE_SEALED_DF_BLOCK`) -- Kör 21 F18 (HW21-01) óta a
    # `Bq34z110DataFlashService` is ugyanazt az EGYETLEN definíciót használja.
    def _reject_degenerate_sealed_df_block(self, block: bytes, *, field_name: str, subclass: int, block_id: int) -> None:
        if block == DEGENERATE_SEALED_DF_BLOCK:
            raise InstrumentInvalidReading(
                f"Bq34z110: {field_name} Data Flash blokk a HIL-igazolt "
                f"SEALED-módú degenerált mintát adta vissza (21×0x00+11×0xFF, "
                f"subclass={subclass} block={block_id}) — l. CLAIM_LEDGER.yaml "
                f"BQ-DF-SEALED-001. Ez NEM valós chip-tartalom (SLUSB55B: "
                f"SEALED módban a DataFlashBlock() Manufacturer Information "
                f"Block-ra vált át), a chip UNSEAL-állapotát ellenőrizni kell."
            )

    def read_pack_configuration(self) -> int:
        """R16-B6: SLUSB55B Rev. B p40 Table 20, SC64/off0, H2.

        DF big-endian: BQ-ENDIAN-001 (SLUA665 Fig. 8 + HIL).
        """
        block = self.transport.read_data_flash_block(64, 0)
        self._reject_degenerate_sealed_df_block(
            block, field_name="Pack Configuration", subclass=64, block_id=0,
        )
        if len(block) != 32:
            raise InstrumentInvalidReading(
                f"Bq34z110: Pack Configuration DF blokk hossza {len(block)}, vart 32"
            )
        return int.from_bytes(block[:2], byteorder="big", signed=False)

    def read_voltage_mV(self) -> int:
        return self.transport.read_standard_command_word(self.STD_CMD_VOLTAGE)

    def read_avg_current_mA(self) -> int:
        """Előjeles: pozitív = töltés, negatív = kisütés.

        Review-kör 2 (2026-09-09), friss Metrológia F-01 (l. CLAIM_LEDGER.yaml
        BQ-AVGCURR-SIGN-001) -- a jelenlegi Pico transport (TF1 repo
        pico_bq_service.py::bq_current()) MÁR előjelesen dekódol két'es-
        komplemens konverzióval, mielőtt az érték a PC-hez érne, tehát az
        alábbi sor a JELENLEGI transporton NO-OP defenzív redundancia (nem
        valódi ellentmondás a bq_gauge_calibration.py abs()-alapú
        útjával). Szándékosan megtartva: egy jövőbeli, eltérő transport-
        implementáció (pl. nyers, előjel nélküli szó) alatt ez a védelem
        válna aktívvá."""
        raw = self.transport.read_standard_command_word(self.STD_CMD_AVERAGE_CURRENT)
        return raw - 0x10000 if raw > 0x7FFF else raw

    def read_temperature_C(self) -> float:
        """A BQ Temperature() 0.1 K egységben ad vissza — Celsiusra konvertálva."""
        raw_01K = self.transport.read_standard_command_word(self.STD_CMD_TEMPERATURE)
        return raw_01K / 10.0 - 273.15

    def read_flags(self) -> int:
        return self.transport.read_standard_command_word(self.STD_CMD_FLAGS)

    def read_remaining_capacity_mAh(self) -> int:
        return self.transport.read_standard_command_word(self.STD_CMD_REMAINING_CAPACITY)

    def read_full_charge_capacity_mAh(self) -> int:
        return self.transport.read_standard_command_word(self.STD_CMD_FULL_CHARGE_CAPACITY)

    def read_state_of_charge_percent(self) -> int:
        return self.transport.read_standard_command_word(self.STD_CMD_STATE_OF_CHARGE)

    def read_cycle_count(self) -> int:
        return self.transport.read_standard_command_word(self.STD_CMD_CYCLE_COUNT)

    def read_dod0(self) -> int:
        return self.transport.read_standard_command_word(self.STD_CMD_DOD0)

    def read_control_status(self) -> int:
        return self.transport.read_control_subcommand_word(self.MAC_CONTROL_STATUS)

    def read_chem_id(self) -> int:
        return self.transport.read_control_subcommand_word(self.MAC_CHEM_ID)

    def read_device_type(self) -> int:
        return self.transport.read_control_subcommand_word(self.MAC_DEVICE_TYPE)

    def read_fw_version(self) -> int:
        return self.transport.read_control_subcommand_word(self.MAC_FW_VERSION)

    def read_hw_version(self) -> int:
        return self.transport.read_control_subcommand_word(self.MAC_HW_VERSION)

    def read_update_status(self) -> int:
        """[WP-AK-07 kapu-javítás] korábban egy tul rövid (malformed) blokkra
        csendesen 0-t adott vissza — ez egy VALÓS, ÉRVÉNYES UpdateStatus
        értékkel (0x00) összetéveszthető, félrevezető adat lett volna. A
        WP-AK-07 Konszolidált terv explicit kapuja ("BQ disconnect / stale
        data / malformed reply nem válhat csendesen 0 értékké") miatt most
        explicit InstrumentInvalidReading-et dob — ugyanaz a hiba-osztály,
        amit device_dmm.py már használ NaN/Inf/overload DMM-válaszokra."""
        block = self.transport.read_data_flash_block(self.DF_SC_STATE, self.DF_BLOCK_STATE)
        self._reject_degenerate_sealed_df_block(
            block, field_name="UpdateStatus", subclass=self.DF_SC_STATE, block_id=self.DF_BLOCK_STATE
        )
        if len(block) <= self.DF_OFFSET_UPDATE_STATUS:
            raise InstrumentInvalidReading(
                f"Bq34z110: UpdateStatus Data Flash blokk túl rövid "
                f"({len(block)} bájt, subclass={self.DF_SC_STATE} "
                f"block={self.DF_BLOCK_STATE}) — malformed reply, nem 0"
            )
        return block[self.DF_OFFSET_UPDATE_STATUS]

    def read_qmax(self) -> int:
        """[WP-AK-07 kapu-javítás] l. read_update_status() docstring — ugyanaz
        az elv, Qmax=0 is egy hihetőnek tűnő, de HAMIS érték lenne egy
        malformed blokkra.

        Governance-v2 review-kör 1 (CF-41, batch 5): a mező TI-SLUSB55B
        Table 20 szerint "Qmax Cell 0" (subclass 82/State, offset 0),
        Data Type **I2 (előjeles 16 bit)**, Min 0, Max 32767, Default
        4000 mAh -- korábban itt tévesen signed=False (előjel nélküli)
        dekódolás történt. Forrás-lánc (MAX independent verifier által
        2026-09-08-án önállóan megerősítve, l. CLAIM_LEDGER.yaml
        BQ-QMAX-001): (1) `pdftotext -f 41 -l 41 -table
        "Leírások/TI_BQ/BQ34Z110PWR.pdf"` -- ez a kinyerési mód
        torzulásmentesen adja a Table 20 41. oldalát, és egyértelműen
        elkülöníti "Qmax Cell 0" (subclass=82/State/offset=0) sorát a
        korábban ezzel összekevert "Board Offset" (subclass=104/
        Calibration/offset=10) sortól; (2) C:\\Users\\KS\\Desktop\\CLI\\
        TF1\\Prog\\TF1_basic.c (VALÓBAN FÜGGETLEN, HIL-validált
        gyártási firmware, más kódbázis) sor 2006/2684/2690-2691:
        `BQ_SC_STATE=82u`, "Qmax Cell 0 (Off 0)", és a readback-verify
        is `int16_t`-ként (előjelesen) dekódol. (A korábban hivatkozott
        Folyamatok/tervek/TRADITIONAL_LEARNING_BQ_PROVISIONING_
        AUDIT_2026-08-24.md és bq_data_flash_service.py::FIELD_REGISTRY
        NEM voltak egymástól független korroborációk -- a FIELD_REGISTRY
        forráskommentje maga is az audit dokumentumra hivatkozik --,
        ezt a MAX verifier tárta fel; l. CLAIM_LEDGER.yaml részletek.)
        Gyakorlati hatás: a
        FIAMM AGM/VRLA profilok tipikus Qmax-értékei (néhány ezer mAh)
        messze a 0x8000 (32768) határ alatt vannak, ahol az előjeles/
        előjel nélküli értelmezés eltérne -- a fix elsősorban a
        forrás-konformitást és a jövőbeli, nagyobb kapacitású
        profilokra/hibás DF-tartalomra való biztonságos viselkedést
        javítja (egy malformed/sérült blokk MSB-je most negatív,
        nyilvánvalóan implauzibilis értéket adna nagy pozitív helyett)."""
        block = self.transport.read_data_flash_block(self.DF_SC_STATE, self.DF_BLOCK_STATE)
        self._reject_degenerate_sealed_df_block(
            block, field_name="Qmax", subclass=self.DF_SC_STATE, block_id=self.DF_BLOCK_STATE
        )
        if len(block) < self.DF_OFFSET_QMAX + 2:
            raise InstrumentInvalidReading(
                f"Bq34z110: Qmax Data Flash blokk túl rövid "
                f"({len(block)} bájt, subclass={self.DF_SC_STATE} "
                f"block={self.DF_BLOCK_STATE}) — malformed reply, nem 0"
            )
        return int.from_bytes(
            block[self.DF_OFFSET_QMAX:self.DF_OFFSET_QMAX + 2],
            byteorder="big", signed=True,
        )

    def read_avg_i_last_run_mA(self) -> int:
        """[WP-AK-10, ugyanaz az elv mint read_qmax()/read_update_status()]
        malformed/túl rövid blokkra explicit InstrumentInvalidReading, nem
        csendes 0/hihető-hamis érték."""
        block = self.transport.read_data_flash_block(self.DF_SC_STATE, self.DF_BLOCK_STATE)
        self._reject_degenerate_sealed_df_block(
            block, field_name="Avg I Last Run", subclass=self.DF_SC_STATE, block_id=self.DF_BLOCK_STATE
        )
        if len(block) < self.DF_OFFSET_AVG_I_LAST_RUN + 2:
            raise InstrumentInvalidReading(
                f"Bq34z110: Avg I Last Run Data Flash blokk túl rövid "
                f"({len(block)} bájt, subclass={self.DF_SC_STATE} "
                f"block={self.DF_BLOCK_STATE}) — malformed reply, nem 0"
            )
        return int.from_bytes(
            block[self.DF_OFFSET_AVG_I_LAST_RUN:self.DF_OFFSET_AVG_I_LAST_RUN + 2],
            byteorder="big", signed=True,
        )

    def read_avg_p_last_run(self) -> int:
        """[WP-AK-10] l. read_avg_i_last_run_mA() docstring — ugyanaz az elv."""
        block = self.transport.read_data_flash_block(self.DF_SC_STATE, self.DF_BLOCK_STATE)
        self._reject_degenerate_sealed_df_block(
            block, field_name="Avg P Last Run", subclass=self.DF_SC_STATE, block_id=self.DF_BLOCK_STATE
        )
        if len(block) < self.DF_OFFSET_AVG_P_LAST_RUN + 2:
            raise InstrumentInvalidReading(
                f"Bq34z110: Avg P Last Run Data Flash blokk túl rövid "
                f"({len(block)} bájt, subclass={self.DF_SC_STATE} "
                f"block={self.DF_BLOCK_STATE}) — malformed reply, nem 0"
            )
        return int.from_bytes(
            block[self.DF_OFFSET_AVG_P_LAST_RUN:self.DF_OFFSET_AVG_P_LAST_RUN + 2],
            byteorder="big", signed=True,
        )

    RA_TABLE_VALUE_COUNT = 15
    _RA_TABLE_EXPECTED_LEN = DF_RA_TABLE_FLAG_BYTES + RA_TABLE_VALUE_COUNT * 2  # = 32

    def read_ra_table(self) -> list[float]:
        """15 db Ra érték mOhm-ban (Cell0 profil, subclass 88 block 0,
        SOC-racspontokhoz igazodó sorrendben). A blokk első
        DF_RA_TABLE_FLAG_BYTES bájtja a "Cell0 R_a flag" (l.
        DF_RA_TABLE_FLAG_BYTES docstring) — NEM resistance-érték, kihagyva.

        [WP-AK-07 kapu-javítás, független review I-1 lelet] KORÁBBAN egy túl
        rövid/hiányzó blokkra csendesen egy rövidebb/üres listát adott
        vissza — ez a WP-AK-07 kapu-szabály MEGSÉRTÉSE volt: a
        `BqDiagnosticsSnapshot`-ban `ra_table_mohm.valid=True, error=None`
        jelent volna meg, miközben az adat hiányos/hiányzó (a `qmax`/
        `update_status` UGYANARRA a hiányzó blokkra ekkor már helyesen
        `valid=False`-t adott — inkonzisztencia). Most a `read_qmax()`/
        `read_update_status()`-hoz hasonlóan explicit `InstrumentInvalidReading`-et
        dob, ha a blokk nem tartalmazza mind a 15 értéket."""
        block = self.transport.read_data_flash_block(
            self.DF_SC_RA_TABLE_CELL0, self.DF_BLOCK_RA_TABLE
        )
        self._reject_degenerate_sealed_df_block(
            block, field_name="Ra Table", subclass=self.DF_SC_RA_TABLE_CELL0, block_id=self.DF_BLOCK_RA_TABLE
        )
        if len(block) < self._RA_TABLE_EXPECTED_LEN:
            raise InstrumentInvalidReading(
                f"Bq34z110: Ra Table Data Flash blokk túl rövid "
                f"({len(block)} bájt, {self._RA_TABLE_EXPECTED_LEN} vártunk, "
                f"subclass={self.DF_SC_RA_TABLE_CELL0} block={self.DF_BLOCK_RA_TABLE}) "
                f"— malformed reply, nem üres/csonka lista"
            )
        values: list[float] = []
        for i in range(self.DF_RA_TABLE_FLAG_BYTES, self._RA_TABLE_EXPECTED_LEN, 2):
            raw = int.from_bytes(block[i:i + 2], byteorder="big", signed=True)
            values.append(float(raw))
        return values
