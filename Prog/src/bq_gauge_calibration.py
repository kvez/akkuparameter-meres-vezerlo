"""
bq_gauge_calibration.py — WP-AK-09 (BQ_GAUGE_CALIBRATION_ASSISTED workflow):
PC-oldali (workflow-master) állapotgépek a BQ34Z110 gyári/gyártási
kalibrációjához (SUP03-CAL-001..009).

Két KÜLÖN állapotgép, mert a forrás (Prog/TF1_basic.c, l. lent) is
ténylegesen külön eljárásként kezeli őket — a feszültség-kalibráció NEM
igényel ENTER_CAL-t, a CC/Board Offset + Current Gain viszont MINDIG egy
közös ENTER_CAL/EXIT_CAL munkameneten belül fut (a gain lépés az offset
lépés UTÁN, ugyanabban a kalibrációs módban):

- `CcGainCalibrationController` — Coulomb Counter/Board Offset (SUP03-
  CAL-002/006) + Pack Current gain (SUP03-CAL-003) egy közös munkamenetben.
- `VoltageCalibrationController` — Voltage Divider kalibráció (SUP03-
  CAL-004/008), önálló, ENTER_CAL nélkül.

FORRÁS: mindkét állapotgép 1:1 port-ja a Prog/TF1_basic.c-ben MÁR
production-ban bizonyítottan működő `bq_cal_start()`/`bq_cal_offset_done()`/
`bq_cal_gain_start()`/`bq_cal_gain_measure()` (TF1_basic.c:2789-2999) és
`bq_cal_vd_start()`/`bq_cal_vd_step()` (TF1_basic.c:3001-3070) algoritmusnak
— a felhasználó (2026-08-22) megerősítette, hogy ez a TF1_basic.c-s
implementáció a gyakorlatban helyesen működik. A pontos képletek/tartományok
forrása (MAX verifier, 2026-09-09, P3-1 -- a korábbi Leírások/TUDÁSBÁZIS.txt
sorhivatkozás mára feloldhatatlan, a fájl azóta 593 soros index lett):
Leírások/TI_BQ/BQ34Z110PWR.pdf (SLUSB55B) Table 20 (Data Flash Min/Max) és
Leírások/TI_BQ/BQ34Z110 leírások/slua665.pdf "Going to Production With the
bq34z1xx" PRODUCTION STEP 2 (Voltage Divider képlet).

BOARD/CC OFFSET várakozás forrása (2026-09-12, felhasználói kérésre végzett
max-effort dokumentumkutatás a teljes Leírások/TI_BQ fán, l. CC_OFFSET_SAVE_
RETRY_WAIT_S/CONTROL_STATUS_POLL_INTERVAL_S konstansok docstringje): Leírások/
TI_BQ/BQ34Z110 leírások/sluubf7a.pdf (SLUUBF7A, bq34110 Technical Reference
Manual -- azonos "bq34110" jelölésű rokon eszköz, HÁTTÉR/analóg forrás, NEM
bq34z110-específikus) Sec 5.5/5.6 "CC Offset"/"Board Offset" Factory
Calibration folyamatábrái és a Sec 5.5 CONTROL_STATUS pollozási rátakorlát
NOTE-ja; Leírások/TI_BQ/BQ34Z110 leírások/sluubw5a.pdf (SLUUBW5A, BQ34Z100-G1
Technical Reference Manual -- szintén analóg, más eszközcsalád) Sec 8.1/8.3
"Gauging Effects on I2C/HDQ Transactions" a Data Flash-commit okozta
busz-viselkedésről és a 70ms/3-retry ajánlásról.

ARCHITEKTÚRÁLIS KÜLÖNBSÉG a TF1_basic.c-hez képest — 0A akkuáram
előfeltétel: a TF1_basic.c a normál (nem szerviz-mód) firmware, SAJÁT
relévezérléssel tudja garantálni a 0A akkuáramot (`g_cal_req.bat_on=0`,
TF1_basic.c:2806-2820 kommentje). Ez a PC-oldali workflow a Pico
szerviz-módú BQ service rétegén (TF1 repo `pico_bq_service.py`) keresztül
éri el a BQ34Z110-et, aminek NINCS relévezérlése (l. TF1 repo modul
fejléce: "a BAT és PSU út fixen csatlakoztatott (ON) állapotban marad").
Ez NEM hiányzó funkció, hanem a golden-image/gyártási kalibráció fizikai
kontextusából adódó, forrásolt tény (SLUA665 8-lépéses gyártási
folyamatában a Step 2 "Calibrate the Voltage" a Step 5 "Connect the
cells" ELŐTT történik — Kör 19 D10/SRC19-03, saját pdftotext-kinyerés,
2026-09-18: a Step 4 "Perform any desired board level tests..." ÁLTALÁNOS
board-szintű TESZT, nem kalibráció -- az 8-lépéses folyamat SEHOL nem
nevez meg CC/board-offset kalibrációs lépést; a "kalibráció a cellák
csatlakoztatása ELŐTT történik" mérnöki következtetés Step 2-n keresztül
TOVÁBBRA IS érvényes, csak a lépésszám-hivatkozás volt pontatlan). Azaz a
bare board-on, battery nélkül végzett kalibrációnál a 0A előfeltétel a
fizikai felépítésből magától adódik. Ezért a
`CcGainCalibrationController` egy explicit `confirm_battery_disconnected()`
operátor-megerősítési lépéssel kezdődik — a workflow ezt NEM tudja saját
maga ellenőrizni/kikényszeríteni, csak naplózza az operátor megerősítését
(SUP03-CAL-001: "kezelő felé lépéseket adhat" — l. modul-szintű docstring
minden state-hez tartozó, GUI-nak szánt üzenetét is).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status
from Prog.src.exceptions import InstrumentError

# ── Időzítések (Prog/TF1_basic.c ÉRTÉKE alapján, MARGÓVAL a labor/gyártási
#    folyamat megbízhatóságáért — felhasználói döntés, 2026-08-22) ──────
# Kör 19 D10/SRC19-05 (2026-09-18) PONTOSÍTÁS: TF1_basic.c itt NEM
# forrás-hierarchia szerinti forrás (CLAUDE.md §3, 8. szint -- implemen-
# táció), csak egy MÁR MEGVALÓSÍTOTT AVR-referenciakód átvett száma --
# SLUSB55B (a device-specifikus elsődleges forrás) NEM ad kalibrációs
# időzítés/stabilizálási útmutatót. Ugyanez a DFAM-038 család, mint a
# lent már korrigált BOARD_OFFSET_WAIT_S/CC_OFFSET_WAIT_S -- azokat éles
# HIL-mérés ~18x túl rövidnek bizonyította (l. lent) -- tehát a TF1
# időzítési konstansok EBBEN a modulban BIZONYÍTOTTAN NEM validált
# minimumok. Érték-változás NEM javasolt itt (HIL-mérést igényelne,
# l. CLAUDE.md §3/§17), csak a forrás-állítás pontosítása -- PROJECT_
# POLICY rögzítve CLAIM_LEDGER.yaml-ban (a jelenlegi, egyetlen HIL-
# adatponttal). TF1_basic.c a fő ciklus rögzített ~195ms tick-jével
# számolt ciklusokban fejezi ki az ÁTVETT (nem forrás-igazolt) minimumot
# (CAL_OFFSET_CYC=82≈16.0s, CAL_GAIN_CYC=615≈120.0s, TF1_basic.c:576-579)
# — ez a PC-oldali port valós dt_s-t kap az `advance()` hívótól
# (CLAUDE.md §12 "tényleges dt_s, nem névleges runner tick"). A
# ténylegesen alkalmazott várakozás az átvett értéknél NAGYOBB, tudatos
# biztonsági/megbízhatósági tartalékkal — ez a döntés CSAK NÖVELI a
# várakozást (sosem csökkenti a hardveresen szükségesnek GONDOLT idő
# alá), ezért nem safety-limit-lazítás (CLAUDE.md §10.7), hanem annak
# fordítottja: konzervatívabb, hosszabb stabilizálási idő.
GAIN_STABILIZE_S = 150.0             # TF1-átvett minimum ≈120.0s (615×0.195s, TF1_basic.c:2855, NEM forrás-igazolt)
                                      # + felhasználói margó (labor/gyártási folyamat megbízhatósága)

# ── BOARD/CC OFFSET várakozás: BCA/CCA CONTROL_STATUS bit-pollozás ──────
# [2026-09-12, HARMADIK root-cause revízió] Az EREDETI, a TF1_basic.c-ből
# átvett `BOARD_OFFSET_WAIT_S=2.0` / `CC_OFFSET_WAIT_S=20.0` BLIND (bit-
# ellenőrzés nélküli) várakozásokat éles HIL-mérés cáfolta meg: az AVR
# referenciakód (`bq_cal_start()`) SOHA nem ellenőrzi a BCA/CCA bitet, csak
# vakon vár egy fix ideig és továbblép -- ez a Python port volt az ELSŐ,
# ami ezt ténylegesen ellenőrizte, és kiderült, hogy a "~2s" feltételezés
# EZEN az egységen kb. 18x alulbecsülte a valós időt.
#
# Két korábbi, önmagában életképes root-cause hipotézis (Pico `bq_control()`
# 2ms/200ms delay-eltérés; BQ34Z110 SLEEP mód automatikus belépése 0A
# alatt -- utóbbi HIL-lel MEGCÁFOLVA, l. Mérések/nyers_logok/hil_bq_cal_
# cc_offset_save_retry_20260912.md UPDATE 5) egyike sem bizonyult
# ELÉGSÉGESNEK önmagában: egy `_keepalive_ping()`-alapú, gyakori
# CONTROL_STATUS-forgalmat generáló mitigáció (MAX-verifikálva, commit
# `70a4aea`) élőben TOVÁBBRA IS teljes kudarcot vallott (6/6, majd 8/8
# hibás próbálkozás).
#
# Felhasználói kérésre elvégzett forráskutatás (max-effort agent, teljes
# `Leírások/TI_BQ` fa) feltárta: a `_keepalive_ping()` maga valószínűleg
# EGYENESEN SÉRTETTE a TI bq34110 Technical Reference Manual (SLUUBF7A,
# analóg forrás -- l. modul-szintű forrás-megjegyzés) explicit szabályát:
#
#   SLUUBF7A Sec 5.5 "CC Offset", NOTE (nyomtatott p.104):
#   "While the device is calibrating CC Offset, the host system must not
#   read the CONTROL_STATUS register at a rate greater than once every
#   0.5 seconds."
#
# Ugyanez a TRM (Sec 5.5/5.6, Figure 5-3/5-4) adja meg a HELYES eljárást
# BOARD_OFFSET_CAL(0x0009)/CC_OFFSET_CAL(0x000A)-ra: CONTROL_STATUS
# pollozás (legfeljebb 2 Hz-en) a BCA/CCA bit törléséig, NEM egy fix
# blind várakozás. Élő HIL-mérés (2026-09-12, COM10, 2/2 reprodukálva,
# `bq_cal_diag_ti_conformant.py`) ezt a mintát alkalmazva: BCA
# ~36.58s-nál törlődött (mindkét futásnál PONTOSAN ugyanakkor -- erősen
# reprodukálható, nem zajszintű ingadozás), CCA ~17.67s-nál. `cc_offset_
# save()` MINDKÉT futásnál 1 hiba + 1 gyors (70ms utáni) sikeres retry
# után lezárult -- 2/2 siker, szemben a `_keepalive_ping()`-es tervezet
# 0/6, 0/8 eredményeivel.
CONTROL_STATUS_POLL_INTERVAL_S = 0.6  # TI SLUUBF7A Sec 5.5 NOTE: "not... greater than
                                       # once every 0.5 seconds" -- 0.6s a betartandó
                                       # minimum fölötti margó (CLAUDE.md §10.7: csak
                                       # növeli, sosem csökkenti a kötelező távolságot)
BOARD_OFFSET_MAX_WAIT_S = 90.0        # HIL-mért BCA-törlés ~36.58s (2/2 reprodukálva,
                                       # egyetlen fizikai egységen) -- bőséges (>2x)
                                       # biztonsági margóval. Ha ennyi idő alatt sem
                                       # törlődik a BCA, az TRANSPORT hibaként bukik el
                                       # (nem végtelen várakozás).
CC_OFFSET_MAX_WAIT_S = 60.0           # HIL-mért CCA-törlés ~17.67s (2/2 reprodukálva)
                                       # -- bőséges margóval, ugyanazon elv szerint.

# ── CC_OFFSET_SAVE (0x000B) retry: TI SLUUBW5A analóg minta ─────────────
# TI egyetlen bq34z110-specifikus forrása (SLUSB55B) sem SLUUBF7A egyetlen
# folyamatábrája sem ad befejezés-ellenőrzést vagy retry-eljárást a
# CC_OFFSET_SAVE (0x000B) parancsra -- ez a parancs NEM szerepel egyik TI
# kalibrációs flowchartban sem (SOURCE_REQUIRED marad, l. CLAIM_LEDGER.yaml
# BQ-CAL-CCOFFSETSAVE-001). A legközelebbi, analóg TI-forrású mechanizmus
# (BQ34Z100-G1 TRM, SLUUBW5A):
#   Sec 8.1 "Gauging Effects on I2C Transactions" (p.49): egy Data Flash
#   frissítés "a series of 2-ms to 20-ms clock stretches"-t okozhat,
#   "random occurrences"-ként jelentkezve a host felé -- ez a projekt
#   TÉNYLEGES buszán (I2C) a hardver-szintű megoldás: a gauge saját maga
#   NYÚJTJA MEG az órajelet (clock stretch) a flash-frissítés idejére,
#   tehát a host-oldali várakozás/retry NEM szükséges emiatt I2C-n.
#   Sec 8.3 "Gauging Effects on HDQ Transactions" (p.49) -- Kör 19
#   D10/SRC19-04 (saját pdftotext-kinyerés, 2026-09-18) KORREKCIÓ: a
#   korábbi "a mechanizmus busz-független, csak a konkrét számadatok
#   HDQ-kontextusúak" állítás FORDÍTOTT volt. A forrás EXPLICIT: "HDQ,
#   like I2C, runs in the highest priority process. However, it CANNOT
#   CLOCK STRETCH, so flow control between the host and gauge is not
#   possible... It is recommended on timing-violation detection for the
#   host to delay 70 ms before restarting a transaction to allow for a
#   typical single-page flash update time. Three retries should be
#   allowed to account for a possible flash write recovery and a
#   possible two-page flash write." A 70ms/3-retry AJÁNLÁS MAGA is
#   HDQ-specifikus (mert HDQ nem tud clock-stretch-elni) -- I2C-n a
#   clock stretch a MÁSIK, natívan beépített mechanizmus ugyanarra a
#   problémára. A projekt ITT (I2C-n) TUDATOSAN, SOURCE_REQUIRED
#   döntésként alkalmazza az analóg HDQ-mintát defenzív host-oldali
#   biztonságként (a HIL 2/2 sikere ezt a döntést, nem a forrás-
#   értelmezést igazolja) -- l. CLAIM_LEDGER.yaml BQ-CAL-CCOFFSETSAVE-001
#   frissített megjegyzése.
# Élő HIL-mérés (2/2 reprodukálva) ezzel a mintával: 1 hiba + 1 retry
# (70ms után) = SIKER mindkétszer -- jelentősen megbízhatóbb, mint bármely
# korábbi (fix 2.0s egyetlen retry; 5-lépéses rövid-résű retry+keepalive)
# kísérlet.
CC_OFFSET_SAVE_RETRY_WAIT_S = 0.07    # TI SLUUBW5A Sec 8.3: "delay 70 ms"
CC_OFFSET_SAVE_MAX_RETRIES = 3        # TI SLUUBW5A Sec 8.3: "Three retries should be
                                       # allowed" -- retry-k száma az ELSŐ próbálkozás
                                       # UTÁN (tehát legfeljebb 4 összesített
                                       # próbálkozás). HIL-en a siker mindkétszer már
                                       # az 1. retry-nél bekövetkezett -- ez a konstans
                                       # a TI-forrású teljes margót tartja meg, nem a
                                       # csak a ténylegesen megfigyelt minimumot.
#
# ISMÉTELT-KIADÁS BIZTONSÁGOSSÁGA (változatlan): mivel a `[Errno 110]
# ETIMEDOUT` önmagában nem különbözteti meg az írás és az olvasás oldalát,
# a konzervatív feltevés az, hogy egy hibázó próbálkozás Control()-írása
# MÁR végrehajtódhatott a chipen -- minden retry EGY VALÓS TOVÁBBI
# CC_OFFSET_SAVE Data Flash commit LEHET. Ez SOURCE_REQUIRED marad (l.
# CLAIM_LEDGER.yaml BQ-CAL-CCOFFSETSAVE-001) -- empirikusan (több tucat
# megfigyelt ismétlés ezen a napon, ezen az egységen) minden próbálkozás
# után a chip biztonságosan SEALED-be tért vissza és a visszaolvasott
# cc_offset/board_offset értékek konzisztensek maradtak, de ez NEM
# gyártói idempotencia-garancia.
#
# [MAX verifier, 2026-09-12, TOVÁBBRA IS ÉRVÉNYES] TOVÁBBI KITETTSÉG:
# `PicoBQTransport._call()` saját maga is ismétel (max. `max_retries=3`x)
# soros I/O hiba, üres válasz (timeout) VAGY id-eltérés esetén (SZEMBEN az
# explicit `ok:false` METHOD_ERROR válasszal, ami AZONNAL felfelé terjed,
# retry nélkül) -- ez a kontroller-szintű retry MINDEN EGYES lépését akár
# 3 valós wire-kiadássá szorozhatja, tehát a legrosszabb esetben ennek a
# konstansnak 3 retry-je (+ az első próbálkozás = 4 kontroller-szintű
# próbálkozás) akár 4×3=12 valós CC_OFFSET_SAVE Control()-írást is
# jelenthet a chip felé egyetlen mentési kísérletre.
# R5-B2 (R5-MET-1, DFAM-008; DFAM-043): AverageCurrent() "It is updated
# every 1 second" (TI SLUSB55B Rev. B, p12). Az 1,25 s PROJEKTOLDALI
# időzítési margó az 1 s-os gyártói frissítés fölött (a korábbi 0,25 s a
# TF1_basic.c:2877 _delay_ms(250) átvétele volt -- forráshierarchia 8.
# szint). Az 1,25 s a tickenként mért dt_s-ből felhalmozott idő küszöbe:
# az akkumulátor a küszöb elérésekor, az olvasás ELŐTT nullázódik, így
# két minta-olvasást kiváltó tick KEZDETE között legalább 1,25 s mért idő
# telik el (0,5 s-os névleges produkciós tickkel ténylegesen ~1,5 s). A
# tényleges buszolvasások távolsága ennél kisebb lehet, ha egy olvasás
# RPC-latenciája (pl. transport-retry) változik -- ez dokumentált korlát.
# Névleges tickmodellben, elhanyagolható/változatlan RPC-latenciával a
# minták KÜLÖN regiszterfrissítésekből származnak; statisztikai
# függetlenséget ez nem bizonyít. HIL_PENDING: élő gain-validálás.
GAIN_SAMPLE_INTERVAL_S = 1.25
GAIN_SAMPLE_COUNT = 4                # TF1_basic.c:2867 "4x mérés"

# Érvényességi tartományok — SLUSB55B Table 20 "CC Gain/CC Delta (Note 4)"
# Min/Max oszlopa (nyomtatott p.41; DF-domain: CC Gain 0.1..40.0,
# CC Delta 2.98e4..1.19e6 — a Table 21 EVSW-domain konverziós szorzói
# (4.768/DF, 5677445/DF) itt NEM alkalmazandók, l. BQ-CAL-CCGAIN-001,
# F4-4/DFAM-020). A CC Delta tartomány ellenőrzése [audit-lelet, WP-AK-09]
# a TF1_basic.c-ből HIÁNYZIK (csak new_gain-t ellenőrzi) — ez a port
# explicit pótolja.
CAL_GAIN_MIN, CAL_GAIN_MAX = 0.1, 40.0
CAL_DELTA_MIN, CAL_DELTA_MAX = 2.98e4, 1.19e6


class CalState(Enum):
    IDLE = "IDLE"
    AWAITING_BATTERY_DISCONNECT_CONFIRM = "AWAITING_BATTERY_DISCONNECT_CONFIRM"
    ENTERING_CAL = "ENTERING_CAL"
    BOARD_OFFSET_RUNNING = "BOARD_OFFSET_RUNNING"
    CC_OFFSET_RUNNING = "CC_OFFSET_RUNNING"
    CC_OFFSET_SAVE_RETRY_WAIT = "CC_OFFSET_SAVE_RETRY_WAIT"
    AWAITING_REFERENCE_CURRENT = "AWAITING_REFERENCE_CURRENT"
    GAIN_STABILIZING = "GAIN_STABILIZING"
    MEASURING_GAIN = "MEASURING_GAIN"
    DONE = "DONE"
    ERROR = "ERROR"
    ABORTED = "ABORTED"


# CALERR_* nevek 1:1 megfeleltetve a Prog/TF1_basic.c:563-574 kódjainak
# (traceability — ugyanaz a hibaosztály ugyanazt a nevet kapja mindkét
# repóban), + DELTA_OUT_OF_RANGE az audit-lelet pótlásához és
# X10_UNSUPPORTED a WP-X10-ig tiltott áramskála jelzéséhez (R16-B6).
class CalErrorCode(Enum):
    NONE = "NONE"
    UNSEAL = "UNSEAL"
    FA = "FA"
    ENTER_CAL = "ENTER_CAL"
    DFREAD = "DFREAD"
    NOCURR = "NOCURR"
    DFWRITE = "DFWRITE"
    GAIN_OUT_OF_RANGE = "GAIN_OUT_OF_RANGE"
    DELTA_OUT_OF_RANGE = "DELTA_OUT_OF_RANGE"
    TRANSPORT = "TRANSPORT"
    X10_UNSUPPORTED = "X10_UNSUPPORTED"


# GUI-nak szánt, operátor felé mutatandó üzenetek állapotonként — l.
# SUP03-CAL-001 "kezelő felé lépéseket adhat" követelmény, ugyanaz a
# lépés-szám mintát követve, mint a Prog/pico_tf1_olvas.py "[CAL 1/4]"..
# "[CAL 4/4]" konzol-promptjai (TF1 repo, production-ban bizonyítottan
# használt UX minta).
OPERATOR_PROMPTS: dict[CalState, str] = {
    CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM:
        "[1/4] Ellenőrizd: az akku NINCS csatlakoztatva (bare board kalibráció). "
        "Erősítsd meg a folytatáshoz.",
    CalState.BOARD_OFFSET_RUNNING:
        "[1/4] Board Offset mérés folyamatban (BCA jelzőbit törlésére várva, "
        "HIL-mérés alapján akár ~40-90s is lehet) — várj.",
    CalState.CC_OFFSET_RUNNING:
        "[1/4] CC Offset mérés folyamatban (CCA jelzőbit törlésére várva, "
        "HIL-mérés alapján akár ~20-60s is lehet) — várj.",
    CalState.CC_OFFSET_SAVE_RETRY_WAIT:
        "[1/4] CC Offset mentés újrapróbálkozás alatt (Pico-BQ I2C "
        "visszaolvasás időtúllépés, TI-forrású 70ms/3-retry minta) — várj.",
    CalState.AWAITING_REFERENCE_CURRENT:
        "[2/4] Kösd rá a referencia áramforrást, mérd DMM-mel a pontos "
        "áramot (mA), majd add meg.",
    CalState.GAIN_STABILIZING:
        "[3/4] Gain stabilizálás folyamatban (~150s, ~1A referencia-áramra "
        "méretezve — eltérő áramnál hosszabb várakozás szükséges lehet) — "
        "ne szakítsd meg, ne módosítsd a referencia áramot.",
    CalState.MEASURING_GAIN:
        "[3/4] Gain mérés és Data Flash írás folyamatban — várj.",
    CalState.DONE:
        "[4/4] Kalibráció kész — CC Gain/Delta és offset értékek elmentve.",
}


@dataclass
class CcGainCalibrationSnapshot:
    """GUI-nak szánt, minden ismert adatot hordozó pillanatkép — soha nem
    csak egy `state`, hanem a mögötte álló bizonyíték is (ugyanaz az elv,
    mint `bq_controlled_write.ControlledWriteResult`)."""

    state: CalState
    error: CalErrorCode = CalErrorCode.NONE
    fail_reason: str = ""
    operator_prompt: str = ""
    cc_offset: Optional[int] = None
    board_offset: Optional[int] = None
    reference_current_mA: Optional[int] = None
    measured_current_samples_mA: tuple[int, ...] = field(default_factory=tuple)
    measured_current_mA: Optional[float] = None
    old_cc_gain: Optional[float] = None
    old_cc_delta: Optional[float] = None
    new_cc_gain: Optional[float] = None
    new_cc_delta: Optional[float] = None
    # Review-kör 5 (2026-09-09), Architektúra A-1 (P1, NEW_FAMILY
    # unverified_security_state_restore, MAX verifier kötelező): abort()/
    # _fail() az exit_cal()/seal() "best effort" takarítás hibáit korábban
    # csendben elnyelte (`except Exception: pass`) — a chip UNSEALED +
    # CALMODE állapotban maradhatott anélkül, hogy ez BÁRHOL (operátor,
    # log, artefaktum) attesztálódott volna. Üres tuple = a takarítás
    # minden lépése sikeres volt (vagy a takarítás meg sem kísérelt, l.
    # abort() AWAITING_BATTERY_DISCONNECT_CONFIRM ága, ahol ENTER_CAL sem
    # történt meg).
    cleanup_failures: tuple[str, ...] = field(default_factory=tuple)
    # HIL evidencia (2026-09-12, l. CC_OFFSET_SAVE_RETRY_WAIT_S/
    # CC_OFFSET_SAVE_MAX_RETRIES docstringje): a cc_offset_save() nem
    # mindig sikerül elsőre, TI SLUUBW5A Sec 8.3-forrású 70ms/3-retry
    # mintával kezelve. 0 = elsőre sikerült (vagy még nem jutott el idáig);
    # >0 = ennyi retry kellett a sikerhez -- ugyanaz az attesztálási elv,
    # mint a cleanup_failures mezőnél (ne csak a végeredmény, hanem az
    # ÚT is látható legyen, mert ez a SOURCE_REQUIRED ismételt-kiadás-
    # biztonságosság kérdés szempontjából releváns nyom).
    cc_offset_save_retries_used: int = 0


def _verify_sealed_or_record(transport, cleanup_failures: list[str], context: str) -> None:
    """[Kör 17 MAX-effort review, R17-09, DFAM-001+DFAM-032] EGYETLEN,
    NEM DOBÓ seal-állapot-ellenőrzés a `seal(confirm=True)` RPC UTÁN --
    ugyanazt a mintát követi, mint `traditional_learning_controller.py::
    _ensure_bq_sealed_or_flag()` (R17-05), de itt szándékosan SOHA nem
    dob kivételt: ezt a hívók KIZÁRÓLAG a `seal()` RPC már SIKERES
    visszatérése UTÁN, best-effort takarítási ágakból hívják (`_fail()`/
    `abort()`) vagy a DONE-út végén -- egy itt dobott kivétel a `_fail()`
    ágon a MÁR rögzített eredeti diagnosztikát maszkolná el (DFAM-033),
    a DONE-úton pedig egy jó eredményt alakítana ERROR-rá. Csak OLVAS,
    SOHA nem küld újabb `seal`-t (nem duplikált privilegizált írás).
    Hiba esetén a `cleanup_failures` listába rögzít, a hívó saját
    (már meglévő) attesztálási csatornáján keresztül éri el az
    operátort/riportot -- nincs új mező, nincs új GUI-elem."""
    try:
        cs = decode_control_status(
            transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS)
        )
    except Exception as exc:
        cleanup_failures.append(f"SEAL_VERIFY_FAILED:{context}:{exc}")
        return
    if not cs.ss:
        cleanup_failures.append(f"SEAL_NOT_VERIFIED:{context}:SS bit still clear")


class CcGainCalibrationController:
    """CC/Board Offset + Current Gain kalibráció állapotgépe — 1:1 port
    Prog/TF1_basic.c:2789-2999 (bq_cal_start/bq_cal_offset_done/
    bq_cal_gain_start/bq_cal_gain_measure) algoritmusából."""

    def __init__(self, transport) -> None:
        self._transport = transport
        self._state = CalState.IDLE
        self._error = CalErrorCode.NONE
        self._fail_reason = ""
        # Review-kör 5, Architektúra A-1 (P1) -- l. CcGainCalibrationSnapshot
        # docstringje.
        self._cleanup_failures: list[str] = []
        self._elapsed_in_state_s = 0.0
        # BOARD_OFFSET_RUNNING/CC_OFFSET_RUNNING alatt: teljes eltelt idő az
        # állapotba lépés óta, a BOARD_OFFSET_MAX_WAIT_S/CC_OFFSET_MAX_WAIT_S
        # timeout-hibához (l. advance()) -- külön az _elapsed_in_state_s-től,
        # ami itt a KÖVETKEZŐ CONTROL_STATUS pollig hátralévő időt méri.
        self._total_wait_in_state_s = 0.0
        self._cc_offset_save_retries_used = 0
        self._cc_offset: Optional[int] = None
        self._board_offset: Optional[int] = None
        self._reference_current_mA: Optional[int] = None
        self._measured_samples: list[int] = []
        self._gain_samples: list[int] = []
        self._old_gain: Optional[float] = None
        self._old_delta: Optional[float] = None
        self._new_gain: Optional[float] = None
        self._new_delta: Optional[float] = None
        # A modul GAIN_STABILIZE_S alapértéke ~1A referencia-áramra van
        # méretezve (a felhasználó 2026-08-22-i megjegyzése) - AverageCurrent()
        # szűrő-stabilizálási ideje a mért áram nagyságrendjétől/zajviszonyától
        # függhet, ezért submit_reference_current_mA() operátoronként
        # felülírhatja (l. ott), ha jelentősen eltérő referencia-áramot
        # használnak. Alapértelmezésben a sourced+margós konstans érvényes.
        self._gain_stabilize_s: float = GAIN_STABILIZE_S

    @property
    def state(self) -> CalState:
        return self._state

    def snapshot(self) -> CcGainCalibrationSnapshot:
        measured_avg = (
            sum(self._measured_samples) / len(self._measured_samples)
            if self._measured_samples else None
        )
        return CcGainCalibrationSnapshot(
            state=self._state,
            error=self._error,
            fail_reason=self._fail_reason,
            operator_prompt=OPERATOR_PROMPTS.get(self._state, ""),
            cc_offset=self._cc_offset,
            board_offset=self._board_offset,
            reference_current_mA=self._reference_current_mA,
            measured_current_samples_mA=tuple(self._measured_samples),
            measured_current_mA=measured_avg,
            old_cc_gain=self._old_gain,
            old_cc_delta=self._old_delta,
            new_cc_gain=self._new_gain,
            new_cc_delta=self._new_delta,
            cleanup_failures=tuple(self._cleanup_failures),
            cc_offset_save_retries_used=self._cc_offset_save_retries_used,
        )

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """UNSEAL/FULL ACCESS ELŐTT megáll az operátor 0A-akku
        megerősítésénél (l. modul docstring "architekturális különbség") —
        a TF1_basic.c-vel ellentétben ez a réteg NEM tudja saját maga
        lekapcsolni az akkut."""
        if self._state != CalState.IDLE:
            raise RuntimeError(f"start() csak IDLE állapotból hívható, jelenlegi: {self._state}")
        self._state = CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM

    def confirm_battery_disconnected(self) -> None:
        if self._state != CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM:
            raise RuntimeError(
                f"confirm_battery_disconnected() csak AWAITING_BATTERY_DISCONNECT_CONFIRM "
                f"állapotból hívható, jelenlegi: {self._state}"
            )
        self._guarded(self._do_unseal_and_start_offset, CalErrorCode.UNSEAL)

    def _check_non_x10(self) -> bool:
        # R16-B6: WP-X10-ig nincs aramskalazas; modositas elott friss DF-olvasas.
        # SLUSB55B Rev. B p20 Table 12: X10 = low byte bit7 (0x0080), p21 X10 MODE.
        try:
            pack_configuration = Bq34z110ReadOnlyDriver(self._transport).read_pack_configuration()
        except InstrumentError as exc:
            self._fail(CalErrorCode.DFREAD, f"Pack Configuration: {exc}")
            return False
        if pack_configuration & 0x0080:
            self._fail(
                CalErrorCode.X10_UNSUPPORTED,
                f"X10 kalibracio tiltva a WP-X10-ig (Pack Configuration=0x{pack_configuration:04X})",
            )
            return False
        return True

    def _do_unseal_and_start_offset(self) -> None:
        # R16-B6F1, DFAM-016: már az UNSEAL válasza is megszakadhat;
        # az abort() a részlegesen végrehajtott RPC után is takarítson.
        self._state = CalState.ENTERING_CAL
        self._transport.unseal(confirm=True)
        try:
            self._transport.full_access(confirm=True)
        except InstrumentError as exc:
            self._fail(CalErrorCode.FA, str(exc))
            return
        if not self._check_non_x10():
            return
        self._transport.enter_cal(confirm=True)
        self._transport.board_offset_cal(confirm=True)
        self._state = CalState.BOARD_OFFSET_RUNNING
        self._elapsed_in_state_s = 0.0
        self._total_wait_in_state_s = 0.0

    def advance(self, dt_s: float) -> CalState:
        """Az operátor-vezérelt promptok (AWAITING_*) KIVÉTELÉVEL minden
        más állapot itt haladhat előre — időzített várakozások (BOARD_
        OFFSET_RUNNING/CC_OFFSET_RUNNING/GAIN_STABILIZING) a felhalmozott
        `dt_s` alapján, nem egy blokkoló `sleep()`-pel (a hívó — a GUI
        worker QTimer-je — dönti el a tick gyakoriságát).

        [2026-09-12, HARMADIK root-cause revízió] BOARD_OFFSET_RUNNING/
        CC_OFFSET_RUNNING MOST a BCA/CCA CONTROL_STATUS bitet POLLOZZA
        (legfeljebb `CONTROL_STATUS_POLL_INTERVAL_S`-enként, TI SLUUBF7A
        Sec 5.5 rátakorlátja szerint), NEM egy fix blind várakozást
        alkalmaz -- l. a modul elején a `CONTROL_STATUS_POLL_INTERVAL_S`
        konstans docstringje a HIL-evidenciáért. Egy korábbi tervezet
        (`_keepalive_ping()`, l. git history commit `70a4aea`) minden
        ticknél olvasta a CONTROL_STATUS-t, ami valószínűleg ÉPPEN a fenti
        TI rátakorlátot sértette -- eltávolítva.

        MEGJEGYZÉS a válaszidőről: ez a metódus maga NEM blokkol
        `sleep()`-pel, DE a benne hívott transport-metódusok szinkron I/O-t
        végeznek, és egy halott/lassú soros linken `PicoBQTransport._call()`
        a saját `max_retries`-én és a `connect(timeout=...)` értékén át akár
        másodperces nagyságrendű ideig is blokkolhatja EZT a hívást (és
        ezzel a hívó GUI worker szálát). Ez NEM új safety-kockázat (a chip
        UNSEALED+FULL_ACCESS+CALMODE-ban marad, amíg ez az `advance()` hívás
        vissza nem tér, és az `abort()`/operátor-válaszidő ezzel arányosan
        nő egy döglött linken) -- ez minden `_guarded(...)` hívásnál
        fennáll, függetlenül a fenti pollozási átterezéstől."""
        if self._state == CalState.BOARD_OFFSET_RUNNING:
            self._elapsed_in_state_s += dt_s
            self._total_wait_in_state_s += dt_s
            if self._total_wait_in_state_s >= BOARD_OFFSET_MAX_WAIT_S:
                self._fail(
                    CalErrorCode.TRANSPORT,
                    f"BCA nem törlődött {BOARD_OFFSET_MAX_WAIT_S}s alatt",
                )
            elif self._elapsed_in_state_s >= CONTROL_STATUS_POLL_INTERVAL_S:
                self._elapsed_in_state_s = 0.0
                self._guarded(self._poll_board_offset_done, CalErrorCode.TRANSPORT)
        elif self._state == CalState.CC_OFFSET_RUNNING:
            self._elapsed_in_state_s += dt_s
            self._total_wait_in_state_s += dt_s
            if self._total_wait_in_state_s >= CC_OFFSET_MAX_WAIT_S:
                self._fail(
                    CalErrorCode.TRANSPORT,
                    f"CCA nem törlődött {CC_OFFSET_MAX_WAIT_S}s alatt",
                )
            elif self._elapsed_in_state_s >= CONTROL_STATUS_POLL_INTERVAL_S:
                self._elapsed_in_state_s = 0.0
                self._guarded(self._poll_cc_offset_done, CalErrorCode.TRANSPORT)
        elif self._state == CalState.CC_OFFSET_SAVE_RETRY_WAIT:
            self._elapsed_in_state_s += dt_s
            if self._elapsed_in_state_s >= CC_OFFSET_SAVE_RETRY_WAIT_S:
                self._attempt_save_offset()
        elif self._state == CalState.GAIN_STABILIZING:
            self._elapsed_in_state_s += dt_s
            if self._elapsed_in_state_s >= self._gain_stabilize_s:
                self._enter_measuring_gain()
        elif self._state == CalState.MEASURING_GAIN:
            # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás
            # NEW-1 (DFAM-008 4. előfordulás): a 4 mintát korábban egyetlen
            # advance()-hívás alatt, egymás után, demeres nélkül olvastuk —
            # a GAIN_SAMPLE_INTERVAL_S=0.25 (TF1_basic.c:2877 _delay_ms(250))
            # konstans deklarálva volt, de nulla fogyasztóval. Mivel
            # AverageCurrent() a TI forrás szerint csak 1s-enként frissül,
            # a demeres nélküli 4 olvasás ~40-120ms alatt lezajlott, tehát a
            # 4 "minta" valójában UGYANAZ a regiszter-érték volt -- az
            # átlagolás metrológiailag üres volt. Most a mintákat a
            # dt_s-akkumulátoron át, egymást követő advance()-tickeken
            # keresztül vesszük (blokkoló sleep() NÉLKÜL, ugyanúgy, mint a
            # BOARD_OFFSET_RUNNING/CC_OFFSET_RUNNING/GAIN_STABILIZING
            # időzítések -- a hívó GUI worker QTimer-je dönti el a tick
            # gyakoriságát).
            #
            # R5-B2 (R5-MET-1): a NEW-1 utáni 0,25 s-os köz a 0,5 s-os
            # produkciós tick mellett minden tickben olvasott, így a 4
            # minta csak 2-3 KÜLÖN frissítést fedett le. A köz mostantól
            # GAIN_SAMPLE_INTERVAL_S (1,25 s, l. a konstans kommentjét).
            self._elapsed_in_state_s += dt_s
            if self._elapsed_in_state_s >= GAIN_SAMPLE_INTERVAL_S:
                self._elapsed_in_state_s = 0.0
                self._guarded(self._take_one_gain_sample, CalErrorCode.TRANSPORT)
        return self._state

    def _enter_measuring_gain(self) -> None:
        self._state = CalState.MEASURING_GAIN
        self._gain_samples = []
        self._elapsed_in_state_s = 0.0

    def _poll_board_offset_done(self) -> None:
        """[2026-09-12, HARMADIK root-cause revízió] TI SLUUBF7A Sec 5.6
        "Board Offset" folyamatábrájának (analóg forrás) portja: a BCA
        CONTROL_STATUS bit pollozása a `advance()` BOARD_OFFSET_RUNNING
        ágából, `CONTROL_STATUS_POLL_INTERVAL_S`-enként hívva. Ha a poll
        (a CONTROL_STATUS olvasás maga) hibázik, ezt a hívó `_guarded(...,
        TRANSPORT)` a megszokott módon `_fail()`-re fordítja -- ha a poll
        SIKERES, de BCA MÉG 1, ez a metódus egyszerűen visszatér (a state
        BOARD_OFFSET_RUNNING marad, a következő tick újra próbál)."""
        cs = decode_control_status(
            self._transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS)
        )
        if not cs.bca:
            self._start_cc_offset()

    def _start_cc_offset(self) -> None:
        if not self._check_non_x10():
            return
        self._transport.cc_offset_cal(confirm=True)
        self._state = CalState.CC_OFFSET_RUNNING
        self._elapsed_in_state_s = 0.0
        self._total_wait_in_state_s = 0.0

    def _poll_cc_offset_done(self) -> None:
        """Mint `_poll_board_offset_done()`, a CCA bitre (TI SLUUBF7A
        Sec 5.5 "CC Offset")."""
        cs = decode_control_status(
            self._transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS)
        )
        if not cs.cca:
            self._attempt_save_offset()

    def _finish_save_offset(self) -> None:
        data = self._transport.read_calibration_data()
        self._cc_offset = data["cc_offset"]
        self._board_offset = data["board_offset"]
        self._state = CalState.AWAITING_REFERENCE_CURRENT

    def _attempt_save_offset(self) -> None:
        """`cc_offset_save()` non-blocking újrapróbálkozása TI SLUUBW5A Sec
        8.3 (BQ34Z100-G1 TRM, analóg forrás) 70ms/3-retry mintája szerint —
        l. `CC_OFFSET_SAVE_RETRY_WAIT_S`/`CC_OFFSET_SAVE_MAX_RETRIES`
        docstringje a root cause elemzésért és a SOURCE_REQUIRED
        forrásstátuszért (minden retry egy VALÓS további Data Flash commit
        lehet, nem csak egy ártalmatlan ismétlés — l. SCF-012,
        BQ-CAL-CCOFFSETSAVE-001). A retry SZÁNDÉKOSAN csak a
        `cc_offset_save()` hívást fedi le, a rákövetkező
        `read_calibration_data()`-t (`_finish_save_offset()`) NEM — az
        utóbbi hibája (pl. hiányzó/csonka mező) adatintegritási probléma,
        amit egy időzítési retry nem old meg, ezért ott a korábbi,
        változatlan `_guarded(..., DFWRITE)` út érvényes. Ha
        `CC_OFFSET_SAVE_MAX_RETRIES` retry is hibázik, a hiba a korábbi,
        változatlan `_fail(DFWRITE, ...)` úton terjed tovább (nincs
        végtelen retry-hurok: `_cc_offset_save_retries_used` monoton nő,
        és `CC_OFFSET_SAVE_MAX_RETRIES` elérésekor MINDIG `_fail()`-hez
        vezet, függetlenül attól, hány retry történt)."""
        if not self._check_non_x10():
            return
        try:
            self._transport.cc_offset_save(confirm=True)
        except InstrumentError as exc:
            if self._cc_offset_save_retries_used < CC_OFFSET_SAVE_MAX_RETRIES:
                self._cc_offset_save_retries_used += 1
                self._state = CalState.CC_OFFSET_SAVE_RETRY_WAIT
                self._elapsed_in_state_s = 0.0
            else:
                self._fail(CalErrorCode.DFWRITE, str(exc))
            return
        self._guarded(self._finish_save_offset, CalErrorCode.DFWRITE)

    def submit_reference_current_mA(
        self, reference_current_mA: int, stabilize_s: Optional[float] = None,
    ) -> None:
        """CAL:OK:<mA> megfelelője (Prog/pico_tf1_olvas.py:421-434 mintája) —
        a DMM-mel mért, a kalibráláshoz csatlakoztatott referencia-áram.

        `stabilize_s` (opcionális): felülírja a modul `GAIN_STABILIZE_S`
        alapértékét erre a futásra - az alapérték ~1A referencia-áramra lett
        méretezve (sourced minimum + felhasználói margó), az AverageCurrent()
        szűrő stabilizálási ideje viszont a mért áram nagyságrendjétől is
        függhet. Ha jelentősen eltérő (pl. sokkal kisebb/nagyobb) referencia-
        áramot használnak, a hívó (GUI/operátor) itt tud ennek megfelelő,
        empirikusan meghatározott várakozási időt megadni - alapértelmezésben
        a sourced+margós konstans érvényes, nincs automatikus (nem validált)
        áram-arányos képlet."""
        if self._state != CalState.AWAITING_REFERENCE_CURRENT:
            raise RuntimeError(
                f"submit_reference_current_mA() csak AWAITING_REFERENCE_CURRENT "
                f"állapotból hívható, jelenlegi: {self._state}"
            )
        if reference_current_mA == 0:
            self._fail(CalErrorCode.NOCURR, "reference_current_mA nem lehet 0")
            return
        self._reference_current_mA = reference_current_mA
        if stabilize_s is not None:
            self._gain_stabilize_s = stabilize_s
        self._measured_samples = []
        self._state = CalState.GAIN_STABILIZING
        self._elapsed_in_state_s = 0.0

    def _take_one_gain_sample(self) -> None:
        """L. az advance() MEASURING_GAIN ágának NEW-1 kommentje: egy
        GAIN_SAMPLE_INTERVAL_S-enkénti hívás, EGYETLEN AverageCurrent()
        mintát vesz fel. A GAIN_SAMPLE_COUNT-adik minta után azonnal
        lezárja a mérést (_finish_gain_measurement())."""
        sample = self._transport.read_standard_command_word(
            Bq34z110ReadOnlyDriver.STD_CMD_AVERAGE_CURRENT
        )
        self._gain_samples.append(sample)
        self._measured_samples = list(self._gain_samples)
        if len(self._gain_samples) >= GAIN_SAMPLE_COUNT:
            self._finish_gain_measurement()

    def _finish_gain_measurement(self) -> None:
        samples = self._gain_samples
        i_meas = abs(sum(samples) / len(samples))
        if i_meas == 0:
            self._fail(CalErrorCode.NOCURR, "mért AverageCurrent() átlaga 0 mA")
            return

        data = self._transport.read_calibration_data()
        gain_f = data["cc_gain"]
        delta_f = data["cc_delta"]
        self._old_gain, self._old_delta = gain_f, delta_f
        if not (CAL_GAIN_MIN <= gain_f <= CAL_GAIN_MAX):
            self._fail(CalErrorCode.DFREAD, f"tárolt cc_gain={gain_f!r} tartományon kívül")
            return

        # Korrekció: new = old × (I_ref / I_meas) — Prog/TF1_basic.c:2916-2922
        # mintája, Gain és Delta azonos faktorral skálázódik.
        # (reference_current_mA itt garantáltan nem None - csak
        # submit_reference_current_mA() után jutunk GAIN_STABILIZING-be.)
        assert self._reference_current_mA is not None
        k = self._reference_current_mA / i_meas
        new_gain = gain_f * k
        new_delta = delta_f * k if delta_f > 0 else delta_f
        if not (CAL_GAIN_MIN <= new_gain <= CAL_GAIN_MAX):
            self._fail(CalErrorCode.GAIN_OUT_OF_RANGE, f"new_gain={new_gain!r} tartományon kívül")
            return
        if not (CAL_DELTA_MIN <= new_delta <= CAL_DELTA_MAX):
            self._fail(CalErrorCode.DELTA_OUT_OF_RANGE, f"new_delta={new_delta!r} tartományon kívül")
            return

        if not self._check_non_x10():
            return
        self._transport.write_calibration_gain_delta(new_gain, new_delta, confirm=True)
        self._new_gain, self._new_delta = new_gain, new_delta
        self._transport.exit_cal(confirm=True)
        self._transport.seal(confirm=True)
        # R17-09 (DFAM-001+032): a legmagasabb privilégiumú BQ-útvonal
        # (unseal+full_access) DONE-lezárása -- l. _verify_sealed_or_record()
        # docstringje. NEM DOBÓ, NEM küld újabb seal-t.
        _verify_sealed_or_record(self._transport, self._cleanup_failures, "CC_GAIN_DONE")
        self._state = CalState.DONE

    # ------------------------------------------------------------------ #
    def abort(self) -> None:
        """CAL:ABORT megfelelője. A TF1 bq_cal_abort()-tól szándékosan eltér:
        a _fail()-lel egységesen már az UNSEAL megkísérlésétől EXIT_CAL +
        SEAL takarítást végez, akkor is, ha az ENTER_CAL még nem történt meg.
        Így a részlegesen végrehajtott RPC után is megkísérli a lezárást."""
        if self._state in (CalState.IDLE, CalState.DONE, CalState.ERROR, CalState.ABORTED):
            return
        if self._state != CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM:
            # Review-kör 5, Architektúra A-1 (P1) -- l. CcGainCalibrationSnapshot
            # docstringje: a korábbi `except Exception: pass` a chip
            # UNSEALED/CALMODE-ban maradását csendben elnyelte volna.
            try:
                self._transport.exit_cal(confirm=True)
            except Exception as exc:
                self._cleanup_failures.append(f"EXIT_CAL_FAILED:{exc}")
            try:
                self._transport.seal(confirm=True)
            except Exception as exc:
                self._cleanup_failures.append(f"SEAL_FAILED:{exc}")
            else:
                # R17-09: csak SIKERES seal() UTAN ellenorzunk -- soha nem
                # futtatunk masodik RPC-t egy mar hibazo seal utan
                # (DFAM-033: elkerulnenk egy masodik hiba altal elfedni az
                # elsot).
                _verify_sealed_or_record(self._transport, self._cleanup_failures, "CC_GAIN_ABORT")
        self._state = CalState.ABORTED

    def _guarded(self, fn, error_code: CalErrorCode) -> None:
        try:
            fn()
        except InstrumentError as exc:
            self._fail(error_code, str(exc))

    def _fail(self, code: CalErrorCode, reason: str) -> None:
        # Minden hiba-ág EXIT_CAL + SEAL "best effort" takarítással zár —
        # Prog/TF1_basic.c minden CALERR_* ágának mintája (l. pl.
        # TF1_basic.c:2873/2886/2891/2898 stb.). Review-kör 5, Architektúra
        # A-1 (P1): a takarítás EREDMÉNYE (nem csak a megkísérlése) most
        # attesztálva van -- l. CcGainCalibrationSnapshot.cleanup_failures.
        try:
            self._transport.exit_cal(confirm=True)
        except Exception as exc:
            self._cleanup_failures.append(f"EXIT_CAL_FAILED:{exc}")
        try:
            self._transport.seal(confirm=True)
        except Exception as exc:
            self._cleanup_failures.append(f"SEAL_FAILED:{exc}")
        else:
            # R17-09: l. abort() azonos hivatkozású megjegyzése.
            _verify_sealed_or_record(self._transport, self._cleanup_failures, "CC_GAIN_FAIL")
        self._error = code
        self._fail_reason = reason
        self._state = CalState.ERROR


class VoltageCalState(Enum):
    IDLE = "IDLE"
    UNSEALING = "UNSEALING"
    AWAITING_DMM_VOLTAGE = "AWAITING_DMM_VOLTAGE"
    WRITING = "WRITING"
    DONE = "DONE"
    ERROR = "ERROR"
    ABORTED = "ABORTED"


@dataclass
class VoltageCalibrationSnapshot:
    state: VoltageCalState
    fail_reason: str = ""
    operator_prompt: str = ""
    bq_voltage_mV: Optional[int] = None
    old_voltage_divider: Optional[int] = None
    new_voltage_divider: Optional[int] = None
    dmm_voltage_mV: Optional[int] = None
    # Review-kör 5, Architektúra A-1 (P1) -- l. CcGainCalibrationSnapshot
    # azonos hivatkozású mező docstringje.
    cleanup_failures: tuple[str, ...] = field(default_factory=tuple)


_VOLTAGE_PROMPTS: dict[VoltageCalState, str] = {
    VoltageCalState.AWAITING_DMM_VOLTAGE:
        "[1/2] Mérd meg DMM-mel a csomag/board feszültségét (mV), majd add meg.",
    VoltageCalState.DONE:
        "[2/2] Feszültség-kalibráció kész — Voltage Divider elmentve.",
}


class VoltageCalibrationController:
    """Voltage Divider kalibráció — 1:1 port Prog/TF1_basic.c:3001-3070
    (bq_cal_vd_start/bq_cal_vd_step) algoritmusból. Egyedi, gyártott
    boardonként/packonként végzendő (SUP03-CAL-008: NEM átlagolható a
    20-boardos current/temp statisztikával)."""

    def __init__(self, transport) -> None:
        self._transport = transport
        self._state = VoltageCalState.IDLE
        self._fail_reason = ""
        self._bq_voltage_mV: Optional[int] = None
        self._old_vd: Optional[int] = None
        self._new_vd: Optional[int] = None
        self._dmm_mV: Optional[int] = None
        # Review-kör 5, Architektúra A-1 (P1) -- l. CcGainCalibrationSnapshot
        # azonos hivatkozású mező docstringje.
        self._cleanup_failures: list[str] = []

    @property
    def state(self) -> VoltageCalState:
        return self._state

    def snapshot(self) -> VoltageCalibrationSnapshot:
        return VoltageCalibrationSnapshot(
            state=self._state,
            fail_reason=self._fail_reason,
            operator_prompt=_VOLTAGE_PROMPTS.get(self._state, ""),
            bq_voltage_mV=self._bq_voltage_mV,
            old_voltage_divider=self._old_vd,
            new_voltage_divider=self._new_vd,
            dmm_voltage_mV=self._dmm_mV,
            cleanup_failures=tuple(self._cleanup_failures),
        )

    def start(self) -> None:
        if self._state != VoltageCalState.IDLE:
            raise RuntimeError(f"start() csak IDLE állapotból hívható, jelenlegi: {self._state}")
        # R16-B6b, DFAM-016: az UNSEAL megkísérlését már az RPC előtt jelöljük;
        # megszakadt válasz után az abort() SEAL + SS-ellenőrzést végezzen.
        self._state = VoltageCalState.UNSEALING
        try:
            self._transport.unseal(confirm=True)
            self._transport.full_access(confirm=True)
            self._state = VoltageCalState.AWAITING_DMM_VOLTAGE
        except InstrumentError as exc:
            self._fail(str(exc))

    def submit_dmm_voltage_mV(self, dmm_voltage_mV: int) -> None:
        """CAL:V:<mV> megfelelője (Prog/pico_tf1_olvas.py:443-453 mintája)."""
        if self._state != VoltageCalState.AWAITING_DMM_VOLTAGE:
            raise RuntimeError(
                f"submit_dmm_voltage_mV() csak AWAITING_DMM_VOLTAGE állapotból "
                f"hívható, jelenlegi: {self._state}"
            )
        if dmm_voltage_mV <= 0:
            self._fail("dmm_voltage_mV nem lehet <= 0")
            return
        self._dmm_mV = dmm_voltage_mV
        try:
            self._state = VoltageCalState.WRITING
            bq_v = self._transport.read_standard_command_word(
                Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE
            )
            if bq_v == 0:
                self._fail("a BQ jelentett feszültsége 0 mV")
                return
            self._bq_voltage_mV = bq_v
            old_vd = self._transport.read_voltage_divider()
            self._old_vd = old_vd
            # new_VD = cur_VD x dmm_mv / fg_volt — SLUA665 "Going to
            # Production With the bq34z1xx" nyomtatott 10. oldal, PRODUCTION
            # STEP 2 "Calibrate the Voltage": "New Voltage Divider = Voltage
            # Divider * Known Applied Voltage / I2C Reported Voltage" (Kör 19
            # D10/SRC19-02, saját pdftotext-kinyerés, 2026-09-18: a korábbi
            # "13721. sor" egy TUDÁSBÁZIS.txt sorszám volt, nem SLUA665-
            # locator) / Prog/TF1_basic.c:3027.
            #
            # Review-kör 2 (2026-09-08), Gyári forrás F-4 (felhasználói
            # jóváhagyással, 2026-09-09): a KÉPLET NYERS (nem vágott)
            # eredményét ellenőrizzük az U2 (0-65535) tartományon KÍVÜL --
            # ha ez túllóg (pl. rossz VOLSEL-bit vagy hibás soros-cellaszám
            # miatt), az korábban CSENDBEN 65535-re/0-ra vágódott és a
            # session DONE-t jelentett, miközben egy ILYEN hibás Voltage
            # Divider érték a chipre írva MINDEN JÖVŐBELI feszültségmérést
            # korrumpálna, amíg valaki ezt észre nem veszi -- eval
            # screenshot (3.PNG, Host Calibration Step 2) és SLUSB55B
            # egyaránt explicit előfeltételként jelöli a VOLSEL bit/soros-
            # cellaszám/külső-osztás helyességét. Ha a kép TÚLLÓG, ez maga
            # az előfeltétel sérülésének a jele -- ERROR, nincs írás.
            new_vd_raw = round(old_vd * dmm_voltage_mV / bq_v)
            if not (0 <= new_vd_raw <= 65535):
                self._fail(
                    f"a számított Voltage Divider ({new_vd_raw}) kívül esik "
                    f"az U2 (0-65535) tartományon -- rossz VOLSEL-bit vagy "
                    f"hibás soros-cellaszám gyanús, nincs írás"
                )
                return
            new_vd = new_vd_raw
            self._transport.write_voltage_divider(new_vd, confirm=True)
            self._new_vd = new_vd
            self._transport.seal(confirm=True)
            # R17-09 (DFAM-001+032): l. CcGainCalibrationController azonos
            # hivatkozású megjegyzése.
            _verify_sealed_or_record(self._transport, self._cleanup_failures, "VOLTAGE_CAL_DONE")
            self._state = VoltageCalState.DONE
        except InstrumentError as exc:
            self._fail(str(exc))

    def abort(self) -> None:
        if self._state in (VoltageCalState.IDLE, VoltageCalState.DONE,
                            VoltageCalState.ERROR, VoltageCalState.ABORTED):
            return
        # Review-kör 5, Architektúra A-1 (P1) -- l. CcGainCalibrationSnapshot
        # docstringje ugyanerről a csendben elnyelt seal-hibáról.
        try:
            self._transport.seal(confirm=True)
        except Exception as exc:
            self._cleanup_failures.append(f"SEAL_FAILED:{exc}")
        else:
            _verify_sealed_or_record(self._transport, self._cleanup_failures, "VOLTAGE_CAL_ABORT")
        self._state = VoltageCalState.ABORTED

    def _fail(self, reason: str) -> None:
        try:
            self._transport.seal(confirm=True)
        except Exception as exc:
            self._cleanup_failures.append(f"SEAL_FAILED:{exc}")
        else:
            _verify_sealed_or_record(self._transport, self._cleanup_failures, "VOLTAGE_CAL_FAIL")
        self._fail_reason = reason
        self._state = VoltageCalState.ERROR
