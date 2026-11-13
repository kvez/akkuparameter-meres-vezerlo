"""
RelaxController — relaxációs várakozási időszak.
Állapotok: INIT → RELAXING → RELAX_DONE. A minimumidő időalapja a runner dt_s.
SERVICE módban a Q-21 worker rekordjainak monoton középbélyege hajtja az OLS-t.
LEGACY_TICK az explicit kompatibilitási alapmód: service bekötése nélkül
a régi tickenkénti DMM-olvasás és az összegzett dt_s bélyeg változatlan.
A runner az önálló és a TLC/GoldenGG beágyazott relaxot is SERVICE módba köti.
LEGACY_TICK csak service nélküli közvetlen használatban marad, explicit
kompatibilitásként; SERVICE-hiba nem válthat vissza rá.
A dV/dt nem zárja le a relaxot; Q-18 B és a fizikai HIL_PENDING megmarad.
Későbbi P3 batch: WORKER/RELEASE hibák számlálóbontása, heartbeat
külön eseménykódja/dupla CRITICAL-ja és reset/NPLC retry-policy egységesítése.
A MockDMM további owner-felülete, a GUI pending-disconnect életciklusa és
az összes mintabélyeg perzisztálása külön API-/séma-/életciklusmunka.
HIL_PENDING: read/clear idő, késői válasz, disconnect, monotonóra-felbontás
és az 1,5 s-os ütem DMM_T/BQ-poll mellett; a hostbélyeg nem fizikai mintabélyeg.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from itertools import count, pairwise
from math import floor, isfinite
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol

from Prog.src.integrator import Integrator


# Q-21: saját órájú DMM-szolgáltatás; a worker nem ér el PSU-t vagy Loadot.
# A hostoldali középbélyeg nem műszer-apertúrabélyeg. HIL_PENDING:
# a pyvisa/NI-VISA session szálak közötti használata külön igazolandó.
class RelaxDMM(Protocol):
    def reset_to_single_sample(self) -> None: ...
    def acquire_measurement(self, owner: object) -> None: ...
    def read_voltage(self, *, owner: object) -> float: ...
    def release_measurement(self, owner: object) -> None: ...


@dataclass(frozen=True)
class RelaxSample:
    epoch: int
    sequence: int
    read_start: float
    read_end: float
    sample_time: float
    voltage_V: float | None
    error: str | None = None
    missed_deadlines: int = 0


@dataclass(frozen=True)
class RelaxSampleBatch:
    records: tuple[RelaxSample, ...]
    overflow: bool
    released: bool


@dataclass(frozen=True)
class RelaxVoltageSnapshot:
    voltage_V: float | None
    read_start: float | None
    age_s: float | None
    quality: str


@dataclass(frozen=True)
class RelaxAcceptanceSnapshot:
    voltage: RelaxVoltageSnapshot
    dvdt_V_per_s: float
    dvdt_valid: bool
    reason: str
    sample_count: int
    coverage_s: float
    max_gap_s: float
    epoch: int | None
    released: bool


class RelaxSamplingService:
    PERIOD_S = 1.5
    _epochs = count(1)

    def __init__(
        self, dmm: RelaxDMM, *, capacity: int = 256,
        clock: Callable[[], float] = monotonic,
        wait: Callable[[Event, float], bool] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("A relaxpuffer kapacitása legyen pozitív")
        self._dmm = dmm
        self._clock = clock
        self._wait = wait if wait is not None else lambda event, delay: event.wait(delay)
        self._records: deque[RelaxSample] = deque(maxlen=capacity)
        self._buffer_lock = Lock()
        self._start_stop_lock = Lock()
        self._stop = Event()
        self._owner = object()
        self._thread: Thread | None = None
        self._started = False
        self._released = False
        self._overflow = False
        self._shutdown_error: str | None = None
        self.epoch = next(self._epochs)
        self._sequence = 0
        self._latest: RelaxSample | None = None
        self._heartbeat = self._clock()
        # Hostoldali liveness-határ a read retry-keretéből, nem teljes I/O-plafon.
        # HIL_PENDING (F4): a retry-nkénti clear ideje nincs benne; a valós
        # read/clear időt és heartbeat-határt külön kell igazolni. Limitet nem lazítunk.
        self.shutdown_timeout_s = (
            getattr(dmm, "timeout_ms", 10000) * getattr(dmm, "max_retries", 3) / 1000
            + self.PERIOD_S
        )

    def snapshot(self) -> RelaxVoltageSnapshot:
        with self._buffer_lock:
            record = self._latest
        return self.snapshot_for(record)

    def snapshot_for(self, record: RelaxSample | None) -> RelaxVoltageSnapshot:
        """A megadott rekord kora; a worker újabb rekordja nem helyettesítheti."""
        now = self._clock()
        if record is None:
            return RelaxVoltageSnapshot(None, None, None, "NO_DATA")
        age = max(0.0, now - record.read_start)
        quality = (
            "READ_ERROR" if record.error is not None or record.voltage_V is None
            else "STALE" if age > 4.0 else "CACHE_FRESH"
        )
        return RelaxVoltageSnapshot(record.voltage_V, record.read_start, age, quality)

    @property
    def heartbeat_age_s(self) -> float:
        now = self._clock()
        with self._buffer_lock:
            return max(0.0, now - self._heartbeat)

    def _beat(self) -> None:
        now = self._clock()
        with self._buffer_lock:
            self._heartbeat = now

    @property
    def shutdown_error(self) -> str | None:
        with self._buffer_lock:
            return self._shutdown_error

    def start(self, *, configuration_ready: bool) -> None:
        with self._start_stop_lock:
            if self._started or self._stop.is_set():
                raise RuntimeError("A relaxgyűjtő nem indítható újra")
            if not configuration_ready:
                raise RuntimeError("Hiányzik a relax-NPLC konfigurációs nyugtája")
            # Az előkészítés is életciklus: közben a stop nem adhat nyugtát.
            self._started = True
        acquired = False
        launched = False
        try:
            # Az atomikus reset még tulajdonos nélkül fut (B5a szerződés).
            self._dmm.reset_to_single_sample()
            self._dmm.acquire_measurement(self._owner)
            acquired = True
            with self._start_stop_lock:
                if not self._stop.is_set():
                    self._thread = Thread(target=self._run, name="relax-dmm-v", daemon=False)
                    self._thread.start()
                    launched = True
        finally:
            if not launched:
                if acquired:
                    self._release()
                else:
                    with self._buffer_lock:
                        self._released = True

    def stop(self) -> None:
        # A lock csak az olvasás kijelölését védi, I/O nincs alatta.
        with self._start_stop_lock:
            self._stop.set()
            if not self._started:
                with self._buffer_lock:
                    self._released = True

    def drain(self) -> RelaxSampleBatch:
        # A nyugta és a végső rekordok ugyanazon záron át láthatók.
        with self._buffer_lock:
            batch = RelaxSampleBatch(tuple(self._records), self._overflow, self._released)
            self._records.clear()
            self._overflow = False
            return batch

    def _publish(
        self, start: float, end: float, voltage: float | None,
        error: str | None = None, missed: int = 0,
    ) -> None:
        self._sequence += 1
        record = RelaxSample(
            self.epoch, self._sequence, start, end,
            start + (end - start) / 2, voltage, error, missed,
        )
        with self._buffer_lock:
            if len(self._records) == self._records.maxlen:
                self._overflow = True
            self._records.append(record)
            self._latest = record
            self._heartbeat = end

    def _run(self) -> None:
        try:
            deadline = self._clock()
            while not self._stop.is_set():
                self._beat()
                now = self._clock()
                if now < deadline:
                    if self._wait(self._stop, deadline - now):
                        break
                    continue
                # Egy teljes mintavételi helynyi ébredési késés már kihagyás.
                # Az alatta maradó jittert a tényleges bélyegek őrzik.
                if now >= deadline + self.PERIOD_S:
                    missed = floor((now - deadline) / self.PERIOD_S) + 1
                    self._publish(now, now, None, "DEADLINE_MISSED", missed)
                    deadline += missed * self.PERIOD_S
                    continue
                with self._start_stop_lock:
                    if self._stop.is_set():
                        break
                    # Ettől a ponttól in-flight; a stop ezt az I/O-t megváratja
                    # a nyugtával, de maga a stop() azonnal visszatér.
                start = self._clock()
                try:
                    voltage = self._dmm.read_voltage(owner=self._owner)
                    if not isfinite(voltage):
                        raise ValueError("Nem véges DMM-feszültség")
                except Exception as exc:  # noqa: BLE001 -- a rekord megőrzi a DMM-hibát
                    self._publish(start, self._clock(), None, f"DMM_READ_ERROR: {exc}")
                else:
                    self._publish(start, self._clock(), voltage)
                deadline += self.PERIOD_S
                now = self._clock()
                if now > deadline:
                    missed = floor((now - deadline) / self.PERIOD_S) + 1
                    self._publish(now, now, None, "DEADLINE_MISSED", missed)
                    deadline += missed * self.PERIOD_S
        except BaseException as exc:  # noqa: BLE001 -- végső hibarekord és felszabadítás
            now = self._clock()
            self._publish(now, now, None, f"WORKER_ERROR: {exc}")
        finally:
            self._release()

    def _release(self) -> None:
        try:
            self._dmm.release_measurement(self._owner)
        except BaseException as exc:  # noqa: BLE001 -- nincs hamis felszabadítási nyugta
            # Sikertelen felszabadításból sosem lesz nyugta.
            now = self._clock()
            self._publish(now, now, None, f"RELEASE_ERROR: {exc}")
            with self._buffer_lock:
                self._shutdown_error = f"RELEASE_ERROR: {exc}"
        else:
            with self._buffer_lock:
                self._released = True


class RelaxState(Enum):
    INIT = "INIT"
    RELAXING = "RELAXING"
    RELAX_DONE = "RELAX_DONE"


@dataclass
class RelaxConfig:
    min_relax_s: float = 7200.0     # 2h töltés után; 18000s (5h) kisütés után
    # Nem használt kompatibilitási mező; a RelaxController dV/dt alapján nem lép ki.
    dvdt_threshold_V_per_s: float = 0.0
    # R16-B4 / Q-18 B: a sample_period_s az OLS-ablak hossza, nem olvasási ütem.
    sample_period_s: float = 60.0
    dvdt_min_samples: int = 31
    dvdt_max_gap_s: float = 4.0


class RelaxController:
    def __init__(self, dmm_voltage, config: RelaxConfig, dmm_temperature=None, safety=None):
        self._dmm_v = dmm_voltage
        self._config = config
        self._state = RelaxState.INIT
        self._elapsed_s: float = 0.0
        self._last_voltage: float = 0.0
        self._dvdt: float = float("nan")
        self._dvdt_samples: deque[tuple[float, float]] = deque()
        self._dmm_fault_count: int = 0
        self._sampling_service: RelaxSamplingService | None = None
        self._sampling_release_poll: Callable[[], bool] | None = None
        self._dvdt_invalid_reason = "INSUFFICIENT_DATA"
        self._sampling_shutdown_complete = True
        self._sample_epoch: int | None = None
        self._sample_sequence = 0
        self._voltage_record: RelaxSample | None = None
        self._missed_deadline_count = 0
        self._deadline_missed_active = False
        # A szülő safety-kapuja minden nyers rekordot az összesítés előtt lát.
        self.on_sample: Callable[[RelaxSample], None] | None = None
        self.on_event: Callable[[dict], None] | None = None
        # Teljes kódbázis review 22. kör (2026-09-07), Szoftver/Architektúra
        # P2 lelet: a testvér controllerek (Charge/Discharge/GoldenGg/
        # TraditionalLearning/ChemId/OcvSoc) MIND figyelik a hőmérséklet-DMM
        # kiesését (check_temperature_dmm_fault()) és a profil hőmérséklet-
        # tartományát (check_test_temperature_range()) — a beágyazott relax-
        # alállapotokban is (a golden_gg/traditional_learning KÜLSŐ
        # advance()-je végzi ezt, l. golden_gg_controller.py:_read_temperature
        # _only()). Az ÖNÁLLÓ StepKind.RELAX úton (CHARACTERIZATION/
        # BQ_LEARNING_PHYSICAL/CHARGE_ONLY tervek 2-5 órás relax-lépései,
        # main_window.py::_make_relax_ctrl()) viszont NINCS külső controller,
        # ami ezt pótolná — a hőmérséklet-DMM/safety paraméter opcionális
        # (None), hogy a golden_gg/traditional_learning beágyazott haszná-
        # lata (ahol a KÜLSŐ controller már fedi ezt) NE duplázza a
        # figyelmeztetést/számlálót.
        self._dmm_t = dmm_temperature
        self._safety = safety
        self._battery_temperature_C: float = 20.0
        # Review-kör 3 (2026-09-09), friss Metrológia M3-01 -- l.
        # temp_dmm_valid property docstringje. Csak a self._dmm_t is not
        # None (önálló StepKind.RELAX) ágon frissül, ugyanúgy, mint a
        # a kozos kiesesi ido es _temperature_out_of_range_count -- beágyazott
        # használatnál (self._dmm_t is None) inaktív marad (True), mert a
        # KÜLSŐ controller fedi a hőmérséklet-DMM-et.
        self._temp_dmm_valid: bool = True
        # Review-kör 4 (2026-09-09), Metrológia M4-01, MAX verifier
        # (a4d9e82debe0bdfcb)/(a306e7e764e3a717b) láncának 9. testvére -- a
        # RelaxController az EGYETLEN step-controller volt, amelyik a
        # temp_dmm_valid property mellé nem kapta meg a session-szintű,
        # tick-alapú temp_dmm_fault_ticks számlálót, holott a testvér
        # temperature_out_of_range_count-ot (lent) már exponálja. Emiatt a
        # test_runner.py generikus, minden StepKind-et lefedő aggregációja
        # (getattr(controller, "temp_dmm_fault_ticks", 0)) egy önálló
        # StepKind.RELAX session hőmérséklet-DMM-kiesését sosem látta.
        self._temp_dmm_fault_ticks: int = 0
        # R11-B3: a fault-eredmeny atmenetet kovetjuk, nem a kiesesi idot.
        self._temp_dmm_fault_active: bool = False
        self._temperature_out_of_range_count: int = 0
        self._last_warning_code: str = ""
        # Teljes kódbázis review 10. kör (2026-08-31), Szoftver/Architektúra
        # P2 lelet: a testvér controllerek (Charge/Discharge/GoldenGg/
        # TraditionalLearning/ChemId/OcvSoc) mind exponálnak dmm_valid/
        # extreme_dt_s_count/max_observed_dt_s property-t — ez a controller
        # (önálló StepKind.RELAX úton, test_runner.py::_build_sample()
        # getattr(controller, ...)-mintája szerint) egyik property-t sem
        # exponálta. Következmény: dmm_valid hiányában a samples.csv
        # dmm_voltage_valid mezője mindig True-ra esett vissza (last_voltage_V
        # sticky, sosem None), extreme_dt_s_count hiányában egy kiugró dt_s
        # a RELAX fázis alatt sosem jutott el a session-szintű
        # TestResult.extreme_dt_s_count-ig (9. kör capacity_result_quality
        # kapuja). Az Integrator "relax 0" esete (l. integrator.py docstring)
        # pontosan erre a helyzetre lett tervezve.
        self._dmm_valid: bool = True
        self._integrator = Integrator()
        # BATCH 4.2b F-2 (2026-09-13, DECISION_REGISTER.md GOV-024, "5.
        # csoport"): a RELAXING-ági DMM-hiba eseményt (lent) korábban
        # MINDEN ticken tüzeltük, amíg a DMM kiesés tartott -- egy hosszú
        # (5h/18h) relax alatti tartós kiesés ~9000 azonos eseményt
        # generálhatott, kiürítve az EventLogWidget 500-soros gyűrűjéből
        # a korábbi kontextust. Ez a jelző teszi lehetővé, hogy csak az
        # ÁTMENETKOR (érvényes -> érvénytelen) tüzeljünk -- a
        # dmm_fault_count számláló továbbra is MINDEN ticken nő.
        self._dmm_was_valid_before_relaxing_tick: bool = True

    @property
    def sampling_mode(self) -> str:
        """A service hiánya választott kompatibilitási mód, nem hibafallback."""
        return "LEGACY_TICK" if self._sampling_service is None else "SERVICE"

    @property
    def sampling_shutdown_complete(self) -> bool:
        return self._sampling_shutdown_complete

    def attach_sampling_service(
        self, service: RelaxSamplingService, *, release_poll: Callable[[], bool] | None = None,
    ) -> None:
        if self._state != RelaxState.INIT or self._sampling_service is not None:
            raise RuntimeError("A relaxgyűjtő csak az első advance előtt köthető be")
        self._sampling_service = service
        self._sampling_release_poll = release_poll
        self._dvdt_invalid_reason = "INSUFFICIENT_DATA"
        self._sampling_shutdown_complete = False
        self._dmm_valid = False
        self._dvdt_samples.clear()
        self._dvdt = float("nan")

    def request_sampling_stop(self) -> None:
        if self._sampling_service is not None:
            self._sampling_service.stop()

    def poll_sampling_stop(self) -> bool:
        """Stopkérés és egyetlen poll; a nyugta előtt nincs átadás."""
        self.request_sampling_stop()
        if self._sampling_release_poll is not None:
            return self._sampling_release_poll()
        self.collect_sampling_records()
        if self._sampling_service is not None and self._sampling_service.shutdown_error is not None:
            raise RuntimeError(self._sampling_service.shutdown_error)
        return self.sampling_shutdown_complete

    def collect_sampling_records(self) -> None:
        """Nem blokkoló drain; a hívó runner-szálon történik minden esemény."""
        service = self._sampling_service
        if service is None:
            return
        batch = service.drain()
        self._sampling_shutdown_complete = batch.released
        if batch.overflow:
            self._dvdt_invalid_reason = "BUFFER_OVERRUN"
            self._dvdt_samples.clear()
            self._dvdt = float("nan")
            self._dmm_valid = False
            self._emit_sampling_event("BUFFER_OVERRUN", "Hiányos relaxminta-folytonosság")
        for record in batch.records:
            if self.on_sample is not None:
                self.on_sample(record)
            if record.epoch != service.epoch:
                self._dvdt_invalid_reason = "GAP_EXCEEDED"
                self._dvdt_samples.clear()
                self._dvdt = float("nan")
                self._dmm_valid = False
                self._emit_sampling_event("EPOCH_MISMATCH", "Idegen relaxepoch elutasítva")
                continue
            if record.epoch != self._sample_epoch:
                self._sample_epoch = record.epoch
                self._sample_sequence = 0
                self._dvdt_samples.clear()
                self._dvdt = float("nan")
            if record.sequence <= self._sample_sequence:
                self._dvdt_invalid_reason = "GAP_EXCEEDED"
                self._dvdt_samples.clear()
                self._dvdt = float("nan")
                self._dmm_valid = False
                self._emit_sampling_event("SEQUENCE_ERROR", "Hibás relaxrekord-sorrend")
                continue
            if record.sequence != self._sample_sequence + 1:
                # A puffervesztés közvetlen következménye nem írhatja felül a gyökérokot.
                if not batch.overflow:
                    self._dvdt_invalid_reason = "GAP_EXCEEDED"
                self._dvdt_samples.clear()
                self._dvdt = float("nan")
                self._emit_sampling_event("SEQUENCE_GAP", "Kimaradt relaxrekord")
            self._sample_sequence = record.sequence
            if record.missed_deadlines:
                self._dvdt_invalid_reason = "GAP_EXCEEDED"
                self._dvdt_samples.clear()
                self._dvdt = float("nan")
                self._dmm_valid = False
                self._missed_deadline_count += record.missed_deadlines
                if not self._deadline_missed_active:
                    self._emit_sampling_event(
                        "DEADLINE_MISSED", f"Kihagyott relaxhatáridők: {record.missed_deadlines}",
                    )
                self._deadline_missed_active = True
                continue
            if record.error is not None or record.voltage_V is None:
                self._dvdt_invalid_reason = "DMM_READ_ERROR"
                was_valid = self._dmm_valid
                self._dmm_valid = False
                self._dmm_fault_count += 1
                self._dvdt_samples.clear()
                self._dvdt = float("nan")
                if was_valid or self._dmm_fault_count == 1:
                    self._emit_sampling_event("DMM_FAULT", record.error or "Hiányzó feszültség")
                continue
            self._last_voltage = record.voltage_V
            self._voltage_record = record
            self._dmm_valid = True
            # Az OLS/programhiba nincs DMM-kivételként elnyelve.
            self._update_dvdt(record.voltage_V, record.sample_time)
        # A csomag végén helyreállt gyűjtés nyit új eseményszakaszt.
        if self._dmm_valid:
            self._deadline_missed_active = False

    def voltage_snapshot(self) -> RelaxVoltageSnapshot:
        """Az érték és minősítése ugyanazon feldolgozott rekordhoz tartozik."""
        if self._sampling_service is None:
            return RelaxVoltageSnapshot(self._last_voltage, None, None, "LEGACY_TICK")
        snapshot = self._sampling_service.snapshot_for(self._voltage_record)
        if self._voltage_record is not None and not self._dmm_valid:
            return replace(snapshot, quality="READ_ERROR")
        return snapshot

    def read_handoff_voltage(
        self, *, emit: Callable[[dict], None] | None = None,
    ) -> RelaxVoltageSnapshot:
        """Release utáni külön kapumérés; nem része az elfogadott OLS-ablaknak."""
        # Az emit a szülő cleanupjáig gyűjtheti a handoff diagnosztikáját.
        service = self._sampling_service
        if service is None or not self.sampling_shutdown_complete:
            raise RuntimeError("RELAX_SAMPLING_RELEASE_PENDING")
        start = service._clock()
        try:
            voltage = self._dmm_v.read_voltage()
            if not isfinite(voltage):
                raise ValueError("Nem véges DMM-feszültség")
        except Exception as exc:  # noqa: BLE001 -- diagnosztizált kapumérési hiba
            self._dmm_valid = False
            self._dmm_fault_count += 1
            self._emit_sampling_event("DMM_FAULT", f"Átadási DMM_READ_ERROR: {exc}", emit=emit)
            return RelaxVoltageSnapshot(None, start, max(0.0, service._clock() - start), "READ_ERROR")
        age = max(0.0, service._clock() - start)
        self._last_voltage = voltage
        self._dmm_valid = age <= 4.0
        return RelaxVoltageSnapshot(
            voltage, start, age, "CACHE_FRESH" if self._dmm_valid else "STALE",
        )

    def acceptance_snapshot(self, threshold_V_per_s: float) -> RelaxAcceptanceSnapshot:
        """A feldolgozott rekord és az OLS közös, döntéskori bizonyítéka."""
        voltage = self.voltage_snapshot()
        samples = tuple(self._dvdt_samples)
        coverage = samples[-1][0] - samples[0][0] if samples else 0.0
        max_gap = max((b[0] - a[0] for a, b in pairwise(samples)), default=0.0)
        if not self._dmm_valid or not isfinite(self._last_voltage):
            reason = self._dvdt_invalid_reason
        elif self._sampling_service is not None and voltage.quality != "CACHE_FRESH":
            reason = "STALE" if voltage.quality == "STALE" else "INSUFFICIENT_DATA"
        elif not self.dvdt_valid:
            reason = self._dvdt_invalid_reason
        else:
            reason = (
                "VALID_BELOW_THRESHOLD" if abs(self._dvdt) < threshold_V_per_s
                else "VALID_AT_OR_ABOVE_THRESHOLD"
            )
        return RelaxAcceptanceSnapshot(
            voltage, self._dvdt, self.dvdt_valid, reason, len(samples),
            coverage, max_gap, self._sample_epoch, self.sampling_shutdown_complete,
        )

    @property
    def voltage_sample_age_s(self) -> float | None:
        return self.voltage_snapshot().age_s

    @property
    def voltage_sample_quality(self) -> str:
        return self.voltage_snapshot().quality

    @property
    def missed_deadline_count(self) -> int:
        """Kihagyott határidők száma; nem DMM-hibák és nem runner-tickek."""
        return self._missed_deadline_count

    def _emit_sampling_event(
        self, code: str, message: str, *, emit: Callable[[dict], None] | None = None,
    ) -> None:
        callback = self.on_event if emit is None else emit
        if callback is not None:
            callback({
                "event_code": f"RELAX_{code}",
                "event_message": message,
            })

    @property
    def state(self) -> RelaxState:
        return self._state

    @property
    def elapsed_s(self) -> float:
        return self._elapsed_s

    @property
    def dvdt_V_per_s(self) -> float:
        """Ablakos OLS-meredekség V/s-ban; NaN = nincs érvényes becslés.

        A TLC elfogadási kapuja használja; a mintavételi ütem korlátját és
        a fizikai relax-HIL HIL_PENDING státuszát l. a modul leírásában.
        """
        return self._dvdt

    @property
    def dvdt_valid(self) -> bool:
        return isfinite(self._dvdt)

    @property
    def last_voltage_V(self) -> float:
        """A legutóbb SIKERESEN olvasott feszültség (ugyanaz az érték, amit a
        dvdt_V_per_s számításhoz is használ) — a hívó ezt használja
        self._u_batt-hoz, HOGY NE olvassa MÉG EGYSZER a DMM-et (CLAUDE.md §9:
        egy DMM-et egy tickben egy komponens olvasson). Teljes kódbázis
        review 1. kör (2026-08-26), P2 lelet."""
        return self._last_voltage

    @property
    def u_batt_V(self) -> float | None:
        """Teljes kódbázis review 7. kör (2026-08-30), Szoftver/Architektúra
        P2 lelet: last_voltage_V alias — test_runner.py::_build_sample()
        getattr(controller, "u_batt_V", None)-je StepKind.RELAX önálló
        lépéseknél (CHARACTERIZATION/BQ_LEARNING_PHYSICAL/CHARGE_ONLY relax-
        lépései) korábban mindig None-t adott vissza, ezért InstrumentManager.
        read_all() MÁSODSZOR is lekérdezte ugyanazt a fizikai feszültség-
        DMM-et minden ticken, holott advance() MÁR olvasta — CLAUDE.md §9
        "egy fizikai DMM-et egy tickben egy kijelölt komponens olvasson"
        megsértése. A last_voltage_V property maga az 1. kör (2026-08-26)
        javítása volt, de sosem lett rákötve a TestRunner getattr-mintájára
        ezen az önálló StepKind.RELAX úton (csak a golden_gg/traditional_
        learning beágyazott használatra)."""
        # SERVICE módban a logger None-szerződése jelzi a még nem mért értéket.
        return self.voltage_snapshot().voltage_V

    @property
    def dmm_fault_count(self) -> int:
        """Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: korábban a
        hívó sosem olvasta ki ezt — egy relax alatti tartós DMM-kiesés
        nyomtalanul maradt (sem a quality_flags-ben, sem a riportban)."""
        # SERVICE: hibás rekordok száma (DMM_READ/WORKER/RELEASE_ERROR),
        # LEGACY_TICK: hibás tickek száma. DEADLINE_MISSED nem DMM-hiba.
        # B5c/F9: a hibafajták bontása és a rekordbélyegek perzisztálása külön munka.
        return self._dmm_fault_count

    @property
    def dmm_valid(self) -> bool:
        """Teljes kódbázis review 10. kör (2026-08-31), Szoftver/Architektúra
        P2 lelet — l. __init__ docstringje ugyanerről a hiányzó property-
        mintáról. Az adott TICKEN ténylegesen sikerült-e olvasni a DMM-et
        (nem session-szintű összeg, mint a dmm_fault_count)."""
        if self._sampling_service is not None:
            return self.voltage_snapshot().quality == "CACHE_FRESH"
        return self._dmm_valid

    @property
    def extreme_dt_s_count(self) -> int:
        """L. __init__ docstringje ugyanerről a hiányzó property-mintáról."""
        return self._integrator.extreme_dt_s_count

    @property
    def max_observed_dt_s(self) -> float:
        return self._integrator.max_observed_dt_s

    @property
    def battery_temperature_C(self) -> float:
        return self._battery_temperature_C

    @property
    def temp_dmm_valid(self) -> bool:
        """Review-kör 3 (2026-09-09), friss Metrológia M3-01 (P1, DFAM-001+
        DFAM-018) -- l. discharge_controller.py azonos property docstringje
        a dmm_valid (feszültség) testvér-mintájáról. Csak önálló StepKind.
        RELAX úton frissül -- l. __init__ komment."""
        return self._temp_dmm_valid

    @property
    def temperature_out_of_range_count(self) -> int:
        """L. __init__ docstringje: csak akkor nem-nulla, ha a konstruktor
        kapott hőmérséklet-DMM-et/safety-t (önálló StepKind.RELAX út)."""
        return self._temperature_out_of_range_count

    @property
    def temp_dmm_fault_ticks(self) -> int:
        """Review-kör 4 (2026-09-09), Metrológia M4-01 -- l. __init__
        docstringje ugyanerről a 9. testvérről, amelyikről ez a property
        hiányzott. L. discharge_controller.py azonos property docstringje
        a tick-alapú (nem másodperc-alapú) számlálási elvről."""
        return self._temp_dmm_fault_ticks

    @property
    def last_warning_code(self) -> str:
        return self._last_warning_code

    def advance(self, dt_s: float) -> RelaxState:
        # Governance-v2 review-kör 1 (CF-05, DFAM-008, ceiling P1 a BQ-
        # tanulási úton): korábban itt egy explicit "if RELAX_DONE: return"
        # állt, ami a min_relax_s elérése UTÁN teljesen leállította a
        # feszültség-DMM olvasását/dV-dt frissítését. traditional_learning_
        # controller.py::_run_relax() a RELAX_DONE elérése UTÁN egy külön,
        # akár max_relax_extra_wait_s-ig (alapból 3600s) tartó BQ-elfogadási
        # hurokban (OCVTAKEN/VOK + dvdt_ok<threshold) TOVÁBB hívja advance()-
        # et minden ticken -- de a fenti korai return miatt a dvdt_V_per_s a
        # RELAX_DONE-t ELSŐKÉNT kiváltó EGYETLEN tick mintájára fagyott,
        # sosem frissült a hurok további (akár 1 órányi) ideje alatt. Ez
        # pontosan a DFAM-008 (single_sample_false_zero_delta) mintája: egy
        # egyetlen mintából számolt delta (itt: az utolsó "friss" dV/dt)
        # megkülönböztethetetlen egy valódi, folyamatos relaxációtól, és a
        # 34465A felbontása (~10 µV/2s tick ≈ 5 µV/s) a 4 µV/s SUP03-LEARN-
        # 006 küszöb közelébe esik (l. CLAIM_LEDGER.yaml BQ-RELAX-001
        # bejegyzés -- a 4 µV/s ÉRTÉK maga VERIFIED_PRIMARY marad, csak a
        # jelenlegi implementáció nem mérte megbízhatóan).
        # A javítás: RELAX_DONE elérése UTÁN is folytatjuk a DMM-olvasást/
        # dV-dt frissítést (az állapot NEM regrediál RELAXING-be, csak a
        # lenti kód fut tovább) -- a GoldenGgController/main_window.py
        # standalone StepKind.RELAX útja ETTŐL NEM érintett, mert egyikük
        # sem hívja advance()-et újra, miután RELAX_DONE-t észlelt (a
        # relax_ctrl-t azonnal eldobják/másik lépésre váltanak).
        self._last_warning_code = ""
        self._elapsed_s += dt_s
        self._integrator.add_sample(0.0, self._last_voltage, dt_s)

        if self._dmm_t is not None and self._safety is not None:
            try:
                self._battery_temperature_C = self._dmm_t.read_temperature()
                self._safety.record_temperature_dmm_read(valid=True, dt_s=dt_s)
                self._temp_dmm_valid = True
            except Exception:
                self._safety.record_temperature_dmm_read(valid=False, dt_s=dt_s)
                self._temp_dmm_fault_ticks += 1
                self._temp_dmm_valid = False

            t_result = self._safety.check_temperature_dmm_fault(
                self._safety.temperature_dmm_fault_s,
            )
            # A RelaxController-nek nincs FAULT állapota (a relax alatt PSU/
            # Load már OFF, nincs aktív energiaátadás védendő) — egy fault-
            # szintű eredményt (TEMPERATURE_MONITOR_LOST_CRITICAL) is
            # last_warning_code-ként exponálunk (nem néma elnyelés), PLUSZ
            # egy CRITICAL súlyosságú eseményt naplózunk, hogy a riportban/
            # eseménynaplóban látható maradjon.
            if t_result.fault is not None:
                self._last_warning_code = t_result.fault.name
                if not self._temp_dmm_fault_active and self.on_event is not None:
                    self.on_event({
                        "event_code": "RELAX_TEMPERATURE_DMM_FAULT",
                        "event_message": f"Hőmérséklet-DMM kiesés relaxáció közben: {t_result.fault.name}",
                        "severity": "CRITICAL",
                    })
            elif t_result.warning is not None:
                self._last_warning_code = t_result.warning.name
            self._temp_dmm_fault_active = t_result.fault is not None

            range_result = self._safety.check_test_temperature_range(self._battery_temperature_C)
            if range_result.warning is not None:
                self._temperature_out_of_range_count += 1
                if not self._last_warning_code:
                    self._last_warning_code = range_result.warning.name

        if self._sampling_service is not None:
            self.collect_sampling_records()
            if self._state == RelaxState.INIT:
                self._state = RelaxState.RELAXING
            elif self._elapsed_s >= self._config.min_relax_s:
                self._state = RelaxState.RELAX_DONE
            return self._state

        # LEGACY_TICK: a régi olvasási és hibakezelési sorrend megmarad.
        if self._state == RelaxState.INIT:
            try:
                self._last_voltage = self._dmm_v.read_voltage()
                self._dmm_valid = True
            except Exception:
                self._dvdt_invalid_reason = "DMM_READ_ERROR"
                self._dvdt_samples.clear()
                self._dvdt = float("nan")
                self._dmm_valid = False
                self._dmm_fault_count += 1
                if self.on_event is not None:
                    self.on_event({
                        "event_code": "RELAX_DMM_FAULT",
                        "event_message": f"DMM olvasási hiba relaxáció INIT fázisában (hiba #{self._dmm_fault_count})",
                    })
            else:
                self._update_dvdt(self._last_voltage, self._elapsed_s)
            self._state = RelaxState.RELAXING
            return self._state

        # RELAXING
        self._dmm_was_valid_before_relaxing_tick = self._dmm_valid
        try:
            v = self._dmm_v.read_voltage()
            self._last_voltage = v
            self._dmm_valid = True
        except Exception:
            self._dvdt_invalid_reason = "DMM_READ_ERROR"
            self._dmm_valid = False
            self._dmm_fault_count += 1
            self._dvdt_samples.clear()
            self._dvdt = float("nan")
            # BATCH 4.2b F-2 (2026-09-13): csak az átmenetkor tüzelünk,
            # l. _dmm_was_valid_before_relaxing_tick docstringje.
            if self._dmm_was_valid_before_relaxing_tick and self.on_event is not None:
                self.on_event({
                    "event_code": "RELAX_DMM_FAULT",
                    "event_message": f"DMM olvasási hiba relaxáció közben (hiba #{self._dmm_fault_count})",
                })
        else:
            self._update_dvdt(v, self._elapsed_s)

        if self._elapsed_s >= self._config.min_relax_s:
            self._state = RelaxState.RELAX_DONE

        return self._state

    def _update_dvdt(self, voltage_V: float, sample_time: float) -> None:
        # R16-B5b: explicit mintabélyeg; az OLS és a Q-18 B kapu változatlan.
        # Hiányzó adaton át nincs interpoláció.
        self._dvdt = float("nan")
        now = sample_time
        if not isfinite(now) or not isfinite(voltage_V):
            self._dvdt_invalid_reason = "DMM_READ_ERROR"
            self._dvdt_samples.clear()
            return
        if self._dvdt_samples:
            gap = now - self._dvdt_samples[-1][0]
            if gap <= 0 or gap > self._config.dvdt_max_gap_s:
                self._dvdt_invalid_reason = "GAP_EXCEEDED"
                self._dvdt_samples.clear()
        self._dvdt_samples.append((now, voltage_V))
        cutoff = now - self._config.sample_period_s
        # A bal ablakhatáron vagy előtte mért utolsó pont megmarad.
        while len(self._dvdt_samples) > 1 and self._dvdt_samples[1][0] <= cutoff:
            self._dvdt_samples.popleft()
        if (
            len(self._dvdt_samples) < max(2, self._config.dvdt_min_samples)
            or self._dvdt_samples[0][0] > cutoff
        ):
            return
        times = [t - now for t, _ in self._dvdt_samples]
        mean_t = sum(times) / len(times)
        mean_v = sum(v for _, v in self._dvdt_samples) / len(times)
        denominator = sum((t - mean_t) ** 2 for t in times)
        if denominator > 0:
            self._dvdt = sum(
                (t - mean_t) * (v - mean_v)
                for t, (_, v) in zip(times, self._dvdt_samples)
            ) / denominator
