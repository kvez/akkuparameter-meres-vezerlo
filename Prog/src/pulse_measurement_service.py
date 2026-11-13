"""
PulseMeasurementService — SUP01-ARCH-001: külön acquisition komponens a pulse-
resistance méréshez, hogy a fő 2 s TestRunner tick ne kezelje közvetlenül a
preciz mintavételt (SUP01-ACCEPT-001/6).

A feszültség-mintavétel a Keysight 34465A determinisztikus, időzített
mintavételi funkcióját használja (SAMPle:SOURce TIMer — SUP01-PULSE-003/
SRC-009, driver: device_dmm.py::configure_timed_acquisition/arm_timed_acquisition/
fetch_timed_acquisition), rövid, egymást követő "chunk"-okban. Egy chunk
lekérdezése (step_chunk()) egyetlen advance() tick alatt fut le — ez biztosítja,
hogy a TestRunner saját stop/emergency-stop ellenőrzése (minden advance() hívás
előtt, l. test_runner.py::_run_step) és a GUI élő frissítése (on_sample minden
tick után) a pulse teljes ideje alatt is megmarad, nem csak a pulse végén.

VALÓS IDŐZÍTÉS (2. review-kör javítása): a mintaidőket NEM a deklarált
index×interval_s képlettel számoljuk (az feltételezné, hogy a chunkok között
nincs holtidő), hanem egy injektálható monotonic-clock forrással (time_source,
alapértelmezés time.monotonic) mérjük a chunk VÉGÉNEK valós faliidejét a pulse
indulásához (t0) képest, és ebből vezetjük vissza a chunk egyes mintáinak
idejét (a chunkon BELÜLI relatív időzítés a DMM saját SAMP:TIM-je szerint
pontos). Ez azért szükséges, mert a TestRunner saját tick-padding alvása
(test_runner.py `time.sleep(max(0.0, runner_tick_s - actual_dt_s))`) valós
üzemben holtidőt told a chunkok közé — enélkül a t1/t10/t30_actual_s és a
pulse alatt eltávolított Ah CSENDBEN eltérne a valóságtól. A drift emiatt a
`reached`/`within_tolerance`/`time_error_s` mezőkben őszintén megjelenik,
nem hallgatjuk el.

R16-B15b / Q-6 — DOKUMENTÁLT IDŐALAPKORLÁT (HIL_PENDING):
A standard OCV hívó 1 NPLC/AZ ON, 0,2 s mintaközt kér. A 30 s névleges
ablakból round(30/0,2)=150 minta, hárommintás chunkkal 50 acquisition lesz.
A DMM időalapja csak EGY chunkon belül folytonos. Minden chunk újraindul;
a köztes T-olvasás, konfigurálás, runner-pacing és I/O nem mintaidő.
Ezért a globális mintasorszám × 0,2 s NEM a terhelés valós idővonala.
A lezárás mintaszámra történik; 5/30 s runner tick megnyújtja az impulzust.
Ideális, az első ACQUIRE előtti pacinget is tartalmazó modellben rendre
250,6/1500,6 s az ON–OFF ablak, nem 30 s. Nulla pacing, de chunkonként
0,45 s T-olvasás mellett ugyanez 52,5 s; ezek modellek, nem fizikai korlátok.
A t1/t10/t30 a valós fetch-horgonyhoz legközelebbi minta, előjeles időhibával
és a változatlan 0,5 s toleranciával; a fetch-latencia továbbra is bizonytalan.
Az Ah a tényleges monotonic ON–OFF ablakon, mért chunk-áramokkal és becsült
OFF-farokkal számolódik; a controller/session/SOC-rács ugyanazt veszi át.
A 30 s-os fizikai garanciához folytonos acquisition és megszakítható
kiolvasás kell, a B13/B10/B10b kapuk és a T-safety megőrzésével. Ez külön
mérési algoritmusmódosítás; a jelen batch csak konfigurációt javít és a
korlátot rögzíti. Q-6 időalap/pacing része NYITOTT. Aktív impulzus-HIL kell
az 1/10/30 s pontokra és az Ah-konzisztenciára; nincs tolerancialazítás.

DMM STICKY ÁLLAPOT (2. review-kör javítása, Critical): a Keysight 34465A-n a
SAMPle:COUNt/SAMPle:SOURce beállítás TARTÓS — configure_timed_acquisition()
után minden ezt követő sima read_voltage() (READ?) a chunk mérete szerinti
CSV-t adná vissza egyetlen float helyett, amíg vissza nem állítjuk. Ezért
minden chunk után azonnal `dmm_v.reset_to_single_sample()`-t hívunk (l.
device_dmm.py), mielőtt bármilyen más komponens (a következő pulse
előellenőrzése, vagy akár a TestRunner._build_sample() párhuzamos olvasása)
ismét sima READ?-et küldene.

── RB Pulse Digitalizálás spec kiegészítés (Folyamatok/tervek/RB_Pulse_
   Digitalizalas_Fejlesztesi_Spec_v1_2026-08.md, WP-AK-04 kiegészítő terv,
   2026-08-22) ──────────────────────────────────────────────────────────
Négy ÚJ, alapértelmezésben KIKAPCSOLT/VÁLTOZATLAN-VISELKEDÉSŰ, opt-in
képesség (a meglévő hívók/tesztek MÓDOSÍTÁS NÉLKÜL, bit-pontosan azonos
viselkedést kapnak, ha nem adják meg az új paramétereket):

1. `trigger_mode=TRIGGER_MODE_HARDWARE_PULSE` (alapértelmezés:
   TRIGGER_MODE_INSTRUMENT_TIMED, azaz a fenti, MEGLÉVŐ folytonos-CC +
   DMM-chunk logika) — a Keithley 2380 SAJÁT hardveres tranziens PULSE
   funkcióját (device_load.py::configure_transient_pulse/fire_transient_pulse,
   SUP01-PULSE-002, korábban additív/be nem kötött) és trace bufferét
   (configure_trace/fetch_trace, SUP01-SRC-010) köti be — az impulzus
   IDŐALAPJA maga a műterhelés (spec §2.1 "Következmény"), nem a DMM chunk-
   ütemezés. **KRITIKUS, DOKUMENTÁLT KORLÁT (safety)**: ebben a módban a
   pulse a MŰSZER saját, önálló időzítésével fut le a teljes `duration_s`
   alatt — a Python-oldali, chunkonkénti cutoff-ellenőrzés (SUP01-PULSE-010/
   TEST-005), ami az alapértelmezett módban minden chunk UTÁN azonnal Load
   OFF-ot tud kiváltani, EBBEN a módban NEM tud közbeavatkozni a pulse
   KÖZBEN — a cutoff-detektálás csak UTÓLAGOS (a lekérdezett waveformból). Ez
   nem hallgatott el: `PulseAcquisitionResult.trigger_mode` és a
   `synchronization_quality` mező mindig tükrözi, melyik mód futott, a hívó
   (workflow-réteg) felelőssége mérlegelni, mikor vállalható ez a korlát
   (pl. rövid, ms-tartományú FAST_TRANSIENT pulzusoknál az expozíciós idő
   eleve bekorlátozott). Fizikai HIL validálás nélkül ez a mód NEM ajánlott
   a hosszú (30s+) STANDARD_PULSE profilhoz — l. spec §6.1/§20 HIL-2..HIL-7.

2. `baseline_window_s>0` — a jelenlegi EGYETLEN `read_voltage()`-es
   baseline-olvasás helyett egy instrumentum-időzített ABLAK (medián) a
   pulse indulása ELŐTT (spec §4 "mért I_baseline", §9.3 "U_baseline:
   median(-1.0…-0.2s)"). Alapértelmezés 0.0 (kikapcsolva) = pontosan a régi,
   egyetlen-mintás viselkedés.

3. `recovery_duration_s>0` — a pulse (Load OFF) UTÁNI, külön instrumentum-
   időzített feszültség-mintavétel (spec §8 "RECOVERY", RC/polarizációs
   modellhez hasznos). Alapértelmezés 0.0 (kikapcsolva) = nincs recovery-
   capture, pontosan a régi viselkedés.

4. Gazdagabb `PulseAcquisitionResult` mezők (current_trace_incomplete/
   voltage_trace_incomplete/measured_baseline_current_A/recovery_samples/
   trigger_mode) — mind additív, a meglévő mezők NEM változtak.

A FAST_TRANSIENT_CHARACTERIZATION (20/100/500 ms) réteg architekturálisan
NEM ehhez a modulhoz tartozik — az egy MÁSIK `PulseMeasurementService`
példány (más `target_times_s`/`sample_interval_s`), amit a hívó (pl.
`OcvSocController`) opcionálisan, külön állapotban indít — l. ott.

── 2026-09-12, FAST_TRANSIENT NPLC-javítás ────────────────────────────────
A `sample_interval_s` a műszernek KÉRT mintavételi köz; a ténylegesen
elérhető minimumot a DCV integrációs idő (NPLC/apertúra) határozza meg. A
projekt a session alatt NPLC=10-en méri a feszültséget (main_window.py::
_bring_up() `configure_dcv(range_V=100, nplc=10)` + test_runner.py::
_configure_dmm_for_step() `relax_ocv_nplc=10.0` OCV_SOC lépésre), ami a
34465A apertúra-táblája szerint (34460-70-Manual.txt 14028-14052. sor,
nyomtatott 453. o.) 50 Hz-en 0,2 s / 60 Hz-en 0,167 s integrációs idő
mintánként. A FAST_TRANSIENT réteg 5 ms-os mintavételi köze ezzel
fizikailag teljesíthetetlen: a kézikönyv szerint (uo. 9067-9072. sor,
nyomtatott 311. o.) ilyenkor a műszer -221,"Settings conflict; cannot meet
requested timing; SAMP:TIM changed" hibát tesz a hibasorba ÉS megváltoztatja
a sample timert.

Ezért a hívó az `acquisition_nplc`/`restore_nplc` opt-in paraméterekkel
ideiglenesen alacsony integrációs időre állíthatja a DMM-et az impulzus
idejére; a szolgáltatás a beállítás UTÁN a MŰSZERTŐL (SAMPle:TIMer? MIN)
kérdezi vissza, hogy a kért köz teljesíthető-e, és ha nem, az impulzust
EL SEM INDÍTJA (`timing_infeasible` + ok), majd MINDEN terminális úton
visszaállítja az eredeti integrációs időt. R16-B15a / Q-6: az OCV STANDARD
(1/10/30 s) hívó explicit acquisition_nplc=1.0 és AZ ON kaput kér;
a generikus szolgáltatás alapértéke továbbra is None, a fast-transient külön
0,02 NPLC-t használ. AZ/NPLC-readback után, baseline és ON előtt fut a MIN-kapu.

── 2026-09-12, musszer-only HIL-javítás (INSTR-DMM-SAMPTIM-001) ──────────
A fenti MIN-előellenőrzés csak az `acquisition_nplc` beállításakor fut —
a STANDARD útvonalon (`acquisition_nplc=None`) korábban SEMMI nem
ellenőrizte, hogy a `configure_timed_acquisition()`-nel ténylegesen kért
`SAMP:TIM` valóban alkalmazódott-e. Musszer-only HIL-méréssel (valós
Keysight 34465A, NPLC=10, 0,2 s kérés) igazolva, hogy a műszer ezt a
kérést CSENDBEN, hibasor-bejegyzés NÉLKÜL is figyelmen kívül hagyhatja —
a meglévő, `check_error()`-alapú védelem (CF-04) ezt nem fedezte fel.
Ezért MINDKÉT `configure_timed_acquisition()` hívó ágon (`_step_
instrument_timed()`/`_step_hardware_pulse()`, azaz a STANDARD ÉS a
FAST_TRANSIENT útvonalon egyaránt, `acquisition_nplc`-től függetlenül) a
`check_error()` után egy TOVÁBBI, `get_sample_timer_s()`-en (ha a driver
támogatja) alapuló readback-ellenőrzés fut — eltérés esetén a chunk
ugyanúgy `DMM_TIMED_ACQUISITION_CONFIG_REJECTED`-ként bukik, Load OFF-fal.
Ez NEM opt-in. Hiányzó `get_sample_timer_s()` esetén a kapu kimarad;
a jelen lévő getter lekérdezési hibája R16-B3 óta láncoltan
`DMM_TIMED_ACQUISITION_CONFIG_REJECTED`-ként bukik. Egyező readback
esetén a kapu nem változtat a meglévő SCPI-forgalmon."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, Thread
from typing import Callable, Optional

from Prog.drivers.device_load import INPUT_ACTIVATION_ACK_TIMEOUT_S
from Prog.src.battery_profile import validate_normal_discharge
from Prog.src.exceptions import InstrumentInvalidReading
from Prog.src.safety import SafetyManager

_DEFAULT_TARGET_TIMES_S: tuple[float, ...] = (1.0, 10.0, 30.0)

# Governance-v2 review-kör 1 (CF-09/MET-R1-05): ugyanaz a küszöb, mint
# device_dmm.py::OVERLOAD_THRESHOLD/device_load.py::_OVERLOAD_THRESHOLD
# (9.9E37 Keysight overload-kód felismerésére). Kör 21 F17: device_dmm.py
# sajátja átnevezve (ARC21-6 sibling, réteghatáron át importálva egy
# másik hívóhelyről) -- ez a helyi másolat NEM import, hanem SAJÁT,
# független definíció (DFAM-017 hatóköre, nem ARC21-6), ezért
# változatlanul a vezető aláhúzásos nevet tartja.
_OVERLOAD_THRESHOLD = 1e30

# Szinkronizációs minőség (RB Pulse spec §18) — a HARDWARE_SYNCHRONIZED szint
# (közös fizikai trigger-vezetékezés) fizikai HIL validálást igényel, ez a
# modul azt NEM állítja elő, csak a nevet tartja fenn a jövőbeli bővítéshez.
SYNC_QUALITY_HARDWARE_SYNCHRONIZED = "HARDWARE_SYNCHRONIZED"
SYNC_QUALITY_INSTRUMENT_TIMED_SOFTWARE_TRIGGERED = "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"
SYNC_QUALITY_SOFTWARE_TICK_TIMED = "SOFTWARE_TICK_TIMED"

TRIGGER_MODE_INSTRUMENT_TIMED = "INSTRUMENT_TIMED_SOFTWARE_TRIGGERED"
TRIGGER_MODE_HARDWARE_PULSE = "HARDWARE_PULSE_SOFTWARE_TRIGGERED"
_VALID_TRIGGER_MODES = (TRIGGER_MODE_INSTRUMENT_TIMED, TRIGGER_MODE_HARDWARE_PULSE)

# ── 2026-09-12, FAST_TRANSIENT NPLC-javítás ────────────────────────────────
# A `timing_infeasible_reason` mező előre definiált okkódjai. Mindegyik azt
# jelenti, hogy az impulzus EL SEM INDULT (a Load NEM kapcsolt be) — soha nem
# egy "megtörtént, de gyanús" mérést minősítenek, hanem a mérés megtagadását
# (CLAUDE.md §6/§7: aktív energiaátadásnál tilos csendben helyettesíteni).
TIMING_INFEASIBLE_SAMPLE_TIMER_MIN = "SAMPLE_TIMER_MIN_EXCEEDS_REQUESTED_INTERVAL"
TIMING_INFEASIBLE_CAPABILITY_UNSUPPORTED = "SAMPLE_TIMER_CAPABILITY_UNSUPPORTED"
TIMING_INFEASIBLE_CAPABILITY_QUERY_FAILED = "SAMPLE_TIMER_CAPABILITY_QUERY_FAILED"
TIMING_INFEASIBLE_CAPABILITY_INVALID = "SAMPLE_TIMER_CAPABILITY_INVALID_RESPONSE"
TIMING_INFEASIBLE_NPLC_SET_FAILED = "ACQUISITION_NPLC_SET_FAILED"
TIMING_INFEASIBLE_NPLC_RESTORE_UNKNOWN = "ACQUISITION_NPLC_RESTORE_VALUE_UNKNOWN"


def _parse_csv_floats(raw: str, *, reject_invalid: bool = True) -> list[float]:
    """Governance-v2 review-kör 1 (CF-09/MET-R1-05/DFAM-001): a skalár
    olvasási utakkal (device_dmm.py::_validate_voltage, device_load.py::
    _validate_reading) azonos NaN/Inf/overload (9.9E37) védelem a CSV-
    blokk minden mintájára. `reject_invalid=True` (default) a
    cutoff-kritikus/safety-releváns hívóknál: egyetlen érvénytelen minta a
    TELJES chunk elutasítását váltja ki (InstrumentInvalidReading), mert a
    minta-index/időbélyeg hozzárendelés (`t = ... - (n-1-i)*interval`) a
    chunk teljes, sértetlen hosszára épül — részleges szűrés eltolná a
    többi minta időbélyegét. `reject_invalid=False` a nem safety-kritikus,
    best-effort diagnosztikai olvasásokhoz (baseline-ablak medián, recovery-
    minták), ahol egy érvénytelen minta egyszerű kihagyása (nem a teljes
    olvasás elvetése) a helyes, meglévő fail-soft viselkedést tartja meg."""
    raw = raw.strip()
    if not raw:
        return []
    result: list[float] = []
    for x in raw.split(","):
        x = x.strip()
        if x == "":
            # R3-B1 (R3-metrology-01, DFAM-001): az üres/whitespace mező
            # kihagyása is részleges szűrés (l. docstring) -- szigorú módban
            # a teljes chunk elutasítva, best-effort módban kimarad.
            if reject_invalid:
                raise InstrumentInvalidReading(
                    "PulseMeasurementService: üres minta-mező a CSV-ben"
                )
            continue
        # Kör 20 Batch E14 (HW20-05, DFAM-002+DFAM-001): a bare `float(x)`
        # korábban a device_dmm.py::_parse_float() referencia-mintájával
        # ellentétben NEM fogta el a ValueError-t (egy csonka/torzult SCPI-
        # válasz nem numerikus tokenje) -- a hívók KIZÁRÓLAG
        # InstrumentInvalidReading-et kapnak el (ami az aktív-impulzus
        # Load-OFF láncot hajtja), egy ValueError emiatt KIVÉTELKÉNT
        # PROPAGÁLT volna, elkerülve azt a láncot. Elérhetőség nem
        # bizonyított (defense-in-depth veszteség, nem csendes veszélyes
        # állapot -- a controller végül FAULT-ol), de a testvér-minta
        # (device_dmm.py::_parse_float()) már megoldotta ugyanezt.
        try:
            v = float(x)
        except ValueError as exc:
            if reject_invalid:
                raise InstrumentInvalidReading(
                    f"PulseMeasurementService: nem értelmezhető minta a CSV-ben: {x!r}"
                ) from exc
            continue
        if math.isnan(v) or math.isinf(v) or abs(v) > _OVERLOAD_THRESHOLD:
            if reject_invalid:
                raise InstrumentInvalidReading(
                    f"PulseMeasurementService: érvénytelen minta a CSV-ben: {x!r}"
                )
            continue
        result.append(v)
    return result


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@dataclass
class PulseSamplePoint:
    """SUP01-DATA-002: egy időzített feszültségminta a pulse alatt. A
    time_from_pulse_start_s VALÓS (falióra-alapú) idő, nem deklarált index."""
    time_from_pulse_start_s: float
    voltage_V: float


@dataclass
class PulseResultPoint:
    """SUP01-DATA-003: egy kiértékelt (target_time_s-hez tartozó) pulse pont."""
    target_time_s: float
    actual_time_s: Optional[float]
    voltage_V: Optional[float]
    current_A: Optional[float]
    delta_voltage_V: Optional[float]
    delta_current_A: Optional[float]
    resistance_ohm: Optional[float]
    resistance_mOhm: Optional[float]
    time_error_s: Optional[float]
    within_tolerance: bool
    reached: bool


@dataclass
class PulseAcquisitionResult:
    started: bool
    skipped_low_voltage: bool
    cutoff_during_pulse: bool
    pulse_current_degraded: bool
    v_baseline_V: float
    pulse_current_set_A: float
    pulse_current_measured_mean_A: float | None
    pulse_duration_set_s: float
    # R1 B8a, F-21 (2026-10): INSTRUMENT_TIMED módban a Load-terhelés
    # legacy úton ON-visszatérés -> OFF-visszatérés; saját órás OCV-blokkban
    # ON-küldés -> OFF-visszatérés (a command célablak külön auditmező).
    # Ez szoftveres becslési ablak, NEM az
    # R(t)-értékelési ablak vége (az az utolsó elfogadott minta, samples[-1])
    # és NEM igazolt fizikai kapcsolási idő (a műszer saját ON/OFF-késése
    # HIL_PENDING). HARDWARE_PULSE módban (D-B8-1, változatlan) továbbra is
    # az utolsó waveform-minta ideje -- a két mód szemantikája ELTÉR.
    pulse_duration_actual_s: float
    synchronization_quality: str
    method_origin: str
    # R1 B8a, F-21: INSTRUMENT_TIMED módban a removed_Ah/removed_Wh a teljes
    # [t0, Load-OFF-nyugta] terhelési ablakra vonatkozik (l. finalize()), a
    # cutoff utáni / mintavételen kívüli szakasz BECSLÉS: az utolsó
    # áramleolvasás értékének (ha azon a chunkon az áram nem volt mérhető,
    # az ott rögzített setpoint-értéknek; ilyenkor pulse_current_degraded=
    # True) és az ahhoz legközelebbi feszültségmintának a kiterjesztése.
    removed_Ah: float
    # Teljes kódbázis review 16. kör (2026-09-04), Metrológia/
    # Adatintegritás P2 lelet: a removed_Ah testvérpárja — enélkül az
    # OcvSocController session-szintű accumulated_discharge_Wh-ja nem
    # tudta figyelembe venni az impulzus-fázisok energia-fogyasztását,
    # csak az Ah-t (amit a 15. kör, 5d868d2, már megoldott).
    removed_Wh: float = 0.0
    # SUP01-MET-005-szerű, review-lelet nyomán hozzáadott jelző: legalább egy
    # ELÉRT (reached=True) pontnál a mért DeltaI <= 0 volt, ezért
    # resistance_ohm=None — az OcvSocController ezt NEM csendben 0.0 mOhm-ra
    # cseréli minősítés nélkül, hanem degraded quality-t jelöl.
    has_invalid_resistance: bool = False
    samples: list[PulseSamplePoint] = field(default_factory=list)
    results: dict[float, PulseResultPoint] = field(default_factory=dict)
    # RB Pulse Digitalizálás spec kiegészítés (2026-08-22, additív mezők):
    trigger_mode: str = TRIGGER_MODE_INSTRUMENT_TIMED
    measured_baseline_current_A: Optional[float] = None
    current_trace_incomplete: bool = False
    voltage_trace_incomplete: bool = False
    recovery_samples: list[PulseSamplePoint] = field(default_factory=list)
    # Teljes kódbázis review 4. kör (2026-08-29), Metrológia/Adatintegritás
    # P2 lelet: True, ha baseline_window_s>0 mellett a mért I_baseline
    # lekérdezése hibázott — ekkor az R(t) számítás a feltételezett (nem
    # mért) bázisáramra esett vissza, l. _capture_baseline_window().
    baseline_current_degraded: bool = False
    # ── 2026-09-12, FAST_TRANSIENT NPLC-javítás ──────────────────────────
    # True, ha a kért `sample_interval_s` a műszer SAJÁT, visszaolvasott
    # képessége szerint (SAMPle:TIMer? MIN) NEM teljesíthető az adott
    # integrációs idő mellett, vagy a képesség egyáltalán nem volt
    # ellenőrizhető. Ekkor az impulzus EL SEM INDUL (started=False, a Load
    # nem kapcsol be) — a `pulse_current_degraded` mintáját követő explicit
    # minősítés, NEM csendes, téves idővonalú adat.
    timing_infeasible: bool = False
    timing_infeasible_reason: str = ""
    # True, ha az ideiglenesen átállított DCV NPLC VISSZAÁLLÍTÁSA hibázott —
    # ilyenkor a session hátralévő (normál sebességű) DMM-olvasásai a
    # gyors/zajos integrációs időn maradnak, ami metrológiai adatintegritási
    # esemény (rules/20-metrology.md), ezért nem nyelhető el csendben.
    nplc_restore_failed: bool = False
    # Q-36: parancsidők, nem igazolt fizikai kapcsolási pillanatok.
    load_on_command_monotonic_s: float | None = None
    load_on_return_monotonic_s: float | None = None
    load_off_command_monotonic_s: float | None = None
    load_off_return_monotonic_s: float | None = None
    load_command_duration_s: float | None = None
    load_command_time_error_s: float | None = None

    def result_at(self, target_time_s: float) -> Optional[PulseResultPoint]:
        return self.results.get(target_time_s)


@dataclass(frozen=True)
class MeasuredPulseEnergy:
    """R2-B4 (R2-metrology-01, DFAM-033): az aktív impulzus eddig
    ténylegesen RÖGZÍTETT áramleolvasásaira integrált energia -- a
    finalize() téglalap-integráljának a terminális OFF-farok NÉLKÜLI része
    -- és minősítése. Tiszta adat."""

    removed_Ah: float
    removed_Wh: float
    reading_count: int
    last_reading_t_s: float
    current_degraded: bool


NO_MEASURED_PULSE_ENERGY = MeasuredPulseEnergy(0.0, 0.0, 0, 0.0, False)


class PulseOvervoltageError(RuntimeError):
    """R8-B2: a pulzus friss feszültségmintája a profil felső határa felett."""


class PulsePowerLimitError(RuntimeError):
    """R9-B2: a friss pulzusadat a Load teljesitmenyhatara felett."""


class PulseDischargeError(RuntimeError):
    """GOV-029/Q-34: a pulzus alsó kapuja vagy HIL-tiltása; nem quality flag."""


def require_battery_pulse_mode(trigger_mode: str) -> None:
    """HIL-ig nincs akkus hardverpulzus és nincs csendes módváltás."""
    if trigger_mode == TRIGGER_MODE_HARDWARE_PULSE:
        raise PulseDischargeError(
            "HARDWARE_PULSE_HIL_PENDING: GOV-029/Q-34; akkus terhelés tiltva, "
            "a reakcióidő és a tartalék HIL-igazolása hiányzik"
        )


class _PulseDmmWorker:
    """Egy DMM-tulajdonos, legfeljebb egy függő rövid chunk; energia-I/O nincs."""

    def __init__(self, service: PulseMeasurementService) -> None:
        self._service = service
        self.cancel = Event()
        self._closing = Event()
        self._requests: Queue[int | None] = Queue()
        self.results: Queue[tuple[str | None, float, BaseException | None]] = Queue()
        self._cleanup_error: BaseException | None = None
        self.owner_release_confirmed = True
        self._thread = Thread(target=self._run, name="ocv-pulse-dmm", daemon=True)
        self._thread.start()

    def request(self, count: int) -> None:
        self._requests.put(count)

    def _inactive(self) -> bool:
        service = self._service
        return (
            self.cancel.is_set()
            or service._window_expired()
            or (
                service.handoff_stop_requested is not None
                and service.handoff_stop_requested()
            )
        )

    def _capture(self, owner: object, count: int) -> str | None:
        service = self._service
        dmm = service._dmm_v
        if self._inactive():
            return None
        dmm.configure_timed_acquisition(
            sample_count=count, interval_s=service._sample_interval_s, owner=owner,
        )
        if self._inactive():
            return None
        errors = dmm.check_error(owner=owner)
        if errors:
            raise RuntimeError("DMM_TIMED_ACQUISITION_CONFIG_REJECTED: " + "; ".join(errors))
        if self._inactive():
            return None
        service._verify_actual_sample_timer_or_raise(owner=owner)
        if self._inactive():
            return None
        dmm.arm_timed_acquisition(owner=owner)
        if self._inactive():
            return None
        return dmm.fetch_timed_acquisition(owner=owner)

    def _run(self) -> None:
        owner = object()
        acquired = False
        try:
            self._service._dmm_v.acquire_measurement(owner)
            acquired = True
            self.owner_release_confirmed = False
            while True:
                count = self._requests.get()
                if count is None:
                    break
                try:
                    raw = self._capture(owner, count)
                    # Közvetlen fetch-horgony; reset és átadás még nem történt.
                    ended = self._service._time_source() - self._service._t0
                except BaseException as exc:  # noqa: BLE001 - eredeti worker/cleanup hiba átadása
                    self.results.put((None, 0.0, exc))
                    break
                self.results.put((raw, ended, None))
        except BaseException as exc:  # noqa: BLE001 - eredeti worker/cleanup hiba átadása
            self.results.put((None, 0.0, exc))
        finally:
            # Stop/fault/deadline önmagában nem engedélyez DMM-cleanupot.
            # A runner előbb mindkét OFF-ot megkísérli.
            self._closing.wait()
            if acquired:
                try:
                    self._service._dmm_v.reset_to_single_sample(owner=owner)
                except BaseException as exc:  # noqa: BLE001 - eredeti worker/cleanup hiba átadása
                    self._cleanup_error = exc
                finally:
                    try:
                        self._service._dmm_v.release_measurement(owner)
                    except BaseException as exc:  # noqa: BLE001 - eredeti worker/cleanup hiba átadása
                        self._cleanup_error = exc
                    else:
                        self.owner_release_confirmed = True

    def close_after_safe_off(self) -> None:
        self.cancel.set()
        self._closing.set()
        self._requests.put(None)
        # Kizárólag a runner szálán; a GUI csak stopjelzőt állít.
        self._thread.join(timeout=2.0)
        if self._thread.is_alive() or not self.owner_release_confirmed:
            raise RuntimeError(f"PULSE_DMM_RELEASE_PENDING: {self._cleanup_error or ''}")
        if self._cleanup_error is not None:
            raise RuntimeError(f"PULSE_DMM_CLEANUP_FAILED: {self._cleanup_error}") from self._cleanup_error


class PulseMeasurementService:
    """Chunkonként instrumentum-időzített pulse-resistance acquisition.

    Hívási minta (l. OcvSocController._run_impulse_on/_run_impulse_acquire):
      1. begin_pulse() — SUP01-PULSE-010 előzetes feszültség-ellenőrzés, majd
         a terhelés bekapcsolása CC módban. False-t ad vissza, ha a pulse
         alacsony feszültség miatt el sem indul (SKIPPED_LOW_VOLTAGE).
      2. step_chunk() ismételt hívása: a közvetlen legacy hívó tickenként,
         az OCV saját blokkja külső runner-várakozás nélkül, a monoton
         határidőig. A DMM-worker csak rövid chunkokat mér; a felügyelet
         és minden Load/PSU-parancs a runner szálán marad. Egy chunk egyetlen,
         instrumentum-időzített DMM mintavételi blokk + egy mért áramérték
         (INSTRUMENT_TIMED mód) — vagy HARDWARE_PULSE módban EGYETLEN,
         teljes-pulzusú hívás (l. modul docstring). Cutoff (SUP01-PULSE-010/
         TEST-005) esetén azonnal Load OFF, a hívó nem kényszerítheti a
         mérést a teljes időtartamig — a cutoff UTÁNI, már lekérdezett
         mintákat a chunkból eldobjuk (nem csak az utolsó mintát nézzük).
      3. finalize() — a kiértékelt eredmény (PulseAcquisitionResult).
    """

    def __init__(
        self,
        load,
        dmm_v,
        terminate_voltage_V: float,
        chunk_sample_count: int = 3,
        sample_interval_s: float = 0.2,
        target_times_s: tuple[float, ...] = _DEFAULT_TARGET_TIMES_S,
        time_tolerance_s: float = 0.5,
        time_source: Callable[[], float] = time.monotonic,
        trigger_mode: str = TRIGGER_MODE_INSTRUMENT_TIMED,
        baseline_window_s: float = 0.0,
        baseline_sample_interval_s: float = 0.2,
        recovery_duration_s: float = 0.0,
        recovery_sample_interval_s: float = 0.2,
        trace_interval_s: float = 0.04,
        trace_points: int = 1000,
        acquisition_nplc: Optional[float] = None,
        restore_nplc: Optional[float] = None,
        *,
        safety: SafetyManager,
        max_voltage_V: float,
        max_load_power_W: float,
        handoff_stop_requested: Callable[[], bool] | None = None,
        require_autozero_on: bool = False,
        controller_managed_cleanup: bool = False,
    ) -> None:
        """`acquisition_nplc` / `restore_nplc` — 2026-09-12, FAST_TRANSIENT
        NPLC-javítás, opt-in (None = nincs ideiglenes NPLC-váltás).
        Az OCV standard hívó Q-6 szerint 1 NPLC-t és kötelező AZ ON-t kér.
        A require_autozero_on belső konfigurációs kapu, nem GUI/YAML-opció:
        AZ ON és NPLC-readback a MIN-lekérdezés és a baseline ELŐTT.

        Miért kell: a `sample_interval_s` a műszernek KÉRT mintavételi köz,
        de a ténylegesen elérhető minimum az integrációs időtől (NPLC/
        apertúra) függ. A kézikönyv (34460-70-Manual.txt 9043-9047. sor,
        nyomtatott 310. o.): "MIN is automatically determined so that the
        sample interval is always greater than the sampling time". A
        34465A DCV apertúra-táblája (uo. 14028-14052. sor, nyomtatott 453.
        o.) szerint NPLC=10 mellett az apertúra 50 Hz-en 0,2 s, 60 Hz-en
        0,167 s — egy 5 ms-os FAST_TRANSIENT mintavételi köz ezzel
        fizikailag teljesíthetetlen. Ha a kért SAMP:TIM az apertúra alatt
        van, a műszer NEM a kért időzítéssel mér, hanem -221-et tesz a
        hibasorba és MEGVÁLTOZTATJA a sample timert (uo. 9067-9072. sor,
        nyomtatott 311. o.):
        "Settings conflict; cannot meet requested timing; SAMP:TIM
        changed".

        Ezért a FAST_TRANSIENT hívónak át kell állítania a DCV integrációs
        időt az impulzus idejére. A 34465A-n MINDEN változatban (DIG opció
        nélkül is) elérhető legkisebb NPLC 0,02 (uo. 11428-11433. sor,
        nyomtatott 382. o.), ami 50 Hz-en 0,4 ms, 60 Hz-en 0,3 ms apertúra
        (uo. 14028-14052. sor) — a 0,02 alatti fokozatok (0,006/0,002/
        0,001) CSAK a DIG opcióval rendelkező műszeren léteznek, ezért a
        projekt NEM támaszkodhat rájuk (a laborpéldány DIG-opciója
        SOURCE_REQUIRED/HIL_PENDING).

        `restore_nplc` a visszaállítási FALLBACK érték arra az esetre, ha a
        driver nem tud NPLC-t visszaolvasni (`get_nplc()`); ha a driver tud,
        a VISSZAOLVASOTT érték élvez elsőbbséget (a DCV NPLC-nek több,
        egymástól független írója van, l. device_dmm.py::get_nplc()). Ha
        egyik sem áll rendelkezésre, a szolgáltatás NEM nyúl az NPLC-hez, és
        a mérést `timing_infeasible`-lel megtagadja — soha nem hagyunk a
        műszeren olyan állapotot, amit nem tudunk visszaállítani."""
        if trigger_mode not in _VALID_TRIGGER_MODES:
            raise ValueError(
                f"trigger_mode={trigger_mode!r} — {_VALID_TRIGGER_MODES} egyike lehet"
            )
        self._load = load
        self._dmm_v = dmm_v
        # A pulzus eredete nem adatlapi C20; a közös primitív dönt.
        validate_normal_discharge(safety.profile, 1.0, terminate_voltage_V)
        self._safety = safety
        self._discharge_fault: PulseDischargeError | None = None
        self._terminate_voltage_V = terminate_voltage_V
        self._max_voltage_V = max_voltage_V
        self._max_load_power_W = max_load_power_W
        self.handoff_stop_requested = handoff_stop_requested
        self._chunk_sample_count = max(1, chunk_sample_count)
        self._sample_interval_s = sample_interval_s
        self._target_times_s = target_times_s
        self._time_tolerance_s = time_tolerance_s
        self._time_source = time_source
        self._trigger_mode = trigger_mode
        self._baseline_window_s = max(0.0, baseline_window_s)
        self._baseline_sample_interval_s = baseline_sample_interval_s
        self._recovery_duration_s = max(0.0, recovery_duration_s)
        self._recovery_sample_interval_s = recovery_sample_interval_s
        self._trace_interval_s = trace_interval_s
        self._trace_points = trace_points
        if require_autozero_on and acquisition_nplc is None:
            raise ValueError("AZ ON kapuhoz acquisition_nplc szükséges")
        self._acquisition_nplc = acquisition_nplc
        self._restore_nplc = restore_nplc
        self._require_autozero_on = require_autozero_on
        # A controller csak a Load OFF és PSU OFF után engedi a DMM-cleanupot.
        self._controller_managed_cleanup = controller_managed_cleanup
        self._worker: _PulseDmmWorker | None = None
        self._owned_requested = False
        self._block_supervise: Callable[[], bool] | None = None
        self._block_cleanup: Callable[[], bool] | None = None
        self._block_deadline = 0.0
        self._reset()

    # ------------------------------------------------------------------ #
    def _reset(self) -> None:
        self._pulse_current_A: float = 0.0
        self._baseline_current_A: float = 0.0
        self._measured_baseline_current_A: Optional[float] = None
        self._duration_s: float = 0.0
        self._total_samples_target: int = 0
        self._v_baseline_V: float = 0.0
        self._t0: float = 0.0
        self._samples: list[PulseSamplePoint] = []
        self._current_readings: list[tuple[float, float]] = []
        self._started: bool = False
        self._done: bool = False
        self._skipped_low_voltage: bool = False
        self._cutoff: bool = False
        # R1 B8a, F-21: a terminális input_off() SIKERES visszatérésének
        # ideje t0-hoz képest (INSTRUMENT_TIMED); None, amíg nem történt meg.
        self._load_off_real_s: float | None = None
        self._pulse_current_degraded: bool = False
        self._baseline_current_degraded: bool = False
        self._current_trace_incomplete: bool = False
        self._voltage_trace_incomplete: bool = False
        self._recovery_samples: list[PulseSamplePoint] = []
        # 2026-09-12, FAST_TRANSIENT NPLC-javítás
        self._nplc_applied: bool = False
        self._nplc_restore_value: Optional[float] = None
        self._nplc_restore_failed: bool = False
        self._timing_infeasible: bool = False
        self._timing_infeasible_reason: str = ""
        self._owned_window = self._owned_requested
        self._owned_requested = False
        self._owned_recovery_done = False
        self._worker_cleanup_diagnostic = ""
        self._on_command_at: float | None = None
        self._on_return_at: float | None = None
        self._off_command_at: float | None = None
        self._activation_ack_expired_at_off = False
        self._off_return_at: float | None = None

    # ------------------------------------------------------------------ #
    def begin_pulse(
        self, pulse_current_A: float, duration_s: float, baseline_current_A: float = 0.0
    ) -> bool:
        """SUP01-PULSE-010 előzetes feszültség-ellenőrzés + a terhelés
        bekapcsolása. False-t ad vissza, ha a pulse el sem indul
        (SKIPPED_LOW_VOLTAGE) — ekkor a terhelés NEM kapcsol be.
        Élő handoff-stop esetén szintén False; a hívó véglegesítés nélkül
        térjen vissza a runner eredeti okot őrző, közös terminális útjára.

        `baseline_current_A`: ha `baseline_window_s==0` (alapértelmezés), ezt
        az értéket használjuk feltételezett I_baseline-ként VÁLTOZATLANUL,
        mint korábban. Ha `baseline_window_s>0`, a paramétert felülírja egy
        TÉNYLEGESEN mért érték (RB Pulse spec §4 "mért I_baseline")."""
        if self._worker is not None:
            raise RuntimeError("PULSE_DMM_RELEASE_PENDING")
        if self._discharge_fault is not None:
            raise self._discharge_fault
        try:
            require_battery_pulse_mode(self._trigger_mode)
            validate_normal_discharge(self._safety.profile, pulse_current_A, self._terminate_voltage_V)
        except (PulseDischargeError, ValueError) as exc:
            self._discharge_fault = PulseDischargeError(str(exc))
            self._done = True
            raise self._discharge_fault from exc
        self._reset()
        self._pulse_current_A = pulse_current_A
        self._baseline_current_A = baseline_current_A
        self._duration_s = duration_s
        self._total_samples_target = max(1, round(duration_s / self._sample_interval_s))

        # 2026-09-12, FAST_TRANSIENT NPLC-javítás: az integrációs idő
        # átállítása + képesség-ellenőrzés MINDEN mérés (a baseline-olvasás
        # is) ELŐTT fut. Szándékosan a baseline ELŐTT: a R(t) = ΔU/ΔI
        # számítás a `v_baseline_V` és az impulzus alatti minták KÜLÖNBSÉGÉRE
        # épül, ezért a kettőnek AZONOS apertúrával kell készülnie —
        # különböző integrációs idő eltérő offset/zajkarakterisztikát ad
        # (34460-70-Manual.txt 11528-11531. sor, nyomtatott 385. o.: az
        # autozero "takes a new zero measurement each time you change the
        # function, range or integration time"), ami egy néhány mΩ-os
        # belső ellenállásnál szisztematikus hibaként jelenne meg.
        # Ha nem teljesíthető, az impulzus EL SEM INDUL: a Load nem kapcsol
        # be, `started=False`, és a minősítés a `timing_infeasible` mezőben
        # jut ki (CLAUDE.md §7: "Nyers és származtatott érték külön;
        # quality/minősítés kötelező").
        if not self._apply_acquisition_nplc():
            self._done = True
            return False

        try:
            if self._baseline_window_s > 0:
                v_before = self._capture_baseline_window()
            else:
                v_before = self._dmm_v.read_voltage()
            self._check_max_voltage([v_before])
            self._check_load_power([v_before], pulse_current_A)
            self._check_discharge_floor([v_before])
        except BaseException:
            # Cleanup minden terminális úton (rules/10-safety.md 9. pont): a
            # baseline-olvasás hibája sem hagyhatja a műszert az ideiglenes,
            # gyors integrációs időn — a hívó ezt a kivételt FAULT-ba viszi,
            # és utána már senki nem hívná a finalize()-t.
            self._restore_acquisition_nplc()
            raise
        self._v_baseline_V = v_before
        if v_before <= self._terminate_voltage_V:
            self._skipped_low_voltage = True
            self._done = True
            self._restore_acquisition_nplc()
            return False

        # R16-B13: a friss safety-kapuk után, minden Load-konfiguráció előtt.
        # A hívó és a runner őrzi az eredeti leállítási okot és az OFF-utat.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return False

        if self._trigger_mode == TRIGGER_MODE_HARDWARE_PULSE:
            # [review-lelet] a `self._t0`-t SZÁNDÉKOSAN NEM itt rögzítjük —
            # a tényleges impulzus-konfiguráció/tüzelés step_chunk()-ban
            # történik, ami a hívási konvenció szerint (l. OcvSocController
            # IMPULSE_ON -> IMPULSE_ACQUIRE) egy KÉSŐBBI advance() tick-ben
            # fut le, akár több másodperces (TestRunner tick) réssel
            # begin_pulse() UTÁN. Ha itt rögzítenénk t0-t, ez a rés HAMISAN
            # beépülne a "time_from_pulse_start_s" idővonalba (a waveform
            # összes mintája ennyivel később tűnne fel, mint a valóságban -
            # az R_1s/10s/30s kiértékelés téves célidőponthoz illesztene). A
            # t0-t ezért `_step_hardware_pulse()` rögzíti, KÖZVETLENÜL
            # `fire_transient_pulse()` előtt - a load-ot sem kapcsoljuk be
            # előre (a hardveres PULSE saját maga vezérli az A/B szintek
            # közti átmenetet, l. configure_transient_pulse()).
            self._started = True
            return True

        try:
            self._load.set_mode_cc()
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                return False
            self._load.set_current(pulse_current_A)
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                return False
            # R16-B10: a pulse időorigója és mintavételi sorrendje maradjon meg.
            if self._owned_window:
                self._on_command_at = self._time_source()
            self._load.input_on(wait_for_ack=False)
            if self._owned_window:
                self._on_return_at = self._time_source()
        except BaseException:
            # L. a baseline-ág azonos hivatkozású kommentje (2026-09-12,
            # FAST_TRANSIENT NPLC-javítás): a Load bekapcsolásának hibája is
            # terminális út, a hívó emergency_stop-ba viszi.
            # Kör 20 Batch E14 (HW20-07, DFAM-016): ez volt az EGYETLEN
            # Load-energizáló ág a fájlban, ami hiba esetén NEM kísérelte
            # meg az input_off()-ot -- a hívó controller emergency_stop()-ja
            # ma lefedi ezt (defense-in-depth veszteség, nem élő hiba), de
            # rules/10-safety.md #9 ("Cleanup/restore minden terminális úton
            # vizsgálandó") szerint a szolgáltatás SAJÁT szintjén is meg
            # kell kísérelnie -- energia ELŐBB, metrológia (NPLC-visszaállítás)
            # UTÓBB, best-effort, az eredeti kivételt SOSEM elfedve.
            try:
                if self._owned_window:
                    self._owned_off()
                else:
                    self._load.input_off()
            except Exception:
                pass
            self._restore_acquisition_nplc()
            raise
        self._t0 = self._on_command_at if self._on_command_at is not None else self._time_source()
        if self._on_return_at is None:
            # A közvetlen, runner nélküli út sem fizikai ON-readbacket tárol.
            self._on_return_at = self._t0
        self._started = True
        # Az input_on alatt érkező jelzés után az OFF az első műszerparancs.
        self._stop_active_pulse_if_requested()
        return True

    # ------------------------------------------------------------------ #
    # 2026-09-12, FAST_TRANSIENT NPLC-javítás — integrációs idő kezelése   #
    # ------------------------------------------------------------------ #

    def _apply_acquisition_nplc(self) -> bool:
        """Ideiglenesen átállítja a DCV integrációs időt (NPLC) az impulzus
        idejére, majd a MŰSZERTŐL kérdezi meg (SAMPle:TIMer? MIN), hogy a
        kért `sample_interval_s` egyáltalán teljesíthető-e.

        True: mehet a mérés. False: `self._timing_infeasible` beállítva, az
        NPLC visszaállítva (vagy hozzá sem nyúltunk) — a hívó ne indítsa el az
        impulzust.

        `acquisition_nplc is None` esetén azonnal True, SCPI-hívás nélkül.
        Az OCV standard példánya explicit 1 NPLC/AZ ON konfigurációt kér."""
        if self._acquisition_nplc is None:
            return True

        prior_nplc = self._restore_nplc
        getter = getattr(self._dmm_v, "get_nplc", None)
        if getter is not None:
            try:
                prior_nplc = float(getter())
            except Exception:
                # A visszaolvasás hibája nem végzetes, ha van konfigurált
                # fallback (l. __init__ docstring) — csak akkor áll meg a
                # mérés, ha egyik sem ismert.
                pass
        if prior_nplc is None:
            self._timing_infeasible = True
            self._timing_infeasible_reason = TIMING_INFEASIBLE_NPLC_RESTORE_UNKNOWN
            return False

        setter = getattr(self._dmm_v, "set_nplc", None)
        if setter is None:
            self._timing_infeasible = True
            self._timing_infeasible_reason = (
                f"{TIMING_INFEASIBLE_NPLC_SET_FAILED}: a driver nem támogatja a set_nplc()-t"
            )
            return False
        # Az írás érvényesülhet akkor is, ha az OPC már hibát ad.
        self._nplc_applied = True
        self._nplc_restore_value = prior_nplc
        try:
            setter(self._acquisition_nplc)
        except Exception as exc:
            self._timing_infeasible = True
            self._timing_infeasible_reason = f"{TIMING_INFEASIBLE_NPLC_SET_FAILED}: {exc}"
            self._restore_acquisition_nplc()
            return False

        if self._require_autozero_on:
            try:
                # PROJECT_POLICY Q-6: AZ ON marad; NPLC terminálisan visszaáll.
                # VERIFIED_PRIMARY: 34460-90901 Ed.7, 382., 385., 310–311. o.
                self._dmm_v.enable_autozero()
                actual_nplc = float(self._dmm_v.get_nplc())
                if not math.isclose(actual_nplc, self._acquisition_nplc, rel_tol=1e-9):
                    raise RuntimeError(f"NPLC readback: {actual_nplc!r}")
                errors = self._dmm_v.check_error()
                if errors:
                    raise RuntimeError("; ".join(errors))
            except Exception as exc:  # noqa: BLE001 - diagnosztizált konfigurációs hiba, ON előtt
                self._timing_infeasible = True
                self._timing_infeasible_reason = f"ACQUISITION_CONFIG_READBACK_FAILED: {exc}"
                self._restore_acquisition_nplc()
                return False

        ok, reason = self._verify_sample_timer_capability()
        if not ok:
            self._timing_infeasible = True
            self._timing_infeasible_reason = reason
            self._restore_acquisition_nplc()
            return False
        return True

    def _verify_sample_timer_capability(self) -> tuple[bool, str]:
        """SAMPle:TIMer? MIN a MÁR beállított integrációs idő mellett — a
        kézikönyv által előírt sorrendben (l. device_dmm.py::
        query_sample_timer_capability() docstringje).

        Ellenőrizetlen képességet NEM tekintünk teljesítettnek: ha a driver
        nem tudja lekérdezni, vagy a válasz értelmezhetetlen, az eredmény
        `infeasible` (fail-safe), nem "valószínűleg jó lesz"."""
        query = getattr(self._dmm_v, "query_sample_timer_capability", None)
        if query is None:
            return False, TIMING_INFEASIBLE_CAPABILITY_UNSUPPORTED
        try:
            raw = query()
        except Exception as exc:
            return False, f"{TIMING_INFEASIBLE_CAPABILITY_QUERY_FAILED}: {exc}"
        try:
            min_interval_s = float(str(raw).strip())
        except (TypeError, ValueError):
            return False, f"{TIMING_INFEASIBLE_CAPABILITY_INVALID}: {raw!r}"
        if (
            math.isnan(min_interval_s)
            or math.isinf(min_interval_s)
            or min_interval_s <= 0.0
        ):
            return False, f"{TIMING_INFEASIBLE_CAPABILITY_INVALID}: {raw!r}"
        if min_interval_s > self._sample_interval_s:
            return False, (
                f"{TIMING_INFEASIBLE_SAMPLE_TIMER_MIN}: SAMP:TIM? MIN="
                f"{min_interval_s:.6g}s > kért {self._sample_interval_s:.6g}s "
                f"(NPLC={self._acquisition_nplc})"
            )
        return True, ""

    def _restore_acquisition_nplc(self, *, after_safe_off: bool = False) -> None:
        """A `_apply_acquisition_nplc()` által átállított integrációs idő
        visszaállítása. IDEMPOTENS — minden terminális útról hívható
        (siker, cutoff, skipped_low_voltage, acquisition-hiba, a hívó
        emergency_stopja), többszöri hívás nem küld újabb SCPI-t.

        A hiba itt nem dobódik tovább (a Load már OFF, a mérés lezárt),
        de NEM is némul el: `nplc_restore_failed` jelzőként jut ki a
        `PulseAcquisitionResult`-ban (rules/10-safety.md 4./20-metrology.md
        — degraded adat csak explicit minősítéssel)."""
        if self._worker is not None or not self._nplc_applied:
            return
        if self._controller_managed_cleanup and not after_safe_off:
            return
        self._nplc_applied = False
        value = self._nplc_restore_value
        self._nplc_restore_value = None
        # Csak az igazolt teljes helyreállítás törölheti a hibajelzőt.
        self._nplc_restore_failed = True
        if value is None:
            return
        try:
            self._dmm_v.set_nplc(value)
            # AZ ON marad; ez a driverhívás AZ?-val is ellenőriz.
            self._dmm_v.enable_autozero()
            actual = float(self._dmm_v.get_nplc())
            if not math.isclose(actual, value, rel_tol=1e-9):
                raise RuntimeError(f"NPLC restore readback: {actual!r}, várt: {value!r}")
        except Exception:
            return
        self._nplc_restore_failed = False

    @property
    def nplc_restore_failed(self) -> bool:
        """Terminális cleanup-hiba finalize() és új I/O nélkül."""
        return self._nplc_restore_failed

    def restore_acquisition_nplc(self) -> bool:
        """Publikus, idempotens cleanup-belépő a HÍVÓ terminális útjaihoz
        (2026-09-12, FAST_TRANSIENT NPLC-javítás).

        Azért kell, mert az impulzus TÖBB `advance()` ticken át tart, és a
        hívó (ocv_soc_controller.py) a `step_chunk()`/`finalize()` cikluson
        KÍVÜLI okból is megszakíthatja: PSU/Load kölcsönös kizárás sérülése,
        hőmérséklet-fault, vagy a TestRunner felőli külső
        `emergency_stop()`. Ilyenkor a szolgáltatás soha nem kapna több
        hívást, és a DMM az ideiglenes, gyors integrációs időn maradna a
        session hátralévő részére.

        Visszatérés: True, ha volt mit visszaállítani ÉS sikerült."""
        was_applied = self._nplc_applied
        drain_error: BaseException | None = None
        try:
            self.finish_owned_acquisition()
        except BaseException as exc:
            drain_error = exc
            raise
        finally:
            # Drain-hiba sem hagyhat gyors NPLC-t; függő ownernél a belső őr tilt.
            try:
                self._restore_acquisition_nplc(after_safe_off=True)
            except BaseException as restore_error:
                if drain_error is None:
                    raise
                drain_error.add_note(f"NPLC_RESTORE_INTERRUPTED:{restore_error}")
        return was_applied and not self._nplc_restore_failed

    def _verify_actual_sample_timer_or_raise(self, *, owner: object | None = None) -> None:
        """2026-09-12, musszer-only HIL-javítás (INSTR-DMM-SAMPTIM-001):
        `configure_timed_acquisition()` UTÁN, a MŰSZERTŐL visszaolvasott
        TÉNYLEGES `SAMP:TIM` ellenőrzése a KÉRT `self._sample_interval_s`
        ellen — a meglévő `check_error()`-alapú védelem (CF-04) MELLETT,
        nem helyette.

        Miért kell külön ellenőrzés: valós Keysight 34465A-n (NPLC=10,
        0,2 s SAMP:TIM kérés) empirikusan igazolva, hogy a műszer a kért
        intervallumot CSENDBEN, A HIBASORBA SEMMIT NEM ÍRVA is figyelmen
        kívül hagyhatja (a `SAMP:TIM` az előző értékén marad) — enélkül a
        `_step_instrument_timed()`/`_step_hardware_pulse()` a KÉRT (nem a
        tényleges) intervallummal számolna visszafelé időbélyeget minden
        mintára, csendben rossz `dt`-vel (rules/20-metrology.md). L.
        Folyamatok/canonical/CLAIM_LEDGER.yaml INSTR-DMM-SAMPTIM-001
        UPDATE 2026-09-12 a teljes SCPI-sweep-eredményért.

        R16-B3: a jelen lévő getter hibája konfigurációs elutasítás.
        Hiányzó getter esetén a régi test double-ok viselkedése megmarad;
        a meglévő `check_error()`-kapu továbbra is aktív."""
        getter = getattr(self._dmm_v, "get_sample_timer_s", None)
        if getter is None:
            return
        try:
            actual_s = float(getter() if owner is None else getter(owner=owner))
        except Exception as exc:
            raise RuntimeError(
                "DMM_TIMED_ACQUISITION_CONFIG_REJECTED: SAMP:TIM readback failed"
            ) from exc
        if math.isclose(actual_s, self._sample_interval_s, rel_tol=1e-3, abs_tol=1e-6):
            return
        raise RuntimeError(
            "DMM_TIMED_ACQUISITION_CONFIG_REJECTED: SAMP:TIM tényleges "
            f"{actual_s:.6g}s eltér a kért {self._sample_interval_s:.6g}s-től "
            "(hibasor üres maradt — csendes elutasítás, l. INSTR-DMM-SAMPTIM-001)"
        )

    def _check_discharge_floor(self, values: list[float]) -> None:
        # Teljes nyers chunk, a cutoff-csonkolás ELŐTT; Q-35 itt nem kivétel.
        for voltage in values:
            result = self._safety.check_normal_discharge(
                voltage, self._pulse_current_A, self._terminate_voltage_V,
            )
            if result.fault is not None:
                self._done = True
                self._discharge_fault = PulseDischargeError(
                    f"{result.fault.name}: {result.message}"
                )
                raise self._discharge_fault

    def _check_max_voltage(self, values: list[float]) -> None:
        # R8-B2: a medián sem fedhet el felsőhatár-túllépést.
        for voltage in values:
            if voltage > self._max_voltage_V:
                self._done = True
                raise PulseOvervoltageError(
                    f"BATTERY_OVERVOLTAGE: {voltage:.6g} V > {self._max_voltage_V:.6g} V"
                )

    def _check_load_power(self, values: list[float], current_A: float) -> None:
        # R9-B2: a median sem fedhet el teljesitmeny-tullepest.
        for voltage in values:
            power_W = abs(voltage * current_A)
            if power_W > self._max_load_power_W:
                self._done = True
                raise PulsePowerLimitError(
                    f"LOAD_POWER_LIMIT_EXCEEDED: U={voltage:.6g} V, I={current_A:.6g} A, "
                    f"P={power_W:.6g} W > limit={self._max_load_power_W:.6g} W"
                )

    def _capture_baseline_window(self) -> float:
        """RB Pulse spec §9.3: instrumentum-időzített ablak a pulse ELŐTT,
        medián kiértékeléssel — nem egyetlen minta. Az I_baseline-t (load
        még KI van kapcsolva ezen a ponton) egyetlen `measure_current()`
        hívással mérjük (a load nyugalmi állapotában a medián-ablak nem ad
        érdemi többletet, ellentétben a feszültség polarizáció/zaj
        érzékenységével)."""
        sample_count = max(1, round(self._baseline_window_s / self._baseline_sample_interval_s))
        # Review-kör 5, Hardver/Műszervezérlés HW-R5-F (P2, a HW5-04
        # [68a4ee7] befejezetlen sibling-felülete -- l.
        # _capture_recovery_if_configured() azonos hivatkozású kommentje):
        # a DMM connect()-kori VISA I/O timeoutja (alapból 10s) egy ennél
        # hosszabb `baseline_window_s` esetén GARANTÁLTAN lejárna a lenti
        # `fetch_timed_acquisition()`-nál, mielőtt a műszer befejezné a
        # kért mintaszám felvételét. Rövid ablakra (a `max()` miatt) nulla
        # hatású -- csak akkor terjeszti ki a timeoutot, ha az ablak
        # ténylegesen hosszabb, mint a jelenlegi timeout margóval csökkentve.
        original_timeout_ms = getattr(self._dmm_v, "timeout_ms", None)
        set_timeout = getattr(self._dmm_v, "set_io_timeout_ms", None)
        if set_timeout is not None and original_timeout_ms is not None:
            fetch_timeout_ms = max(
                original_timeout_ms, round(self._baseline_window_s * 1000) + 5000
            )
            set_timeout(fetch_timeout_ms)
        try:
            self._dmm_v.configure_timed_acquisition(
                sample_count=sample_count, interval_s=self._baseline_sample_interval_s
            )
            self._dmm_v.arm_timed_acquisition()
            raw = self._dmm_v.fetch_timed_acquisition()
        finally:
            if set_timeout is not None and original_timeout_ms is not None:
                try:
                    set_timeout(original_timeout_ms)
                except Exception:
                    pass
            # Review-kör 5 (2026-09-09), Hardver HW5-01 / Architektúra A-3
            # (P1, DFAM-001+DFAM-005) -- l. _step_instrument_timed() azonos
            # hivatkozású kommentje ugyanerről a DMM sticky-állapot
            # hibaágon-kimaradó reset-jéről. Itt korábban EGYÁLTALÁN nem
            # volt try/except a 3 hívás körül, tehát bármelyikük kivétele
            # teljesen megkerülte a resetet.
            try:
                self._dmm_v.reset_to_single_sample()
            except Exception:
                pass
        # reject_invalid=False: best-effort diagnosztikai medián-ablak, a
        # load ezen a ponton még nincs bekapcsolva — egy érvénytelen minta
        # kihagyása (nem a teljes olvasás elvetése) a helyes, meglévő
        # fail-soft viselkedés (l. _parse_csv_floats docstringje, CF-09).
        values = _parse_csv_floats(raw, reject_invalid=False)
        self._check_max_voltage(values)
        self._check_load_power(values, self._pulse_current_A)
        median_v = _median(values)
        v_before = median_v if median_v is not None else self._dmm_v.read_voltage()

        # Teljes kódbázis review 4. kör (2026-08-29), Metrológia/
        # Adatintegritás P2 lelet: a docstring (fent) ígéri, hogy
        # baseline_window_s>0 esetén a mért I_baseline FELÜLÍRJA a hívó
        # feltételezett baseline_current_A-ját — korábban ez sosem történt
        # meg, self._baseline_current_A a begin_pulse()-nak átadott
        # (jelenleg mindenhol 0.0 default) feltételezett értéken maradt, és
        # a finalize()-beli R(t) számítás (delta_i = current_A -
        # self._baseline_current_A) MINDIG ezt, nem a ténylegesen mért
        # bázisáramot használta — a measured_baseline_current_A csak
        # diagnosztikai mezőként jutott ki. Siker esetén most ténylegesen
        # átveszi a mért érték a számításban betöltött szerepet; hiba esetén
        # (self._pulse_current_degraded mintáját követve) degraded jelzővel
        # a feltételezett érték marad, nem csendes hallgatás.
        try:
            self._measured_baseline_current_A = self._load.measure_current()
            self._baseline_current_A = self._measured_baseline_current_A
        except Exception:
            self._measured_baseline_current_A = None
            self._baseline_current_degraded = True
        return v_before

    def prepare_owned_acquisition(self) -> None:
        """Belső runner-belépő: a command-óra már az ON-küldés előtt él."""
        if self._worker is not None:
            raise RuntimeError("PULSE_DMM_RELEASE_PENDING")
        self._owned_requested = True

    def cancel_acquisition(self) -> None:
        """Csak jelzés; nincs DMM-I/O és nincs várakozás."""
        if self._worker is not None:
            self._worker.cancel.set()

    def note_load_off_command(self) -> None:
        if self._owned_window and self._on_command_at is not None and self._off_command_at is None:
            self._off_command_at = self._time_source()
            pending_at = getattr(self._load, "input_on_commanded_at", None)
            self._activation_ack_expired_at_off = (
                self._load.input_commanded_on and pending_at is not None
                and self._off_command_at - pending_at >= INPUT_ACTIVATION_ACK_TIMEOUT_S
            )

    @property
    def activation_ack_diagnostic(self) -> str | None:
        if not self._activation_ack_expired_at_off:
            return None
        return (
            f"INPUT_STATE_POLL_FAILED: ON-nyugta a {INPUT_ACTIVATION_ACK_TIMEOUT_S:g} s "
            "határig nem igazolt; a lejárt újraellenőrzést az OFF-prioritás miatt "
            "nem indítottuk. Ez a korábbi ON bizonyítéka, nem az OFF eredménye."
        )

    def note_load_off_return(self) -> None:
        if (
            (self._on_return_at is not None or self._off_command_at is not None)
            and self._off_return_at is None and self._load_off_real_s is None
        ):
            self._off_return_at = self._time_source()
            self._load_off_real_s = self._off_return_at - self._t0

    @property
    def load_window_diagnostic(self) -> str | None:
        """Tiszta auditadat STOP/FAULT esetén is; finalize és új I/O nélkül."""
        if not self._owned_window or self._on_command_at is None:
            return None
        duration = (
            self._off_command_at - self._on_command_at
            if self._off_command_at is not None else None
        )
        error = duration - self._duration_s if duration is not None else None
        return (
            f"ON-küldés={self._on_command_at}; ON-visszatérés={self._on_return_at}; "
            f"OFF-küldés={self._off_command_at}; OFF-visszatérés={self._off_return_at}; "
            f"parancsablak={duration} s; cél-eltérés={error} s. "
            "A fizikai kapcsolási idő HIL_PENDING."
        )

    @property
    def acquisition_cleanup_diagnostic(self) -> str:
        return self._worker_cleanup_diagnostic

    def capture_owned_recovery(self) -> None:
        if not self._owned_window or self._owned_recovery_done:
            return
        if self._worker is not None:
            raise RuntimeError("PULSE_DMM_RELEASE_PENDING")
        self._owned_recovery_done = True
        if self.handoff_stop_requested is None or not self.handoff_stop_requested():
            self._capture_recovery_if_configured()

    def begin_owned_acquisition(
        self, supervise: Callable[[], bool], cleanup: Callable[[], bool],
    ) -> None:
        """A runner az ON után, pacing és további mérés előtt hívja."""
        if self._worker is not None:
            raise RuntimeError("PULSE_DMM_RELEASE_PENDING")
        if self._done or not self._started:
            return
        self._owned_window = True
        self._t0 = self._on_command_at if self._on_command_at is not None else self._t0
        self._block_deadline = self._t0 + self._duration_s
        self._block_supervise = supervise
        self._block_cleanup = cleanup
        self._worker = _PulseDmmWorker(self)
        self._request_owned_chunk()

    def allow_owned_io(self) -> bool:
        """Élő kapu a felügyeleti T/poll minden új I/O-határán."""
        if self._done:
            return False
        stopped = self.handoff_stop_requested is not None and self.handoff_stop_requested()
        if stopped or self._window_expired():
            self.cancel_acquisition()
            self._done = True
            self._owned_off()
            return False
        return True

    def _window_expired(self) -> bool:
        now = self._time_source()
        # Lebegőpontos összeadási maradék; nem mérési tolerancia.
        return now >= self._block_deadline or math.isclose(
            now, self._block_deadline, rel_tol=0.0, abs_tol=1e-9,
        )

    def _request_owned_chunk(self) -> None:
        remaining = self._block_deadline - self._time_source()
        count = min(
            self._chunk_sample_count,
            max(0, math.floor(remaining / self._sample_interval_s + 1e-9)),
        )
        # A mintaszám csak tervez: ha már nem fér új minta, az óra zár.
        if count and self._worker is not None:
            self._worker.request(count)

    def _owned_off(self) -> None:
        self.note_load_off_command()
        self._load.input_off()
        self.note_load_off_return()

    def finish_owned_acquisition(self) -> None:
        """Csak a hívó két OFF-kísérlete után: drain/release, majd restore."""
        worker = self._worker
        if worker is None:
            return
        self._done = True
        cleanup_error: BaseException | None = None
        release_pending = False
        try:
            worker.close_after_safe_off()
        except BaseException as exc:  # noqa: BLE001 - a raw-kapu után az eredeti cleanuphiba megy tovább
            self._worker_cleanup_diagnostic = str(exc)
            # A már visszatért raw a reset/release várakozása alatt is kapuzandó.
            release_pending = worker._thread.is_alive() or not worker.owner_release_confirmed
            cleanup_error = exc
        # A reset hibája sem ugorhatja át a visszatért TELJES raw safety-kaput.
        try:
            while True:
                raw, ended, error = worker.results.get_nowait()
                if error is not None:
                    raise error
                if raw is not None:
                    values = _parse_csv_floats(raw)
                    if not values:
                        raise ValueError("PULSE_DMM_NO_VALID_SAMPLES")
                    self._accept_timed_chunk(raw, ended, True, off_attempted=True)
        except Empty:
            pass
        finally:
            if not release_pending and worker.owner_release_confirmed:
                self._worker = None
            self._block_supervise = None
            self._block_cleanup = None
        if cleanup_error is not None:
            raise cleanup_error

    def _step_owned_timed(self) -> bool:
        worker = self._worker
        assert worker is not None
        off_error: BaseException | None = None

        def stop_requested() -> bool:
            nonlocal off_error
            try:
                return self._stop_active_pulse_if_requested()
            except BaseException as exc:  # noqa: BLE001 - a raw-kapu után az eredeti kivétel propagál
                off_error = exc
                return True

        def turn_off() -> None:
            nonlocal off_error
            try:
                self._owned_off()
            except BaseException as exc:  # noqa: BLE001 - a raw-kapu után az eredeti kivétel propagál
                off_error = exc

        def accept_raw(raw: str | None, ended: float, stopped: bool,
                       error: BaseException | None = None) -> bool:
            try:
                if error is not None:
                    raise error
                more = self._accept_timed_chunk(
                    raw, ended, stopped, off_attempted=off_error is not None,
                ) if raw is not None else False
            except BaseException as gate_error:
                if off_error is not None:
                    raise off_error from gate_error
                raise
            if off_error is not None:
                raise off_error
            return more

        while True:
            stopped = stop_requested()
            expired = self._window_expired()
            if stopped or expired:
                self._done = True
                worker.cancel.set()
                if not stopped:
                    turn_off()
                # Előbb a már visszatért chunk kapuja, utána a két OFF és a drain.
                try:
                    raw, ended, error = worker.results.get_nowait()
                except Empty:
                    raw, ended, error = None, 0.0, None
                accept_raw(raw, ended, True, error)
                if self._block_cleanup is not None:
                    self._block_cleanup()
                return False
            if self._block_supervise is not None and not self._block_supervise():
                return False
            if self._done:
                return False
            if stop_requested() or self._window_expired():
                continue
            try:
                raw, ended, error = worker.results.get(
                    timeout=min(0.02, max(0.0, self._block_deadline - self._time_source())),
                )
            except Empty:
                continue
            if error is not None:
                raise error
            # Várakozás és minden új Load-I/O előtt élő stop/deadline-kapu.
            stopped = stop_requested()
            expired = self._window_expired()
            if raw is None:
                if stopped or expired:
                    continue
                raise RuntimeError("PULSE_DMM_CAPTURE_INTERRUPTED")
            if expired and not stopped:
                self._done = True
                worker.cancel.set()
                turn_off()
            more = accept_raw(raw, ended, stopped or expired)
            if not more:
                if self._block_cleanup is not None:
                    self._block_cleanup()
                return False
            self._request_owned_chunk()
            return True

    def _stop_active_pulse_if_requested(self) -> bool:
        """R16-B13F1: energia OFF minden további I/O és cleanup előtt.

        Az okot a meglévő hívói/runner feloldó őrzi. Új mérés és
        energiabecslés nincs; a már rögzített prefix könyvelhető marad.
        """
        if self.handoff_stop_requested is None or not self.handoff_stop_requested():
            return False
        if not self._done:
            self._done = True
            if self._owned_window:
                self._owned_off()
            else:
                self._load.input_off()
                if self._trigger_mode == TRIGGER_MODE_INSTRUMENT_TIMED:
                    self._load_off_real_s = self._time_source() - self._t0
        return True

    def step_chunk(self) -> bool:
        """Egy mintavételi blokk lekérdezése. Visszaadja, van-e még hátra
        (False = kész vagy megszakadt — cutoff). INSTRUMENT_TIMED módban
        több chunk, HARDWARE_PULSE módban EGYETLEN, teljes-pulzusú hívás
        (l. modul docstring)."""
        if self._done:
            return False
        # 2026-09-12, FAST_TRANSIENT NPLC-javítás: EGY helyen, a publikus
        # belépőn kötjük az integrációs idő visszaállítását az impulzus
        # LEZÁRÁSÁHOZ (nem a chunk végéhez — a gyors NPLC-nek a chunkok
        # KÖZÖTT is érvényben kell maradnia). Így mindkét belső ág
        # (`_step_instrument_timed`/`_step_hardware_pulse`) ÖSSZES terminális
        # útja (mintaszám elérve, cutoff, DMM/Load acquisition-hiba,
        # NO_VALID_SAMPLES, HARDWARE_PULSE_LOAD_OFF_FAILED...) lefedett, új
        # elágazás nélkül — nem kellett minden `self._done = True` helyszínt
        # külön kezelni (rules/10-safety.md 9.: cleanup MINDEN terminális
        # úton). `acquisition_nplc is None` esetén no-op.
        try:
            # A chunkok közötti várakozás alatt is érkezhetett jelzés.
            if (
                self._trigger_mode == TRIGGER_MODE_INSTRUMENT_TIMED
                and self._worker is None
                and self._stop_active_pulse_if_requested()
            ):
                self._restore_acquisition_nplc()
                return False
            if self._trigger_mode == TRIGGER_MODE_HARDWARE_PULSE:
                more = self._step_hardware_pulse()
            else:
                more = self._step_instrument_timed()
        except BaseException:
            self._restore_acquisition_nplc()
            raise
        if not more:
            self._restore_acquisition_nplc()
        return more

    def _step_instrument_timed(self) -> bool:
        if self._worker is not None:
            return self._step_owned_timed()
        remaining = self._total_samples_target - len(self._samples)
        this_chunk = min(self._chunk_sample_count, remaining)
        stopped = False
        owner = object()
        acquired = False

        try:
            try:
                # R16-B21a: a foglalás a konfigurációtól a reset végéig él.
                self._dmm_v.acquire_measurement(owner)
                acquired = True
                if self._stop_active_pulse_if_requested():
                    return False
                self._dmm_v.configure_timed_acquisition(
                    sample_count=this_chunk, interval_s=self._sample_interval_s, owner=owner,
                )
                if self._stop_active_pulse_if_requested():
                    return False
                # Governance-v2 review-kör 1 (CF-04/MET-R1-01/INSTR-DMM-SAMPTIM-001):
                # a Load-ág configure_transient_pulse()/configure_trace() UTÁN már
                # ellenőrzi az error-queue-t (l. _step_hardware_pulse() lent) — a
                # DMM-ágon ez korábban hiányzott. Ha a kért SAMP:TIM az apertúra alatt
                # van, a műszer CSENDBEN megnöveli az intervallumot és -221-et tesz a
                # hibasorba ("Settings conflict; cannot meet requested timing") — enélkül
                # a kód a KÉRT (nem a tényleges) intervallummal számolna visszafelé
                # időbélyeget minden mintára.
                config_errors = self._dmm_v.check_error(owner=owner)
                if config_errors:
                    raise RuntimeError(
                        "DMM_TIMED_ACQUISITION_CONFIG_REJECTED: " + "; ".join(config_errors)
                    )
                if self._stop_active_pulse_if_requested():
                    return False
                self._verify_actual_sample_timer_or_raise(owner=owner)
                if self._stop_active_pulse_if_requested():
                    return False
                self._dmm_v.arm_timed_acquisition(owner=owner)
                if self._stop_active_pulse_if_requested():
                    return False
                raw = self._dmm_v.fetch_timed_acquisition(owner=owner)
                # R1 B8a, F-22 (INSTR-DMM-CONFIG-ATOMIC-001 F3): a chunk
                # végének valós ideje KÖZVETLENÜL a fetch visszatérése után,
                # a lenti reset_to_single_sample() round-tripje ELŐTT --
                # korábban a reset UTÁN vettük, így minden minta ideje a
                # reset idejével későbbre tolódott. A fetch-válasz
                # átviteli késése továbbra is benne van (HIL_PENDING).
                chunk_end_real_s = self._time_source() - self._t0
                stopped = self._stop_active_pulse_if_requested()
            except Exception as exc:
                self._done = True
                self._turn_load_off_or_chain("DMM_ACQUISITION_ERROR", exc)
                raise
        finally:
            # Review-kör 5 HW5-01 / A-3, DFAM-001+DFAM-005: reset minden terminális úton.
            # Idegen foglalást sikertelen acquire után nem resetelhetünk.
            # A reset hibája továbbra sem fedheti el az acquisition eredeti okát.
            if acquired:
                try:
                    self._dmm_v.reset_to_single_sample(owner=owner)
                except Exception:
                    pass
                finally:
                    self._dmm_v.release_measurement(owner)

        return self._accept_timed_chunk(raw, chunk_end_real_s, stopped)

    def _accept_timed_chunk(
        self, raw: str, chunk_end_real_s: float, stopped: bool, *, off_attempted: bool = False,
    ) -> bool:
        # OFF-hiba vagy a hivo ket OFF-ja utan csak a nyers bizonyitek kapuzhato.
        # A reset alatt érkező jelzés se indíthasson új áramolvasást.
        stopped = stopped or off_attempted or self._stop_active_pulse_if_requested()
        try:
            values = _parse_csv_floats(raw)
        except InstrumentInvalidReading as exc:
            # CF-09/MET-R1-05: NaN/Inf/overload minta a chunkban — a cutoff-
            # detektálás minden összehasonlítása csendben False-t adna
            # NaN-nal, egy 9.9E37 overload pedig "OK" minőségű R(t) pontnak
            # tűnne. Ugyanaz a fail-safe minta, mint a DMM_ACQUISITION_ERROR
            # ágon: Load OFF, majd a hiba a hívóhoz.
            self._done = True
            if not off_attempted:
                self._turn_load_off_or_chain("DMM_INVALID_SAMPLE_VALUE", exc)
            raise
        if not values:
            self._done = True
            no_samples_exc = ValueError(
                "PulseMeasurementService: nincs érvényes DMM minta a chunkban"
            )
            if not off_attempted:
                self._turn_load_off_or_chain("NO_VALID_SAMPLES", no_samples_exc)
            raise no_samples_exc

        try:
            self._check_max_voltage(values)
        except PulseOvervoltageError as exc:
            if not off_attempted:
                self._turn_load_off_or_chain("BATTERY_OVERVOLTAGE", exc)
            raise

        try:
            self._check_discharge_floor(values)
        except PulseDischargeError as exc:
            if not off_attempted:
                self._turn_load_off_or_chain("BATTERY_UNDERVOLTAGE", exc)
            raise

        i_now: float | None = None
        if stopped:
            # Csak a már ismert árammal ellenőrzünk; új mérés/energia nincs.
            self._check_load_power(values, self._nearest_current(chunk_end_real_s))
        if not stopped:
            try:
                i_now = self._load.measure_current()
            except Exception as exc:
                if self._owned_window:
                    self._done = True
                    self.cancel_acquisition()
                    if not off_attempted:
                        self._turn_load_off_or_chain("LOAD_CURRENT_READ_FAILED", exc)
                    raise
                i_now = self._pulse_current_A
                self._pulse_current_degraded = True
            stopped = self._stop_active_pulse_if_requested()
            try:
                self._check_load_power(values, i_now)
            except PulsePowerLimitError as exc:
                if not off_attempted:
                    self._turn_load_off_or_chain("LOAD_POWER_LIMIT_EXCEEDED", exc)
                raise

        n = len(values)
        cutoff_hit = False
        for i, v in enumerate(values):
            # A chunkon BELÜLI relatív időzítés a DMM saját SAMP:TIM-je szerint
            # pontos (nincs Python-oldali jitter); a chunk VÉGÉNEK valós idejéhez
            # (chunk_end_real_s) viszonyítva visszafelé számolt abszolút idő.
            t = chunk_end_real_s - (n - 1 - i) * self._sample_interval_s
            if self._owned_window and self._load_off_real_s is not None and t > self._load_off_real_s:
                continue
            self._samples.append(PulseSamplePoint(time_from_pulse_start_s=t, voltage_V=v))
            if v <= self._terminate_voltage_V:
                # SUP01-PULSE-010/TEST-005: a cutoff UTÁNI mintákat (ugyanabban
                # a chunkban) eldobjuk — nem vehetők érvényes pulse-adatnak.
                cutoff_hit = True
                break

        if i_now is not None and self._samples:
            self._current_readings.append((self._samples[-1].time_from_pulse_start_s, i_now))
        if stopped:
            # A visszatért feszültségminták megmaradnak; OFF utáni áramot
            # nem mérünk, és a korábbi Ah-prefixet nem hosszabbítjuk meg.
            self._cutoff = cutoff_hit
            return False

        if cutoff_hit:
            self._cutoff = True
            self._done = True
            if self._owned_window:
                self._owned_off()
                return False
            self._load.input_off()
            # R1 B8a, F-21: OFF-nyugta horgony az input_off() SIKERES
            # visszatérése után, a recovery-ablak ELŐTT (a recovery
            # terheletlen, sosem kerülhet az energiába).
            self._load_off_real_s = self._time_source() - self._t0
            # Az OFF alatt érkező jelzés után már ne induljon recovery.
            if not self._stop_active_pulse_if_requested():
                self._capture_recovery_if_configured()
            return False

        if not self._owned_window and len(self._samples) >= self._total_samples_target:
            self._done = True
            self._load.input_off()
            # R1 B8a, F-21 -- l. a cutoff-ág azonos kommentjét.
            self._load_off_real_s = self._time_source() - self._t0
            # A normál terminális OFF alatt is érkezhet megszakítás.
            if not self._stop_active_pulse_if_requested():
                self._capture_recovery_if_configured()
            return False

        return True

    def _step_hardware_pulse(self) -> bool:
        """Egyetlen, teljes-pulzusú hívás: a Keithley 2380 SAJÁT tranziens
        PULSE generátora adja az áramlépcsőt (device_load.py::
        configure_transient_pulse/fire_transient_pulse), a Keysight DMM a
        TELJES pulzus alatt instrumentum-időzítve mintavételez, a Keithley
        trace bufferéből pedig a tényleges áram-waveform kerül elő.

        **Biztonsági korlát (l. modul docstring)**: a cutoff-detektálás itt
        csak UTÓLAGOS — a pulse a teljes `duration_s` alatt a műszer saját
        időzítésével fut, Python-oldali közbeavatkozás nélkül."""
        # Review-kör 5, Hardver/Műszervezérlés HW-R5-F (P2, a HW5-04
        # [68a4ee7] befejezetlen sibling-felülete -- l.
        # _capture_recovery_if_configured() azonos hivatkozású kommentje):
        # a DMM connect()-kori VISA I/O timeoutja (alapból 10s) a
        # `self._duration_s` szerinti TELJES pulzus alatt GARANTÁLTAN
        # lejárna a lenti `fetch_timed_acquisition()`-nál (a modul
        # docstringje szerint tipikus OCV/SOC impulzus 30s+). Rövid
        # pulzusra (a `max()` miatt) nulla hatású. A `try:` ELŐTT
        # inicializálva -- ha egy korábbi write (pl. configure_transient_
        # pulse()) hibázna, a `finally` ág enélkül `UnboundLocalError`-t
        # dobna a valódi kivétel elfedve.
        # A hardveres konfiguráció egy későbbi tickben indul; a jelzés élő.
        if self.handoff_stop_requested is not None and self.handoff_stop_requested():
            return False
        original_timeout_ms = getattr(self._dmm_v, "timeout_ms", None)
        set_timeout = getattr(self._dmm_v, "set_io_timeout_ms", None)
        stopped = False
        capture_returned = False
        stopped_end_real_s: float | None = None
        require_battery_pulse_mode(TRIGGER_MODE_HARDWARE_PULSE)
        try:
            # Teljes kódbázis review 4. kör (2026-08-29), Hardver/
            # Műszervezérlés P1 lelet: input_on() korábban ITT, a
            # configure_transient_pulse() (BLEV-beállítás + TRAN 1) ELŐTT
            # futott — a Keithley 2380 kézikönyve szerint TRAN 1 előtt a
            # load a REGULÁRIS (nem-tranziens), korábban beállított SOUR:CURR
            # szinten szabályoz (l. device_load.py::configure_transient_
            # pulse() docstringje, ugyanezt egy korábbi HIL-lelet kapcsán
            # dokumentálva) — tehát a load egy KORÁBBI teszt-fázis
            # setpontjával kapcsolt volna be, nem a szándékolt baseline_A-val,
            # rövid, nem-szándékolt áram-tranzienst okozva a mérés alatt lévő
            # akkumulátoron. A nem-hardveres (INSTRUMENT_TIMED) ág helyesen
            # sorrendez (set_current -> input_on) — most ez az ág is azt
            # követi: input_on() csak azután, hogy a BLEV/TRAN 1 már be van
            # állítva ÉS a konfiguráció hibátlansága igazolt (lásd lentebb
            # check_error()), közvetlenül fire_transient_pulse() előtt.
            self._load.configure_transient_pulse(
                pulse_A=self._pulse_current_A,
                baseline_A=self._baseline_current_A,
                pulse_width_s=self._duration_s,
            )
            # R16-B9: az A/B readback még a trace indítása előtt fusson,
            # mert a lekérdezések alapból Device Clear-t küldenek.
            config_errors = self._load.check_error()
            if config_errors:
                raise RuntimeError(
                    "HARDWARE_PULSE_CONFIG_REJECTED: " + "; ".join(config_errors)
                )
            try:
                actual_levels = self._load.read_transient_current_levels()
            except Exception as exc:
                raise RuntimeError(
                    "HARDWARE_PULSE_CONFIG_REJECTED: ALEV/BLEV readback sikertelen"
                ) from exc
            # A .6g formátumban elküldött érték a parancsolt szint;
            # nincs setpoint-fallback vagy eltérést elfedő tolerancia.
            expected_levels = (
                float(format(self._pulse_current_A, ".6g")),
                float(format(self._baseline_current_A, ".6g")),
            )
            if actual_levels != expected_levels:
                raise RuntimeError(
                    "HARDWARE_PULSE_CONFIG_REJECTED: "
                    f"ALEV/BLEV várt={expected_levels!r}, alkalmazott={actual_levels!r}"
                )
            self._load.configure_trace(
                feed="CURR", interval_s=self._trace_interval_s, points=self._trace_points,
            )
            # Teljes kódbázis review 1. kör (2026-08-26), P1 lelet: korábban
            # a configure_transient_pulse()/configure_trace() UTÁN, a tényleges
            # fire_transient_pulse() ELŐTT sosem ellenőriztük a Load SCPI
            # error-queue-ját — pontosan ez a mintázat okozta a dokumentált,
            # HIL-lel felfedezett TRAN 1 hiányzó parancs hibát (l. modul
            # docstring 142-150. sora). Egy csendben elutasított konfiguráció
            # (pl. -221 Settings Conflict) a fetch_trace()-t/fetch_timed_
            # acquisition()-t akkor is lefuttatná, hamis, lapos (nem-pulzáló)
            # waveform-ot eredményezve, amit a rendszer OK minőségű R(t)
            # adatpontként fogadna el.
            #
            # MAX verifier follow-up (2026-09-08, HW2-08 P1-1): a fenti
            # configure_trace() UTOLSÓ write-ja (TRAC:FEED:CONT NEXT) MÁR
            # elindítja a trace-capture-t (kézikönyv p.97) -- ez a
            # check_error() és a lenti input_on()/fire_transient_pulse()
            # hívás EZEN A PONTON TÚL mind a capture-ablakon BELÜL fut,
            # ezért mindhárom clear_first=False-t kap, l. device_load.py::
            # _query() docstringje.
            config_errors = self._load.check_error(clear_first=False)
            if config_errors:
                raise RuntimeError(
                    "HARDWARE_PULSE_CONFIG_REJECTED: " + "; ".join(config_errors)
                )
            sample_count = max(1, round(self._duration_s / self._sample_interval_s))
            self._dmm_v.configure_timed_acquisition(
                sample_count=sample_count, interval_s=self._sample_interval_s,
            )
            # CF-04/MET-R1-01/INSTR-DMM-SAMPTIM-001 — l. _step_instrument_timed()
            # azonos indoklása.
            dmm_config_errors = self._dmm_v.check_error()
            if dmm_config_errors:
                raise RuntimeError(
                    "DMM_TIMED_ACQUISITION_CONFIG_REJECTED: " + "; ".join(dmm_config_errors)
                )
            self._verify_actual_sample_timer_or_raise()
            if set_timeout is not None and original_timeout_ms is not None:
                fetch_timeout_ms = max(
                    original_timeout_ms, round(self._duration_s * 1000) + 5000
                )
                set_timeout(fetch_timeout_ms)
            self._dmm_v.arm_timed_acquisition()
            # A konfiguráció és a readback alatt is érkezhetett leállítás.
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                return False
            self._load.input_on(clear_first=False)
            # [review-lelet] t0 KÖZVETLENÜL a tényleges tüzelés előtt, nem
            # begin_pulse()-ban - l. begin_pulse() HARDWARE_PULSE ágának
            # kommentje.
            self._t0 = self._time_source()
            if self._stop_active_pulse_if_requested():
                return False
            self._load.fire_transient_pulse(clear_first=False)
            if self._stop_active_pulse_if_requested():
                return False
            voltage_raw = self._dmm_v.fetch_timed_acquisition()
            capture_returned = True
            stopped = self._stop_active_pulse_if_requested()
            if stopped:
                stopped_end_real_s = self._time_source() - self._t0
            # A már felvett trace OFF után is kiolvasható; új INIT nincs.
            current_raw = self._load.fetch_trace()
            if not stopped:
                stopped = self._stop_active_pulse_if_requested()
                if stopped:
                    stopped_end_real_s = self._time_source() - self._t0
        except Exception as exc:
            self._done = True
            self._turn_load_off_or_chain("HARDWARE_PULSE_ACQUISITION_ERROR", exc)
            raise
        finally:
            if set_timeout is not None and original_timeout_ms is not None:
                try:
                    set_timeout(original_timeout_ms)
                except Exception:
                    pass
            try:
                # A timeout-visszaállítás alatt érkező jelzés a resetet is megelőzi.
                if capture_returned and not stopped:
                    stopped = self._stop_active_pulse_if_requested()
                    if stopped:
                        stopped_end_real_s = self._time_source() - self._t0
            finally:
                # Az OFF hibája sem hagyhatja ki a DMM visszaállítását.
                try:
                    self._dmm_v.reset_to_single_sample()
                except Exception:
                    pass

        # Teljes kódbázis review 5. kör (2026-08-29), Hardver/Műszervezérlés
        # P0 lelet: a SIKERES ágon a Load OFF hibája korábban a fenti finally
        # blokk try/except Exception: pass-ébe volt csomagolva — csendben
        # elnyelődött, finalize() egy teljesen sikeres pulzust jelentett,
        # miközben a Load fizikailag "bizonytalan ON" maradhatott (a
        # baseline_A szinten húzva áramot) FAULT/WARNING/riport-jelzés
        # nélkül (CLAUDE.md §10.3/10.4). A testvér _step_instrument_timed()
        # NEM csomagolja try/except-be a saját input_off()-ját — itt ugyanaz
        # az elv: a hiba felszínre kerül, a hívó (pl. ocv_soc_controller.py::
        # _run_impulse_acquire()) ezt már FAULT-ba viszi, ugyanúgy, mint a
        # _turn_load_off_or_chain() a hibaágon.
        try:
            if not stopped:
                self._load.input_off()
        except Exception as off_exc:
            self._done = True
            raise RuntimeError(
                f"HARDWARE_PULSE_LOAD_OFF_FAILED_AFTER_SUCCESSFUL_ACQUISITION: {off_exc}"
            ) from off_exc

        # A reset és a terminális OFF alatt is érkezhetett jelzés.
        # A Load már OFF: csak az élő állapotot frissítjük, új I/O nélkül.
        stopped = stopped or (
            self.handoff_stop_requested is not None and self.handoff_stop_requested()
        )

        # Ugyanaz az "anchor a lekérdezés VALÓS befejezési idejéből, visszafelé
        # számolva" minta, mint az INSTRUMENT_TIMED módban (l. modul docstring
        # "VALÓS IDŐZÍTÉS") — a configure/arm/fire SCPI-hívások közötti Python-
        # oldali holtidő miatt a self._t0 (begin_pulse()-kori időbélyeg) NEM
        # esik egybe a tényleges fire_transient_pulse() pillanatával, ezért a
        # mintaidőket a fetch VÉGÉHEZ (ismert, mért pillanat) kell kötni, nem
        # a bizonytalan kezdőponthoz előre számolva.
        waveform_end_real_s = (
            stopped_end_real_s if stopped_end_real_s is not None
            else self._time_source() - self._t0
        )
        try:
            voltage_values = _parse_csv_floats(voltage_raw)
            current_values = _parse_csv_floats(current_raw)
        except InstrumentInvalidReading as exc:
            # CF-09/MET-R1-05: l. _step_instrument_timed() azonos ága. A
            # load itt már fizikailag OFF (fent) — a _turn_load_off_or_chain()
            # hívása idempotens, ugyanazt a mintát követi, mint a lenti
            # NO_VALID_SAMPLES ág.
            self._done = True
            self._turn_load_off_or_chain("DMM_INVALID_SAMPLE_VALUE", exc)
            raise

        if not voltage_values:
            self._done = True
            no_samples_exc = ValueError(
                "PulseMeasurementService: nincs érvényes DMM waveform a hardveres pulzusból"
            )
            self._turn_load_off_or_chain("NO_VALID_SAMPLES", no_samples_exc)
            raise no_samples_exc

        self._check_max_voltage(voltage_values)
        expected_voltage_n = sample_count
        expected_current_n = max(1, round(self._duration_s / self._trace_interval_s))
        self._voltage_trace_incomplete = len(voltage_values) < expected_voltage_n
        self._current_trace_incomplete = (
            not current_values or len(current_values) < min(expected_current_n, self._trace_points)
        )

        n = len(voltage_values)
        cutoff_hit = False
        cutoff_index = n
        for i, v in enumerate(voltage_values):
            t = waveform_end_real_s - (n - 1 - i) * self._sample_interval_s
            self._samples.append(PulseSamplePoint(time_from_pulse_start_s=t, voltage_V=v))
            if v <= self._terminate_voltage_V and not cutoff_hit:
                cutoff_hit = True
                cutoff_index = i
        if cutoff_hit:
            self._cutoff = True

        # Áram-trace igazítás a feszültség-idővonalhoz — ugyanaz a "fetch
        # végéhez kötött, visszafelé számolt" minta, mint a feszültség-
        # waveformnál (a trace saját interval_s-je szerint egyenletesen
        # mintavételezett, de ugyanahhoz a valós fetch-pillanathoz kötve).
        if current_values:
            n_curr = len(current_values)
            for i, i_val in enumerate(current_values):
                t = waveform_end_real_s - (n_curr - 1 - i) * self._trace_interval_s
                self._current_readings.append((t, i_val))
        elif not stopped:
            # Ha a trace nem adott vissza semmit, a beállított pulse-áramot
            # használjuk degraded jelzéssel — ugyanaz az elv, mint az
            # INSTRUMENT_TIMED mód measure_current()-hibájánál.
            self._pulse_current_degraded = True
            self._current_readings.append(
                (self._samples[cutoff_index if cutoff_hit else -1].time_from_pulse_start_s
                 if self._samples else 0.0, self._pulse_current_A)
            )

        # R9-B2: a waveform teljesitmenyellenorzese csak utolagos.
        for sample in self._samples:
            self._check_load_power(
                [sample.voltage_V], self._nearest_current(sample.time_from_pulse_start_s)
            )
        for t, current in self._current_readings:
            nearest = self._nearest_sample(t)
            if nearest is not None:
                self._check_load_power([nearest.voltage_V], current)
        if cutoff_hit:
            self._samples = self._samples[: cutoff_index + 1]

        self._done = True
        if not stopped:
            self._capture_recovery_if_configured()
        return False

    def _capture_recovery_if_configured(self) -> None:
        """RB Pulse spec §8: a pulse (Load OFF) UTÁNI recovery-ablak, külön
        instrumentum-időzített mintavétellel. Hiba esetén NEM szakítja meg a
        már lezárt pulse-t (a recovery csak kiegészítő diagnosztika) — csendes
        best-effort, mert a fő mérés eredménye ekkorra már véglegesült."""
        if self._recovery_duration_s <= 0:
            return
        sample_count = max(1, round(self._recovery_duration_s / self._recovery_sample_interval_s))
        # Review-kör 5, Hardver/Műszervezérlés HW5-04 (P3): ellentétben a fő
        # pulse-mintavétellel (l. modul docstring "rövid, egymást követő
        # chunk-okban" indoklása), ez a hívás EGYETLEN, chunkolatlan
        # fetch_timed_acquisition()-t (FETC?) ad ki a TELJES recovery-
        # ablakra. A DMM fix, connect()-kori VISA I/O timeoutja (alapból
        # 10s, l. device_dmm.py::timeout_ms) egy ennél hosszabb recovery_
        # duration_s esetén GARANTÁLTAN lejár, mielőtt a műszer befejezné a
        # kért mintaszám felvételét -- a hívó ezt eddig csendben, a lenti
        # best-effort except-ágban nyelte el, tehát egy 10s-nél hosszabb
        # recovery-ablak SOHA nem adott volna vissza tényleges mintát. Az
        # I/O timeoutot a hívás idejére a várható időtartamhoz igazítjuk
        # (+5s margó a mintavételi/kommunikációs rezsire), majd a
        # finally-ban visszaállítjuk az eredeti (normál üzemi) értékre --
        # ha a driver nem támogatja (régi mock/test double), a getattr
        # None-t ad, és a viselkedés a régi (esetlegesen timeoutoló)
        # marad, nem új hibaosztály.
        original_timeout_ms = getattr(self._dmm_v, "timeout_ms", None)
        set_timeout = getattr(self._dmm_v, "set_io_timeout_ms", None)
        if set_timeout is not None and original_timeout_ms is not None:
            fetch_timeout_ms = max(
                original_timeout_ms, round(self._recovery_duration_s * 1000) + 5000
            )
            set_timeout(fetch_timeout_ms)
        try:
            if self._owned_window and self._stop_active_pulse_if_requested():
                return
            self._dmm_v.configure_timed_acquisition(
                sample_count=sample_count, interval_s=self._recovery_sample_interval_s,
            )
            if self._owned_window and self._stop_active_pulse_if_requested():
                return
            self._dmm_v.arm_timed_acquisition()
            if self._owned_window and self._stop_active_pulse_if_requested():
                return
            raw = self._dmm_v.fetch_timed_acquisition()
            if self._owned_window and self._stop_active_pulse_if_requested():
                return
            # VALÓS IDŐZÍTÉS (review-lelet, RB Pulse spec kiegészítés): a fő
            # chunk-mintavétellel és a HARDWARE_PULSE waveform/trace fetch-ekkel
            # azonos mintát követve — a MÉRT befejezési pillanathoz anchor-olva,
            # visszafelé számolva, NEM egy nominális (potenciálisan holtidőt
            # rejtő) offsettel előre a pulse végétől.
            recovery_end_real_s = self._time_source() - self._t0
        except Exception:
            return
        finally:
            if set_timeout is not None and original_timeout_ms is not None:
                try:
                    set_timeout(original_timeout_ms)
                except Exception:
                    pass
            try:
                self._dmm_v.reset_to_single_sample()
            except Exception:
                pass
        # reject_invalid=False: csendes best-effort diagnosztika (l. modul
        # docstring fent) — egy érvénytelen minta kihagyása, nem a teljes
        # recovery-ablak elvetése (CF-09).
        values = _parse_csv_floats(raw, reject_invalid=False)
        n = len(values)
        for i, v in enumerate(values):
            t = recovery_end_real_s - (n - 1 - i) * self._recovery_sample_interval_s
            self._recovery_samples.append(PulseSamplePoint(time_from_pulse_start_s=t, voltage_V=v))

    def finalize(self) -> PulseAcquisitionResult:
        """A kiértékelt eredmény. begin_pulse() után (akár SKIPPED_LOW_VOLTAGE
        esetén is), a step_chunk() ciklus lezárása (False visszatérés) után
        hívandó."""
        # 2026-09-12, FAST_TRANSIENT NPLC-javítás: utolsó védővonal — ha a
        # hívó a step_chunk() ciklust nem vitte terminális állapotig (l. lent
        # a `not self._done` védekező ág), az integrációs idő itt is
        # visszaáll. Idempotens, a normál úton már nincs mit tennie.
        self._restore_acquisition_nplc()
        if not self._started:
            return PulseAcquisitionResult(
                started=False,
                skipped_low_voltage=self._skipped_low_voltage,
                cutoff_during_pulse=False,
                pulse_current_degraded=False,
                v_baseline_V=self._v_baseline_V,
                pulse_current_set_A=self._pulse_current_A,
                pulse_current_measured_mean_A=self._pulse_current_A,
                pulse_duration_set_s=self._duration_s,
                pulse_duration_actual_s=0.0,
                synchronization_quality=SYNC_QUALITY_INSTRUMENT_TIMED_SOFTWARE_TRIGGERED,
                method_origin="PROJECT_ENGINEERING_METHOD",
                removed_Ah=0.0,
                trigger_mode=self._trigger_mode,
                measured_baseline_current_A=self._measured_baseline_current_A,
                baseline_current_degraded=self._baseline_current_degraded,
                timing_infeasible=self._timing_infeasible,
                timing_infeasible_reason=self._timing_infeasible_reason,
                nplc_restore_failed=self._nplc_restore_failed,
            )

        if not self._done:
            # Védekező út — finalize() a step_chunk() ciklus lezárása előtt
            # hívva; biztonságos leállítás, nem szabadna bekövetkeznie.
            self._done = True
            self._load.input_off()
            # R1 B8a, F-21: ugyanaz az OFF-nyugta horgony, mint a
            # step_chunk() terminális ágaiban. Csak INSTRUMENT_TIMED módban:
            # a HARDWARE_PULSE ág (D-B8-1) változatlan, ott a t0 csak a
            # tüzeléskor íródik.
            if self._trigger_mode == TRIGGER_MODE_INSTRUMENT_TIMED:
                self._load_off_real_s = self._time_source() - self._t0

        # SUP01-PULSE-008: ha BÁRMELY chunk-nál a mért áram nem volt elérhető,
        # a teljes pontot degraded minősítéssel, setpoint-tal kell jelölni —
        # nem csak az érintett mintát.
        if self._pulse_current_degraded:
            currents_for_eval: Optional[float] = self._pulse_current_A
            mean_current_A: float | None = self._pulse_current_A
        else:
            currents = [i for _, i in self._current_readings]
            mean_current_A = sum(currents) / len(currents) if currents else self._pulse_current_A
            currents_for_eval = None  # per-pont nearest-current lekérdezés

        # R2-B4 (R2-metrology-01, DFAM-033): EGYETLEN numerikus forrás a hívó
        # controller chunkonkénti könyvelésével -- l. measured_energy_so_far().
        measured = self.measured_energy_so_far()
        removed_Ah = measured.removed_Ah
        removed_Wh = measured.removed_Wh
        prev_t = measured.last_reading_t_s
        # R1 B8a, F-21 (NEW_FAMILY evaluation_window_conflated_with_physical_
        # integration_window): az R(t)-értékelési ablak a cutoffig elfogadott
        # mintáknál véget ér (SUP01-PULSE-010, változatlan), a Load viszont
        # az OFF-nyugtáig (_load_off_real_s) terhelt -- az energiaelszámolás
        # eddig az utolsó áramleolvasás időpontjánál megállt, így a
        # [utolsó áramleolvasás, OFF-nyugta] szakasz kimaradt. Ez cutoff
        # esetén a cutoff-minta utáni teljes szakasz (az eldobott minták
        # ideje, a reset_to_single_sample() round-trip, a measure_current()
        # és az input_off() latenciája), cutoff nélkül pedig az utolsó chunk
        # fetch-visszatérése (F-22 horgony) utáni reset_to_single_sample()
        # round-trip, a measure_current() és az input_off() latenciája.
        # MÓDSZER: a meglévő téglalap-integrálással összhangban az UTOLSÓ
        # áramleolvasás értékével (degraded esetben az ott rögzített
        # setpoint-értékkel, l. pulse_current_degraded) terjesztjük ki a
        # szakaszra; a Wh-hoz az ahhoz legközelebbi feszültségminta
        # (cutoff esetén a cutoff-minta) feszültségét használjuk. Ez a farok
        # BECSLÉS: a tényleges feszültség/áram a szakaszban nem mért, a
        # becslési hiba nagysága és előjele NEM bizonyított (HIL_PENDING).
        # R(t)-hez új minta NEM jön létre. A recovery-ablak (OFF után)
        # sosem kerül ide.
        if (
            self._load_off_real_s is not None
            and self._current_readings
            and self._load_off_real_s > prev_t
        ):
            t_last, i_last = self._current_readings[-1]
            dt = self._load_off_real_s - prev_t
            removed_Ah += i_last * dt / 3600.0
            nearest = self._nearest_sample(t_last)
            v_for_wh = nearest.voltage_V if nearest is not None else self._v_baseline_V
            removed_Wh += i_last * v_for_wh * dt / 3600.0

        if self._owned_window and not self._current_readings:
            # Nincs mért áram: az üres prefix nem bizonyított nulla fogyasztás.
            mean_current_A = None
            self._current_trace_incomplete = True
        results: dict[float, PulseResultPoint] = {}
        has_invalid_resistance = False
        for target in self._target_times_s:
            nearest = self._nearest_sample(target)
            # A cél csak akkor "elért", ha a legközelebbi minta VALÓS ideje
            # legalább a toleranciasávon belül van a célidőponthoz — ha a
            # pulse korábban megszakadt (cutoff/abort), a legutolsó minta
            # jóval a cél előtt marad, ezért NEM "elért" (l. review-lelet:
            # ez a valós-idő-alapú megfelelője a régi index<len(samples)
            # ellenőrzésnek).
            reached = (
                nearest is not None
                and nearest.time_from_pulse_start_s >= target - self._time_tolerance_s
            )
            if reached and nearest is not None:
                current_A = (
                    None if self._owned_window and not self._current_readings
                    else currents_for_eval
                    if currents_for_eval is not None
                    else self._nearest_current(nearest.time_from_pulse_start_s)
                )
                delta_v = self._v_baseline_V - nearest.voltage_V
                delta_i = current_A - self._baseline_current_A if current_A is not None else None
                # Review-kör 4 (2026-09-09), Metrológia M4-03 (P2, DFAM-001
                # + DFAM-018): korábban csak delta_i előjelére volt kapu --
                # egy fizikailag lehetetlen delta_v<=0 (kisütő impulzusnál a
                # terhelés alatti feszültség NEM lehet magasabb/egyenlő a
                # baseline-nál -- csak mérési hibából jöhet: meglazult
                # Kelvin-érzékelő, DMM range-váltás az impulzus alatt,
                # baseline-medián ablak szennyeződése) negatív/nulla
                # resistance_ohm-ot adott quality-jelzés nélkül (rules/20-
                # metrology.md "Egyetlen sample-ből ne jelents valódi 0
                # deltát"). Az IEC testvér (iec_dc_resistance_controller.py)
                # már véd erre explicit INVALID_RESISTANCE-szel.
                resistance_ohm = (
                    (delta_v / delta_i) if (delta_i is not None and delta_i > 0 and delta_v > 0) else None
                )
                if resistance_ohm is None:
                    has_invalid_resistance = True
                time_error = nearest.time_from_pulse_start_s - target
                results[target] = PulseResultPoint(
                    target_time_s=target,
                    actual_time_s=nearest.time_from_pulse_start_s,
                    voltage_V=nearest.voltage_V,
                    current_A=current_A,
                    delta_voltage_V=delta_v,
                    delta_current_A=delta_i,
                    resistance_ohm=resistance_ohm,
                    resistance_mOhm=(resistance_ohm * 1000.0) if resistance_ohm is not None else None,
                    time_error_s=time_error,
                    within_tolerance=abs(time_error) <= self._time_tolerance_s,
                    reached=True,
                )
            else:
                results[target] = PulseResultPoint(
                    target_time_s=target,
                    actual_time_s=None,
                    voltage_V=None,
                    current_A=None,
                    delta_voltage_V=None,
                    delta_current_A=None,
                    resistance_ohm=None,
                    resistance_mOhm=None,
                    time_error_s=None,
                    within_tolerance=False,
                    reached=False,
                )

        if self._owned_window and (
            len(self._samples) < self._total_samples_target
            or any(not point.within_tolerance for point in results.values())
        ):
            self._voltage_trace_incomplete = True

        # R1 B8a, F-21 / B21b: legacy ON-visszatérés, own ON-küldés -> OFF-visszatérés,
        # becslési idő (nem fizikai kapcsolás), ha az OFF-
        # horgony megvan (INSTRUMENT_TIMED); egyébként (HARDWARE_PULSE,
        # D-B8-1) a régi kifejezés -- l. a PulseAcquisitionResult mező
        # kommentjét a módonként eltérő szemantikáról.
        if self._load_off_real_s is not None:
            pulse_duration_actual_s = self._load_off_real_s
        else:
            pulse_duration_actual_s = (
                self._samples[-1].time_from_pulse_start_s if self._samples else 0.0
            )

        # Review-kör 2 (2026-09-09), friss Szoftver/Architektúra F8b: a
        # korábbi if/else mindkét ága ugyanazt az értéket adta (ruff
        # RUF034) — ez azt sugallta, hogy a _trigger_mode-nak hatása van a
        # minőség-jelzőre, holott a döntés (l. lent) pont az, hogy NINCS.
        # Egyenes értékadásra egyszerűsítve, a komment megtartásával.
        sync_quality = SYNC_QUALITY_INSTRUMENT_TIMED_SOFTWARE_TRIGGERED
        # Megjegyzés: HARDWARE_PULSE módban a szinkronizáció alapja MÉG
        # mindig szoftveres trigger (FORC:TRIG egy Python-hívásra) — a
        # SYNC_QUALITY_HARDWARE_SYNCHRONIZED elnevezés a spec §11.2 szerint
        # KÜLÖN, fizikai közös trigger-vezetékezést jelent, amit ez a mód
        # NEM valósít meg, ezért a minőség-jelző itt is
        # INSTRUMENT_TIMED_SOFTWARE_TRIGGERED marad (nem hamis "jobb"
        # minősítés a hardveres pulse-generálásért).

        return PulseAcquisitionResult(
            started=True,
            skipped_low_voltage=False,
            cutoff_during_pulse=self._cutoff,
            pulse_current_degraded=self._pulse_current_degraded,
            v_baseline_V=self._v_baseline_V,
            pulse_current_set_A=self._pulse_current_A,
            pulse_current_measured_mean_A=mean_current_A,
            pulse_duration_set_s=self._duration_s,
            pulse_duration_actual_s=pulse_duration_actual_s,
            synchronization_quality=sync_quality,
            method_origin="PROJECT_ENGINEERING_METHOD",
            removed_Ah=removed_Ah,
            removed_Wh=removed_Wh,
            has_invalid_resistance=has_invalid_resistance,
            samples=list(self._samples),
            results=results,
            trigger_mode=self._trigger_mode,
            measured_baseline_current_A=self._measured_baseline_current_A,
            current_trace_incomplete=self._current_trace_incomplete,
            voltage_trace_incomplete=self._voltage_trace_incomplete,
            recovery_samples=list(self._recovery_samples),
            baseline_current_degraded=self._baseline_current_degraded,
            timing_infeasible=self._timing_infeasible,
            timing_infeasible_reason=self._timing_infeasible_reason,
            nplc_restore_failed=self._nplc_restore_failed,
            load_on_command_monotonic_s=self._on_command_at,
            load_on_return_monotonic_s=self._on_return_at,
            load_off_command_monotonic_s=self._off_command_at,
            load_off_return_monotonic_s=self._off_return_at,
            load_command_duration_s=(
                self._off_command_at - self._on_command_at
                if self._off_command_at is not None and self._on_command_at is not None else None
            ),
            load_command_time_error_s=(
                self._off_command_at - self._on_command_at - self._duration_s
                if self._off_command_at is not None and self._on_command_at is not None else None
            ),
        )

    # ------------------------------------------------------------------ #
    def measured_energy_so_far(self) -> MeasuredPulseEnergy:
        """R2-B4 (R2-metrology-01, DFAM-033): a finalize() téglalap-
        integrálja az eddig RÖGZÍTETT áramleolvasásokig, a terminális OFF-
        farok NÉLKÜL. Tiszta számítás: műszer-I/O és óralekérdezés nincs. A
        finalize() is ezt használja, így a hívó chunkonkénti könyvelése és
        a lezárási eredmény ugyanabból a numerikus forrásból származik."""
        removed_Ah = 0.0
        removed_Wh = 0.0
        prev_t = 0.0
        for t, i in self._current_readings:
            dt = max(0.0, t - prev_t)
            removed_Ah += i * dt / 3600.0
            # Teljes kódbázis review 16. kör (2026-09-04), Metrológia/
            # Adatintegritás P2 lelet: a legközelebbi feszültségminta
            # felhasználásával (a resistance_ohm kiértékelés is ezt a
            # mintát használja, l. lent _nearest_sample()) — a HARDWARE_
            # PULSE módban az áram/feszültség trace-ek eltérő rácson
            # mintavételezettek, tehát nincs garantált pontos t-egyezés.
            nearest = self._nearest_sample(t)
            v_for_wh = nearest.voltage_V if nearest is not None else self._v_baseline_V
            removed_Wh += i * v_for_wh * dt / 3600.0
            prev_t = t
        return MeasuredPulseEnergy(
            removed_Ah=removed_Ah,
            removed_Wh=removed_Wh,
            reading_count=len(self._current_readings),
            last_reading_t_s=prev_t,
            current_degraded=self._pulse_current_degraded,
        )

    def _nearest_current(self, t: float) -> float:
        if not self._current_readings:
            return self._pulse_current_A
        best = min(self._current_readings, key=lambda pair: abs(pair[0] - t))
        return best[1]

    def _nearest_sample(self, t: float) -> Optional[PulseSamplePoint]:
        if not self._samples:
            return None
        return min(self._samples, key=lambda s: abs(s.time_from_pulse_start_s - t))

    def _turn_load_off_or_chain(self, context: str, original_exc: Exception) -> None:
        """SUP01-SAFE-002: acquisition-hiba esetén is Load OFF kísérlet,
        MIELŐTT az eredeti hibát a hívóhoz továbbengednénk. Ha maga a Load
        OFF is hibázik, ezt egy külön, láncolt kivétellel jelezzük (a régi
        kód elkülönült IMPULSE_LOAD_OFF_FAILED diagnosztikájának megfelelően)
        — l. review-lelet."""
        try:
            self.cancel_acquisition()
            if self._owned_window:
                self._owned_off()
            else:
                self._load.input_off()
        except Exception as off_exc:
            raise RuntimeError(
                f"IMPULSE_LOAD_OFF_FAILED after {context}: {off_exc}"
            ) from original_exc
