import sys
import time
sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"
RETRY_DELAYS_S = [0.0, 1.0, 2.0, 3.0, 5.0]  # novekvo varakozas ujraprobalkozasok kozott


def main():
    transport = PicoBQTransport(max_retries=2)
    print("=== Diagnosztika: cc_offset_save() ujraprobalkozas novekvo varakozassal ===")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        cs0 = decode_control_status(driver.read_control_status())
        print(f"kezdeti allapot: SS={int(cs0.ss)} FAS={int(cs0.fas)} CALMODE={int(cs0.calmode)}")

        print("-> unseal + full_access + enter_cal + board_offset_cal (2s)...")
        transport.unseal(confirm=True)
        transport.full_access(confirm=True)
        transport.enter_cal(confirm=True)
        transport.board_offset_cal(confirm=True)
        time.sleep(2.0)

        print("-> cc_offset_cal(), varakozas CCA torlesere...")
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
        print("-> cc_offset_save() novekvo varakozasu ujraprobalkozasokkal...")
        success = False
        for i, delay in enumerate(RETRY_DELAYS_S):
            if delay > 0:
                print(f"  varakozas {delay}s a kovetkezo probalkozas elott...")
                time.sleep(delay)
            try:
                transport.cc_offset_save(confirm=True)
                print(f"  {i+1}. probalkozas ({delay}s varakozas utan): SIKERES!")
                success = True
                break
            except Exception as exc:
                print(f"  {i+1}. probalkozas ({delay}s varakozas utan): HIBA -- {exc}")

        if success:
            data = transport.read_calibration_data()
            print(f"\n  read_calibration_data() = {data}")
        else:
            print("\n  MINDEN probalkozas hibazott.")
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
