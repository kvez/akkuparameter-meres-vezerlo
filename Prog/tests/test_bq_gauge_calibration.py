"""
test_bq_gauge_calibration.py — WP-AK-09: Prog/src/bq_gauge_calibration.py
állapotgép-tesztek, FakePicoSerial-lel (valós Pico/BQ HIL nélkül — l. TF1
repo TF1_BLOCKED_VALIDATION_BACKLOG.md BLK-025, ez a réteg is alá tartozik).

A CC/Board Offset + Current Gain (CcGainCalibrationController) és a Voltage
Divider (VoltageCalibrationController) állapotgépet külön teszteli, minden
sikeres és hiba-ágat is (a projekt saját elve: mock/host teszt nem
bizonyíthat fizikai megfelelőséget, csak a szoftverlogikát — l. CLAUDE.md
§7/§10.6/§21).
"""
from __future__ import annotations

import json
import math

import pytest

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.bq_gauge_calibration import (
    BOARD_OFFSET_MAX_WAIT_S,
    CC_OFFSET_MAX_WAIT_S,
    CC_OFFSET_SAVE_MAX_RETRIES,
    CC_OFFSET_SAVE_RETRY_WAIT_S,
    CONTROL_STATUS_POLL_INTERVAL_S,
    GAIN_SAMPLE_COUNT,
    GAIN_SAMPLE_INTERVAL_S,
    GAIN_STABILIZE_S,
    CalErrorCode,
    CalState,
    CcGainCalibrationController,
    VoltageCalibrationController,
    VoltageCalState,
)
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial


class _StatefulCalDevice:
    """In-memory kalibrációs BQ-állapot a FakePicoSerial "results" dict-jébe
    illesztve - a write_calibration_gain_delta hívás UTÁN a
    read_calibration_data ténylegesen a frissen írt értéket adja vissza."""

    def __init__(self, cc_gain: float, cc_delta: float, cc_offset: int = 0,
                 board_offset: int = 0, average_current_mA: int = 1000,
                 voltage_mV: int = 12500, voltage_divider: int = 20000,
                 control_status_word: int = 0,
                 bca_polls_to_clear: int = 1, cca_polls_to_clear: int = 1,
                 pack_configuration: int = 0x0961):
        self.pack_configuration = pack_configuration
        self.cc_gain = cc_gain
        self.cc_delta = cc_delta
        self.cc_offset = cc_offset
        self.board_offset = board_offset
        self.average_current_mA = average_current_mA
        self.voltage_mV = voltage_mV
        self.voltage_divider = voltage_divider
        self.control_status_word = control_status_word
        # [2026-09-12, harmadik root-cause revízió] BCA/CCA CONTROL_STATUS
        # bit szimuláció -- l. bq_gauge_calibration.py advance()
        # BOARD_OFFSET_RUNNING/CC_OFFSET_RUNNING ágai, amik MOST pollozzák
        # ezeket a biteket blind wait helyett. `bca_polls_to_clear=1`
        # (alapértelmezett) -- az ELSŐ poll még 1-et (foglalt) ad vissza, a
        # MÁSODIK már 0-t (kész), realisztikusan modellezve "legalább egy
        # foglalt olvasás, majd kész" mintát.
        self.bca_polls_to_clear = bca_polls_to_clear
        self.cca_polls_to_clear = cca_polls_to_clear
        self._bca_polls_remaining = 0
        self._cca_polls_remaining = 0
        self.calls: list[str] = []

    def data_flash_block(self, params):
        assert params == {"subclass": 64, "block": 0}
        self.calls.append("data_flash_block")
        block = bytearray(32)
        block[:2] = self.pack_configuration.to_bytes(2, "big")
        return block.hex()

    def read_calibration_data(self, params):
        self.calls.append("read_calibration_data")
        return {
            "cc_gain": self.cc_gain, "cc_delta": self.cc_delta,
            "cc_offset": self.cc_offset, "board_offset": self.board_offset,
        }

    def write_calibration_gain_delta(self, params):
        self.calls.append("write_calibration_gain_delta")
        self.cc_gain = params["new_gain"]
        self.cc_delta = params["new_delta"]
        return {"status": "ok"}

    def read_voltage_divider(self, params):
        self.calls.append("read_voltage_divider")
        return self.voltage_divider

    def write_voltage_divider(self, params):
        self.calls.append("write_voltage_divider")
        self.voltage_divider = params["new_value_mV"]
        return {"status": "ok"}

    def average_current(self, params):
        self.calls.append("average_current_mA")
        return self.average_current_mA

    def voltage(self, params):
        self.calls.append("voltage_mV")
        return self.voltage_mV

    def control_status(self, params):
        # [2026-09-12, harmadik root-cause revízió] a bq_gauge_calibration.py
        # advance() BOARD_OFFSET_RUNNING/CC_OFFSET_RUNNING ágai ezt hívják
        # legfeljebb CONTROL_STATUS_POLL_INTERVAL_S-enként (TI SLUUBF7A
        # Sec 5.5 rátakorlátja szerint) a BCA/CCA bit pollozásához.
        self.calls.append("control_status")
        bca = 0
        if self._bca_polls_remaining > 0:
            bca = 1
            self._bca_polls_remaining -= 1
        cca = 0
        if self._cca_polls_remaining > 0:
            cca = 1
            self._cca_polls_remaining -= 1
        word = self.control_status_word
        if bca:
            word |= 0x0400
        if cca:
            word |= 0x0800
        return word

    def board_offset_cal(self, params):
        self.calls.append("board_offset_cal")
        self._bca_polls_remaining = self.bca_polls_to_clear
        return {"status": "ok"}

    def cc_offset_cal(self, params):
        self.calls.append("cc_offset_cal")
        self._cca_polls_remaining = self.cca_polls_to_clear
        return {"status": "ok"}

    def trigger(self, name):
        def _handler(params):
            self.calls.append(name)
            return {"status": "ok"}
        return _handler


def _make_transport(
    device: _StatefulCalDevice, *, errors: dict[str, tuple[str, str]] | None = None,
    **extra_results,
) -> PicoBQTransport:
    results = {
        "unseal": device.trigger("unseal"),
        "full_access": device.trigger("full_access"),
        "seal": device.trigger("seal"),
        "enter_cal": device.trigger("enter_cal"),
        "exit_cal": device.trigger("exit_cal"),
        "board_offset_cal": device.board_offset_cal,
        "cc_offset_cal": device.cc_offset_cal,
        "cc_offset_save": device.trigger("cc_offset_save"),
        "data_flash_block": device.data_flash_block,
        "read_calibration_data": device.read_calibration_data,
        "write_calibration_gain_delta": device.write_calibration_gain_delta,
        "read_voltage_divider": device.read_voltage_divider,
        "write_voltage_divider": device.write_voltage_divider,
        "average_current_mA": device.average_current,
        "voltage_mV": device.voltage,
        "control_status": device.control_status,
    }
    results.update(extra_results)
    serial = FakePicoSerial(results=results, errors=errors or {})
    return PicoBQTransport(serial=serial)


def _advance_until_state(
    ctrl: CcGainCalibrationController, target_states: set[CalState],
    tick_s: float = CONTROL_STATUS_POLL_INTERVAL_S, max_ticks: int = 300,
) -> None:
    """[2026-09-12, harmadik root-cause revízió] Segédfüggvény: a BOARD_
    OFFSET_RUNNING/CC_OFFSET_RUNNING állapotok MOST a BCA/CCA CONTROL_
    STATUS bitet pollozzák (`CONTROL_STATUS_POLL_INTERVAL_S`-enként), NEM
    egy fix blind várakozást -- ez a helper `tick_s`-enként (alapból a
    modul saját poll-intervallumával) tickeli a kontrollert, amíg
    `target_states` egyikébe nem ér, `max_ticks` felső korláttal a
    végtelen ciklus ellen (ha a teszt-fixture hibásan sosem törli a
    figyelt bitet)."""
    for _ in range(max_ticks):
        if ctrl.state in target_states:
            return
        ctrl.advance(tick_s)
    raise AssertionError(
        f"nem ért el {target_states} állapotot {max_ticks} tick alatt "
        f"(jelenlegi: {ctrl.state})"
    )


def _run_cc_gain_offset_phase(ctrl: CcGainCalibrationController) -> None:
    """Segédfüggvény: IDLE -> AWAITING_REFERENCE_CURRENT-ig viszi a
    kontrollert (a BOARD_OFFSET/CC_OFFSET BCA/CCA-pollozáson 'áttickel',
    l. `_advance_until_state()`)."""
    ctrl.start()
    assert ctrl.state == CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM
    ctrl.confirm_battery_disconnected()
    assert ctrl.state == CalState.BOARD_OFFSET_RUNNING
    _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING, CalState.ERROR})
    assert ctrl.state == CalState.CC_OFFSET_RUNNING
    _advance_until_state(ctrl, {CalState.AWAITING_REFERENCE_CURRENT, CalState.ERROR})
    assert ctrl.state == CalState.AWAITING_REFERENCE_CURRENT


def _run_gain_measurement_ticks(ctrl: CcGainCalibrationController) -> None:
    """[Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás NEW-1]
    A GAIN_STABILIZING lejárta után a kontroller MEGSZAKÍTOTT állapotban
    (MEASURING_GAIN, 0 minta) áll meg -- a 4 mintát ettől kezdve
    GAIN_SAMPLE_INTERVAL_S-enkénti KÜLÖN advance()-hívások veszik fel
    (l. a fő fájl advance() MEASURING_GAIN ágának kommentje), NEM egyetlen
    hívás alatt. Ez a segédfüggvény az eddigi tesztek 'egy nagy
    advance()' mintáját egészíti ki a szükséges GAIN_SAMPLE_COUNT
    tickkel."""
    for _ in range(GAIN_SAMPLE_COUNT):
        ctrl.advance(GAIN_SAMPLE_INTERVAL_S)


class TestCcGainCalibrationHappyPath:
    def test_full_flow_reaches_done_and_scales_gain_delta(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0, average_current_mA=980)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)

        _run_cc_gain_offset_phase(ctrl)
        snap = ctrl.snapshot()
        assert snap.cc_offset == 0
        assert snap.board_offset == 0

        ctrl.submit_reference_current_mA(1000)
        assert ctrl.state == CalState.GAIN_STABILIZING
        ctrl.advance(GAIN_STABILIZE_S)
        assert ctrl.state == CalState.MEASURING_GAIN, (
            "GAIN_STABILIZING lejarta utan a merest a kovetkezo tickek "
            "veszik fel (NEW-1), nem egyetlen hivas alatt"
        )
        _run_gain_measurement_ticks(ctrl)

        assert ctrl.state == CalState.DONE
        snap = ctrl.snapshot()
        assert snap.error == CalErrorCode.NONE
        expected_k = 1000 / 980
        assert snap.new_cc_gain == pytest.approx(0.47095 * expected_k)
        assert snap.new_cc_delta == pytest.approx(559500.0 * expected_k)
        assert device.cc_gain == pytest.approx(0.47095 * expected_k)
        assert "write_calibration_gain_delta" in device.calls
        assert "exit_cal" in device.calls
        assert "seal" in device.calls

    def test_advance_before_poll_interval_does_not_poll_yet(self):
        """[2026-09-12, harmadik root-cause revízió] BOARD_OFFSET_RUNNING
        MOST a BCA bitet pollozza `CONTROL_STATUS_POLL_INTERVAL_S`-enként
        (TI SLUUBF7A Sec 5.5 rátakorlátja), NEM egy fix blind várakozást."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        n_before = device.calls.count("control_status")

        ctrl.advance(CONTROL_STATUS_POLL_INTERVAL_S / 2)
        assert ctrl.state == CalState.BOARD_OFFSET_RUNNING
        assert device.calls.count("control_status") == n_before, (
            "a poll-intervallum fele után még NEM szabad CONTROL_STATUS-t "
            "olvasnia"
        )

        ctrl.advance(CONTROL_STATUS_POLL_INTERVAL_S / 2)
        assert device.calls.count("control_status") == n_before + 1, (
            "a teljes poll-intervallum letelte után PONTOSAN egy "
            "CONTROL_STATUS olvasásnak kell történnie"
        )
        # bca_polls_to_clear alapertelmezett=1: az ELSŐ poll még "foglalt"
        # (BCA=1), a kontroller BOARD_OFFSET_RUNNING marad.
        assert ctrl.state == CalState.BOARD_OFFSET_RUNNING

        ctrl.advance(CONTROL_STATUS_POLL_INTERVAL_S)
        assert ctrl.state == CalState.CC_OFFSET_RUNNING, (
            "a MÁSODIK poll után (BCA már törölve) át KELL lépnie"
        )

    def test_submit_reference_current_with_stabilize_s_override_uses_custom_wait(self):
        # [2026-08-22, felhasznaloi megjegyzes] a GAIN_STABILIZE_S alapertek
        # ~1A referencia-aramra van meretezve - eltero aramnal a hivo
        # (GUI/operator) felulirhatja submit_reference_current_mA()
        # stabilize_s parameterevel, kod-modositas nelkul.
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0, average_current_mA=98)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)

        ctrl.submit_reference_current_mA(100, stabilize_s=5.0)
        assert ctrl.state == CalState.GAIN_STABILIZING

        ctrl.advance(4.99)
        assert ctrl.state == CalState.GAIN_STABILIZING, (
            "az egyedi (5.0s) varakozasi ido lejarta ELOTT nem szabad tovabblepnie"
        )
        ctrl.advance(0.02)
        assert ctrl.state == CalState.MEASURING_GAIN, (
            "az egyedi (5.0s, NEM a modul GAIN_STABILIZE_S=150.0s) varakozasi "
            "ido lejarta UTAN tovabb kell lepnie"
        )
        _run_gain_measurement_ticks(ctrl)
        assert ctrl.state == CalState.DONE

    def test_submit_reference_current_without_override_uses_module_default(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0, average_current_mA=980)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)

        ctrl.submit_reference_current_mA(1000)  # nincs stabilize_s megadva
        ctrl.advance(GAIN_STABILIZE_S - 0.01)
        assert ctrl.state == CalState.GAIN_STABILIZING, (
            "az alapertelmezett GAIN_STABILIZE_S (150.0s) lejarta ELOTT nem "
            "szabad tovabblepnie"
        )
        ctrl.advance(0.02)
        assert ctrl.state == CalState.MEASURING_GAIN
        _run_gain_measurement_ticks(ctrl)
        assert ctrl.state == CalState.DONE


class TestCcGainCalibrationErrorPaths:
    def test_zero_reference_current_fails_with_nocurr(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)

        ctrl.submit_reference_current_mA(0)
        assert ctrl.state == CalState.ERROR
        assert ctrl.snapshot().error == CalErrorCode.NOCURR

    def test_zero_measured_current_fails_with_nocurr_and_exits_cal(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0, average_current_mA=0)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)

        ctrl.submit_reference_current_mA(1000)
        ctrl.advance(GAIN_STABILIZE_S)
        _run_gain_measurement_ticks(ctrl)

        assert ctrl.state == CalState.ERROR
        assert ctrl.snapshot().error == CalErrorCode.NOCURR
        assert "exit_cal" in device.calls, "hiba esetén is EXIT_CAL-t kell küldeni (TF1_basic.c minta)"
        assert "seal" in device.calls

    def test_fail_path_records_exit_cal_and_seal_cleanup_failures(self):
        """Review-kör 5, Architektúra A-1 (P1): a _fail() ág EXIT_CAL/SEAL
        takarítás-hibái is attesztálva vannak, ugyanúgy, mint abort()-nál —
        mindkét takarítási lépést önállóan megkísérli, egy hiba nem
        akadályozza a másikat."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0, average_current_mA=0)
        transport = _make_transport(
            device,
            errors={
                "exit_cal": ("METHOD_ERROR", "simulated exit_cal failure"),
                "seal": ("METHOD_ERROR", "simulated seal failure"),
            },
        )
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)

        ctrl.submit_reference_current_mA(1000)
        ctrl.advance(GAIN_STABILIZE_S)
        _run_gain_measurement_ticks(ctrl)

        assert ctrl.state == CalState.ERROR
        assert ctrl.snapshot().error == CalErrorCode.NOCURR
        failures = ctrl.snapshot().cleanup_failures
        assert any(f.startswith("EXIT_CAL_FAILED:") for f in failures)
        assert any(f.startswith("SEAL_FAILED:") for f in failures), (
            "az EXIT_CAL hibája nem akadalyozhatja meg, hogy a SEAL is "
            "megkiserletessek"
        )

    def test_new_gain_out_of_range_fails_without_writing(self):
        # 0.1 alatti uj gain-t provokalunk: nagyon nagy referencia-aram/meres arany.
        device = _StatefulCalDevice(cc_gain=0.15, cc_delta=559500.0, average_current_mA=10000)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)

        ctrl.submit_reference_current_mA(100)  # k = 100/10000 = 0.01 -> new_gain = 0.0015
        ctrl.advance(GAIN_STABILIZE_S)
        _run_gain_measurement_ticks(ctrl)

        assert ctrl.state == CalState.ERROR
        assert ctrl.snapshot().error == CalErrorCode.GAIN_OUT_OF_RANGE
        assert "write_calibration_gain_delta" not in device.calls, (
            "tartományon kívüli gain esetén NEM szabad DF-be írni"
        )

    def test_new_delta_out_of_range_fails_without_writing(self):
        # [audit-lelet, WP-AK-09] a TF1_basic.c-ben HIANYZO delta-tartomany-
        # ellenorzest ez a teszt igazolja: nagy gain/kicsi delta kombinacioval
        # ugy allitjuk be a k-t, hogy new_gain meg ervenyes maradjon, de
        # new_delta mar ne (delta also hatara 2.98e4).
        device = _StatefulCalDevice(cc_gain=1.0, cc_delta=30000.0, average_current_mA=10000)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)

        ctrl.submit_reference_current_mA(1000)  # k=0.1 -> new_gain=0.1 (meg eppen ok), new_delta=3000 (< 2.98e4)
        ctrl.advance(GAIN_STABILIZE_S)
        _run_gain_measurement_ticks(ctrl)

        assert ctrl.state == CalState.ERROR
        assert ctrl.snapshot().error == CalErrorCode.DELTA_OUT_OF_RANGE
        assert "write_calibration_gain_delta" not in device.calls

    def test_transport_error_during_offset_phase_becomes_error_state(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device, unseal=None)
        transport.serial.errors["unseal"] = ("METHOD_ERROR", "simulated unseal failure")  # type: ignore[attr-defined]
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        assert ctrl.state == CalState.ERROR
        assert ctrl.snapshot().error == CalErrorCode.UNSEAL

    def test_missing_calibration_field_at_offset_save_becomes_error_not_uncaught_exception(self):
        """[regresszió, Teljes kódbázis review 10. kör (2026-08-31), Hardver/
        Műszervezérlés P1 lelet] read_calibration_data() egy HIÁNYZÓ mezőt
        (nem NaN/típushiba, hanem a kulcs teljes hiánya — pl. egy csonka
        Pico-válasz) tartalmazó választ adott vissza a cc_offset_save()
        UTÁNI olvasásnál (_save_offset()) — korábban ez csendben átment a
        pico_bq_transport.py validáción, a data["board_offset"] bare
        KeyError-t dobott, ami NEM InstrumentError, tehát a _guarded() nem
        kapta el: a controller elakadt volna CC_OFFSET_RUNNING-ban,
        safe-off takarítás (exit_cal/seal) nélkül, és a következő advance()
        ismét megpróbálta volna a cc_offset_save()-et a valós hardveren
        (CLAUDE.md §10.5: bizonytalan előző eredményű parancs vak
        ismétlése tilos)."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device)

        def _incomplete_read_calibration_data(params):
            device.calls.append("read_calibration_data")
            return {"cc_offset": device.cc_offset}  # board_offset/cc_gain/cc_delta hiányzik

        transport.serial.results["read_calibration_data"] = _incomplete_read_calibration_data

        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING, CalState.ERROR})
        assert ctrl.state == CalState.CC_OFFSET_RUNNING

        _advance_until_state(ctrl, {CalState.ERROR, CalState.AWAITING_REFERENCE_CURRENT})

        assert ctrl.state == CalState.ERROR
        assert ctrl.snapshot().error == CalErrorCode.DFWRITE
        assert "exit_cal" in device.calls, "hiányos válasz esetén is EXIT_CAL-t kell küldeni"
        assert "seal" in device.calls
        # [MAX verifier P2-1, 4. kör, 2026-09-12] A `_advance_until_state()`
        # helper (a BCA/CCA-pollozás bevezetése óta) TÜRELMES retry-körökkel
        # szemben -- e nélkül az asszerció egy olyan MUTÁCIÓ, ami tévesen a
        # cc_offset_save() retry-jába vonná be a read_calibration_data()-t
        # is, ÉSZREVÉTLEN maradna (a végállapot ugyanaz lenne, csak több
        # retry után). Explicit ellenőrizzük, hogy a hiba AZONNAL, retry
        # NÉLKÜL következett be.
        assert ctrl.snapshot().cc_offset_save_retries_used == 0, (
            "a read_calibration_data() hibája NEM vezethet retry-hoz -- "
            "ha ez >0, az implementáció tévesen a cc_offset_save() retry-"
            "körébe vonta be ezt a lépést is"
        )
        assert device.calls.count("cc_offset_save") == 1, (
            "a cc_offset_save() maga csak EGYSZER futott -- a hiba a "
            "RÁKÖVETKEZŐ read_calibration_data()-nál van, ez nem okozhat "
            "több cc_offset_save()-kiadást"
        )

    def test_transport_timeout_sibling_class_is_also_caught(self):
        # [feedback_exception_sibling_class_not_caught.md, WP-AK-08 lelet]
        # InstrumentTimeoutError es PicoBQTransportError TESTVEREK a kozos
        # InstrumentError alatt - a controllernek MINDKETTOT el kell kapnia.
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device)
        transport.serial.simulate_no_response = True  # type: ignore[attr-defined]
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        assert ctrl.state == CalState.ERROR
        assert ctrl.snapshot().error == CalErrorCode.UNSEAL


def _record_device_call_on_wire_write(
    transport: PicoBQTransport, device: _StatefulCalDevice, method: str,
) -> None:
    """[MAX verifier, 2026-09-12, P2-A] A `results`/`errors` dict-alapú
    FakePicoSerial-válasz FÜGGETLENÜL dönt arról, hogy `device.calls`-ba
    bekerül-e a hívás (`_StatefulCalDevice.trigger()` csak SIKERES válasz
    esetén naplóz). A root cause elemzés szerint (l. bq_gauge_calibration.py
    `CC_OFFSET_SAVE_RETRY_WAIT_S` docstring) a valós hardveren a Control()-
    ÍRÁS valószínűleg VÉGREHAJTÓDIK a chipen MÉG AKKOR IS, ha a Pico<->BQ
    visszaolvasás időtúllépik és a Pico ok:false METHOD_ERROR választ küld --
    ezt itt a `serial.write()` becsomagolásával modellezzük: a kérés
    METÓDUSNEVÉT a válasz KIMENETELÉTŐL FÜGGETLENÜL naplózzuk, mert a
    wire-kérés ténylegesen elment és a device (a valós BQ34Z110 analógjaként)
    megkapta/végrehajtotta azt. A korábbi teszt-verzió ennek ELLENKEZŐJÉT
    (a parancs sosem ért célba) feltételezte, ami a saját HIL-bizonyítékunkkal
    (determinisztikus, 3/3 első-próbálkozás-hiba) ellentétes volt."""
    original_write = transport.serial.write

    def _write(data: bytes) -> int:
        request = json.loads(data.decode("utf-8"))
        if request.get("method") == method:
            device.calls.append(method)
        return original_write(data)

    transport.serial.write = _write  # type: ignore[method-assign]


def _fail_method_n_times_then_succeed(
    transport: PicoBQTransport, device: _StatefulCalDevice, method: str, n_failures: int,
) -> None:
    """Mint `_record_device_call_on_wire_write`, DE az N-edik hiba UTÁN
    (tehát az (N+1)-edik hívástól kezdve) automatikusan eltávolítja a
    hiba-injektálást, így az adott hívás már sikeres lesz -- a retry
    (`CC_OFFSET_SAVE_RETRY_WAIT_S`/`CC_OFFSET_SAVE_MAX_RETRIES`)
    tesztelésére, ahol a sikerhez vezető retry-k száma VÁLTOZÓ (0..
    `CC_OFFSET_SAVE_MAX_RETRIES`), nem mindig 1."""
    transport.serial.errors["cc_offset_save"] = ("METHOD_ERROR", "[Errno 110] ETIMEDOUT")  # type: ignore[attr-defined]
    original_write = transport.serial.write
    call_count = {"n": 0}

    def _write(data: bytes) -> int:
        request = json.loads(data.decode("utf-8"))
        if request.get("method") == method:
            device.calls.append(method)
            call_count["n"] += 1
            if call_count["n"] > n_failures:
                transport.serial.errors.pop(method, None)  # type: ignore[attr-defined]
        return original_write(data)

    transport.serial.write = _write  # type: ignore[method-assign]


class TestCcGainCalibrationOffsetSaveRetry:
    """[2026-09-12, HARMADIK root-cause revízió] HIL evidencia (l. Mérések/
    nyers_logok/hil_bq_cal_cc_offset_save_retry_20260912.md UPDATE 3-6): a
    cc_offset_save() Pico<->BQ I2C visszaolvasása ELSŐRE DETERMINISZTIKUSAN
    "[Errno 110] ETIMEDOUT"-tal hibázik. Két korábbi mitigáció (fix 2.0s
    egyetlen retry; SLEEP-mód elméletre épülő keepalive-ping + 5-lépéses
    rövid-résű retry) élő újratesztkor TOVÁBBRA IS teljes kudarcot vallott.
    Felhasználói kérésre végzett forráskutatás (max-effort agent) feltárta:
    a BOARD_OFFSET_WAIT_S/CC_OFFSET_WAIT_S EREDETI, AVR-ből átvett blind
    várakozásai (~2s/~20s) drasztikusan alábecsülték a BCA/CCA CONTROL_
    STATUS bit valós törlési idejét (élőben mérve ~36.6s/~17.7s) -- a
    kalibráció ELŐZŐ LÉPÉSE (board/cc offset mérés) még NEM fejeződött be,
    amikor a kód már a KÖVETKEZŐ lépésre (végül cc_offset_save()-re)
    lépett. TI SLUUBF7A (analóg forrás) Sec 5.5/5.6 folyamatábrája szerinti
    BCA/CCA-pollozásra átállva + TI SLUUBW5A (analóg forrás) Sec 8.3
    70ms/3-retry mintáját alkalmazva a cc_offset_save()-re: 2/2 élő
    reprodukálható siker (1 hiba + 1 gyors retry mindkétszer). Ez a
    teszt-osztály a JELENLEGI (egyetlen `CC_OFFSET_SAVE_RETRY_WAIT_S`
    konstans, `CC_OFFSET_SAVE_MAX_RETRIES` retry) tervet diszkriminálja."""

    def test_succeeds_after_several_retries_documents_retry_count(self):
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0, cc_offset=-1375, board_offset=-11,
        )
        transport = _make_transport(
            device,
            # a sikeres válasz-ág itt NE naplózzon a device.trigger() saját
            # útján -- a wire-write wrapper az EGYETLEN számláló
            cc_offset_save=lambda params: {"status": "ok"},
        )
        n_failures = CC_OFFSET_SAVE_MAX_RETRIES - 1
        assert n_failures >= 1, "a teszt legalább 1 hibás próbálkozást feltételez"
        _fail_method_n_times_then_succeed(transport, device, "cc_offset_save", n_failures=n_failures)
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING})
        _advance_until_state(
            ctrl, {CalState.CC_OFFSET_SAVE_RETRY_WAIT, CalState.AWAITING_REFERENCE_CURRENT},
        )

        for i in range(n_failures):
            assert ctrl.state == CalState.CC_OFFSET_SAVE_RETRY_WAIT, (
                f"a(z) {i+1}. hibás cc_offset_save() után retry-várakozásba "
                f"kell lépnie, NEM azonnal ERROR-ba"
            )
            assert ctrl.snapshot().cc_offset_save_retries_used == i + 1, (
                "a retry-számláló minden hibás próbálkozás UTÁN nőjön eggyel"
            )
            ctrl.advance(CC_OFFSET_SAVE_RETRY_WAIT_S / 2)
            assert ctrl.state == CalState.CC_OFFSET_SAVE_RETRY_WAIT, (
                "a retry-várakozási idő FELE után még nem szabad "
                "újraprobálkoznia"
            )
            ctrl.advance(CC_OFFSET_SAVE_RETRY_WAIT_S / 2)

        assert ctrl.state == CalState.AWAITING_REFERENCE_CURRENT, (
            f"az utolsó próbálkozásnak ({n_failures} hiba UTÁN) sikeresnek "
            f"kell lennie"
        )
        snap = ctrl.snapshot()
        assert snap.error == CalErrorCode.NONE
        assert snap.cc_offset == -1375
        assert snap.board_offset == -11
        assert snap.cc_offset_save_retries_used == n_failures, (
            "a snapshot-nak attesztálnia kell, HÁNY retry kellett a "
            "sikerhez -- ugyanaz az elv, mint a cleanup_failures mezőnél"
        )
        assert device.calls.count("cc_offset_save") == n_failures + 1, (
            f"{n_failures} hibás + 1 sikeres wire-kiadás -- minden "
            f"próbálkozás valós CC_OFFSET_SAVE Control()-írás a "
            f"(szimulált) chip felé"
        )

    def test_exhausts_all_scheduled_retries_then_fails_no_infinite_loop(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(
            device, errors={"cc_offset_save": ("METHOD_ERROR", "[Errno 110] ETIMEDOUT")},
            cc_offset_save=lambda params: {"status": "ok"},
        )
        _record_device_call_on_wire_write(transport, device, "cc_offset_save")
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING})
        _advance_until_state(ctrl, {CalState.CC_OFFSET_SAVE_RETRY_WAIT})

        for _ in range(CC_OFFSET_SAVE_MAX_RETRIES):
            assert ctrl.state == CalState.CC_OFFSET_SAVE_RETRY_WAIT
            ctrl.advance(CC_OFFSET_SAVE_RETRY_WAIT_S)

        assert ctrl.state == CalState.ERROR, (
            "az ÖSSZES engedélyezett retry kimerítése UTÁN a korábbi, "
            "változatlan _fail(DFWRITE, ...) úton kell terjednie a hibának "
            "-- nincs végtelen retry"
        )
        snap = ctrl.snapshot()
        assert snap.error == CalErrorCode.DFWRITE
        assert "ETIMEDOUT" in snap.fail_reason
        assert snap.cc_offset_save_retries_used == CC_OFFSET_SAVE_MAX_RETRIES
        assert device.calls.count("cc_offset_save") == CC_OFFSET_SAVE_MAX_RETRIES + 1, (
            "1 kezdeti + N engedélyezett retry -- MINDEGYIK wire-kérése "
            "ténylegesen elment a (szimulált) chiphez, mind a "
            "legrosszabb (perzisztens) esetben is"
        )
        assert "exit_cal" in device.calls
        assert "seal" in device.calls

    def test_retry_wait_uses_exact_constant_not_shorter_or_longer(self):
        """Szoros (0,01s-os) határértékkel ellenőrzi, hogy a retry pontosan
        `CC_OFFSET_SAVE_RETRY_WAIT_S` után (nem előbb, nem sokkal utóbb)
        következik be."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(
            device, errors={"cc_offset_save": ("METHOD_ERROR", "[Errno 110] ETIMEDOUT")},
            cc_offset_save=lambda params: {"status": "ok"},
        )
        _record_device_call_on_wire_write(transport, device, "cc_offset_save")
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING})
        _advance_until_state(ctrl, {CalState.CC_OFFSET_SAVE_RETRY_WAIT})

        eps = 0.01
        n_calls_before = device.calls.count("cc_offset_save")
        ctrl.advance(CC_OFFSET_SAVE_RETRY_WAIT_S - eps)
        assert ctrl.state == CalState.CC_OFFSET_SAVE_RETRY_WAIT, (
            "a retry-várakozás LEJÁRTA ELŐTT még nem szabad újraprobálkoznia"
        )
        assert device.calls.count("cc_offset_save") == n_calls_before

        ctrl.advance(2 * eps)
        assert device.calls.count("cc_offset_save") == n_calls_before + 1, (
            "a retry-várakozás LEJÁRTA UTÁN pontosan egy új próbálkozásnak "
            "kell történnie"
        )

    def test_read_calibration_data_error_after_successful_save_fails_immediately(self):
        """A retry KIZÁRÓLAG a cc_offset_save() hívásra vonatkozik -- a
        rákövetkező read_calibration_data() hibája (pl. hiányzó mező,
        l. test_missing_calibration_field_at_offset_save_becomes_error_
        not_uncaught_exception) adatintegritási probléma, amit egy
        időzítési retry nem old meg, ezért ott AZONNAL ERROR-ba kell lépnie,
        retry-várakozás NÉLKÜL."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device)

        def _incomplete_read_calibration_data(params):
            device.calls.append("read_calibration_data")
            return {"cc_offset": device.cc_offset}  # board_offset hiányzik

        transport.serial.results["read_calibration_data"] = _incomplete_read_calibration_data

        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING})
        _advance_until_state(ctrl, {CalState.ERROR, CalState.AWAITING_REFERENCE_CURRENT})

        assert ctrl.state == CalState.ERROR, (
            "read_calibration_data() hibája NEM léphet retry-várakozásba"
        )
        assert ctrl.snapshot().error == CalErrorCode.DFWRITE
        assert "cc_offset_save" in device.calls, (
            "a cc_offset_save() maga sikeres volt -- csak az UTÁNA "
            "következő olvasás hibázott"
        )
        # [MAX verifier P2-1, 4. kör, 2026-09-12] l. a hasonló, korábbi
        # (test_missing_calibration_field_...) teszt kommentje: a türelmes
        # _advance_until_state() helper NEM fogná el, ha az implementáció
        # tévesen retry-körbe vonná be a read_calibration_data()-t is --
        # explicit szám szerint ellenőrizzük.
        assert ctrl.snapshot().cc_offset_save_retries_used == 0, (
            "a read_calibration_data() hibája NEM vezethet retry-hoz"
        )
        assert device.calls.count("cc_offset_save") == 1, (
            "a cc_offset_save() csak EGYSZER futott (sikeresen) -- a hiba "
            "a rákövetkező olvasásnál van"
        )


class TestCcGainCalibrationOffsetPollTimeout:
    """[2026-09-12, harmadik root-cause revízió] Ha a BCA/CCA CONTROL_STATUS
    bit SOHA nem törlődik (valós hardverhiba -- pl. a chip beragadt egy
    kalibrációs módban), a kontrollernek NEM szabad örökké pollozva várnia:
    `BOARD_OFFSET_MAX_WAIT_S`/`CC_OFFSET_MAX_WAIT_S` felső korlát után
    TRANSPORT hibaként kell elbuknia, takarítással."""

    def test_bca_never_clearing_fails_with_transport_error_after_max_wait(self):
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0,
            bca_polls_to_clear=10_000,  # sose törlődjön ésszerű időn belül
        )
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()

        _advance_until_state(
            ctrl, {CalState.ERROR, CalState.CC_OFFSET_RUNNING},
            tick_s=BOARD_OFFSET_MAX_WAIT_S / 10, max_ticks=20,
        )

        assert ctrl.state == CalState.ERROR, (
            "ha a BCA bit SOHA nem törlődik, a kontrollernek a "
            "BOARD_OFFSET_MAX_WAIT_S felső korlát után hibával kell "
            "leállnia, NEM örökké várakoznia"
        )
        assert ctrl.snapshot().error == CalErrorCode.TRANSPORT
        assert "exit_cal" in device.calls
        assert "seal" in device.calls

    def test_cca_never_clearing_fails_with_transport_error_after_max_wait(self):
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0,
            cca_polls_to_clear=10_000,  # sose törlődjön ésszerű időn belül
        )
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING})

        _advance_until_state(
            ctrl, {CalState.ERROR, CalState.AWAITING_REFERENCE_CURRENT},
            tick_s=CC_OFFSET_MAX_WAIT_S / 10, max_ticks=20,
        )

        assert ctrl.state == CalState.ERROR, (
            "ha a CCA bit SOHA nem törlődik, a kontrollernek a "
            "CC_OFFSET_MAX_WAIT_S felső korlát után hibával kell "
            "leállnia"
        )
        assert ctrl.snapshot().error == CalErrorCode.TRANSPORT
        assert "exit_cal" in device.calls
        assert "seal" in device.calls

    def test_total_wait_budget_resets_when_entering_cc_offset_running(self):
        """[MAX verifier P3-3, 4. kör, 2026-09-12] Mutációval igazolt rés:
        ha a `_total_wait_in_state_s` reset (`_start_cc_offset()`-ben)
        hiányozna, a BOARD_OFFSET_RUNNING alatt felhalmozott idő ÁTÖRÖKLŐDNE
        CC_OFFSET_RUNNING-ba, csendben csökkentve a CCA-timeout budgetet
        (a mért ~17.67s-hoz képesti margó akár ~42s-ról ~6s-ra zsugorodna).
        Ez a teszt biztosítja, hogy a `CC_OFFSET_MAX_WAIT_S` teljes egésze a
        CC_OFFSET_RUNNING-ba LÉPÉS UTÁN mérhető idő."""
        board_offset_polls = 50  # kb. 50*0.6s = 30s BOARD_OFFSET_RUNNING alatt
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0,
            bca_polls_to_clear=board_offset_polls,
            cca_polls_to_clear=10_000,  # CC_OFFSET_RUNNING alatt sose törlődjön
        )
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING}, max_ticks=200)
        assert ctrl.state == CalState.CC_OFFSET_RUNNING

        already_spent_s = board_offset_polls * CONTROL_STATUS_POLL_INTERVAL_S
        assert already_spent_s < CC_OFFSET_MAX_WAIT_S, (
            "a teszt-paraméterezés érvénytelen: a BOARD_OFFSET_RUNNING "
            "alatt már elhasznált idő nem lehet nagyobb, mint a "
            "CC_OFFSET_MAX_WAIT_S, különben a teszt félrevezetően bukna"
        )
        ctrl.advance(already_spent_s + 1.0)

        assert ctrl.state == CalState.CC_OFFSET_RUNNING, (
            "ha ITT már ERROR-ba lépett, az azt jelenti, hogy a BOARD_"
            "OFFSET_RUNNING alatt elhasznált idő ÁTÖRÖKLŐDÖTT a CC_OFFSET_"
            "RUNNING timeout-budgetjébe -- a _total_wait_in_state_s reset "
            "hiányzik/hibás a _start_cc_offset()-ben"
        )


class TestCcGainCalibrationControlStatusPollRate:
    """[2026-09-12, harmadik root-cause revízió] Regresszió-védelem: egy
    KORÁBBI tervezet (`_keepalive_ping()`, l. git history commit `70a4aea`)
    MINDEN `advance()` ticknél olvasta a CONTROL_STATUS-t, ami valószínűleg
    sértette a TI SLUUBF7A Sec 5.5 rátakorlátját ("not... greater than once
    every 0.5 seconds"), és élőben TOVÁBBRA IS teljes kudarcot okozott. Ez a
    teszt biztosítja, hogy a CONTROL_STATUS pollozás gyakorisága SOHA ne
    haladja meg a `CONTROL_STATUS_POLL_INTERVAL_S` által megszabott rátát,
    AKÁRMILYEN (akár annál sokkal rövidebb) tick-kel hívják is az
    `advance()`-t."""

    def test_control_status_never_polled_faster_than_ti_rate_limit(self):
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0,
            bca_polls_to_clear=10_000,  # maradjon BOARD_OFFSET_RUNNING-ban a teszt idejére
        )
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()

        fast_tick = CONTROL_STATUS_POLL_INTERVAL_S / 6  # jóval gyorsabb, mint a TI-korlát
        n_ticks = 60
        for _ in range(n_ticks):
            ctrl.advance(fast_tick)

        total_elapsed_s = n_ticks * fast_tick
        n_polls = device.calls.count("control_status")
        max_allowed_polls = int(total_elapsed_s / CONTROL_STATUS_POLL_INTERVAL_S) + 1
        assert n_polls <= max_allowed_polls, (
            f"{n_polls} CONTROL_STATUS olvasás történt {total_elapsed_s:.2f}s "
            f"alatt -- ez több, mint amit a TI SLUUBF7A Sec 5.5 rátakorlátja "
            f"(legfeljebb {max_allowed_polls} ennyi idő alatt) megenged"
        )


class TestCcGainCalibrationAbort:
    def test_abort_during_offset_wait_sends_exit_cal_and_seal(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        assert ctrl.state == CalState.BOARD_OFFSET_RUNNING

        ctrl.abort()
        assert ctrl.state == CalState.ABORTED
        assert "exit_cal" in device.calls
        assert "seal" in device.calls

    def test_abort_during_offset_save_retry_wait_sends_exit_cal_and_seal(self):
        """[MAX verifier P3/M12, 2026-09-12] A CC_OFFSET_SAVE_RETRY_WAIT
        egy ÚJ, korábban nem létező állapot -- explicit teszt kell arra,
        hogy az abort() ebből az állapotból is helyesen takarít (ENTER_CAL
        már megtörtént idáig, tehát az exit_cal()+seal() útnak kell
        lefutnia, NEM az AWAITING_BATTERY_DISCONNECT_CONFIRM-ra érvényes
        'nincs takarítás' ágnak)."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(
            device, errors={"cc_offset_save": ("METHOD_ERROR", "[Errno 110] ETIMEDOUT")},
        )
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING})
        _advance_until_state(ctrl, {CalState.CC_OFFSET_SAVE_RETRY_WAIT})
        assert ctrl.state == CalState.CC_OFFSET_SAVE_RETRY_WAIT

        ctrl.abort()

        assert ctrl.state == CalState.ABORTED
        assert "exit_cal" in device.calls
        assert "seal" in device.calls

    def test_abort_before_enter_cal_does_not_send_exit_cal(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        assert ctrl.state == CalState.AWAITING_BATTERY_DISCONNECT_CONFIRM

        ctrl.abort()
        assert ctrl.state == CalState.ABORTED
        assert "exit_cal" not in device.calls, (
            "ENTER_CAL meg nem tortent meg, EXIT_CAL felesleges lenne "
            "(Prog/TF1_basic.c:2759-2762 bq_cal_abort() mintaja)"
        )

    def test_abort_records_exit_cal_cleanup_failure(self):
        """Review-kör 5, Architektúra A-1 (P1): ha az EXIT_CAL "best effort"
        takarítás a valós hardveren elhasal, ez korábban nyomtalanul eltűnt
        (`except Exception: pass`) — most a snapshot cleanup_failures
        mezőjében attesztálva van, a chip UNSEALED/CALMODE kockázata NEM
        marad néma."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device, errors={"exit_cal": ("METHOD_ERROR", "simulated exit_cal failure")})
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        assert ctrl.state == CalState.BOARD_OFFSET_RUNNING

        ctrl.abort()
        assert ctrl.state == CalState.ABORTED
        failures = ctrl.snapshot().cleanup_failures
        assert any(f.startswith("EXIT_CAL_FAILED:") for f in failures)
        assert "seal" in device.calls, (
            "az EXIT_CAL hibája NEM akadályozhatja meg a SEAL megkísérlését "
            "(CLAUDE.md safety invariáns: egy OFF-hiba ne blokkolja a másikat)"
        )

    def test_abort_records_seal_cleanup_failure_even_when_exit_cal_succeeds(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device, errors={"seal": ("METHOD_ERROR", "simulated seal failure")})
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()

        ctrl.abort()
        assert ctrl.state == CalState.ABORTED
        assert "exit_cal" in device.calls
        failures = ctrl.snapshot().cleanup_failures
        assert any(f.startswith("SEAL_FAILED:") for f in failures)

    def test_abort_after_done_is_a_noop(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0, average_current_mA=1000)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)
        ctrl.submit_reference_current_mA(1000)
        ctrl.advance(GAIN_STABILIZE_S)
        _run_gain_measurement_ticks(ctrl)
        assert ctrl.state == CalState.DONE
        calls_before = len(device.calls)

        ctrl.abort()
        assert ctrl.state == CalState.DONE, "DONE utan az abort() nem valtoztathat allapotot"
        assert len(device.calls) == calls_before


class TestSealStateVerification:
    """[regresszió, Kör 17 MAX-effort review, R17-09, DFAM-001+DFAM-032]
    a chip legmagasabb-privilégiumú (unseal+full_access) útján a
    seal(confirm=True) RPC sikeres visszatérése önmagában NEM bizonyítja,
    hogy a chip tényleges SS bitje (ControlStatusBits.ss) SEALED-et
    jelez -- l. _verify_sealed_or_record() docstringje. A `_StatefulCal
    Device` alapból control_status_word=0-val (SS bit clear) épül, tehát
    ha nincs explicit control_status_word=0x2000 megadva, a SEAL_NOT_
    VERIFIED: jelzőnek MEGJELENNIE kell -- ez a diszkriminátor."""

    def test_cc_gain_done_path_records_seal_not_verified_when_ss_bit_clear(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0, average_current_mA=980)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)
        ctrl.submit_reference_current_mA(1000)
        ctrl.advance(GAIN_STABILIZE_S)
        _run_gain_measurement_ticks(ctrl)

        assert ctrl.state == CalState.DONE, (
            "a nem-verifikalt seal NEM fokozhatja le a kalibraciot ERROR-ra"
        )
        assert any(
            f.startswith("SEAL_NOT_VERIFIED:CC_GAIN_DONE:")
            for f in ctrl.snapshot().cleanup_failures
        )

    def test_cc_gain_done_path_clean_when_ss_bit_set(self):
        """Over-correction guard: ha az SS bit tényleg beáll, NEM jelenik
        meg SEAL_NOT_VERIFIED:."""
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0, average_current_mA=980,
            control_status_word=0x2000,
        )
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)
        ctrl.submit_reference_current_mA(1000)
        ctrl.advance(GAIN_STABILIZE_S)
        _run_gain_measurement_ticks(ctrl)

        assert ctrl.state == CalState.DONE
        assert ctrl.snapshot().cleanup_failures == ()

    def test_cc_gain_fail_path_preserves_original_error_and_adds_seal_flag(self):
        """A helper SOHA nem maszkolhatja el az eredeti hibát (DFAM-033) --
        a NOCURR eredmény és indoklás változatlan marad, a SEAL_NOT_
        VERIFIED: pedig KIEGÉSZÍTI, nem helyettesíti a meglévő jelzőket."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0, average_current_mA=0)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)
        ctrl.submit_reference_current_mA(1000)
        ctrl.advance(GAIN_STABILIZE_S)
        _run_gain_measurement_ticks(ctrl)

        assert ctrl.state == CalState.ERROR
        snap = ctrl.snapshot()
        assert snap.error == CalErrorCode.NOCURR
        assert any(f.startswith("SEAL_NOT_VERIFIED:CC_GAIN_FAIL:") for f in snap.cleanup_failures)

    def test_cc_gain_abort_path_records_seal_not_verified(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)

        ctrl.abort()
        assert ctrl.state == CalState.ABORTED
        assert any(
            f.startswith("SEAL_NOT_VERIFIED:CC_GAIN_ABORT:")
            for f in ctrl.snapshot().cleanup_failures
        )

    def test_seal_rpc_failure_does_not_trigger_a_second_rpc(self):
        """[DFAM-033 strukturális őr] ha a `seal()` RPC maga hibázik, a
        helper EGYÁLTALÁN nem hívódik (a control_status olvasás-számláló
        nem nő) -- egy második RPC egy már hibázó seal után csak elfedné
        az eredeti diagnosztikát."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(
            device, errors={"seal": ("METHOD_ERROR", "simulated seal failure")}
        )
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)
        n_before = device.calls.count("control_status")

        ctrl.abort()

        assert device.calls.count("control_status") == n_before
        failures = ctrl.snapshot().cleanup_failures
        assert any(f.startswith("SEAL_FAILED:") for f in failures)
        assert not any(f.startswith("SEAL_NOT_VERIFIED:") for f in failures)
        assert not any(f.startswith("SEAL_VERIFY_FAILED:") for f in failures)

    def test_verify_read_failure_is_recorded_and_never_raises(self):
        """A control_status hibaja CSAK a seal-UTANI verify-olvasast erje
        el -- a BOARD_OFFSET/CC_OFFSET BCA/CCA-pollozas (ami is control_
        status-t hasznal) a `_run_cc_gain_offset_phase()` alatt meg
        sikeres kell legyen, kulonben a driving helper maga bukik."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        _run_cc_gain_offset_phase(ctrl)

        transport.serial.errors["control_status"] = (
            "METHOD_ERROR", "simulated control_status failure"
        )
        ctrl.abort()  # nem szabad kivetelt dobnia

        assert ctrl.state == CalState.ABORTED
        assert any(
            f.startswith("SEAL_VERIFY_FAILED:CC_GAIN_ABORT:")
            for f in ctrl.snapshot().cleanup_failures
        )

    def test_voltage_cal_done_path_records_seal_not_verified_when_ss_bit_clear(self):
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0, voltage_mV=12500, voltage_divider=20000,
        )
        transport = _make_transport(device)
        ctrl = VoltageCalibrationController(transport)
        ctrl.start()
        ctrl.submit_dmm_voltage_mV(12600)

        assert ctrl.state == VoltageCalState.DONE
        assert any(
            f.startswith("SEAL_NOT_VERIFIED:VOLTAGE_CAL_DONE:")
            for f in ctrl.snapshot().cleanup_failures
        )

    def test_voltage_cal_fail_path_preserves_original_reason_and_adds_seal_flag(self):
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0, voltage_mV=12500, voltage_divider=20000,
        )
        transport = _make_transport(device)
        ctrl = VoltageCalibrationController(transport)
        ctrl.start()
        ctrl.submit_dmm_voltage_mV(0)

        assert ctrl.state == VoltageCalState.ERROR
        snap = ctrl.snapshot()
        assert snap.fail_reason == "dmm_voltage_mV nem lehet <= 0"
        assert any(
            f.startswith("SEAL_NOT_VERIFIED:VOLTAGE_CAL_FAIL:") for f in snap.cleanup_failures
        )

    def test_voltage_cal_abort_path_records_seal_not_verified(self):
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0, voltage_mV=12500, voltage_divider=20000,
        )
        transport = _make_transport(device)
        ctrl = VoltageCalibrationController(transport)
        ctrl.start()

        ctrl.abort()
        assert ctrl.state == VoltageCalState.ABORTED
        assert any(
            f.startswith("SEAL_NOT_VERIFIED:VOLTAGE_CAL_ABORT:")
            for f in ctrl.snapshot().cleanup_failures
        )


class TestVoltageCalibrationHappyPath:
    def test_full_flow_computes_new_voltage_divider(self):
        # cur_VD=20000, bq feszultseg=12500 mV, DMM=12600 mV
        # new_VD = 20000 * 12600 / 12500 = 20160
        device = _StatefulCalDevice(
            cc_gain=0.47095, cc_delta=559500.0, voltage_mV=12500, voltage_divider=20000,
        )
        transport = _make_transport(device)
        ctrl = VoltageCalibrationController(transport)

        ctrl.start()
        assert ctrl.state == VoltageCalState.AWAITING_DMM_VOLTAGE
        ctrl.submit_dmm_voltage_mV(12600)

        assert ctrl.state == VoltageCalState.DONE
        snap = ctrl.snapshot()
        assert snap.old_voltage_divider == 20000
        assert snap.new_voltage_divider == 20160
        assert device.voltage_divider == 20160
        assert "seal" in device.calls
        assert "enter_cal" not in device.calls, (
            "a feszultseg-kalibracio NEM igenyel ENTER_CAL-t "
            "(Prog/TF1_basic.c bq_cal_vd_start() mintaja)"
        )


class TestVoltageCalibrationErrorPaths:
    def test_out_of_range_voltage_divider_fails_without_writing(self):
        """Review-kör 2 (2026-09-08), Gyári forrás F-4 (felhasználói
        jóváhagyással, 2026-09-09): korábban a képlet nyers eredményét
        `min(65535, ...)`-tal CSENDBEN 65535-re vágtuk, és a session
        DONE-t jelentett, miközben egy ilyen -- rossz VOLSEL-bit/soros-
        cellaszám ELŐFELTÉTEL-sérülésre utaló -- hibás Voltage Divider
        érték a chipre írva minden JÖVŐBELI feszültségmérést
        korrumpált volna. Mostantól ERROR, és NEM ír a chipre."""
        device = _StatefulCalDevice(voltage_mV=1000, voltage_divider=60000, cc_gain=1.0, cc_delta=500000.0)
        transport = _make_transport(device)
        ctrl = VoltageCalibrationController(transport)
        ctrl.start()
        ctrl.submit_dmm_voltage_mV(60000)  # 60000*60000/1000 = 3 600 000 -> tartományon kívül

        assert ctrl.state == VoltageCalState.ERROR
        assert "Voltage Divider" in ctrl.snapshot().fail_reason
        assert "write_voltage_divider" not in device.calls
        assert device.voltage_divider == 60000  # változatlan -- nem íródott
        assert "seal" in device.calls

    def test_zero_dmm_voltage_fails(self):
        device = _StatefulCalDevice(voltage_mV=12500, voltage_divider=20000, cc_gain=1.0, cc_delta=500000.0)
        transport = _make_transport(device)
        ctrl = VoltageCalibrationController(transport)
        ctrl.start()
        ctrl.submit_dmm_voltage_mV(0)
        assert ctrl.state == VoltageCalState.ERROR
        assert "voltage divider" not in ctrl.snapshot().fail_reason.lower()  # sanity: nem a VD-irasnal buktunk el

    def test_zero_bq_voltage_fails_without_writing(self):
        device = _StatefulCalDevice(voltage_mV=0, voltage_divider=20000, cc_gain=1.0, cc_delta=500000.0)
        transport = _make_transport(device)
        ctrl = VoltageCalibrationController(transport)
        ctrl.start()
        ctrl.submit_dmm_voltage_mV(12000)
        assert ctrl.state == VoltageCalState.ERROR
        assert "write_voltage_divider" not in device.calls

    def test_abort_before_submit_seals_and_marks_aborted(self):
        device = _StatefulCalDevice(voltage_mV=12500, voltage_divider=20000, cc_gain=1.0, cc_delta=500000.0)
        transport = _make_transport(device)
        ctrl = VoltageCalibrationController(transport)
        ctrl.start()
        ctrl.abort()
        assert ctrl.state == VoltageCalState.ABORTED
        assert "seal" in device.calls

    def test_abort_records_seal_cleanup_failure(self):
        """Review-kör 5, Architektúra A-1 (P1): a feszültség-kalibráció
        abort()-ja is attesztálja a SEAL takarítás hibáját (korábban
        `except Exception: pass`)."""
        device = _StatefulCalDevice(voltage_mV=12500, voltage_divider=20000, cc_gain=1.0, cc_delta=500000.0)
        transport = _make_transport(device, errors={"seal": ("METHOD_ERROR", "simulated seal failure")})
        ctrl = VoltageCalibrationController(transport)
        ctrl.start()
        ctrl.abort()
        assert ctrl.state == VoltageCalState.ABORTED
        failures = ctrl.snapshot().cleanup_failures
        assert any(f.startswith("SEAL_FAILED:") for f in failures)

    def test_fail_path_records_seal_cleanup_failure(self):
        device = _StatefulCalDevice(voltage_mV=12500, voltage_divider=20000, cc_gain=1.0, cc_delta=500000.0)
        transport = _make_transport(device, errors={"seal": ("METHOD_ERROR", "simulated seal failure")})
        ctrl = VoltageCalibrationController(transport)
        ctrl.start()
        ctrl.submit_dmm_voltage_mV(0)
        assert ctrl.state == VoltageCalState.ERROR
        failures = ctrl.snapshot().cleanup_failures
        assert any(f.startswith("SEAL_FAILED:") for f in failures)


class _OneSecondAverageCurrent:
    """[R5-B2] AverageCurrent()-double: a regiszter a szimulált óra szerint
    1 s-onként frissül (TI SLUSB55B Rev. B, p12: "It is updated every 1
    second"), `phase_s` eltolással. Minden olvasás idejét rögzíti. A
    szándékosan VÁLTOZÓ érték csak a külön frissítések megkülönböztetésére
    szolgál; stabil valós áramnál az azonos értékek helyesek."""

    def __init__(self, base_mA: int, phase_s: float) -> None:
        self.base_mA = base_mA
        self.phase_s = phase_s
        self.t_s = 0.0
        self.read_times: list[float] = []

    def __call__(self, params):
        self.read_times.append(self.t_s)
        return self.base_mA + math.floor(self.t_s + self.phase_s)


def _gain_ctrl_at_measuring(avg: _OneSecondAverageCurrent, device: _StatefulCalDevice):
    transport = _make_transport(device, average_current_mA=avg)
    ctrl = CcGainCalibrationController(transport)
    _run_cc_gain_offset_phase(ctrl)
    ctrl.submit_reference_current_mA(1000, stabilize_s=1.0)
    ctrl.advance(1.0)
    assert ctrl.state == CalState.MEASURING_GAIN
    return ctrl


def _production_tick(ctrl, avg, dt_s: float = 0.5) -> None:
    avg.t_s += dt_s
    ctrl.advance(dt_s)


class TestR5B2GainSamplesFromSeparateRegisterUpdates:
    """[regresszió, Claude-Codex loop R5 B2: R5-MET-1, DFAM-008; DFAM-043]
    a 4 AverageCurrent()-minta a produkciós 0,5 s-os tickkel (Prog/gui/
    bq_calibration_worker.py _DEFAULT_TICK_INTERVAL_MS=500) KÜLÖN 1 s-os
    regiszterfrissítésekből jön. HEAD (0,25 s): olvasás 0,5/1/1,5/2 s-nál,
    2-3 külön érték. Unit/mock szint; élő gain-validálás HIL_PENDING."""

    @pytest.mark.parametrize("phase_s", [0.0, 0.1, 0.3, 0.5, 0.7, 0.9])
    def test_four_reads_from_four_updates_mean_and_single_write(self, phase_s):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        avg = _OneSecondAverageCurrent(base_mA=980, phase_s=phase_s)
        ctrl = _gain_ctrl_at_measuring(avg, device)
        avg.t_s = 0.0
        for _ in range(40):
            if ctrl.state != CalState.MEASURING_GAIN:
                break
            _production_tick(ctrl, avg)

        assert ctrl.state == CalState.DONE
        assert avg.read_times == pytest.approx([1.5, 3.0, 4.5, 6.0])
        samples = ctrl.snapshot().measured_current_samples_mA
        assert len(samples) == GAIN_SAMPLE_COUNT
        assert len(set(samples)) == GAIN_SAMPLE_COUNT
        expected = [980 + math.floor(t + phase_s) for t in (1.5, 3.0, 4.5, 6.0)]
        assert list(samples) == expected
        k = 1000 / (sum(expected) / len(expected))
        snap = ctrl.snapshot()
        assert snap.new_cc_gain == pytest.approx(0.47095 * k)
        assert snap.new_cc_delta == pytest.approx(559500.0 * k)
        assert device.calls.count("write_calibration_gain_delta") == 1

    def test_abort_between_samples_cleans_up_without_gain_write(self):
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        avg = _OneSecondAverageCurrent(base_mA=980, phase_s=0.3)
        ctrl = _gain_ctrl_at_measuring(avg, device)
        avg.t_s = 0.0
        for _ in range(4):  # t = 2,0 s
            _production_tick(ctrl, avg)
        assert ctrl.state == CalState.MEASURING_GAIN
        assert len(ctrl.snapshot().measured_current_samples_mA) == 1
        n_before = len(device.calls)

        ctrl.abort()

        assert ctrl.state == CalState.ABORTED
        tail = device.calls[n_before:]
        assert tail[:2] == ["exit_cal", "seal"]
        assert "control_status" in tail[2:]
        assert "write_calibration_gain_delta" not in device.calls
        _production_tick(ctrl, avg)
        assert ctrl.state == CalState.ABORTED
        assert len(avg.read_times) == 1

    def test_jittered_measured_dt_reads_at_expected_times(self):
        """[R5-B2, Codex kötelező változtatás] A worker MÉRT monotonic
        dt_s-t ad át (Prog/gui/bq_calibration_worker.py), nem ideális
        0,5 s-ot. Ismételt [0.51, 0.51, 0.475] s lépésekkel az olvasási
        időpontok a minta utáni akkumulátor-nullázással
        [1.495, 2.990, 4.485, 5.980] s. Az elvárt időpontok kézzel
        rögzítettek, NEM a vizsgált konstansból számoltak: az 1,01 s-os
        köz [1.020, 2.515, ...], az 1,5 s-os [2.005, 4.010, ...] olvasást
        adna. Unit/mock szint; élő gain-validálás HIL_PENDING."""
        device = _StatefulCalDevice(cc_gain=0.47095, cc_delta=559500.0)
        avg = _OneSecondAverageCurrent(base_mA=980, phase_s=0.0)
        ctrl = _gain_ctrl_at_measuring(avg, device)
        avg.t_s = 0.0
        jitter_steps_s = [0.51, 0.51, 0.475]
        for i in range(60):
            if ctrl.state != CalState.MEASURING_GAIN:
                break
            _production_tick(ctrl, avg, jitter_steps_s[i % len(jitter_steps_s)])

        assert ctrl.state == CalState.DONE
        assert avg.read_times == pytest.approx([1.495, 2.990, 4.485, 5.980])
        expected = [980 + math.floor(t) for t in (1.495, 2.990, 4.485, 5.980)]
        samples = ctrl.snapshot().measured_current_samples_mA
        assert list(samples) == expected
        assert len(set(samples)) == GAIN_SAMPLE_COUNT
        k = 1000 / (sum(expected) / len(expected))
        snap = ctrl.snapshot()
        assert snap.new_cc_gain == pytest.approx(0.47095 * k)
        assert snap.new_cc_delta == pytest.approx(559500.0 * k)
        assert device.calls.count("write_calibration_gain_delta") == 1


class TestR16B6CurrentScaleGuard:
    """R16-energy-03: a fake DF-allapot uj transport-sessionben is megmarad."""

    @pytest.mark.parametrize("phase", ["start", "cc_offset", "save", "retry", "gain"])
    @pytest.mark.parametrize(
        "bad_config",
        ["x10", "rpc_error", "short", "long", "bad_hex", "null", "sealed_pattern"],
    )
    def test_bad_scale_blocks_next_mutation_and_cleans_up(self, phase, bad_config):
        # SLUSB55B Rev. B p20 Table 12: low byte bit7; p40 Table 20: SC64/off0.
        device = _StatefulCalDevice(
            cc_gain=0.1, cc_delta=30000.0, average_current_mA=100,
            control_status_word=0x2000,
        )
        transport = _make_transport(device)
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        if phase != "start":
            ctrl.confirm_battery_disconnected()
            assert ctrl.state == CalState.BOARD_OFFSET_RUNNING
        if phase in {"save", "retry", "gain"}:
            _advance_until_state(ctrl, {CalState.CC_OFFSET_RUNNING, CalState.ERROR})
            assert ctrl.state == CalState.CC_OFFSET_RUNNING
        if phase == "retry":
            _fail_method_n_times_then_succeed(transport, device, "cc_offset_save", 1)
            _advance_until_state(ctrl, {CalState.CC_OFFSET_SAVE_RETRY_WAIT, CalState.ERROR})
            assert ctrl.state == CalState.CC_OFFSET_SAVE_RETRY_WAIT
        if phase == "gain":
            _advance_until_state(ctrl, {CalState.AWAITING_REFERENCE_CURRENT, CalState.ERROR})
            assert ctrl.state == CalState.AWAITING_REFERENCE_CURRENT
            ctrl.submit_reference_current_mA(1000, stabilize_s=0.0)
            ctrl.advance(0.0)
            assert ctrl.state == CalState.MEASURING_GAIN

        before = len(transport.serial.written_requests)
        old_df = (device.cc_gain, device.cc_delta, device.cc_offset, device.board_offset)
        if bad_config == "x10":
            device.pack_configuration = 0x09E1
        elif bad_config == "rpc_error":
            transport.serial.errors["data_flash_block"] = ("METHOD_ERROR", "DF read failed")
        else:
            replies = {
                "short": "0961",
                "long": "0961" + "00" * 31,
                "bad_hex": "not hex",
                "null": None,
                "sealed_pattern": "00" * 21 + "ff" * 11,
            }
            transport.serial.results["data_flash_block"] = replies[bad_config]

        if phase == "start":
            ctrl.confirm_battery_disconnected()
        elif phase == "cc_offset":
            _advance_until_state(ctrl, {CalState.ERROR, CalState.CC_OFFSET_RUNNING})
        elif phase == "save":
            _advance_until_state(ctrl, {CalState.ERROR, CalState.AWAITING_REFERENCE_CURRENT})
        elif phase == "retry":
            ctrl.advance(CC_OFFSET_SAVE_RETRY_WAIT_S)
        else:
            _run_gain_measurement_ticks(ctrl)

        snap = ctrl.snapshot()
        assert snap.state == CalState.ERROR
        expected_error = "X10_UNSUPPORTED" if bad_config == "x10" else "DFREAD"
        assert snap.error.value == expected_error
        assert snap.fail_reason
        if bad_config == "x10":
            assert "Pack Configuration=0x09E1" in snap.fail_reason
        assert snap.cleanup_failures == ()
        requests = transport.serial.written_requests[before:]
        methods = [request["method"] for request in requests]
        forbidden = {
            "enter_cal", "board_offset_cal", "cc_offset_cal", "cc_offset_save",
            "write_calibration_gain_delta", "write_voltage_divider", "write_data_flash_block",
        }
        assert forbidden.isdisjoint(methods)
        assert methods[-3:] == ["exit_cal", "seal", "control_status"]
        assert (device.cc_gain, device.cc_delta, device.cc_offset, device.board_offset) == old_df
        count = len(transport.serial.written_requests)
        ctrl.advance(1000.0)
        assert len(transport.serial.written_requests) == count

    @pytest.mark.parametrize("pack_configuration", [0x0961, 0x8000])
    def test_non_x10_preserves_gain_and_persistent_df_across_sessions(self, pack_configuration):
        # 0x8000 != X10: a high byte bit7 nem tilthatja a kalibraciot.
        device = _StatefulCalDevice(
            cc_gain=0.1, cc_delta=30000.0, average_current_mA=100,
            control_status_word=0x2000, pack_configuration=pack_configuration,
        )
        first_transport = _make_transport(device)
        ctrl = CcGainCalibrationController(first_transport)
        _run_cc_gain_offset_phase(ctrl)
        ctrl.submit_reference_current_mA(1000, stabilize_s=0.0)
        ctrl.advance(0.0)
        _run_gain_measurement_ticks(ctrl)
        assert ctrl.state == CalState.DONE
        assert device.cc_gain == pytest.approx(1.0)
        assert device.cc_delta == pytest.approx(300000.0)
        saved_df = (device.cc_gain, device.cc_delta, device.cc_offset, device.board_offset)
        first_transport.disconnect()

        device.pack_configuration = 0x09E1
        second_transport = _make_transport(device)
        second = CcGainCalibrationController(second_transport)
        second.start()
        second.confirm_battery_disconnected()
        assert second.state == CalState.ERROR
        assert second.snapshot().error.value == "X10_UNSUPPORTED"
        assert second.snapshot().cleanup_failures == ()
        assert (device.cc_gain, device.cc_delta, device.cc_offset, device.board_offset) == saved_df
        assert [r["method"] for r in second_transport.serial.written_requests] == [
            "unseal", "full_access", "data_flash_block", "exit_cal", "seal", "control_status",
        ]

    @pytest.mark.parametrize("failed_cleanup", ["exit_cal", "seal", "control_status"])
    def test_scale_error_preserved_when_cleanup_fails(self, failed_cleanup):
        device = _StatefulCalDevice(
            cc_gain=0.1, cc_delta=30000.0, pack_configuration=0x09E1,
            control_status_word=0x2000,
        )
        transport = _make_transport(device, errors={failed_cleanup: ("METHOD_ERROR", "cleanup")})
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        ctrl.confirm_battery_disconnected()
        snap = ctrl.snapshot()
        assert snap.state == CalState.ERROR
        assert snap.error.value == "X10_UNSUPPORTED"
        expected_prefix = {
            "exit_cal": "EXIT_CAL_FAILED:",
            "seal": "SEAL_FAILED:",
            "control_status": "SEAL_VERIFY_FAILED:CC_GAIN_FAIL:",
        }[failed_cleanup]
        assert len(snap.cleanup_failures) == 1
        assert snap.cleanup_failures[0].startswith(expected_prefix)
        methods = [r["method"] for r in transport.serial.written_requests]
        assert methods.count("exit_cal") == methods.count("seal") == 1
        assert methods.count("control_status") == (0 if failed_cleanup == "seal" else 1)
        assert "enter_cal" not in methods


class TestR16B6F1AbortWindow:
    """DFAM-016: valódi CLI-belépés és RPC, kizárólag memóriabeli soros port."""

    @pytest.mark.parametrize("script", ["step1", "full_run"])
    @pytest.mark.parametrize("interrupt_at", ["unseal", "full_access", "data_flash_block"])
    @pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
    def test_interrupt_reseals_before_disconnect(
        self, monkeypatch, script, interrupt_at, exception_type,
    ):
        from Prog.tools.bq_cal_diagnostics import bq_cal_phase1_full_run as full_run
        from Prog.tools.bq_cal_diagnostics import bq_cal_phase1_step1 as step1

        module = {"step1": step1, "full_run": full_run}[script]
        device = _StatefulCalDevice(
            cc_gain=0.1, cc_delta=30000.0, control_status_word=0x6000,
        )
        transport = _make_transport(device)
        serial = transport.serial

        def unseal(_params):
            device.control_status_word &= ~0x2000
            return {"status": "ok"}

        def full_access(_params):
            device.control_status_word &= ~0x4000
            return {"status": "ok"}

        def seal(_params):
            device.control_status_word |= 0x6000
            return {"status": "ok"}

        serial.results.update(unseal=unseal, full_access=full_access, seal=seal)
        original_readline = serial.readline
        interruption = exception_type("R16-B6F1 megszakítás az RPC-válasz alatt")
        observed_status = []

        def readline():
            # A kérés már eljutott a fake chiphez; csak a válasz szakad meg.
            if serial.written_requests[-1]["method"] == interrupt_at:
                observed_status.append(device.control_status_word)
                raise interruption
            return original_readline()

        monkeypatch.setattr(serial, "readline", readline)
        monkeypatch.setattr(transport, "connect", lambda *_a, **_k: None)
        monkeypatch.setattr(module, "PicoBQTransport", lambda **_k: transport)
        monkeypatch.setattr(
            module.time, "sleep", lambda _seconds: pytest.fail("Váratlan várakozás megszakítás után"),
        )
        # A teljes futás naplója és jelzőfájl-kezelése sem írhat fájlt.
        monkeypatch.setattr(full_run, "log", lambda _message: None)
        monkeypatch.setattr(full_run.os.path, "exists", lambda _path: False)
        controllers = []

        def make_controller(actual_transport):
            ctrl = CcGainCalibrationController(actual_transport)
            controllers.append(ctrl)
            return ctrl

        monkeypatch.setattr(module, "CcGainCalibrationController", make_controller)

        with pytest.raises(exception_type) as caught:
            module.main()

        assert caught.value is interruption
        assert len(observed_status) == 1
        assert observed_status[0] & 0x2000 == 0
        if interrupt_at != "unseal":
            assert observed_status[0] & 0x4000 == 0
        snap = controllers[0].snapshot()
        assert snap.state == CalState.ABORTED
        assert snap.cleanup_failures == ()
        methods = [request["method"] for request in serial.written_requests]
        assert methods[-4:] == [interrupt_at, "exit_cal", "seal", "control_status"]
        forbidden = {
            "enter_cal", "board_offset_cal", "cc_offset_cal", "cc_offset_save",
            "write_calibration_gain_delta",
        }
        assert forbidden.isdisjoint(methods)
        assert device.control_status_word & 0x2000
        assert serial.closed and transport.serial is None


class TestR16B6bVoltageAbortWindow:
    """DFAM-016: megszakadt VD-indulás, valódi RPC és memóriabeli soros port."""

    @pytest.mark.parametrize("entry", ["cli", "controller"])
    @pytest.mark.parametrize("interrupt_at", ["unseal", "full_access"])
    @pytest.mark.parametrize("timing", ["before", "after"])
    @pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("cleanup_failure", [None, "seal", "control_status", "ss_clear"])
    def test_interrupted_start_reseals_or_reports_cleanup_failure(
        self, monkeypatch, entry, interrupt_at, timing, exception_type, cleanup_failure,
    ):
        from Prog.tools.bq_cal_diagnostics import bq_cal_voltage_divider_run as vd_run

        device = _StatefulCalDevice(
            cc_gain=0.1, cc_delta=30000.0, control_status_word=0x6000,
        )
        transport = _make_transport(device)
        serial = transport.serial

        def unseal(_params):
            device.control_status_word &= ~0x2000
            return {"status": "ok"}

        def full_access(_params):
            device.control_status_word &= ~0x4000
            return {"status": "ok"}

        def seal(_params):
            if cleanup_failure != "ss_clear":
                device.control_status_word |= 0x6000
            else:
                device.control_status_word &= ~0x2000
            return {"status": "ok"}

        serial.results.update(unseal=unseal, full_access=full_access, seal=seal)
        if cleanup_failure in ("seal", "control_status"):
            serial.errors[cleanup_failure] = ("METHOD_ERROR", "R16-B6b takarítási hiba")
        original_write = serial.write
        interruption = exception_type("R16-B6b megszakított VD-indulás")
        observed_status = []
        sent_before_cleanup = []

        def write(data):
            method = json.loads(data.decode("utf-8"))["method"]
            if method != interrupt_at or observed_status:
                return original_write(data)
            # Előtte: nincs elküldött kérés. Utána: a fake chip már végrehajtotta.
            if timing == "after":
                original_write(data)
            observed_status.append(device.control_status_word)
            sent_before_cleanup.extend(r["method"] for r in serial.written_requests)
            raise interruption

        monkeypatch.setattr(serial, "write", write)
        # A CLI connectje sem nyithat valódi portot; a transportot injektáljuk.
        monkeypatch.setattr(transport, "connect", lambda *_a, **_k: None)
        monkeypatch.setattr(vd_run, "PicoBQTransport", lambda **_k: transport)
        controllers = []

        def make_controller(actual_transport):
            ctrl = VoltageCalibrationController(actual_transport)
            controllers.append(ctrl)
            return ctrl

        monkeypatch.setattr(vd_run, "VoltageCalibrationController", make_controller)
        if entry == "cli":
            with pytest.raises(exception_type) as caught:
                vd_run.main()
            ctrl = controllers[0]
        else:
            ctrl = VoltageCalibrationController(transport)
            with pytest.raises(exception_type) as caught:
                ctrl.start()
            assert ctrl.state == VoltageCalState.UNSEALING
            # Megszakadt indulás után se új UNSEAL, se korai kalibráció ne induljon.
            sent = list(serial.written_requests)
            with pytest.raises(RuntimeError):
                ctrl.start()
            with pytest.raises(RuntimeError):
                ctrl.submit_dmm_voltage_mV(23640)
            assert serial.written_requests == sent
            ctrl.abort()

        assert caught.value is interruption
        expected_status = 0x6000 if interrupt_at == "unseal" else 0x4000
        if timing == "after":
            expected_status &= ~(0x2000 if interrupt_at == "unseal" else 0x4000)
        assert observed_status == [expected_status]
        prefix = ["capabilities"] if entry == "cli" else []
        if interrupt_at == "full_access":
            prefix.append("unseal")
        if timing == "after":
            prefix.append(interrupt_at)
        assert sent_before_cleanup == prefix
        cleanup = ["seal"] if cleanup_failure == "seal" else ["seal", "control_status"]
        methods = [r["method"] for r in serial.written_requests]
        assert methods == prefix + cleanup
        snap = ctrl.snapshot()
        assert snap.state == VoltageCalState.ABORTED
        assert snap.fail_reason == ""
        assert snap.new_voltage_divider is None
        if cleanup_failure is None:
            assert snap.cleanup_failures == ()
            assert device.control_status_word & 0x2000
        else:
            failure_prefix = {
                "seal": "SEAL_FAILED:",
                "control_status": "SEAL_VERIFY_FAILED:VOLTAGE_CAL_ABORT:",
                "ss_clear": "SEAL_NOT_VERIFIED:VOLTAGE_CAL_ABORT:",
            }[cleanup_failure]
            assert len(snap.cleanup_failures) == 1
            assert snap.cleanup_failures[0].startswith(failure_prefix)
        notes = getattr(caught.value, "__notes__", [])
        if entry == "cli" and cleanup_failure is not None:
            assert len(notes) == 1
            assert "nem igazolt" in notes[0]
            assert snap.cleanup_failures[0] in notes[0]
        else:
            assert notes == []
        # A terminális abort idempotens, nem küld újabb RPC-t.
        ctrl.abort()
        assert [r["method"] for r in serial.written_requests] == methods
        if entry == "cli":
            assert serial.closed and transport.serial is None
        else:
            assert not serial.closed and transport.serial is serial


class TestR16B6cInstrumentErrorCleanup:
    """DFAM-016: InstrumentError és takarítás, valódi RPC csak FakePicoSerial felé."""

    @pytest.mark.parametrize("entry", ["controller", "vd", "full_run"])
    @pytest.mark.parametrize("fail_at", ["unseal", "full_access"])
    @pytest.mark.parametrize("failure_mode", ["rpc_error", "timeout"])
    @pytest.mark.parametrize("cleanup_failure", [None, "seal", "control_status", "ss_clear"])
    def test_start_error_keeps_reason_and_reports_cleanup(
        self, monkeypatch, capsys, entry, fail_at, failure_mode, cleanup_failure,
    ):
        from Prog.tools.bq_cal_diagnostics import bq_cal_phase1_full_run as full_run
        from Prog.tools.bq_cal_diagnostics import bq_cal_voltage_divider_run as vd_run

        device = _StatefulCalDevice(
            cc_gain=0.1, cc_delta=30000.0, control_status_word=0x6000,
        )
        transport = _make_transport(device)
        transport.max_retries = 2
        serial = transport.serial

        def unseal(_params):
            device.control_status_word &= ~0x2000
            return {"status": "ok"}

        def full_access(_params):
            device.control_status_word &= ~0x4000
            return {"status": "ok"}

        def seal(_params):
            if cleanup_failure == "ss_clear":
                device.control_status_word &= ~0x2000
            else:
                device.control_status_word |= 0x6000
            return {"status": "ok"}

        serial.results.update(unseal=unseal, full_access=full_access, seal=seal)
        if failure_mode == "rpc_error":
            serial.errors[fail_at] = ("METHOD_ERROR", "R16-B6c start failure")
            expected_reason = (
                f"PicoBQTransport: Pico RPC hiba method={fail_at!r} "
                "[METHOD_ERROR]: R16-B6c start failure"
            )
        else:
            original_readline = serial.readline

            def readline():
                # Csak a hibás RPC válasza vész el; a takarítás is valódi RPC-t használ.
                if serial.written_requests[-1]["method"] == fail_at:
                    return b""
                return original_readline()

            monkeypatch.setattr(serial, "readline", readline)
            expected_reason = f"PicoBQTransport: nincs v\u00e1lasz (timeout) method={fail_at!r}"
        if cleanup_failure in ("seal", "control_status"):
            serial.errors[cleanup_failure] = ("METHOD_ERROR", "R16-B6c cleanup failure")

        monkeypatch.setattr(transport, "connect", lambda *_a, **_k: None)
        if entry == "controller":
            ctrl = VoltageCalibrationController(transport)
            ctrl.start()
        else:
            module = vd_run if entry == "vd" else full_run
            controller_cls = (
                VoltageCalibrationController if entry == "vd" else CcGainCalibrationController
            )
            controllers = []

            def make_controller(actual_transport):
                ctrl = controller_cls(actual_transport)
                controllers.append(ctrl)
                return ctrl

            monkeypatch.setattr(module, "PicoBQTransport", lambda **_k: transport)
            monkeypatch.setattr(
                module, controller_cls.__name__, make_controller,
            )
            if entry == "full_run":
                monkeypatch.setattr(full_run, "log", print)
                real_exists = full_run.os.path.exists
                monkeypatch.setattr(
                    full_run.os.path, "exists",
                    lambda path: False if path == full_run.FLAG_FILE else real_exists(path),
                )
                monkeypatch.setattr(
                    full_run.time, "sleep",
                    lambda _s: pytest.fail("Unexpected wait after start failure"),
                )
            assert module.main() is None
            assert len(controllers) == 1
            ctrl = controllers[0]

        snap = ctrl.snapshot()
        expected_state = CalState.ERROR if entry == "full_run" else VoltageCalState.ERROR
        assert ctrl.state == snap.state == expected_state
        assert snap.fail_reason == expected_reason
        if entry == "full_run":
            # R16-cal-errcode-FA: a két belépési hiba külön kódot őriz.
            expected_error = CalErrorCode.FA if fail_at == "full_access" else CalErrorCode.UNSEAL
            assert snap.error == expected_error
        else:
            assert snap.dmm_voltage_mV is None
            assert snap.new_voltage_divider is None
        prefix = [] if entry == "controller" else ["capabilities"]
        if fail_at == "full_access":
            prefix.append("unseal")
        prefix += [fail_at] * (2 if failure_mode == "timeout" else 1)
        cleanup = ["exit_cal"] if entry == "full_run" else []
        cleanup += ["seal"] if cleanup_failure == "seal" else ["seal", "control_status"]
        methods = [r["method"] for r in serial.written_requests]
        assert methods == prefix + cleanup
        if cleanup_failure is None:
            assert snap.cleanup_failures == ()
            assert device.control_status_word & 0x2000
        else:
            context = "CC_GAIN_FAIL" if entry == "full_run" else "VOLTAGE_CAL_FAIL"
            expected_prefix = {
                "seal": "SEAL_FAILED:",
                "control_status": f"SEAL_VERIFY_FAILED:{context}:",
                "ss_clear": f"SEAL_NOT_VERIFIED:{context}:",
            }[cleanup_failure]
            assert len(snap.cleanup_failures) == 1
            assert snap.cleanup_failures[0].startswith(expected_prefix)
        if entry != "controller":
            output = capsys.readouterr().out
            assert expected_reason in output
            assert f"cleanup_failures: {snap.cleanup_failures}" in output
            assert output.count("nem igazolt") == (0 if cleanup_failure is None else 1)
            assert serial.closed and transport.serial is None
        else:
            assert not serial.closed and transport.serial is serial
        ctrl.abort()
        assert ctrl.snapshot() == snap
        assert [r["method"] for r in serial.written_requests] == methods
        if entry == "controller":
            transport.disconnect()
            assert serial.closed and transport.serial is None


class TestR16B6cF1TerminalCleanupDiagnostics:
    """DFAM-016: terminális CLI-ágak, valódi transport és memóriabeli soros port."""

    @pytest.mark.parametrize(
        ("case", "cleanup_failure"),
        [
            (case, cleanup_failure)
            for case in (
                "step1_entry", "step1_bca", "step1_save",
                "full_bca", "full_save", "full_timeout",
                "full_gain_error", "full_done", "vd_error", "vd_done",
            )
            for cleanup_failure in (None, "seal", "control_status", "ss_clear")
            if not (case.endswith("_done") and cleanup_failure == "seal")
        ],
    )
    def test_terminal_output_matches_cleanup_evidence(
        self, monkeypatch, capsys, case, cleanup_failure,
    ):
        from io import StringIO

        from Prog.tools.bq_cal_diagnostics import bq_cal_phase1_full_run as full_run
        from Prog.tools.bq_cal_diagnostics import bq_cal_phase1_step1 as step1
        from Prog.tools.bq_cal_diagnostics import bq_cal_voltage_divider_run as vd_run

        module = vd_run if case.startswith("vd_") else (
            step1 if case.startswith("step1_") else full_run
        )
        is_vd = module is vd_run
        device = _StatefulCalDevice(
            cc_gain=0.1, cc_delta=30000.0, control_status_word=0x6000,
        )
        transport = _make_transport(device)
        serial = transport.serial

        def unseal(_params):
            device.control_status_word &= ~0x2000
            return {"status": "ok"}

        def full_access(_params):
            device.control_status_word &= ~0x4000
            return {"status": "ok"}

        def enter_cal(_params):
            device.control_status_word |= 0x1000
            return {"status": "ok"}

        def exit_cal(_params):
            device.control_status_word &= ~0x1000
            return {"status": "ok"}

        def seal(_params):
            if cleanup_failure == "ss_clear":
                device.control_status_word &= ~0x2000
            else:
                device.control_status_word |= 0x6000
            return {"status": "ok"}

        serial.results.update(
            unseal=unseal, full_access=full_access, enter_cal=enter_cal,
            exit_cal=exit_cal, seal=seal,
        )
        primary_method = {
            "step1_entry": "unseal",
            "step1_bca": "control_status", "full_bca": "control_status",
            "step1_save": "cc_offset_save", "full_save": "cc_offset_save",
            "full_gain_error": "average_current_mA", "vd_error": "voltage_mV",
        }.get(case)
        if primary_method not in (None, "control_status"):
            serial.errors[primary_method] = ("METHOD_ERROR", "R16-B6cF1 fo hiba")
        original_write = serial.write
        in_cleanup = False

        def write(data):
            nonlocal in_cleanup
            method = json.loads(data.decode("utf-8"))["method"]
            if method in ("exit_cal", "seal"):
                in_cleanup = True
            if method == "seal" and cleanup_failure == "seal":
                serial.errors["seal"] = ("METHOD_ERROR", "R16-B6cF1 cleanup hiba")
            if method == "control_status":
                serial.errors.pop("control_status", None)
                if in_cleanup and cleanup_failure == "control_status":
                    serial.errors[method] = ("METHOD_ERROR", "R16-B6cF1 cleanup hiba")
                elif not in_cleanup and case.endswith("_bca") and any(
                    r["method"] == "board_offset_cal" for r in serial.written_requests
                ):
                    # A step1 kezdeti olvasása még sikeres; csak a BCA-poll hibás.
                    serial.errors[method] = ("METHOD_ERROR", "R16-B6cF1 fo hiba")
            return original_write(data)

        monkeypatch.setattr(serial, "write", write)
        monkeypatch.setattr(transport, "connect", lambda *_a, **_k: None)
        monkeypatch.setattr(module, "PicoBQTransport", lambda **_k: transport)
        controllers = []
        controller_cls = VoltageCalibrationController if is_vd else CcGainCalibrationController

        def make_controller(actual_transport):
            ctrl = controller_cls(actual_transport)
            controllers.append(ctrl)
            return ctrl

        monkeypatch.setattr(module, controller_cls.__name__, make_controller)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            assert len(sleeps) <= 1000, "A CLI nem érte el a terminális ágat"

        if not is_vd:
            monkeypatch.setattr(module.time, "sleep", sleep)
        if module is full_run:
            monkeypatch.setattr(full_run, "log", print)
            real_exists = full_run.os.path.exists
            flag_checks = []

            def exists(path):
                if path != full_run.FLAG_FILE:
                    return real_exists(path)
                flag_checks.append(path)
                return len(flag_checks) > 1 and case in ("full_gain_error", "full_done")

            def open_flag(path, mode, *, encoding):
                assert (path, mode, encoding) == (full_run.FLAG_FILE, "r", "utf-8")
                return StringIO("1000")

            monkeypatch.setattr(full_run.os.path, "exists", exists)
            monkeypatch.setattr(full_run, "open", open_flag, raising=False)

        assert module.main() is None
        assert len(controllers) == 1
        ctrl = controllers[0]
        snap = ctrl.snapshot()
        expected_state = (
            VoltageCalState.DONE if case == "vd_done" else
            VoltageCalState.ERROR if is_vd else
            CalState.DONE if case == "full_done" else
            CalState.ABORTED if case == "full_timeout" else CalState.ERROR
        )
        assert ctrl.state == snap.state == expected_state
        if not is_vd:
            expected_error = (
                CalErrorCode.UNSEAL if case == "step1_entry" else
                CalErrorCode.DFWRITE if case.endswith("_save") else
                CalErrorCode.NONE if case in ("full_timeout", "full_done") else
                CalErrorCode.TRANSPORT
            )
            assert snap.error == expected_error
        if primary_method is not None:
            expected_reason = (
                f"PicoBQTransport: Pico RPC hiba method={primary_method!r} "
                "[METHOD_ERROR]: R16-B6cF1 fo hiba"
            )
            assert snap.fail_reason == expected_reason
        else:
            assert snap.fail_reason == ""

        methods = [r["method"] for r in serial.written_requests]
        if case.endswith("_save"):
            assert methods.count("cc_offset_save") == 4
            assert snap.cc_offset_save_retries_used == 3
        elif case.endswith("_bca"):
            assert methods.count("board_offset_cal") == 1
            assert "cc_offset_cal" not in methods
        if case == "full_timeout":
            assert sleeps.count(2.0) == 450
        if case == "vd_done":
            assert methods.count("write_voltage_divider") == 1
        if case == "full_done":
            assert methods.count("write_calibration_gain_delta") == 1
        cleanup_tail = [] if is_vd else ["exit_cal"]
        cleanup_tail += ["seal"] if cleanup_failure == "seal" else ["seal", "control_status"]
        assert methods[-len(cleanup_tail):] == cleanup_tail
        assert methods.count("seal") == 1
        context = (
            "VOLTAGE_CAL_DONE" if case == "vd_done" else
            "VOLTAGE_CAL_FAIL" if is_vd else
            "CC_GAIN_DONE" if case == "full_done" else
            "CC_GAIN_ABORT" if case == "full_timeout" else "CC_GAIN_FAIL"
        )
        if cleanup_failure is None:
            assert snap.cleanup_failures == ()
            assert device.control_status_word & 0x2000
        else:
            expected_prefix = {
                "seal": "SEAL_FAILED:",
                "control_status": f"SEAL_VERIFY_FAILED:{context}:",
                "ss_clear": f"SEAL_NOT_VERIFIED:{context}:",
            }[cleanup_failure]
            assert len(snap.cleanup_failures) == 1
            assert snap.cleanup_failures[0].startswith(expected_prefix)

        output = capsys.readouterr().out
        separator = " = " if module is step1 and case != "step1_entry" else (
            "=" if case == "full_timeout" else ": "
        )
        assert output.count(f"cleanup_failures{separator}{snap.cleanup_failures}") == 1
        warning = "FIGYELEM: a BQ visszazarasa nem igazolt."
        assert output.count(warning) == int(cleanup_failure is not None)
        assert output.count("nem igazolt") == int(cleanup_failure is not None)
        if is_vd:
            assert output.count("(a chip most SEALED") == int(
                case == "vd_done" and cleanup_failure is None
            )
        assert serial.closed and transport.serial is None
        ctrl.abort()
        assert ctrl.snapshot() == snap
        assert [r["method"] for r in serial.written_requests] == methods
