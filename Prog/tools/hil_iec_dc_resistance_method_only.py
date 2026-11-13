"""
HIL: IEC 60896-21 Sec.6.3 DC belso ellenallas -- CSAK A MODSZER (LOAD_1 ->
OPEN_CIRCUIT_REST -> LOAD_2 -> COMPUTE_RESULT), a PRECHARGE/PRECHARGE_RELAX
(teljes toltes + 2h relax) elhagyasaval, felhasznaloi kiferes szerint:
"Nem kell a precharg es relax. Csak a metodus a lenyeg."

FONTOS, EZ NEM IEC-KOMPATIBILIS REFERENCIA-MERES: az Ua bazisertek a
jelenlegi (nem feltetlenul teljesen toltott, nem beallitott) akku-allapotot
tukrozi, nem az IEC-elofeltetelt (teljesen feltoltott akku). Ez a futtatas
KIZAROLAG a mereszi ALLAPOTGEP/SAFETY-logika elo hardveren valo
mukodesenek igazolasara szolgal, NEM egy vegleges Ri referenciaertekre.
L. Riportok/... MAX-review (2026-09-12): a PRECHARGE/RELAX elhagyasa NEM
SCF-013-sertes (relax_before_measurement_s PROJECT_POLICY, nem IEC-elofiras).

A LOAD_1 allapotba valo kozvetlen belepes a controller SAJAT, mar letezo
_enter() metodusaval tortenik (nem uj logika, csak a hasznalt belepesi
pont valtozik) -- minden biztonsagi kapu (check_battery_voltage/
check_load_power/check_concurrent_psu_load) a LOAD_1/LOAD_2 sajat,
valtozatlan elofeltetel-ellenorzese formajaban TOVABBRA IS lefut, mert
azok a _run_load_1()/_run_load_2() elejen, minden egyes belepeskor
frissen futnak.

Forras (MAX-review, 2026-09-12) ajanlasa szerint: dt_s=1.0 minden advance()
hivasnal, hogy a mintavetel pontosan a celido pontokon (20.0s/5.0s) essen.
"""
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\KS\Desktop\CLI\Akkuteszter")

import yaml

from Prog.drivers.device_psu import Keithley2220PSU
from Prog.drivers.device_load import Keithley2380Load
from Prog.drivers.device_dmm import Keysight34465ADMM
from Prog.src.battery_profile import BatteryProfile
from Prog.src.safety import SafetyManager, PsuMode, TempCompMode
from Prog.src.iec_dc_resistance_controller import (
    IecDcResistanceController,
    IecDcResistanceConfig,
    IecDcResistanceState,
)

CONFIG_PATH = r"C:\Users\KS\Desktop\CLI\Akkuteszter\Prog\config\local_config.yaml"


def ts():
    return datetime.now(timezone.utc).isoformat()


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    psu = Keithley2220PSU()
    load = Keithley2380Load()
    dmm_v = Keysight34465ADMM()
    dmm_t = Keysight34465ADMM()

    print(f"=== IEC 60896-21 Sec.6.3 -- CSAK A MODSZER (nincs precharge/relax) === {ts()}")
    psu.connect(cfg["instruments"]["psu"]["resource"])
    load.connect(cfg["instruments"]["load"]["resource"])
    dmm_v.connect(cfg["instruments"]["dmm_voltage"]["resource"])
    dmm_t.connect(cfg["instruments"]["dmm_temperature"]["resource"])
    psu_idn = psu.idn()
    load_idn = load.idn()
    print(f"PSU IDN:   {psu_idn.strip()}")
    print(f"Load IDN:  {load_idn.strip()}")
    print(f"DMM_V IDN: {dmm_v.idn().strip()}")
    print(f"DMM_T IDN: {dmm_t.idn().strip()}")

    # HW20-06 (Kör 20 Batch E7, DFAM-001): felcserélt VISA resource-string
    # esetén a driver a MÁSIK fizikai eszközre küldené az energizáló
    # parancsot (a controller sajat _enter()/advance() utja LOAD_1-ben
    # tenylegesen input_on()-t/setpointot ad ki) -- ugyanaz az ellenőrzés,
    # mint az éles úton már bevált instrument_manager.py::identify_all(
    # expected_models=...), ide kiterjesztve, MIELŐTT bármilyen konfiguráció
    # vagy energizálás történne. (MAX verifier N-4: a connect() maga már
    # elvégzi a saját safe-off-ját -- l. device_psu.py/device_load.py --
    # tehát a kapu pillanatában mindkét műszer már biztonságos állapotban
    # van; ez a komment csak a SORRENDET, nem egy hiányzó safety-lépést ír.)
    if "2220" not in psu_idn:
        print(f"⛔ PSU azonosítás sikertelen: várt '2220' substring, kapott IDN={psu_idn!r} "
              f"-- megszakítva (felcserélt VISA resource gyanús).")
        load.disconnect()
        psu.disconnect()
        dmm_v.disconnect()
        dmm_t.disconnect()
        return
    if "2380-120-60" not in load_idn:  # R3-B2 (R3-HW-01): pontos modell
        print(f"⛔ Load azonosítás sikertelen: várt '2380-120-60' substring, kapott IDN={load_idn!r} "
              f"-- megszakítva (felcserélt VISA resource vagy nem támogatott Load-modell gyanús).")
        load.disconnect()
        psu.disconnect()
        dmm_v.disconnect()
        dmm_t.disconnect()
        return

    try:
        # Safe alapallapot -- ELSO dolog, mielott barmit konfiguralnank.
        load.input_off()
        psu.all_outputs_off()

        dmm_v.configure_dcv(range_V=100, nplc=10)
        sensor_mode = cfg.get("temperature_sensor_mode", "RTD_4W_PT100")
        if sensor_mode == "THERMISTOR_2W_NTC_10K_B3950":
            dmm_t.configure_temp_2wire_ntc_thermistor(r25_ohm=10000.0, beta_K=3950.0, nplc=10)
        else:
            dmm_t.configure_temp_4wire_pt100(nplc=10)

        psu.set_mode_independent()

        # FIAMM FG20721 -- primer adatlap-forras (2026-09-12, l. CLAIM_LEDGER.yaml
        # BAT-FG20721-I10-001): C10=6.7 Ah (10H/1.80VPC/25C), nominal=7.2 Ah
        # (20H/1.75VPC/25C), I10=0.67 A.
        profile = BatteryProfile(
            battery_name="FIAMM_12V",
            manufacturer="FIAMM",
            model="FG20721",
            nominal_voltage_V=12.0,
            cell_count=6,
            nominal_capacity_Ah=7.2,
            C10_capacity_Ah=6.7,
            chemistry="AGM_VRLA",
            profile_source="Leirasok/akku_adatlapok/FG_FOLDER_EN.pdf",
            charge_voltage_per_cell_V=2.40,
            terminate_voltage_per_cell_V=1.80,
            float_voltage_per_cell_V=2.27,
            i10_discharge_current_A=0.67,
        )
        print(f"profile: {profile.model}, cell_count={profile.cell_count}, "
              f"nominal_voltage_V={profile.nominal_voltage_V}, "
              f"i10={profile.i10_discharge_current_A} A, "
              f"terminate_pack_V={profile.terminate_voltage_pack_V}")

        safety = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
        )
        config = IecDcResistanceConfig()
        print(f"Ia={4*profile.i10_discharge_current_A:.3f} A  "
              f"Ib={20*profile.i10_discharge_current_A:.3f} A")

        ctrl = IecDcResistanceController(psu, load, dmm_v, dmm_t, profile, safety, config)

        # --- ELHAGYVA: INIT -> PRECHARGE -> PRECHARGE_RELAX ---
        # Kozvetlen belepes LOAD_1-be a controller SAJAT _enter() metodusaval.
        print(f"[SZANDEKOS] PRECHARGE/PRECHARGE_RELAX kihagyva -- kozvetlen "
              f"belepes LOAD_1-be, a controller sajat _enter() metodusaval.")
        ctrl._enter(IecDcResistanceState.LOAD_1)

        t0 = time.monotonic()
        last_state = None
        while ctrl.state not in (IecDcResistanceState.DONE, IecDcResistanceState.FAULT):
            time.sleep(1.0)
            ctrl.advance(1.0)
            elapsed = time.monotonic() - t0
            if ctrl.state != last_state:
                print(f"  [t={elapsed:6.1f}s] *** allapotvaltas -> {ctrl.state} ***")
                last_state = ctrl.state
            else:
                print(f"  [t={elapsed:6.1f}s] state={ctrl.state}  "
                      f"u_batt={ctrl.u_batt_V:.3f}V  temp={ctrl.battery_temperature_C:.1f}C")

        print()
        print(f"=== VEGALLAPOT: {ctrl.state} === (t={time.monotonic()-t0:.1f}s)")
        if ctrl.state == IecDcResistanceState.FAULT:
            print(f"fault_reason: {ctrl.fault_reason}")
        result = ctrl.result
        if result is not None:
            for k, v in result.items():
                print(f"  {k}: {v}")
    finally:
        try:
            load.input_off()
        except Exception as exc:
            print(f"WARNING: load.input_off() hibazott: {exc}")
        try:
            psu.all_outputs_off()
        except Exception as exc:
            print(f"WARNING: psu.all_outputs_off() hibazott: {exc}")
        load.disconnect()
        psu.disconnect()
        dmm_v.disconnect()
        dmm_t.disconnect()
        print("Minden muszer lezarva, Load OFF, PSU OFF.")


if __name__ == "__main__":
    main()
