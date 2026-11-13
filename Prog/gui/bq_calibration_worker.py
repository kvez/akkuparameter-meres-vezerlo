"""
BqCalibrationWorker — WP-AK-09: QObject a Prog/src/bq_gauge_calibration.py
állapotgépek (CcGainCalibrationController/VoltageCalibrationController)
saját QThread-en futtatásához, QTimer-alapú tick-eléssel.

Ugyanaz a mintát követi, mint Prog/gui/bq_diagnostics_worker.py (WP-AK-07,
2 review-kör után stabilizálódott, l. Akkuteszter memória
feedback_qt_worker_self_quit_pattern.md): a leállítás KIZÁRÓLAG a
`stop_requested` szignálon keresztül történhet, a soros port bezárása a
worker SAJÁT szálán, `self.thread().quit()`-tal zárva — sosem a GUI-szálról
közvetlenül hívott `stop_polling()`/`close()`.

Architektúrális határ (CLAUDE.md §9/§15): ez a réteg csak Qt-bridge — a
tényleges kalibrációs DÖNTÉST (állapotátmenet, tartomány-ellenőrzés,
képletek) a Prog/src/bq_gauge_calibration.py kontrollerek hozzák, ez a
modul csak a szál-határon mozgatja át az adatot signal/slot-okkal.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from Prog.src.bq_gauge_calibration import (
    CalErrorCode,
    CalState,
    CcGainCalibrationController,
    VoltageCalibrationController,
)

_DEFAULT_TICK_INTERVAL_MS = 500


class BqCalibrationWorker(QObject):
    cc_gain_snapshot_ready = Signal(object)  # CcGainCalibrationSnapshot
    voltage_snapshot_ready = Signal(object)  # VoltageCalibrationSnapshot
    stop_requested = Signal()

    def __init__(self, transport, serial_conn=None, tick_interval_ms: int = _DEFAULT_TICK_INTERVAL_MS) -> None:
        super().__init__()
        self._transport = transport
        self._serial_conn = serial_conn
        self._tick_interval_ms = tick_interval_ms
        self._cc_ctrl: CcGainCalibrationController | None = None
        self._vd_ctrl: VoltageCalibrationController | None = None
        self._cc_timer: QTimer | None = None
        self._last_tick_perf: float | None = None
        self.stop_requested.connect(self._stop_and_close)

    # ------------------------------------------------------------------ #
    # CC/Board Offset + Current Gain kalibráció                          #
    # ------------------------------------------------------------------ #

    def _guarded_cc(self, fn) -> None:
        """Review-kör 2 (2026-09-09), friss GUI/UX F9 (P3, DFAM-002+011):
        a bq_gauge_calibration.py::CcGainCalibrationController saját
        `_guarded()` helperje KIZÁRÓLAG InstrumentError-t fog el -- bármely
        EGYÉB kivétel (pl. `read_calibration_data()` hiányos válaszából jövő
        KeyError, egy állapot-eltérés miatti RuntimeError) korábban
        kiszállt volna ebből a Qt-slotból: egyetlen snapshot sem
        emittálódott volna, a `BqCalibrationDialog` ezért véglegesen
        befagyott volna (mindkét "Indítás" gomb letiltva marad), MIKÖZBEN a
        BQ34Z110 chip UNSEALED+FULL_ACCESS(+CALMODE) állapotban maradhat.
        A testvér `BqDiagnosticsWorker.poll_once()` már véd egy hasonló
        `except Exception`-nel (l. ottani docstring) -- ez a helper ugyanezt
        a mintát vezeti be mind a 5 CC/Gain-oldali nyilvános slotra
        egységesen, a controller SAJÁT `_fail()`-jén (exit_cal+seal
        best-effort takarítás) keresztül, ÚJ signal bevezetése nélkül."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - ez maga a fix célja: a
            # controller SAJÁT _guarded()-je csak InstrumentError-t fog el,
            # ez a szélesebb háló a hívóhely-védelem (l. docstring fent).
            if self._cc_ctrl is not None:
                self._cc_ctrl._fail(CalErrorCode.TRANSPORT, f"Váratlan hiba: {exc}")
            if self._cc_timer is not None:
                self._cc_timer.stop()
        self._emit_cc_snapshot()

    @Slot()
    def start_cc_gain_calibration(self) -> None:
        def _do() -> None:
            self._cc_ctrl = CcGainCalibrationController(self._transport)
            self._cc_ctrl.start()
            self._last_tick_perf = time.monotonic()
            self._cc_timer = QTimer(self)
            self._cc_timer.setInterval(self._tick_interval_ms)
            self._cc_timer.timeout.connect(self._tick_cc)
            self._cc_timer.start()
        self._guarded_cc(_do)

    @Slot()
    def _tick_cc(self) -> None:
        ctrl = self._cc_ctrl
        last_tick_perf = self._last_tick_perf
        if ctrl is None or last_tick_perf is None:
            return

        def _do() -> None:
            now = time.monotonic()
            dt_s = now - last_tick_perf
            self._last_tick_perf = now
            ctrl.advance(dt_s)
            if ctrl.state in (CalState.DONE, CalState.ERROR, CalState.ABORTED):
                if self._cc_timer is not None:
                    self._cc_timer.stop()
        self._guarded_cc(_do)

    @Slot()
    def confirm_battery_disconnected(self) -> None:
        if self._cc_ctrl is None:
            return
        self._guarded_cc(self._cc_ctrl.confirm_battery_disconnected)

    @Slot(int)
    def submit_reference_current(self, reference_current_mA: int) -> None:
        if self._cc_ctrl is None:
            return
        self._guarded_cc(lambda: self._cc_ctrl.submit_reference_current_mA(reference_current_mA))

    @Slot()
    def abort_cc_gain(self) -> None:
        if self._cc_ctrl is None:
            return
        self._guarded_cc(self._cc_ctrl.abort)
        if self._cc_timer is not None:
            self._cc_timer.stop()

    def _emit_cc_snapshot(self) -> None:
        if self._cc_ctrl is not None:
            self.cc_gain_snapshot_ready.emit(self._cc_ctrl.snapshot())

    # ------------------------------------------------------------------ #
    # Voltage Divider kalibráció                                         #
    # ------------------------------------------------------------------ #

    def _guarded_vd(self, fn) -> None:
        """Review-kör 2 (2026-09-09), friss GUI/UX F9 -- l. _guarded_cc()
        docstringje ugyanerről a mintáról; itt a VoltageCalibrationController
        saját `_fail(reason)`-jén (nincs önálló CalErrorCode enum ezen az
        oldalon) keresztül."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - l. _guarded_cc() indoklása
            if self._vd_ctrl is not None:
                self._vd_ctrl._fail(f"Váratlan hiba: {exc}")
        self._emit_voltage_snapshot()

    @Slot()
    def start_voltage_calibration(self) -> None:
        def _do() -> None:
            self._vd_ctrl = VoltageCalibrationController(self._transport)
            self._vd_ctrl.start()
        self._guarded_vd(_do)

    @Slot(int)
    def submit_dmm_voltage(self, dmm_voltage_mV: int) -> None:
        if self._vd_ctrl is None:
            return
        self._guarded_vd(lambda: self._vd_ctrl.submit_dmm_voltage_mV(dmm_voltage_mV))

    @Slot()
    def abort_voltage(self) -> None:
        if self._vd_ctrl is None:
            return
        self._guarded_vd(self._vd_ctrl.abort)

    def _emit_voltage_snapshot(self) -> None:
        if self._vd_ctrl is not None:
            self.voltage_snapshot_ready.emit(self._vd_ctrl.snapshot())

    # ------------------------------------------------------------------ #
    @Slot()
    def _stop_and_close(self) -> None:
        """Kizárólag a `stop_requested` szignálon keresztül hívandó — l.
        Prog/gui/bq_diagnostics_worker.py._stop_and_close() docstringje,
        UGYANAZ az indoklás/veszélyforrás vonatkozik ide is."""
        if self._cc_timer is not None:
            self._cc_timer.stop()
        # MAX verifier 2. kör (2026-09-14, V2 lelet, l. CLAIM_LEDGER.yaml
        # PICO-RECONNECT-001/BQ-TRANSPORT-ESCALATION-001 — ugyanaz a fix,
        # mint Prog/gui/bq_diagnostics_worker.py::_stop_and_close()-ban):
        # a konstruktorban kapott `serial_conn` egy RÖGZÍTETT pillanat-
        # felvétel — ha időközben a PicoBQTransport egy soros I/O hiba
        # után sikeresen újranyitotta a portot (_reopen_serial()), ez a
        # régi, már lecserélt handle-t zárná, az ÚJ, ténylegesen aktív
        # handle-t NYITVA hagyva. A `transport.disconnect()` mindig a
        # JELENLEGI handle-t zárja — ha a transport nem kínál
        # `disconnect()`-et (pl. teszt-fake), a régi, közvetlen
        # `serial_conn.close()` út marad a tartalék.
        transport = self._transport
        if transport is not None and hasattr(transport, "disconnect"):
            try:
                transport.disconnect()
            except Exception:
                pass
        elif self._serial_conn is not None:
            try:
                self._serial_conn.close()
            except Exception:
                pass
        self.thread().quit()
