import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.bq_gauge_calibration import CcGainCalibrationController, CalState

PORT = "COM10"


def ts():
    return datetime.now(timezone.utc).isoformat()


def main():
    transport = PicoBQTransport(max_retries=2)
    print(f"=== BQ kalibracio, 1. fazis, 1. lepes (offset) === {ts()}")
    transport.connect(PORT, timeout=2.0)
    ctrl = None
    try:
        transport.negotiate()

        # Fuggetlen, elozetes ellenorzes: a chip meg SEALED-e, mielott hozzakezdunk.
        from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
        from Prog.src.bq_diagnostics import decode_control_status
        driver = Bq34z110ReadOnlyDriver(transport=transport)
        cs0 = decode_control_status(driver.read_control_status())
        print(f"  kezdeti allapot: SS={int(cs0.ss)} FAS={int(cs0.fas)} CALMODE={int(cs0.calmode)}")

        ctrl = CcGainCalibrationController(transport)
        ctrl.start()
        print(f"  ctrl.state = {ctrl.state}")

        print("  -> confirm_battery_disconnected() (unseal+full_access+enter_cal+board_offset_cal)...")
        t0 = time.monotonic()
        ctrl.confirm_battery_disconnected()
        print(f"  ctrl.state = {ctrl.state}  (t={time.monotonic()-t0:.1f}s)")

        if ctrl.state == CalState.ERROR:
            snap = ctrl.snapshot()
            print(f"  HIBA: {snap.error} -- {snap.fail_reason}")
            print(f"  cleanup_failures: {snap.cleanup_failures}")
            if snap.cleanup_failures:
                print("FIGYELEM: a BQ visszazarasa nem igazolt.")
            return

        # Board Offset (~2s) + CC Offset (~20s) automatikus varakozassal.
        while ctrl.state not in (CalState.AWAITING_REFERENCE_CURRENT, CalState.ERROR):
            time.sleep(0.5)
            ctrl.advance(0.5)
            elapsed = time.monotonic() - t0
            print(f"  [t={elapsed:5.1f}s] state={ctrl.state}")

        snap = ctrl.snapshot()
        print()
        print("=== EREDMENY (1. lepes vege) ===")
        print(f"  vegso allapot: {ctrl.state}")
        print(f"  cc_offset     = {snap.cc_offset}")
        print(f"  board_offset  = {snap.board_offset}")
        print(f"  error         = {snap.error}")
        print(f"  fail_reason   = {snap.fail_reason!r}")
        print(f"  cleanup_failures = {snap.cleanup_failures}")
        if snap.cleanup_failures:
            print("FIGYELEM: a BQ visszazarasa nem igazolt.")

        if ctrl.state == CalState.AWAITING_REFERENCE_CURRENT:
            # Fuggetlen, MASODIK visszaolvasas -- ne csak a state-machine
            # sajat, egyszeri olvasasat higgyuk el, kerdezzuk le megegyszer
            # kozvetlenul a transporton keresztul, hogy stabil-e az ertek.
            data2 = transport.read_calibration_data()
            print()
            print("  Fuggetlen masodik olvasas (read_calibration_data()):")
            print(f"    {data2}")
            consistent = (
                data2.get("cc_offset") == snap.cc_offset
                and data2.get("board_offset") == snap.board_offset
            )
            print(f"  Konzisztens az elso olvasassal -> {consistent}")

            cs1 = decode_control_status(driver.read_control_status())
            print(f"  Chip allapot MOST (varjuk UNSEALED, CALMODE aktiv): SS={int(cs1.ss)} FAS={int(cs1.fas)} CALMODE={int(cs1.calmode)}")
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


if __name__ == "__main__":
    main()
