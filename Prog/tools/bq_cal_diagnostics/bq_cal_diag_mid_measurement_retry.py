import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"
# Mikor (a triggerelo parancs utan hany masodperccel) adjuk ki MEGEGYSZER a
# trigger parancsot, MIG a meres meg biztosan folyamatban van (BOARD_OFFSET
# ~36.58s, CC_OFFSET ~17.67s -- korabbi HIL-mert ertekek, l. SCF-012).
BOARD_OFFSET_DUP_AT_S = 10.0
CC_OFFSET_DUP_AT_S = 6.0
POLL_INTERVAL_S = 0.7


def ts():
    return datetime.now(timezone.utc).isoformat()


def poll_until_clear(transport, driver, bit_name, dup_at_s, dup_fn, max_wait_s=90.0):
    """Pollozza a CONTROL_STATUS-t (BCA vagy CCA), es a dup_at_s masodpercnel
    megegyszer kiadja a dup_fn() trigger parancsot -- meg AKTIV meres kozben.
    Naplozza a CALMODE/BCA/CCA/SLEEP biteket minden tick-nel."""
    t0 = time.monotonic()
    dup_issued = False
    log = []
    while True:
        elapsed = time.monotonic() - t0
        cs = decode_control_status(
            transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS)
        )
        bit_val = getattr(cs, bit_name)
        row = (elapsed, int(cs.calmode), int(bit_val), int(cs.sleep), int(cs.ss), int(cs.fas))
        log.append(row)
        print(
            f"  [t={elapsed:5.1f}s] CALMODE={row[1]} {bit_name.upper()}={row[2]} "
            f"SLEEP={row[3]} SS={row[4]} FAS={row[5]}"
        )
        if not dup_issued and elapsed >= dup_at_s:
            print(f"  >>> [t={elapsed:5.1f}s] DUPLIKALT trigger kiadasa MEG AKTIV meres kozben...")
            dup_fn()
            dup_issued = True
            print(f"  >>> duplikalt trigger visszaterese OK, folytatjuk a pollozast")
        if not bit_val:
            return elapsed, log
        if elapsed >= max_wait_s:
            raise TimeoutError(f"{bit_name} nem torlodott {max_wait_s}s alatt")
        time.sleep(POLL_INTERVAL_S)


def main():
    transport = PicoBQTransport(max_retries=2)
    print(f"=== Mid-measurement-retry HIL diagnosztika (SCF-012 eredeti kerdes) === {ts()}")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        cs0 = decode_control_status(driver.read_control_status())
        print(f"kezdeti allapot: SS={int(cs0.ss)} FAS={int(cs0.fas)} CALMODE={int(cs0.calmode)}")

        transport.unseal(confirm=True)
        transport.full_access(confirm=True)
        transport.enter_cal(confirm=True)
        cs1 = decode_control_status(driver.read_control_status())
        print(f"unseal+full_access+enter_cal utan: SS={int(cs1.ss)} FAS={int(cs1.fas)} "
              f"CALMODE={int(cs1.calmode)}")

        print()
        print("--- BOARD_OFFSET_CAL, duplikalt trigger t=%.1fs-nal ---" % BOARD_OFFSET_DUP_AT_S)
        transport.board_offset_cal(confirm=True)
        board_elapsed, board_log = poll_until_clear(
            transport, driver, "bca", BOARD_OFFSET_DUP_AT_S,
            lambda: transport.board_offset_cal(confirm=True),
        )
        print(f"BCA torolve t={board_elapsed:.1f}s-nal (korabbi tiszta mérés: ~36.58s)")

        print()
        print("--- CC_OFFSET_CAL, duplikalt trigger t=%.1fs-nal ---" % CC_OFFSET_DUP_AT_S)
        transport.cc_offset_cal(confirm=True)
        cc_elapsed, cc_log = poll_until_clear(
            transport, driver, "cca", CC_OFFSET_DUP_AT_S,
            lambda: transport.cc_offset_cal(confirm=True),
        )
        print(f"CCA torolve t={cc_elapsed:.1f}s-nal (korabbi tiszta mérés: ~17.67s)")

        print()
        print("--- cc_offset_save() (normal retry-logika, NEM duplikalt kulon) ---")
        save_ok = False
        for attempt in range(1, 5):
            try:
                transport.cc_offset_save(confirm=True)
                save_ok = True
                break
            except Exception as exc:
                print(f"  cc_offset_save() {attempt}. probalkozas hibazott: {exc}")
                time.sleep(0.5)
        print(f"cc_offset_save() vegul: {'SIKERES' if save_ok else 'SIKERTELEN'}")

        data = transport.read_calibration_data()
        print()
        print("=== EREDMENY (meg UNSEALED, meg exit_cal ELOTT) ===")
        print(f"read_calibration_data(): {data}")
        print("(korabbi, TISZTA Phase 1 futasok: cc_offset=-1374, board_offset=-11/-12)")
    finally:
        # Kör 20 Batch E6 (HW20-02, P2, DFAM-016): a takaritas (exit_cal +
        # seal) korabban a try blokk VEGEN allt -- barmilyen kivetel (pl. a
        # poll_until_clear() TimeoutError-ja) unseal()/enter_cal() es a
        # takaritas KOZOTT a chipet UNSEALED, sot CALMODE-ban hagyta.
        # Mostantol MINDEN terminalis uton megkiserelve, egymastol
        # fuggetlen try/except-tel (rules/10-safety.md #3: "egy OFF-hiba ne
        # akadalyozza a masikat"). A zaro CONTROL_STATUS-olvasas is ide
        # kerult (nem csak a boldog utra), kulonben hamis feliratot adna a
        # hiba-agakon.
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
        try:
            cs_final = decode_control_status(
                transport.read_control_subcommand_word(
                    Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS
                )
            )
            print(f"exit_cal()+seal() utan: SS={int(cs_final.ss)} FAS={int(cs_final.fas)} "
                  f"CALMODE={int(cs_final.calmode)}")
        except Exception as exc:
            print(f"  zaro CONTROL_STATUS olvasas hiba: {exc}")
        transport.disconnect()
        print("transport lezarva.")


if __name__ == "__main__":
    main()
