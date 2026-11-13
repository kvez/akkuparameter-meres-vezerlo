import sys
sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"


def main():
    transport = PicoBQTransport(max_retries=2)
    print(f"=== Diagnosztika: hiba utani chip-allapot ellenorzese ===")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        cs = decode_control_status(driver.read_control_status())
        print(f"  1. Chip allapot MOST (unseal nelkul): SS={int(cs.ss)} FAS={int(cs.fas)} CALMODE={int(cs.calmode)}")

        if cs.ss:
            print("  -> A chip SEALED. A takaritas (exit_cal+seal) sikeresnek tunik.")
        else:
            print("  -> FIGYELEM: a chip MEG MINDIG UNSEALED! A takaritas nem volt teljes.")

        # Fuggetlen olvasasi probalkozas -- unseal(), majd olvassuk a
        # kalibracios adatokat, majd MINDENKEPPEN seal() vissza.
        print("  -> unseal() + read_calibration_data() (csak olvasas, iras nelkul)...")
        try:
            transport.unseal(confirm=True)
            cs2 = decode_control_status(driver.read_control_status())
            print(f"  2. Allapot unseal() utan: SS={int(cs2.ss)} FAS={int(cs2.fas)} CALMODE={int(cs2.calmode)}")

            data = transport.read_calibration_data()
            print(f"  3. read_calibration_data() = {data}")

            errs = None
            try:
                errs = transport.negotiate()
            except Exception:
                pass
        finally:
            print("  -> seal() (visszazaras)...")
            transport.seal(confirm=True)
            cs3 = decode_control_status(driver.read_control_status())
            print(f"  4. Vegso allapot seal() utan: SS={int(cs3.ss)} FAS={int(cs3.fas)} CALMODE={int(cs3.calmode)}")
    finally:
        transport.disconnect()


if __name__ == "__main__":
    main()
