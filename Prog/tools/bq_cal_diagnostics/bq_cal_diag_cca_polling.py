import sys
import time
sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"


def main():
    transport = PicoBQTransport(max_retries=2)
    print("=== Diagnosztika: BCA/CCA bit polling a fix-varakozas helyett ===")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        cs0 = decode_control_status(driver.read_control_status())
        print(f"kezdeti allapot: SS={int(cs0.ss)} FAS={int(cs0.fas)} CALMODE={int(cs0.calmode)} "
              f"BCA={int(cs0.bca)} CCA={int(cs0.cca)}")

        print("-> unseal + full_access + enter_cal...")
        transport.unseal(confirm=True)
        transport.full_access(confirm=True)
        transport.enter_cal(confirm=True)

        print("-> board_offset_cal()...")
        t0 = time.monotonic()
        transport.board_offset_cal(confirm=True)
        for _ in range(40):
            time.sleep(0.25)
            cs = decode_control_status(driver.read_control_status())
            elapsed = time.monotonic() - t0
            print(f"  [t={elapsed:5.2f}s] BCA={int(cs.bca)} CCA={int(cs.cca)} CALMODE={int(cs.calmode)}")
            if not cs.bca:
                print(f"  -> BCA torolve t={elapsed:.2f}s-nal")
                break
        else:
            print("  FIGYELEM: BCA 10s utan sem torlodott!")

        print()
        print("-> cc_offset_cal()...")
        t1 = time.monotonic()
        transport.cc_offset_cal(confirm=True)
        cca_cleared_at = None
        for _ in range(160):  # max 40s figyelve, 0.25s-enkent
            time.sleep(0.25)
            cs = decode_control_status(driver.read_control_status())
            elapsed = time.monotonic() - t1
            if int(elapsed * 4) % 4 == 0:  # kb. masodpercenkent logolva
                print(f"  [t={elapsed:5.2f}s] BCA={int(cs.bca)} CCA={int(cs.cca)} CALMODE={int(cs.calmode)}")
            if not cs.cca:
                cca_cleared_at = elapsed
                print(f"  -> CCA TOROLVE t={elapsed:.2f}s-nal")
                break
        else:
            print("  FIGYELEM: CCA 40s utan sem torlodott!")

        print()
        print(f"-> cc_offset_save() probalkozas, CCA torles utan kb. 1s margoval...")
        time.sleep(1.0)
        try:
            transport.cc_offset_save(confirm=True)
            print("  -> cc_offset_save() SIKERES!")
            data = transport.read_calibration_data()
            print(f"  read_calibration_data() = {data}")
        except Exception as exc:
            print(f"  -> cc_offset_save() MEG MINDIG HIBAZOTT: {exc}")

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
