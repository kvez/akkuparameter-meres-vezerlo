import sys
import time
sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"

# TI SLUUBF7A (bq34110 TRM) Sec 5.5: "the host system must not read the
# CONTROL_STATUS register at a rate greater than once every 0.5 seconds"
# a CC Offset kalibracio alatt. Ez a diagnosztika szigoruan ezt tartja be
# (>=0.6s marggal), es a keepalive-pinget teljesen elhagyja -- NULLA
# extra CONTROL_STATUS forgalom a varakozasok alatt.
CCA_POLL_INTERVAL_S = 0.6
MAX_CCA_POLLS = 60  # 60*0.6s = 36s felso korlat

# TI SLUUBW5A Sec 8.3 (HDQ-kontextusban, de a mechanizmus [DF-commit] busz-
# fuggetlen): "delay 70 ms before restarting a transaction... Three retries
# should be allowed". CC_OFFSET_SAVE-re (0x000B) nincs TI-dokumentalt
# eljaras, ez az analog minta alkalmazasa -- NINCS kozbeeso pollozas.
SAVE_RETRY_WAIT_S = 0.07
SAVE_MAX_RETRIES = 3


def main():
    transport = PicoBQTransport(max_retries=1)
    print("=== Diagnosztika: TI-szabalykonform (>=0.5s CCA poll, 70ms/3-retry SAVE) ===")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        cs0 = decode_control_status(driver.read_control_status())
        print(f"kezdeti allapot: SS={int(cs0.ss)} FAS={int(cs0.fas)} CALMODE={int(cs0.calmode)} "
              f"SLEEP={int(cs0.sleep)}")

        print("-> unseal + full_access + enter_cal...")
        transport.unseal(confirm=True)
        transport.full_access(confirm=True)
        transport.enter_cal(confirm=True)

        print("-> board_offset_cal() kiadva, BCA pollozasa >=0.6s-enkent (TI SLUUBF7A Sec 5.6)...")
        transport.board_offset_cal(confirm=True)
        t0 = time.monotonic()
        for i in range(MAX_CCA_POLLS):
            time.sleep(CCA_POLL_INTERVAL_S)
            cs = decode_control_status(driver.read_control_status())
            elapsed = time.monotonic() - t0
            print(f"  [BCA poll #{i+1}, t={elapsed:.2f}s] BCA={int(cs.bca)} CCA={int(cs.cca)}")
            if not cs.bca:
                print(f"  -> BCA torolve t={elapsed:.2f}s-nal")
                break
        else:
            print("  FIGYELEM: BCA nem torlodott a felso korlaton belul!")

        print()
        print("-> cc_offset_cal() kiadva, CCA pollozasa >=0.6s-enkent (TI SLUUBF7A Sec 5.5)...")
        transport.cc_offset_cal(confirm=True)
        t1 = time.monotonic()
        for i in range(MAX_CCA_POLLS):
            time.sleep(CCA_POLL_INTERVAL_S)
            cs = decode_control_status(driver.read_control_status())
            elapsed = time.monotonic() - t1
            print(f"  [CCA poll #{i+1}, t={elapsed:.2f}s] CCA={int(cs.cca)} SLEEP={int(cs.sleep)}")
            if not cs.cca:
                print(f"  -> CCA torolve t={elapsed:.2f}s-nal")
                break
        else:
            print("  FIGYELEM: CCA nem torlodott a felso korlaton belul!")

        # A TI 0.5s-os poll-korlat UTOLSO ellenorzese es a save() kozott
        # egy tovabbi >=0.6s szunetet tartunk, nehogy a poll-ciklus utolso
        # olvasasa "szamitson bele" egy tul gyors kovetkezo tranzakcioba.
        time.sleep(CCA_POLL_INTERVAL_S)

        print()
        print(f"-> cc_offset_save() -- {SAVE_MAX_RETRIES} probalkozas, {SAVE_RETRY_WAIT_S*1000:.0f}ms "
              f"varakozassal (TI SLUUBW5A Sec 8.3 analog mintaja), NINCS kozbeeso poll...")
        success = False
        for attempt in range(1, SAVE_MAX_RETRIES + 1):
            t_call0 = time.monotonic()
            try:
                transport.cc_offset_save(confirm=True)
                elapsed_ms = (time.monotonic() - t_call0) * 1000.0
                print(f"  #{attempt}: SIKERES ({elapsed_ms:.1f} ms)")
                success = True
                break
            except Exception as exc:
                elapsed_ms = (time.monotonic() - t_call0) * 1000.0
                print(f"  #{attempt}: HIBA ({elapsed_ms:.1f} ms) -- {exc}")
                if attempt < SAVE_MAX_RETRIES:
                    time.sleep(SAVE_RETRY_WAIT_S)

        print()
        print(f"=== EREDMENY: cc_offset_save() {'SIKERES' if success else 'HIBAS'} ===")

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
