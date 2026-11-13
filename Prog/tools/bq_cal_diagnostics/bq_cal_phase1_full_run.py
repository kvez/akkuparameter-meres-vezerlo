import sys
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.bq_gauge_calibration import (
    CcGainCalibrationController,
    CalState,
    GAIN_STABILIZE_S,
)

PORT = "COM10"
SCRATCH_DIR = r"C:\Users\KS\AppData\Local\Temp\claude\C--Users-KS-Desktop-CLI-Akkuteszter\e21da3ce-49d4-4daf-9c2f-6df6f7c75ff3\scratchpad"
FLAG_FILE = os.path.join(SCRATCH_DIR, "gain_ready.txt")
LOG_FILE = os.path.join(SCRATCH_DIR, "bq_cal_phase1_full_run.log")


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    if os.path.exists(FLAG_FILE):
        os.remove(FLAG_FILE)

    transport = PicoBQTransport(max_retries=2)
    log("=== BQ kalibracio Phase 1, TELJES folytonos futtatas (offset ujra 0A-n + gain) ===")
    transport.connect(PORT, timeout=2.0)
    ctrl = None
    try:
        transport.negotiate()
        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        log(f"ctrl.state = {ctrl.state}")

        t0 = time.monotonic()
        ctrl.confirm_battery_disconnected()
        log(f"ctrl.state = {ctrl.state} (t={time.monotonic()-t0:.1f}s)")

        if ctrl.state == CalState.ERROR:
            snap = ctrl.snapshot()
            log(f"HIBA a belepesnel: {snap.error} -- {snap.fail_reason}")
            log(f"cleanup_failures: {snap.cleanup_failures}")
            if snap.cleanup_failures:
                log("FIGYELEM: a BQ visszazarasa nem igazolt.")
            return

        while ctrl.state not in (CalState.AWAITING_REFERENCE_CURRENT, CalState.ERROR):
            time.sleep(0.5)
            ctrl.advance(0.5)
            elapsed = time.monotonic() - t0
            log(f"  [t={elapsed:5.1f}s] state={ctrl.state}")

        snap = ctrl.snapshot()
        log(
            f"Offset fazis vege: state={ctrl.state} cc_offset={snap.cc_offset} "
            f"board_offset={snap.board_offset} error={snap.error} "
            f"fail_reason={snap.fail_reason!r} cc_offset_save_retries_used="
            f"{snap.cc_offset_save_retries_used}"
        )
        if ctrl.state != CalState.AWAITING_REFERENCE_CURRENT:
            log("Nem sikerult elerni AWAITING_REFERENCE_CURRENT-et, megallok.")
            log(f"cleanup_failures: {snap.cleanup_failures}")
            if snap.cleanup_failures:
                log("FIGYELEM: a BQ visszazarasa nem igazolt.")
            return

        log(
            f"VARAKOZAS a referencia-aramra: csatlakoztasd a ~1A terhelest, majd "
            f"hozd letre ezt a fajlt a DMM-mert aram (mA, egesz szam) tartalmaval: "
            f"{FLAG_FILE}"
        )
        waited = 0.0
        while not os.path.exists(FLAG_FILE):
            time.sleep(2.0)
            waited += 2.0
            if waited >= 900.0:
                log("TIMEOUT: 15 percig nem erkezett jelzofajl -- abort().")
                ctrl.abort()
                snap = ctrl.snapshot()
                log(f"abort utani state={ctrl.state} cleanup_failures={snap.cleanup_failures}")
                if snap.cleanup_failures:
                    log("FIGYELEM: a BQ visszazarasa nem igazolt.")
                return

        with open(FLAG_FILE, "r", encoding="utf-8") as f:
            mA_str = f.read().strip()
        mA = int(mA_str)
        log(f"Jelzofajl beolvasva: reference_current_mA = {mA}")

        ctrl.submit_reference_current_mA(mA)
        log(f"ctrl.state = {ctrl.state} -> GAIN_STABILIZING (~{GAIN_STABILIZE_S:.0f}s)")

        t1 = time.monotonic()
        last_logged_bucket = -1
        while ctrl.state not in (CalState.DONE, CalState.ERROR):
            time.sleep(0.5)
            ctrl.advance(0.5)
            elapsed = time.monotonic() - t1
            bucket = int(elapsed // 15)
            if bucket != last_logged_bucket:
                last_logged_bucket = bucket
                log(f"  [t={elapsed:5.1f}s] state={ctrl.state}")

        snap = ctrl.snapshot()
        log("=== VEGEREDMENY ===")
        log(f"allapot: {ctrl.state}")
        log(f"error: {snap.error} fail_reason: {snap.fail_reason!r}")
        log(f"reference_current_mA: {snap.reference_current_mA}")
        log(f"measured_current_samples_mA: {snap.measured_current_samples_mA}")
        log(f"measured_current_mA (atlag): {snap.measured_current_mA}")
        log(f"old_cc_gain: {snap.old_cc_gain}  old_cc_delta: {snap.old_cc_delta}")
        log(f"new_cc_gain: {snap.new_cc_gain}  new_cc_delta: {snap.new_cc_delta}")
        log(f"cleanup_failures: {snap.cleanup_failures}")
        if snap.cleanup_failures:
            log("FIGYELEM: a BQ visszazarasa nem igazolt.")
    except BaseException as exc:
        # R6-B4 (R6-architecture-01, DFAM-016): rendellenes kilepeskor (pl.
        # Ctrl+C, BrokenPipe, hibas jelzofajl) a controller abort()-ja
        # (az UNSEAL megkiserlesetol EXIT_CAL, SEAL, SS-ellenorzes) a disconnect
        # ELOTT fut, majd az eredeti kivetel propagal. Az abort Exception-
        # hibaja note-kent csatolodik, nem fedi el az eredetit. Az abort a
        # SEAL/EXIT_CAL/SS-ellenorzes hibait nem dobja, csak a snapshot
        # cleanup_failures-ebe rogziti: ha az nem ures, az is note-kent
        # csatolodik (a BQ visszazarasa nem igazolt). Egy abort
        # kozbeni ujabb BaseException (pl. masodik Ctrl+C) viszont propagal,
        # az eredeti csak __context__-kent marad meg: teljes kivetel-
        # megorzesi garancia NINCS.
        if ctrl is not None:
            try:
                ctrl.abort()
                cleanup_failures = ctrl.snapshot().cleanup_failures
            except Exception as abort_exc:  # noqa: BLE001 - note-kent csatolva, l. fent
                exc.add_note(f"R6-B4: controller abort() hiba: {abort_exc!r}")
            else:
                if cleanup_failures:
                    exc.add_note(
                        "R6-B4: a BQ visszazarasa (SEAL/EXIT_CAL) nem igazolt: "
                        + "; ".join(cleanup_failures)
                    )
        raise
    finally:
        transport.disconnect()
        try:
            log("transport lezarva.")
        except Exception:  # noqa: BLE001, S110 - R6-B4: a zaro naplosor
            pass  # hibaja nem fedhet el egy propagalo kivetelt


if __name__ == "__main__":
    main()
