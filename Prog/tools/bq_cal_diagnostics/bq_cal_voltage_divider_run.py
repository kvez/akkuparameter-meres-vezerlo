import sys
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.bq_gauge_calibration import VoltageCalibrationController, VoltageCalState

PORT = "COM10"
DMM_VOLTAGE_MV = 23640  # DMM-mert, stabil (PSU 24.00V setpoint, DMM 23.64V readback)


def ts():
    return datetime.now(timezone.utc).isoformat()


def main():
    transport = PicoBQTransport(max_retries=2)
    print(f"=== BQ Voltage Divider kalibracio === {ts()}")
    transport.connect(PORT, timeout=2.0)
    ctrl = None
    try:
        transport.negotiate()

        ctrl = VoltageCalibrationController(transport)
        ctrl.start()
        print(f"ctrl.state = {ctrl.state}")
        if ctrl.state == VoltageCalState.ERROR:
            snap = ctrl.snapshot()
            print(f"HIBA start()-nal: {snap.fail_reason}")
            print(f"cleanup_failures: {snap.cleanup_failures}")
            if snap.cleanup_failures:
                print("FIGYELEM: a BQ visszazarasa nem igazolt.")
            return

        ctrl.submit_dmm_voltage_mV(DMM_VOLTAGE_MV)
        snap = ctrl.snapshot()
        print()
        print("=== EREDMENY ===")
        print(f"allapot: {ctrl.state}")
        print(f"fail_reason: {snap.fail_reason!r}")
        print(f"dmm_voltage_mV: {snap.dmm_voltage_mV}")
        print(f"bq_voltage_mV (BQ sajat Voltage() olvasasa): {snap.bq_voltage_mV}")
        print(f"old_voltage_divider: {snap.old_voltage_divider}")
        print(f"new_voltage_divider: {snap.new_voltage_divider}")
        print(f"cleanup_failures: {snap.cleanup_failures}")
        if snap.cleanup_failures:
            print("FIGYELEM: a BQ visszazarasa nem igazolt.")

        if ctrl.state == VoltageCalState.DONE and not snap.cleanup_failures:
            # FONTOS (2026-09-12-i eloforduls alapjan javitva): a Voltage
            # Divider Data Flash mezo, a chip pedig submit_dmm_voltage_mV()
            # vegen seal()-t hiv -- egy itteni, SEALED allapotu
            # read_voltage_divider() az ISMERT SCF-011 hibat valtana ki
            # (SEALED alatt a DF-olvasas meghibasodhat, hibajelzes nelkul
            # hamis/degeneralt erteket adva -- l. CLAIM_LEDGER.yaml
            # BQ-DF-SEALED-001). A megbizhato fuggetlen ellenorzeshez
            # UJRA UNSEAL kell -- l. kulon
            # bq_verify_voltage_divider_unsealed.py szkript.
            print()
            print(
                "Fuggetlen ellenorzeshez futtasd le kulon: "
                "bq_verify_voltage_divider_unsealed.py (a chip most SEALED, "
                "egy itteni kozvetlen DF-olvasas az ISMERT SCF-011 hibat "
                "valtana ki, l. a szkript fejlecet)."
            )
    except BaseException as exc:
        # R6-B4 (R6-architecture-01, DFAM-016): rendellenes kilepeskor (pl.
        # Ctrl+C, BrokenPipe, hibas jelzofajl) a controller abort()-ja
        # (SEAL, SS-ellenorzes; a VD nem hasznal ENTER_CAL-t) a disconnect
        # ELOTT fut, majd az eredeti kivetel propagal. Az abort Exception-
        # hibaja note-kent csatolodik, nem fedi el az eredetit. Az abort a
        # SEAL/SS-ellenorzes hibait nem dobja, csak a snapshot
        # cleanup_failures-ebe rogziti: ha az nem ures, az is note-kent
        # csatolodik (a BQ visszazarasa nem igazolt). Egy abort
        # kozbeni ujabb BaseException (pl. masodik Ctrl+C) viszont propagal,
        # az eredeti csak __context__-kent marad meg: teljes kivetel-
        # megorzesi garancia NINCS.
        if ctrl is not None:
            try:
                ctrl.abort()
                cleanup_failures = ctrl.snapshot().cleanup_failures
            except Exception as abort_exc:  # noqa: BLE001 - note-kent csatolva, l. fent
                exc.add_note(f"R6-B4: controller abort() hiba: {abort_exc!r}")
            else:
                if cleanup_failures:
                    exc.add_note(
                        "R6-B4: a BQ visszazarasa (SEAL) nem igazolt: "
                        + "; ".join(cleanup_failures)
                    )
        raise
    finally:
        transport.disconnect()
        try:
            print("transport lezarva.")
        except Exception:  # noqa: BLE001, S110 - R6-B4: a zaro naplosor
            pass  # hibaja nem fedhet el egy propagalo kivetelt


if __name__ == "__main__":
    main()
