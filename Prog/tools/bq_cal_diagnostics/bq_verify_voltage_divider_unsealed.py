import sys
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.src.bq_diagnostics import decode_control_status

PORT = "COM10"


def ts():
    return datetime.now(timezone.utc).isoformat()


def main():
    transport = PicoBQTransport(max_retries=2)
    print(f"=== Voltage Divider fuggetlen ellenorzes, UNSEALED alatt === {ts()}")
    transport.connect(PORT, timeout=2.0)
    try:
        transport.negotiate()
        driver = Bq34z110ReadOnlyDriver(transport=transport)

        cs0 = decode_control_status(driver.read_control_status())
        print(f"kezdeti allapot (SEALED-nek varjuk a submit_dmm_voltage_mV() utan): "
              f"SS={int(cs0.ss)} FAS={int(cs0.fas)} CALMODE={int(cs0.calmode)}")

        vd_sealed = transport.read_voltage_divider()
        print(f"voltage_divider olvasas SEALED alatt (meg unseal ELOTT): {vd_sealed}")

        transport.unseal(confirm=True)
        transport.full_access(confirm=True)
        cs1 = decode_control_status(driver.read_control_status())
        print(f"unseal+full_access utan: SS={int(cs1.ss)} FAS={int(cs1.fas)}")

        vd_unsealed = transport.read_voltage_divider()
        bq_v = transport.read_standard_command_word(Bq34z110ReadOnlyDriver.STD_CMD_VOLTAGE)
        print(f"voltage_divider olvasas UNSEALED alatt: {vd_unsealed}")
        print(f"BQ Voltage() UNSEALED alatt: {bq_v} mV")

        # Masodik, fuggetlen olvasas ugyanabban az UNSEALED allapotban --
        # a stabilitas/reprodukalhatosag ellenorzesere.
        vd_unsealed2 = transport.read_voltage_divider()
        print(f"voltage_divider MASODIK olvasas UNSEALED alatt: {vd_unsealed2}")
    finally:
        # Kör 20 Batch E6 (HW20-02, P2, DFAM-016): a takaritas (seal) korabban
        # a try blokk VEGEN allt -- barmilyen kivetel unseal() es a
        # takaritas KOZOTT a chipet UNSEALED hagyta. Mostantol MINDEN
        # terminalis uton megkiserelve (rules/10-safety.md #3). Ez a
        # szkript sosem hiv enter_cal()-t, ezert itt NINCS exit_cal() --
        # az hozzaadasa hamis muveletet jelentene egy nem-CALMODE allapoton.
        print()
        print("-> takaritas: seal...")
        try:
            transport.seal(confirm=True)
        except Exception as exc:
            print(f"  seal() hiba: {exc}")
        try:
            # NB-1 (MAX verifier, Batch E6): a `driver` a `try` blokk elejen
            # jon letre -- ha `negotiate()` vagy meg elotte hibazik, a
            # `driver` sosem kap erteket, es egy `driver.read_control_
            # status()` hivas itt UnboundLocalError-t dobna a valodi hiba
            # HELYETT. Kozvetlen transport-hivassal ez elkerulve (l. a
            # tobbi 3 celfajl azonos mintaja).
            cs2 = decode_control_status(
                transport.read_control_subcommand_word(
                    Bq34z110ReadOnlyDriver.MAC_CONTROL_STATUS
                )
            )
            print(f"seal() utan: SS={int(cs2.ss)} FAS={int(cs2.fas)}")
        except Exception as exc:
            print(f"  zaro CONTROL_STATUS olvasas hiba: {exc}")
        transport.disconnect()
        print("transport lezarva.")


if __name__ == "__main__":
    main()
