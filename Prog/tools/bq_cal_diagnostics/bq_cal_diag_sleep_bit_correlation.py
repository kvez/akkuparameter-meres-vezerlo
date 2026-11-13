import sys
import time
sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"
N_POLLS = 40
POLL_GAP_S = 0.15


def main():
    transport = PicoBQTransport(max_retries=1)
    print("=== Diagnosztika: SLEEP/FULLSLEEP bit korrelacio I2C hibakkal (CSAK OLVASAS) ===")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        cs0 = decode_control_status(driver.read_control_status())
        print(f"kezdeti allapot: SS={int(cs0.ss)} FAS={int(cs0.fas)} CALMODE={int(cs0.calmode)} "
              f"SLEEP={int(cs0.sleep)} FULLSLEEP={int(cs0.fullsleep)}")

        print("-> unseal + full_access + enter_cal + board_offset_cal (2s) + cc_offset_cal (varakozas CCA torlesere)...")
        transport.unseal(confirm=True)
        transport.full_access(confirm=True)
        transport.enter_cal(confirm=True)
        transport.board_offset_cal(confirm=True)
        time.sleep(2.0)
        transport.cc_offset_cal(confirm=True)
        t0 = time.monotonic()
        for _ in range(160):
            time.sleep(0.25)
            cs = decode_control_status(driver.read_control_status())
            if not cs.cca:
                print(f"  CCA torolve t={time.monotonic()-t0:.2f}s-nal")
                break
        else:
            print("  FIGYELEM: CCA nem torlodott 40s alatt!")

        print()
        print(f"-> {N_POLLS}x control_status() gyors, CSAK OLVASO poll, {POLL_GAP_S}s resssel "
              f"-- SLEEP/FULLSLEEP bit + siker/hiba korrelacio...")
        n_ok = 0
        n_fail = 0
        n_sleep_seen = 0
        n_fullsleep_seen = 0
        for i in range(1, N_POLLS + 1):
            t_call0 = time.monotonic()
            try:
                cs = decode_control_status(driver.read_control_status())
                elapsed_ms = (time.monotonic() - t_call0) * 1000.0
                n_ok += 1
                flag = ""
                if cs.sleep:
                    n_sleep_seen += 1
                    flag += " SLEEP=1"
                if cs.fullsleep:
                    n_fullsleep_seen += 1
                    flag += " FULLSLEEP=1"
                print(f"  #{i:2d}: OK  ({elapsed_ms:6.1f} ms) CALMODE={int(cs.calmode)}{flag}")
            except Exception as exc:
                elapsed_ms = (time.monotonic() - t_call0) * 1000.0
                n_fail += 1
                print(f"  #{i:2d}: HIBA ({elapsed_ms:6.1f} ms) -- {exc}")
            time.sleep(POLL_GAP_S)

        print()
        print("=== OSSZESITES ===")
        print(f"  control_status() sikeres: {n_ok}/{N_POLLS}  hibas: {n_fail}/{N_POLLS}")
        print(f"  SLEEP=1 eszlelve: {n_sleep_seen}x  FULLSLEEP=1 eszlelve: {n_fullsleep_seen}x "
              f"(a {n_ok} SIKERES olvasas kozul)")

        print()
        print("-> fuggetlen visszaolvasas (read_calibration_data())...")
        data = transport.read_calibration_data()
        print(f"  {data}")
    finally:
        print()
        print("-> takaritas: exit_cal + seal...")
        try:
            transport.exit_cal(confirm=True)
        except Exception as exc:
            print(f"  exit_cal() hiba: {exc}")
        try:
            transport.seal(confirm=True)
        except Exception as exc:
            print(f"  seal() hiba: {exc}")
        transport.disconnect()


if __name__ == "__main__":
    main()
