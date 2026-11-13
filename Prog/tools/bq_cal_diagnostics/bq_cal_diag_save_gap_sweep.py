import sys
import time
sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"
# Kulonbozo resek vegigprobalva -- cel: feltérképezni, mely tartomanyok
# megbizhatoak es melyek nem (a chip felteteleezett ~1s-es belso periodikus
# ciklusahoz kepesti fazis miatt).
GAPS_S = [0.3, 0.5, 0.7, 0.9, 1.1, 1.3, 1.5, 1.7, 1.9, 2.1, 2.3, 2.5]


def main():
    transport = PicoBQTransport(max_retries=1)
    print("=== Diagnosztika: cc_offset_save() res-hossz sweep ===")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        cs0 = decode_control_status(driver.read_control_status())
        print(f"kezdeti allapot: SS={int(cs0.ss)} FAS={int(cs0.fas)} CALMODE={int(cs0.calmode)}")

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
        time.sleep(1.0)

        print()
        print(f"-> res-sweep: {GAPS_S}")
        results = []
        first_success_gap = None
        for gap in GAPS_S:
            time.sleep(gap)
            t_call0 = time.monotonic()
            try:
                transport.cc_offset_save(confirm=True)
                elapsed_ms = (time.monotonic() - t_call0) * 1000.0
                results.append((gap, True, elapsed_ms))
                print(f"  res={gap:4.1f}s: SIKERES  ({elapsed_ms:6.1f} ms)")
                if first_success_gap is None:
                    first_success_gap = gap
                    print("  -> ELSO SIKER, leallok a sweep-pel (nincs ok tovabb irni)")
                    break
            except Exception as exc:
                elapsed_ms = (time.monotonic() - t_call0) * 1000.0
                results.append((gap, False, elapsed_ms))
                print(f"  res={gap:4.1f}s: HIBA     ({elapsed_ms:6.1f} ms) -- {exc}")

        print()
        print("=== OSSZESITES ===")
        for gap, ok, ms in results:
            print(f"  res={gap:4.1f}s -> {'SIKERES' if ok else 'HIBA'} ({ms:.1f}ms)")
        if first_success_gap is not None:
            print(f"  ELSO SIKERES RES: {first_success_gap}s")
        else:
            print("  Egyik res sem volt sikeres a sweep soran.")

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
