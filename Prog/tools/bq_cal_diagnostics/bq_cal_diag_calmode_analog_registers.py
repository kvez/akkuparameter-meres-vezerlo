import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"

# Standard commands / extended data block 0x79-0x7F.
# NORMAL mode meaning (per BQ34Z110/BQ27541-family docs):
#   0x79       DeviceChemistryLength (1 byte)
#   0x7A..7C   DeviceChemistry (string, up to 3 bytes shown here)
#   0x7D..7E   ... (varies)  -- treated here as opaque raw bytes only.
# CAL mode meaning (per user's theory, TI app-note pattern):
#   0x79       Analog Conversion Counter (1 byte, increments)
#   0x7A-0x7B  Analog Current (raw ADC, 2 bytes)
#   0x7C-0x7D  Analog Voltage (raw ADC, 2 bytes)
#   0x7E-0x7F  Analog Temperature (raw ADC, 2 bytes)
_BLOCK_ADDR = 0x79
_BLOCK_LEN = 7


def ts():
    return datetime.now(timezone.utc).isoformat()


def read_raw_block(transport):
    return transport.read_raw_block(_BLOCK_ADDR, _BLOCK_LEN)


def main():
    transport = PicoBQTransport(max_retries=2)
    print(f"=== CALMODE analog-register diagnosztika (0x79-0x7F) === {ts()}")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        cs0 = decode_control_status(driver.read_control_status())
        print(f"kezdeti allapot: SS={int(cs0.ss)} FAS={int(cs0.fas)} CALMODE={int(cs0.calmode)}")

        print()
        print("--- NORMAL allapot, 0x79-0x7F elso olvasas ---")
        try:
            block1 = read_raw_block(transport)
            print(f"  block1 = {block1!r}")
        except Exception as exc:
            print(f"  HIBA (read_raw_block RPC nincs implementalva a Pico-n): {exc}")
            print("  Ehelyett a rendelkezesre allo szabvany-parancsokkal probalkozunk.")
            block1 = None

        time.sleep(0.3)
        print("--- NORMAL allapot, 0x79-0x7F MASODIK olvasas (valtozas-detektalashoz) ---")
        if block1 is not None:
            block1b = read_raw_block(transport)
            print(f"  block1b = {block1b!r}")
            print(f"  valtozott-e NORMAL alatt (varjuk: NEM/ritkan) -> {block1 != block1b}")

        transport.unseal(confirm=True)
        transport.full_access(confirm=True)
        transport.enter_cal(confirm=True)
        cs1 = decode_control_status(driver.read_control_status())
        print()
        print(f"unseal+full_access+enter_cal utan: SS={int(cs1.ss)} FAS={int(cs1.fas)} "
              f"CALMODE={int(cs1.calmode)}")

        if block1 is not None:
            print("--- CAL_ENABLE/ENTER_CAL UTAN, 0x79-0x7F tobbszori olvasas ---")
            samples = []
            for i in range(6):
                b = read_raw_block(transport)
                samples.append(b)
                print(f"  [minta {i}] {b!r}")
                time.sleep(0.5)
            changing = len(set(samples)) > 1
            print(f"  valtozik-e kalibracios mod alatt (varjuk: IGEN, ha analog ADC) -> {changing}")
    finally:
        # Kör 20 Batch E6 (HW20-02, P2, DFAM-016): a takaritas (exit_cal +
        # seal) korabban a try blokk VEGEN allt -- barmilyen kivetel
        # unseal()/enter_cal() es a takaritas KOZOTT a chipet UNSEALED,
        # sot CALMODE-ban hagyta. Mostantol MINDEN terminalis uton
        # megkiserelve, egymastol fuggetlen try/except-tel (rules/
        # 10-safety.md #3: "egy OFF-hiba ne akadalyozza a masikat"), a mar
        # bevalt bq_cal_diag_save_retry.py mintajat koveteve. A zaro
        # CONTROL_STATUS-olvasas is ide kerult (nem csak a boldog utra),
        # kulonben hamis feliratot adna a hiba-agakon.
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
