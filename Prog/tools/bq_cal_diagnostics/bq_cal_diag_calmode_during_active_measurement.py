import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"
_BLOCK_ADDR = 0x79
_BLOCK_LEN = 7


def ts():
    return datetime.now(timezone.utc).isoformat()


def main():
    transport = PicoBQTransport(max_retries=2)
    print(f"=== 0x79-0x7F mintavetel AKTIV BCA=1 meres kozben === {ts()}")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        transport.unseal(confirm=True)
        transport.full_access(confirm=True)
        transport.enter_cal(confirm=True)

        block_before = transport.read_raw_block(_BLOCK_ADDR, _BLOCK_LEN)
        print(f"blokk ENTER_CAL utan, board_offset_cal ELOTT: {block_before!r}")

        t0 = time.monotonic()
        transport.board_offset_cal(confirm=True)
        samples = []
        while True:
            elapsed = time.monotonic() - t0
            cs = decode_control_status(
                transport.read_control_subcommand_word(Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS)
            )
            block = transport.read_raw_block(_BLOCK_ADDR, _BLOCK_LEN)
            samples.append((elapsed, cs.bca, block))
            print(f"  [t={elapsed:5.1f}s] BCA={int(cs.bca)} CALMODE={int(cs.calmode)} block={block!r}")
            if not cs.bca:
                break
            if elapsed >= 90.0:
                print("  TIMEOUT: BCA nem torlodott")
                break
            time.sleep(1.0)

        distinct_blocks = set(b for (_, _, b) in samples)
        print()
        print(f"Osszesen {len(samples)} minta, {len(distinct_blocks)} egyedi blokk-ertek.")
        print(f"Valtozott-e a blokk AKTIV BCA=1 meres alatt -> {len(distinct_blocks) > 1}")
    finally:
        # Kör 20 Batch E6 (HW20-02, P2, DFAM-016): a takaritas (exit_cal +
        # seal) korabban a try blokk VEGEN allt -- barmilyen kivetel
        # unseal()/enter_cal() es a takaritas KOZOTT a chipet UNSEALED,
        # sot CALMODE-ban hagyta. Mostantol MINDEN terminalis uton
        # megkiserelve, egymastol fuggetlen try/except-tel (rules/
        # 10-safety.md #3: "egy OFF-hiba ne akadalyozza a masikat").
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
        print("transport lezarva.")


if __name__ == "__main__":
    main()
