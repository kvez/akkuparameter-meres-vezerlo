"""
test_bq_calibration_worker.py — WP-AK-09: BqCalibrationWorker unit tesztek,
FakePicoSerial-lel, valódi QTimer/QThread nélkül (a `_tick_cc()` közvetlen
hívásával, ugyanaz a minta mint test_bq_diagnostics_worker.py poll_once()-e).
A TestBqCalibrationWorkerThreadSafety osztály KIVÉTEL - az VALÓDI QThread-et
indít.
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QThread

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.gui.bq_calibration_worker import BqCalibrationWorker
from Prog.src.bq_gauge_calibration import (
    CONTROL_STATUS_POLL_INTERVAL_S,
    CalState,
    VoltageCalState,
)
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial


def _trigger(calls, name):
    def _handler(params):
        calls.append(name)
        return {"status": "ok"}
    return _handler


def _make_transport(**extra_results):
    calls: list[str] = []
    results = {name: _trigger(calls, name) for name in (
        "unseal", "full_access", "seal", "enter_cal", "exit_cal",
        "board_offset_cal", "cc_offset_cal", "cc_offset_save",
    )}
    results["read_calibration_data"] = lambda params: {
        "cc_gain": 0.47095, "cc_delta": 559500.0, "cc_offset": 0, "board_offset": 0,
    }
    # R16-B6: a fake DF-tartalom session alatt valtozatlanul megmarad.
    pack_configuration_block = bytes.fromhex("0961" + "00" * 30)

    def data_flash_block(params):
        assert params == {"subclass": 64, "block": 0}
        return pack_configuration_block.hex()

    results["data_flash_block"] = data_flash_block
    results["average_current_mA"] = lambda params: 1000
    results["voltage_mV"] = lambda params: 12500
    results["read_voltage_divider"] = lambda params: 20000
    results["write_voltage_divider"] = lambda params: {"status": "ok"}
    results["write_calibration_gain_delta"] = lambda params: {"status": "ok"}
    # [2026-09-12, harmadik root-cause revízió] a BOARD_OFFSET_RUNNING/
    # CC_OFFSET_RUNNING MOST a BCA/CCA CONTROL_STATUS bitet pollozza blind
    # wait helyett -- alapértelmezésben MINDIG "kész" (BCA=0/CCA=0)
    # választ adunk, hogy ez a fixture a tick-integráció mechanikáját
    # tesztelje, ne a BCA/CCA-pollozás részleteit (azt l.
    # test_bq_gauge_calibration.py külön, kimerítően teszteli).
    results["control_status"] = lambda params: 0
    results.update(extra_results)
    serial = FakePicoSerial(results=results)
    return PicoBQTransport(serial=serial), calls


class TestCcGainWorkerSignals:
    def test_start_emits_awaiting_battery_confirm_snapshot(self, qapp):
        transport, _calls = _make_transport()
        worker = BqCalibrationWorker(transport)
        snapshots = []
        worker.cc_gain_snapshot_ready.connect(snapshots.append)
        worker.start_cc_gain_calibration()
        assert len(snapshots) == 1
        assert snapshots[0].state == CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM
        worker._cc_timer.stop()  # ne fusson tovább a teszt utan

    def test_confirm_and_tick_advances_through_offset_waits(self, qapp):
        transport, _calls = _make_transport()
        worker = BqCalibrationWorker(transport)
        snapshots = []
        worker.cc_gain_snapshot_ready.connect(snapshots.append)
        worker.start_cc_gain_calibration()
        worker.confirm_battery_disconnected()
        assert snapshots[-1].state == CalState.BOARD_OFFSET_RUNNING

        # Determinisztikus tick-szimuláció: a valódi elapsed idő helyett
        # a worker belső _last_tick_perf-jét toljuk el, hogy a _tick_cc()
        # a kívánt dt_s-t lássa (nincs valódi sleep a teszt-szuitban).
        worker._last_tick_perf = time.monotonic() - (CONTROL_STATUS_POLL_INTERVAL_S + 0.01)
        worker._tick_cc()
        assert snapshots[-1].state == CalState.CC_OFFSET_RUNNING

        worker._last_tick_perf = time.monotonic() - (CONTROL_STATUS_POLL_INTERVAL_S + 0.01)
        worker._tick_cc()
        assert snapshots[-1].state == CalState.AWAITING_REFERENCE_CURRENT
        worker._cc_timer.stop()

    def test_abort_stops_timer_and_emits_aborted(self, qapp):
        transport, calls = _make_transport()
        worker = BqCalibrationWorker(transport)
        snapshots = []
        worker.cc_gain_snapshot_ready.connect(snapshots.append)
        worker.start_cc_gain_calibration()
        worker.confirm_battery_disconnected()
        worker.abort_cc_gain()
        assert snapshots[-1].state == CalState.ABORTED
        assert not worker._cc_timer.isActive()
        assert "exit_cal" in calls


class TestF9UnexpectedExceptionDoesNotFreezeWorker:
    """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F9 (P3,
    DFAM-002+011)] a bq_gauge_calibration.py kontrollerek saját `_guarded()`
    helperje KIZÁRÓLAG InstrumentError-t fog el -- a `submit_reference_
    current_mA()`/`submit_dmm_voltage_mV()` állapot-ellenőrzése egy rossz
    állapotban (pl. már DONE-nál, vagy AWAITING_REFERENCE_CURRENT előtt)
    bare `RuntimeError`-t dob, amit a `_guarded()` NEM kap el -- korábban ez
    kiszállt volna a Qt-slotból: egyetlen snapshot sem emittálódott volna,
    a dialógus véglegesen befagyott volna, miközben a chip UNSEALED/
    FULL_ACCESS állapotban maradhatott volna naplózatlanul."""

    def test_cc_gain_wrong_state_runtimeerror_still_emits_error_snapshot_and_seals(self, qapp):
        transport, calls = _make_transport()
        worker = BqCalibrationWorker(transport)
        snapshots = []
        worker.cc_gain_snapshot_ready.connect(snapshots.append)
        worker.start_cc_gain_calibration()
        assert snapshots[-1].state == CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM

        # submit_reference_current_mA() csak AWAITING_REFERENCE_CURRENT-ből
        # hívható -- itt még AWAITING_BATTERY_DISCONNECT_CONFIRM-ban vagyunk,
        # tehát a controller bare RuntimeError-t dob (nem InstrumentError).
        worker.submit_reference_current(1000)  # nem szabad kivételt dobnia (ez maga a lelet)

        assert snapshots[-1].state == CalState.ERROR
        assert "seal" in calls, "a chip nem maradhat UNSEALED egy varatlan kivetel utan"
        assert not worker._cc_timer.isActive()

    def test_voltage_calibration_wrong_state_runtimeerror_still_emits_error_snapshot_and_seals(
        self, qapp
    ):
        transport, calls = _make_transport()
        worker = BqCalibrationWorker(transport)
        snapshots = []
        worker.voltage_snapshot_ready.connect(snapshots.append)
        worker.start_voltage_calibration()
        worker.submit_dmm_voltage(12600)
        assert snapshots[-1].state == VoltageCalState.DONE
        calls.clear()

        # Egy MASODIK submit_dmm_voltage() DONE utan bare RuntimeError-t
        # dob (nem InstrumentError) -- pl. egy operatori dupla-kattintas.
        worker.submit_dmm_voltage(12600)  # nem szabad kivételt dobnia (ez maga a lelet)

        assert snapshots[-1].state == VoltageCalState.ERROR


class TestVoltageWorkerSignals:
    def test_start_emits_awaiting_dmm_voltage(self, qapp):
        transport, _calls = _make_transport()
        worker = BqCalibrationWorker(transport)
        snapshots = []
        worker.voltage_snapshot_ready.connect(snapshots.append)
        worker.start_voltage_calibration()
        assert snapshots[-1].state == VoltageCalState.AWAITING_DMM_VOLTAGE

    def test_submit_dmm_voltage_emits_done(self, qapp):
        transport, _calls = _make_transport()
        worker = BqCalibrationWorker(transport)
        snapshots = []
        worker.voltage_snapshot_ready.connect(snapshots.append)
        worker.start_voltage_calibration()
        worker.submit_dmm_voltage(12600)
        assert snapshots[-1].state == VoltageCalState.DONE
        assert snapshots[-1].new_voltage_divider == 20160


class _FakeSerialForThreadTest:
    """MAX verifier 2. kör (2026-09-14, V2 lelet) UTÁN: a `_stop_and_close()`
    mostantól a transport JELENLEGI `serial`-ját zárja (`transport.
    disconnect()`), nem egy külön átadott `serial_conn`-t -- ez a wrapper
    ezért egy VALÓDI, működő `FakePicoSerial`-t csomagol be (a `write`/
    `readline`/`reset_input_buffer`-t hozzá delegálja, hogy a worker
    ténylegesen tudjon RPC-t hívni rajta keresztül), és csak a `close()`
    hívást követi külön a szál-azonosítás céljából."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.close_calls = 0
        self.close_thread_ident: int | None = None

    def write(self, data: bytes) -> int:
        return self._inner.write(data)

    def readline(self) -> bytes:
        return self._inner.readline()

    def reset_input_buffer(self) -> None:
        self._inner.reset_input_buffer()

    def close(self) -> None:
        self.close_calls += 1
        self.close_thread_ident = threading.get_ident()


class TestBqCalibrationWorkerThreadSafety:
    """[mintakövetés: test_bq_diagnostics_worker.py TestBqDiagnosticsWorker
    ThreadSafety, WP-AK-07 Critical fix C-1/C-2] a stop_requested-nek a
    worker SAJÁT szálán kell bezárnia a soros portot, nem a hívóén."""

    def test_stop_requested_closes_serial_on_worker_thread_not_caller_thread(self, qapp):
        transport, _calls = _make_transport()
        # A transport JELENLEGI (a _make_transport() által épített, valódi
        # RPC-vé alaszthato) serial-ját csomagoljuk be -- l. BqCalibration
        # Worker._stop_and_close() V2-fix: a worker a transport.disconnect()
        # -en keresztül ÉRI EL ezt a handle-t, nem egy külön `serial_conn`
        # paraméteren át (az a régi, most már csak fallback út).
        fake_serial = _FakeSerialForThreadTest(transport.serial)
        transport.serial = fake_serial
        worker = BqCalibrationWorker(transport, tick_interval_ms=60000)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.start_cc_gain_calibration)
        thread.start()

        deadline = time.monotonic() + 2.0
        while worker._cc_ctrl is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker._cc_ctrl is not None, "a worker szálnak 2s-en belül el kell indulnia"

        caller_thread_ident = threading.get_ident()
        try:
            worker.stop_requested.emit()
            finished_in_time = thread.wait(5000)
        finally:
            thread.deleteLater()

        assert finished_in_time, "a worker szálnak 5s-en belül le kell állnia"
        assert fake_serial.close_calls == 1
        assert fake_serial.close_thread_ident is not None
        assert fake_serial.close_thread_ident != caller_thread_ident


class TestStopAndCloseUsesLiveTransportHandle:
    """MAX verifier 2. kör (2026-09-14, V2 lelet, CLAIM_LEDGER.yaml
    PICO-RECONNECT-001/BQ-TRANSPORT-ESCALATION-001) -- l. test_bq_
    diagnostics_worker.py azonos nevű osztálya ugyanerre a mintára: a
    konstruktorban kapott `serial_conn` egy RÖGZÍTETT pillanatfelvétel --
    ha a transport időközben (egy soros I/O hiba után, l. PicoBQTransport.
    _reopen_serial()) egy ÚJ handle-re váltott, a régi `serial_conn.
    close()` a MÁR LECSERÉLT, halott objektumot zárná, az ÚJ, ténylegesen
    aktív handle-t nyitva hagyva."""

    class _FakeTransportWithDisconnect:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        def disconnect(self) -> None:
            self.disconnect_calls += 1

    class _FakeTransportWithoutDisconnect:
        """Sem `disconnect()`, sem RPC-metódus -- ilyenkor a _stop_and_
        close()-nak a régi, közvetlen serial_conn.close() útra kell
        esnie, NEM szabad AttributeError-t dobnia."""

    def test_prefers_transport_disconnect_over_stale_serial_conn(self):
        fake_transport = self._FakeTransportWithDisconnect()
        stale_serial = _FakeSerialForThreadTest(inner=None)
        worker = BqCalibrationWorker(fake_transport, serial_conn=stale_serial, tick_interval_ms=60000)

        worker._stop_and_close()

        assert fake_transport.disconnect_calls == 1
        assert stale_serial.close_calls == 0, (
            "a régi, elmentett serial_conn-t NEM szabad zárni, ha a transport "
            "maga fel tudja ajánlani a JELENLEGI handle-t a disconnect()-en keresztül"
        )

    def test_falls_back_to_serial_conn_when_transport_has_no_disconnect(self):
        fake_transport = self._FakeTransportWithoutDisconnect()
        fake_serial = _FakeSerialForThreadTest(inner=None)
        worker = BqCalibrationWorker(fake_transport, serial_conn=fake_serial, tick_interval_ms=60000)

        worker._stop_and_close()

        assert fake_serial.close_calls == 1
