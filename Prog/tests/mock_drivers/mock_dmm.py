"""
MockDMM — determinisztikus szimulált DMM teszteléshez.
Kezeli a DMM1 (feszültség) és DMM2 (hőmérséklet) szerepeket egyaránt.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Optional
from Prog.src.exceptions import InstrumentTimeoutError, InstrumentInvalidReading
from Prog.drivers.device_dmm import DMMMeasurementBusy


# R1 B5 (DFAM-006, M1): a valos Keysight 34465A-n MERT SAMP:TIM? MIN
# (CLAIM_LEDGER INSTR-DMM-SAMPTIM-001, MY60016569, 2026-09-12 muszer-only
# HIL: NPLC=10, 50 Hz, gyari autozero, DCV range 100 V) -- kulcs:
# (NPLC, halozati frekvencia Hz). A korabbi apertura-keplet (NPLC/50 +
# 20 us = 0,20002 s) a koddal AZONOS, ellenorizetlen feltetelezes volt
# (rules/60-testing.md); a kepletet csak a NEM HIL-mert konfiguraciok
# hasznaljak, "nem HIL-mert modell"-kent.
HIL_34465A_SAMPLE_TIMER_MIN_S: dict[tuple[float, float], float] = {
    (10.0, 50.0): 0.404496,
}
# A SAMP:TIM erteke *RST / SYST:PRES utan (34460-70-Manual.txt 9090-9091.
# sor, nyomtatott 311. o.); a HIL 2. futasaban a csendes elutasitas utan is
# ez maradt ervenyben. A MIN a kezikonyv szerint autozero/autorange/range-
# fuggo is -- a tabla csak a fenti, HIL-en mert beallitasra ervenyes.
HIL_34465A_DEFAULT_SAMPLE_TIMER_S = 1.0


@dataclass
class MockDMM:
    # Programozható állapot
    voltage_V: float = 12.5
    autozero_on: bool = True
    temperature_C: float = 22.0
    dmm_valid: bool = True
    simulate_timeout: bool = False
    simulate_nan: bool = False
    simulate_overload: bool = False
    simulate_temp_failure: bool = False

    # Feszültség szimulációhoz: lineáris emelkedés töltés közben
    voltage_slope_V_per_sample: float = 0.0

    # SUP01-PULSE-003/SRC-009: időzített mintavétel szimulációja
    # (PulseMeasurementService tesztekhez). Ha timed_voltage_fn adott,
    # minden mintát time_from_pulse_start_s alapján generál, egyébként
    # a sima voltage_V/voltage_slope_V_per_sample párost folytatja.
    timed_voltage_fn: Optional[Callable[[float], float]] = None
    simulate_timed_acquisition_failure: bool = False

    # ── 2026-09-12, FAST_TRANSIENT NPLC-javítás ──────────────────────────
    # A valós műszeren a SAMPle:TIMer? MIN válasza az AKTUÁLIS integrációs
    # időtől függ (34460-70-Manual.txt 9043-9047. sor): NPLC=10 mellett 50
    # Hz-en ~0,2 s, NPLC=0,02 mellett ~0,4 ms apertúra. A mock ezt a
    # FÜGGŐSÉGET modellezi, nem egy fix konstanst — enélkül a képesség-
    # ellenőrzés tesztje tautologikus lenne (l. rules/60-testing.md: "A
    # fixture ... ne ugyanabból az ellenőrizetlen feltételezésből
    # származzon, mint a kód").
    nplc: float = 10.0
    line_frequency_Hz: float = 50.0
    # Az apertúrán felüli, konstans mintavételi rezsi a műszerben (a
    # kézikönyv szerint a MIN mindig NAGYOBB, mint a sampling time).
    sample_timer_overhead_s: float = 20e-6
    simulate_nplc_set_failure: bool = False
    simulate_nplc_get_failure: bool = False
    simulate_sample_timer_query_failure: bool = False
    # Ha nem None, ezt adja vissza a SAMP:TIM? MIN nyers válaszaként az
    # NPLC-alapú számítás HELYETT (célzott, nem-numerikus/érvénytelen
    # válaszok teszteléséhez).
    sample_timer_min_raw_override: Optional[str] = None
    # 2026-09-12, musszer-only HIL-javitas: get_sample_timer_s() ALAPÉRTELMEZÉSBEN
    # a legutóbb configure_timed_acquisition()-nel BEÁLLÍTOTT értéket adja vissza
    # (a "jól viselkedő" műszert modellezve — l. get_sample_timer_s() docstringje).
    # Ha nem None, EHELYETT ezt adja vissza — így egy teszt szimulálhatja a valós
    # HIL-en talált csendes-elutasítási esetet (a műszer más SAMP:TIM-et alkalmaz,
    # mint amit kértünk, a hibasor mégis üres marad).
    actual_sample_timer_override_s: Optional[float] = None
    simulate_sample_timer_get_failure: bool = False
    # R1 B5 (DFAM-006, M2), OPT-IN: a 2026-09-12-i muszer-only HIL-sweep
    # (1)-es reszmegfigyelesenek modellje (CLAIM_LEDGER INSTR-DMM-SAMPTIM-001):
    # MIN ALATTI keresre a SAMP:TIM az elozo erteken marad, a hibasor URES.
    # R2-B9: LEHETSÉGES mérési műtermék, nem igazolt műszer-viselkedés -- a
    # sweep a 9efa61d elotti, minden _write() elott Device Clear-t kuldo
    # driverrel keszult (confounded), a nyers sweep-naplo nem talalhato, a
    # kezikonyv szerint pedig az abszolut minimum alatti ertek hibat ad
    # (SCF-020, HIL_PENDING).
    # True eseten configure_timed_acquisition() csak MIN <= kert koz
    # mellett irja a tenyleges SAMP:TIM-et, get_sample_timer_s() ezt adja.
    # NEM modellezett: a -221 al-tartomany (pl. 0,3 s -> MIN + -221), a
    # 0,45-0,99 s -> 0,41 s "kvantalas" (szinten nem igazolt, SCF-020), es
    # a fetch_timed_acquisition() idoalapja (tovabbra is a kert koz). Alapbol
    # KI: bekapcsolva ~83 meglevo teszt bukna (D-B5-1) -- a flag nelkuli mod
    # a nyilvantartott DFAM-006 maradek (tautologikus readback).
    simulate_hil_sample_timer_silent_rejection: bool = False
    # Kör 21 F16 (HW21-05(b)): ha nem None, `fetch_timed_acquisition()` ezt
    # a nyers stringet adja vissza a számított tiszta CSV HELYETT -- egy
    # teszt így szimulálhat hibás/nem-numerikus tokent vagy egy "9.9E37"
    # overload-sentinelt a mintasorban, 1:1 a `sample_timer_min_raw_
    # override` már bevált mintáját követve.
    fetch_timed_acquisition_raw_override: Optional[str] = None

    # Review-kör 5, Hardver/Műszervezérlés HW5-04 (P3): a valós
    # device_dmm.py::Keysight34465ADMM.timeout_ms mezőjének tükre --
    # set_io_timeout_ms() ezt frissíti, hogy a hívó (pulse_measurement_
    # service.py) tesztjei ellenőrizhessék az ideiglenes felülírás/
    # visszaállítás párt.
    timeout_ms: int = 10000
    # Review-kör 5, Hardver/Műszervezérlés HW5-05 (P3): a VALÓS *IDN?
    # válasz tartalmazza a műszer egyedi sorozatszámát -- két KÜLÖNBÖZŐ
    # fizikai eszköz sosem adhat vissza azonosat. A korábbi, osztályonként
    # FIX idn()-string emiatt nem tudta megkülönböztetni "két szándékosan
    # KÜLÖNBÖZŐ szerepre létrehozott mock" és "ugyanaz a fizikai eszköz
    # véletlenül két szerepre kötve" eseteket -- ha egy teszt explicit
    # ugyanazt a (nem-üres) `serial`-t adja meg mindkét példánynak, azzal
    # SZÁNDÉKOSAN szimulálja az utóbbit; alapértelmezésben (üres string)
    # minden példány EGYEDI (id(self)-ből származtatott) azonosítót kap,
    # ahogy a valóságban is minden fizikai műszernek sajátja van.
    serial: str = ""
    calls: list[str] = field(default_factory=list)
    pending_errors: list[str] = field(default_factory=list)
    _sample_count: int = field(default=0, init=False, repr=False)
    _timed_sample_count: int = field(default=0, init=False, repr=False)
    _timed_interval_s: float = field(default=0.0, init=False, repr=False)
    _timed_samples_generated: int = field(default=0, init=False, repr=False)
    # WP-AK-04 review-lelet: a valós Keysight 34465A-n a SAMPle:COUNt/SOURce
    # TARTÓS (sticky) — configure_timed_acquisition() után minden sima READ?
    # a chunk méretének megfelelő CSV-t adna vissza, amíg reset_to_single_sample()
    # (SAMP:COUN 1 + SAMP:SOUR IMM) vissza nem állítja. Ezt modellezzük, hogy egy
    # elmaradt reset regresszióként buktassa a tesztet (nem csendben átment volna).
    _timed_mode_active: bool = field(default=False, init=False, repr=False)
    # M2: a muszerben TENYLEGESEN ervenyes SAMP:TIM (csak a fenti opt-in
    # flaggel hasznalt); reset_to_single_sample() NEM torli -- a valos
    # muszeren sem.
    _hil_actual_sample_timer_s: float = field(
        default=HIL_34465A_DEFAULT_SAMPLE_TIMER_S, init=False, repr=False
    )

    # ── 2026-09-12, INSTR-DMM-ABORT-001 ─────────────────────────────────
    # A valós Keysight 34465A trigger-ÁLLAPOTGÉPÉNEK modellje (eddig a mock
    # csak a SAMPle-konfigurációt modellezte, a trigger-állapotot nem):
    # `arm_timed_acquisition()` (INIT) "wait-for-trigger"/mérés-folyamatban
    # állapotba viszi a műszert (34460-70-Manual.txt 5500-5503. sor), amiből
    # csak a `fetch_timed_acquisition()` (FETC?) BEFEJEZŐDÉSE vagy egy
    # explicit `ABORt` hoz ki (uo. 5527. sor: "Use ABORt to return to idle").
    _armed: bool = field(default=False, init=False, repr=False)
    # A 2026-09-12-i élő HIL-en MEGFIGYELT TÜNET (3. futás első sima READ?-je
    # két értéket adott vissza, SAMP:COUN még 2 volt) egyik LEHETSÉGES,
    # PLAUZIBILIS modellje: amíg egy függőben lévő (soha be nem fejezett)
    # INIT él, a SAMPle/CONFigure konfigurációs írások NEM érvényesülnek
    # megbízhatóan -- a hibasor mégis ÜRES marad. FONTOS PONTOSÍTÁS
    # (independent verifier, 2026-09-12): ezt a KONKRÉT mechanizmust a
    # kézikönyv SEHOL nem dokumentálja expliciten -- l. CLAIM_LEDGER.yaml
    # INSTR-DMM-CONFIG-ATOMIC-001 ("ezt a mechanizmust NEM állítjuk"). A mock
    # tehát egy ÖNMAGÁBAN KOHERENS, a megfigyelt tünetet reprodukáló
    # szimulációt ad (hasznos regressziós teszthez), NEM egy forrásból
    # igazolt driver-viselkedést -- ne kezeld bizonyított tényként.
    simulate_config_ignored_while_armed: bool = True
    # A JAVÍTOTT driver modellje: `reset_to_single_sample()` `ABOR`-ral
    # kezd (device_dmm.py). False-ra állítva a teszt a JAVÍTÁS ELŐTTI
    # drivert emulálja (csak SAMP:COUN 1 + SAMP:SOUR IMM, ABORt nélkül) --
    # a diszkrimináló regressziós teszt ezt használja.
    reset_aborts_pending_init: bool = True

    _measurement_owner: object | None = field(default=None, init=False, repr=False)

    @property
    def measurement_owned(self) -> bool:
        """Nem blokkoló belépési jelzés; az I/O-kapu továbbra is a zár alatt él."""
        return self._measurement_owner is not None

    def acquire_measurement(self, owner: object) -> None:
        if owner is None:
            raise ValueError("A tulajdonos nem lehet None")
        if self._measurement_owner is not None:
            raise DMMMeasurementBusy("MockDMM: foglalt")
        self._measurement_owner = owner

    def release_measurement(self, owner: object) -> None:
        if owner is None or self._measurement_owner is not owner:
            raise DMMMeasurementBusy("MockDMM: eltérő tulajdonos")
        self._measurement_owner = None

    def __post_init__(self) -> None:
        if not self.serial:
            self.serial = f"SN{id(self):012X}"

    def connect(self, resource: str) -> None:
        self.calls.append(f"connect({resource!r})")

    def disconnect(self) -> None:
        if self._measurement_owner is not None:
            raise DMMMeasurementBusy("MockDMM: foglalt")
        self.calls.append("disconnect()")

    def idn(self) -> str:
        self.calls.append("idn()")
        # *IDN? formátum: <gyártó>,<modell>,<sorozatszám>,<firmware> -- a
        # 3. mező (korábban fix "MOCK") most a HW5-05 egyedi sorozatszámot
        # hordozza, a valós SCPI *IDN? konvenciónak megfelelően.
        return f"KEYSIGHT TECHNOLOGIES,34465A,{self.serial},A.02.00"

    def reset(self) -> None:
        self.calls.append("reset()")

    def clear_status(self) -> None:
        self.calls.append("clear_status()")

    def check_error(self, *, owner: object | None = None) -> list[str]:
        if self._measurement_owner is not owner:
            raise DMMMeasurementBusy("MockDMM: foglalt")
        self.calls.append("check_error()")
        if self.pending_errors:
            return [self.pending_errors.pop(0)]
        return []

    def is_connected(self) -> bool:
        return True

    def safe_off(self) -> bool:
        self.calls.append("safe_off()")
        return True

    def abort_measurement(self) -> None:
        """ABORt — l. device_dmm.py::abort_measurement(). A trigger-rendszert
        "idle"-be viszi, függetlenül attól, volt-e függőben lévő mérés."""
        self.calls.append("abort_measurement()")
        self._armed = False

    def _config_blocked(self) -> bool:
        """True, ha egy függőben lévő INIT miatt a konfigurációs írások
        (SAMPle/CONFigure) NEM érvényesülnek — l. a mezők docstringjét."""
        return self._armed and self.simulate_config_ignored_while_armed

    def configure_dcv(self, range_V: float, nplc: float) -> None:
        self.calls.append(f"configure_dcv(range_V={range_V}, nplc={nplc})")
        # A JAVÍTOTT device_dmm.py::configure_dcv() `ABOR`-ral kezd, ezért
        # egy előző processzből maradt armed állapotot is felold.
        self._armed = False
        if self._config_blocked():  # pragma: no cover - defenzív
            return
        self._timed_mode_active = False
        self.nplc = nplc

    def set_nplc(self, nplc: float) -> None:
        if self._measurement_owner is not None:
            raise DMMMeasurementBusy("MockDMM: foglalt")
        self.calls.append(f"set_nplc({nplc})")
        if self.simulate_nplc_set_failure:
            raise InstrumentTimeoutError("MockDMM: simulated set_nplc failure")
        self.nplc = nplc

    def enable_autozero(self) -> None:
        """Q-6: a sessionben megmaradó AZ ON állapot modellje."""
        self.calls.append("enable_autozero()")
        self.autozero_on = True

    def get_nplc(self) -> float:
        """2026-09-12, FAST_TRANSIENT NPLC-javítás — l. device_dmm.py::
        get_nplc() (VOLT:DC:NPLC?). A mock a set_nplc()-vel ténylegesen
        együtt mozgó állapotot modellez, hogy egy elmaradt visszaállítás
        regresszióként bukjon (rules/40-instruments.md: "Mock modellezze a
        sessionön át megmaradó hardverállapotot is")."""
        self.calls.append("get_nplc()")
        if self.simulate_nplc_get_failure:
            raise InstrumentTimeoutError("MockDMM: simulated get_nplc failure")
        return self.nplc

    def autorange_off(self) -> None:
        self.calls.append("autorange_off()")

    def configure_temp_4wire_pt100(self, nplc: float = 10.0) -> None:
        self.calls.append(f"configure_temp_4wire_pt100(nplc={nplc})")

    def read_voltage(self, *, owner: object | None = None) -> float:
        """[N8] NaN / overload / timeout szimulálható."""
        if self._measurement_owner is not owner:
            raise DMMMeasurementBusy("MockDMM: eltérő tulajdonos")
        self.calls.append("read_voltage()")
        if self._timed_mode_active:
            # WP-AK-04 review-lelet modellje: a valós műszeren ez multi-értékű
            # CSV-t adna (a beállított SAMP:COUN szerint), nem egyetlen floatot.
            raise InstrumentInvalidReading(
                "MockDMM: read_voltage() timed-acquisition (SAMP:SOUR TIM) módban "
                "hívva — reset_to_single_sample() hiányzik"
            )
        if not self.dmm_valid:
            raise InstrumentInvalidReading("MockDMM: dmm_valid=False")
        if self.simulate_timeout:
            raise InstrumentTimeoutError("MockDMM: simulated timeout")
        if self.simulate_nan:
            raise InstrumentInvalidReading("MockDMM: NaN reading")
        if self.simulate_overload:
            raise InstrumentInvalidReading("MockDMM: overload (9.9E37)")

        v = self.voltage_V + self._sample_count * self.voltage_slope_V_per_sample
        self._sample_count += 1
        return v

    def read_temperature(self) -> float:
        self.calls.append("read_temperature()")
        if self.simulate_timeout or self.simulate_temp_failure:
            raise InstrumentTimeoutError("MockDMM: simulated temp timeout")
        return self.temperature_C

    # ------------------------------------------------------------------ #
    # SUP01-PULSE-003/SRC-009: időzített mintavétel (PulseMeasurementService)
    # ------------------------------------------------------------------ #

    def configure_timed_acquisition(
        self, sample_count: int, interval_s: float, *, owner: object | None = None,
    ) -> None:
        if self._measurement_owner is not owner:
            raise DMMMeasurementBusy("MockDMM: eltérő tulajdonos")
        self.calls.append(
            f"configure_timed_acquisition(sample_count={sample_count}, interval_s={interval_s})"
        )
        # A JAVÍTOTT device_dmm.py::configure_timed_acquisition() is
        # `ABOR`-ral kezd (INSTR-DMM-ABORT-001).
        self._armed = False
        self._timed_sample_count = sample_count
        self._timed_interval_s = interval_s
        self._timed_mode_active = True
        if (
            self.simulate_hil_sample_timer_silent_rejection
            and interval_s >= self._sample_timer_min_s()
        ):
            self._hil_actual_sample_timer_s = interval_s

    def arm_timed_acquisition(self, *, owner: object | None = None) -> None:
        if self._measurement_owner is not owner:
            raise DMMMeasurementBusy("MockDMM: eltérő tulajdonos")
        self.calls.append("arm_timed_acquisition()")
        self._armed = True

    def fetch_timed_acquisition(self, *, owner: object | None = None) -> str:
        if self._measurement_owner is not owner:
            raise DMMMeasurementBusy("MockDMM: eltérő tulajdonos")
        self.calls.append("fetch_timed_acquisition()")
        if self.simulate_timed_acquisition_failure:
            # A mérés NEM fejeződött be: a műszer armed marad (csak egy
            # ABORt vagy egy sikeres FETC? hozza ki ebből az állapotból).
            raise InstrumentTimeoutError("MockDMM: simulated timed acquisition failure")
        self._armed = False
        if self.fetch_timed_acquisition_raw_override is not None:
            return self.fetch_timed_acquisition_raw_override
        values: list[float] = []
        for _ in range(self._timed_sample_count):
            self._timed_samples_generated += 1
            t = self._timed_samples_generated * self._timed_interval_s
            if self.timed_voltage_fn is not None:
                v = self.timed_voltage_fn(t)
            else:
                v = self.voltage_V
            values.append(v)
        return ",".join(f"{v:.6f}" for v in values)

    def _sample_timer_min_s(self) -> float:
        """R1 B5 (DFAM-006, M1): a SAMP:TIM? MIN az AKTUÁLIS integrációs
        időtől függ. HIL-en mért konfigurációra a mért érték (l. HIL_34465A_
        SAMPLE_TIMER_MIN_S); egyébként a régi, NEM HIL-mért apertúra-modell
        (apertúra = NPLC / hálózati frekvencia, 34460-70-Manual.txt
        14028-14052. sor, + konstans rezsi)."""
        measured = HIL_34465A_SAMPLE_TIMER_MIN_S.get(
            (float(self.nplc), float(self.line_frequency_Hz))
        )
        if measured is not None:
            return measured
        return self.nplc / self.line_frequency_Hz + self.sample_timer_overhead_s

    def query_sample_timer_capability(self) -> str:
        """SAMPle:TIMer? MIN — 2026-09-12, FAST_TRANSIENT NPLC-javítás: az
        AKTUÁLIS integrációs időtől függő minimum, nem fix konstans; R1 B5
        óta HIL-mért konfigurációra a mért érték (l. _sample_timer_min_s())."""
        self.calls.append("query_sample_timer_capability()")
        if self.simulate_sample_timer_query_failure:
            raise InstrumentTimeoutError("MockDMM: simulated SAMP:TIM? MIN failure")
        if self.sample_timer_min_raw_override is not None:
            return self.sample_timer_min_raw_override
        return f"{self._sample_timer_min_s():.8E}"

    def get_sample_timer_s(self, *, owner: object | None = None) -> float:
        """2026-09-12, musszer-only HIL-javitas — l. device_dmm.py::
        get_sample_timer_s(). Alapértelmezésben a legutóbb configure_
        timed_acquisition()-nel beállított értéket adja vissza (bit-
        pontosan azonos a kérttel — ez tartja meg a MEGLÉVŐ tesztek
        viselkedését változatlanul); `actual_sample_timer_override_s`-szel
        egy teszt szimulálhatja, hogy a műszer más értéket alkalmazott;
        R1 B5 (M2): `simulate_hil_sample_timer_silent_rejection` mellett a
        műszerben ténylegesen érvényes érték (csendes elutasítás modellje)."""
        if self._measurement_owner is not owner:
            raise DMMMeasurementBusy("MockDMM: eltérő tulajdonos")
        self.calls.append("get_sample_timer_s()")
        if self.simulate_sample_timer_get_failure:
            raise InstrumentTimeoutError("MockDMM: simulated get_sample_timer_s failure")
        if self.actual_sample_timer_override_s is not None:
            return self.actual_sample_timer_override_s
        if self.simulate_hil_sample_timer_silent_rejection:
            return self._hil_actual_sample_timer_s
        return self._timed_interval_s

    def reset_to_single_sample(self, *, owner: object | None = None) -> None:
        """2026-09-12, INSTR-DMM-ABORT-001: a valós driverrel egyezően
        `ABOR`-ral kezd (`reset_aborts_pending_init=True`, a JAVÍTOTT
        viselkedés). `reset_aborts_pending_init=False` a javítás ELŐTTI
        drivert emulálja: a `SAMP:COUN 1`/`SAMP:SOUR IMM` írás önmagában NEM
        zárja le a függőben lévő mérést, és amíg az él, a konfigurációs
        írások sem érvényesülnek -- ezért a sticky timed-mód megmarad."""
        if self._measurement_owner is not owner:
            raise DMMMeasurementBusy("MockDMM: eltérő tulajdonos")
        self.calls.append("reset_to_single_sample()")
        if self.reset_aborts_pending_init:
            self._armed = False
        if self._config_blocked():
            return
        self._timed_mode_active = False

    def set_io_timeout_ms(self, timeout_ms: int) -> None:
        """Review-kör 5, Hardver/Műszervezérlés HW5-04 (P3) -- l.
        device_dmm.py::Keysight34465ADMM.set_io_timeout_ms() docstringje."""
        self.calls.append(f"set_io_timeout_ms({timeout_ms})")
        self.timeout_ms = timeout_ms

    def set_dmm_invalid(self) -> None:
        """Szimulál egy DMM_FEEDBACK_LOST eseményt."""
        self.dmm_valid = False

    def set_dmm_valid(self) -> None:
        self.dmm_valid = True

    def called(self, method: str) -> bool:
        return any(method in c for c in self.calls)

    def reset_calls(self) -> None:
        self.calls.clear()
        self._sample_count = 0
