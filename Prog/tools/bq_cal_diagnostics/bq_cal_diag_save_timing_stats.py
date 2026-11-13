import sys
import time
sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"
N_ATTEMPTS = 20
PAUSE_BETWEEN_S = 1.0


def main():
    transport = PicoBQTransport(max_retries=1)  # NE rejtse el a valodi idozitest a beepitett retry
    print("=== Diagnosztika: cc_offset_save() ismetelt kiadas -- valaszido statisztika ===")
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
        time.sleep(1.0)  # extra margo, mint korabban

        print()
        print(f"-> {N_ATTEMPTS}x cc_offset_save() kozvetlen (kontroller/retry NELKUL), "
              f"{PAUSE_BETWEEN_S}s szunettel, valaszido meresel...")
        results = []
        for i in range(1, N_ATTEMPTS + 1):
            t_call0 = time.monotonic()
            try:
                transport.cc_offset_save(confirm=True)
                elapsed_ms = (time.monotonic() - t_call0) * 1000.0
                results.append((i, True, elapsed_ms, ""))
                print(f"  #{i:2d}: SIKERES  ({elapsed_ms:6.1f} ms)")
            except Exception as exc:
                elapsed_ms = (time.monotonic() - t_call0) * 1000.0
                results.append((i, False, elapsed_ms, str(exc)))
                print(f"  #{i:2d}: HIBA     ({elapsed_ms:6.1f} ms) -- {exc}")
            time.sleep(PAUSE_BETWEEN_S)

        print()
        print("=== OSSZESITES ===")
        n_ok = sum(1 for _, ok, _, _ in results if ok)
        n_fail = N_ATTEMPTS - n_ok
        ok_times = [t for _, ok, t, _ in results if ok]
        fail_times = [t for _, ok, t, _ in results if not ok]
        print(f"  Sikeres: {n_ok}/{N_ATTEMPTS}  Hibas: {n_fail}/{N_ATTEMPTS}")
        if ok_times:
            print(f"  Sikeres valaszido: min={min(ok_times):.1f}ms max={max(ok_times):.1f}ms "
                  f"atlag={sum(ok_times)/len(ok_times):.1f}ms")
        if fail_times:
            print(f"  Hibas (timeout) valaszido: min={min(fail_times):.1f}ms max={max(fail_times):.1f}ms "
                  f"atlag={sum(fail_times)/len(fail_times):.1f}ms")
        pattern = "".join("O" if ok else "X" for _, ok, _, _ in results)
        print(f"  Minta (O=sikeres, X=hiba, idorendben): {pattern}")

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
