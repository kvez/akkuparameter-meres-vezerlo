import sys
import time
sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"
N_CYCLES = 8
PING_GAP_S = 0.08
READY_STREAK_REQUIRED = 4  # ennyi egymast koveto GYORS, SLEEP=0 ping utan probaljuk a mentest
MAX_PINGS_PER_CYCLE = 60


def ping_until_ready(transport, driver):
    """Ismetelt, gyors control_status() ping -- mind a valaszidot, mind a
    SLEEP/FULLSLEEP bitet figyeli. 'Kesz' allapotnak azt tekintjuk, amikor
    READY_STREAK_REQUIRED egymast koveto ping GYORS (<20ms) ES SLEEP=0/
    FULLSLEEP=0. Visszaadja: (ready: bool, pings_used: int, elapsed_s: float,
    history: list[(elapsed_ms, sleep, fullsleep)])."""
    t0 = time.monotonic()
    streak = 0
    history = []
    for i in range(1, MAX_PINGS_PER_CYCLE + 1):
        t_ping0 = time.monotonic()
        try:
            cs = decode_control_status(driver.read_control_status())
            elapsed_ms = (time.monotonic() - t_ping0) * 1000.0
            history.append((elapsed_ms, int(cs.sleep), int(cs.fullsleep)))
            if elapsed_ms < 20.0 and not cs.sleep and not cs.fullsleep:
                streak += 1
            else:
                streak = 0
        except Exception as exc:
            history.append((None, None, None))
            streak = 0
        if streak >= READY_STREAK_REQUIRED:
            return True, i, time.monotonic() - t0, history
        time.sleep(PING_GAP_S)
    return False, MAX_PINGS_PER_CYCLE, time.monotonic() - t0, history


def main():
    transport = PicoBQTransport(max_retries=1)
    print("=== Diagnosztika: 'pingeld ebren, majd probald' -- wakeup-latency + cc_offset_save() ===")
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
        print(f"-> {N_CYCLES}x ciklus: ping-ig-ebren (max {MAX_PINGS_PER_CYCLE} ping, "
              f"{READY_STREAK_REQUIRED} egymas utani gyors+SLEEP=0 ping = 'keszen all'), "
              f"majd cc_offset_save() probalkozas...")
        results = []
        for cycle in range(1, N_CYCLES + 1):
            ready, pings_used, wake_elapsed_s, history = ping_until_ready(transport, driver)
            sleep_seen = sum(1 for h in history if h[1] == 1)
            fullsleep_seen = sum(1 for h in history if h[2] == 1)
            print(f"  [ciklus {cycle}] wakeup: ready={ready} pings={pings_used} "
                  f"elapsed={wake_elapsed_s:.2f}s SLEEP-eszlelve={sleep_seen}x "
                  f"FULLSLEEP-eszlelve={fullsleep_seen}x")

            t_save0 = time.monotonic()
            try:
                transport.cc_offset_save(confirm=True)
                save_ms = (time.monotonic() - t_save0) * 1000.0
                results.append((cycle, ready, pings_used, wake_elapsed_s, True, save_ms))
                print(f"    -> cc_offset_save() SIKERES ({save_ms:.1f} ms)")
            except Exception as exc:
                save_ms = (time.monotonic() - t_save0) * 1000.0
                results.append((cycle, ready, pings_used, wake_elapsed_s, False, save_ms))
                print(f"    -> cc_offset_save() HIBA ({save_ms:.1f} ms) -- {exc}")

        print()
        print("=== OSSZESITES ===")
        n_ok = sum(1 for r in results if r[4])
        print(f"  cc_offset_save() sikeres (ebren-varas UTAN): {n_ok}/{N_CYCLES}")
        wake_times = [r[3] for r in results]
        print(f"  ebredesi ido (wake_elapsed_s): min={min(wake_times):.2f}s max={max(wake_times):.2f}s "
              f"atlag={sum(wake_times)/len(wake_times):.2f}s")

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
