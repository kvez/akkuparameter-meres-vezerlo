"""
HIL: PROJECT_ENGINEERING_METHOD standard 1s/10s/30s pulzus-ellenallas --
CSAK A MODSZER, felhasznaloi kiferes szerint ("Ugyan igy az 1s 10s 30s
esetekben." -- azaz ugyanaz a "nincs precharge/relax, csak a modszer"
filozofia, mint az IEC tesztnel, l. hil_iec_dc_resistance_method_only.py).

A PulseMeasurementService-t KOZVETLENUL hasznalja (nem az OcvSocController
teljes, tobb-oras OCV/SOC racs-jaraton keresztul) -- egyetlen, kb. 30
masodperces impulzus a jelenlegi akku-allapotnal, MINIMALIS terheles
(l. felhasznaloi valasztas az AskUserQuestion-re korabban ebben a
munkameneben).

FONTOS, EZ NEM VEGLEGES REFERENCIA-MERES: az akku jelenlegi (nem
feltetlenul teljesen toltott/relaxalt) allapotat tukrozi. A cel a
MERESI MECHANIZMUS elo hardveren valo mukodesenek igazolasa, es az
eredmeny osszevetese a 21.45 mOhm kulso referenciaertekkel es a mai IEC
"csak a modszer" eredmennyel (Ri=48.82 mOhm, szinten nem referenciaertek).

SAMP:TIM valasztas (2026-09-12, musszer-only HIL + fuggetlen MAX verifier,
l. CLAIM_LEDGER.yaml INSTR-DMM-SAMPTIM-001 UPDATE): a produkcios alapertek
(pulse_sample_interval_s=0.2, ocv_soc_controller.py::OcvSocConfig) a valos
musszeren (Keysight 34465A MY60016569, NPLC=10) mert MIN=0.404496s ALATT
van -- az UJ, ma commitolt readback-ellenorzo kapu (Prog/src/pulse_
measurement_service.py::_verify_actual_sample_timer_or_raise()) emiatt
MINDEN chunkot elutasitana, ha a globalis alapertekkel probalkoznank. Ez a
szkript ezert EXPLICIT 0.41s-ot ad at (a valos SCPI-sweep szerint az
egyetlen, 0.404496-0.99s tartomanyban PONTOSAN, elteres nelkul
visszaigazolt ertek) -- a globalis OcvSocConfig alapertek MODOSITASA
NELKUL, az egy kulon, kesobb meghozando dontes.

Pulzusaram: C/5 (impulse_current_rate_divisor=5 -- production alapertek,
ocv_soc_controller.py::OcvSocConfig), C10_capacity_Ah=6.7 Ah alapjan
1.34 A. Idotartam: 30.0s (production alapertek, impulse_duration_s),
lefedi mindharom celidopontot (1/10/30s).

A safety-kapukat (check_battery_voltage/check_load_power) EXPLICIT,
kulon hivjuk az OcvSocController production mintajat kovetve (l.
ocv_soc_controller.py ~1231. sor) -- a PulseMeasurementService maga NEM
vegez ilyen ellenorzest (az a hivo felelossege).
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
from Prog.src.pulse_measurement_service import PulseMeasurementService

CONFIG_PATH = r"C:\Users\KS\Desktop\CLI\Akkuteszter\Prog\config\local_config.yaml"

# 2026-09-12, musszer-only HIL-sweep alapjan (l. modul docstring) -- EZEN a
# muszeren (NPLC=10) az EGYETLEN, a 0.404496-0.99s tartomanyban pontosan
# visszaigazolt SAMP:TIM ertek.
SAMPLE_INTERVAL_S = 0.41
IMPULSE_CURRENT_RATE_DIVISOR = 5
IMPULSE_DURATION_S = 30.0


def ts():
    return datetime.now(timezone.utc).isoformat()


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    psu = Keithley2220PSU()
    load = Keithley2380Load()
    dmm_v = Keysight34465ADMM()

    print(f"=== Standard 1s/10s/30s pulzus -- CSAK A MODSZER === {ts()}")
    psu.connect(cfg["instruments"]["psu"]["resource"])
    load.connect(cfg["instruments"]["load"]["resource"])
    dmm_v.connect(cfg["instruments"]["dmm_voltage"]["resource"])
    psu_idn = psu.idn()
    load_idn = load.idn()
    print(f"PSU IDN:   {psu_idn.strip()}")
    print(f"Load IDN:  {load_idn.strip()}")
    print(f"DMM_V IDN: {dmm_v.idn().strip()}")

    # HW20-06 (Kör 20 Batch E7, DFAM-001): felcserélt VISA resource-string
    # esetén a driver a MÁSIK fizikai eszközre küldené az energizáló
    # parancsot (load.input_on()/psu setpoint) -- ugyanaz az ellenőrzés,
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
        return
    if "2380-120-60" not in load_idn:  # R3-B2 (R3-HW-01): pontos modell
        print(f"⛔ Load azonosítás sikertelen: várt '2380-120-60' substring, kapott IDN={load_idn!r} "
              f"-- megszakítva (felcserélt VISA resource vagy nem támogatott Load-modell gyanús).")
        load.disconnect()
        psu.disconnect()
        dmm_v.disconnect()
        return

    try:
        # Safe alapallapot -- ELSO dolog, mielott barmit konfiguralnank.
        load.input_off()
        psu.all_outputs_off()

        dmm_v.configure_dcv(range_V=100, nplc=10)
        psu.set_mode_independent()

        # FIAMM FG20721 -- primer adatlap-forras (l. CLAIM_LEDGER.yaml
        # BAT-FG20721-I10-001), ugyanaz a profil, mint a mai IEC teszten.
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
        safety = SafetyManager(
            profile=profile,
            psu_mode=PsuMode.INDEPENDENT,
            temp_comp_mode=TempCompMode.MONITOR_ONLY,
        )

        pulse_current_A = profile._c10_base_Ah / IMPULSE_CURRENT_RATE_DIVISOR
        print(f"profile: {profile.model}, terminate_pack_V={profile.terminate_voltage_pack_V:.2f}V, "
              f"C10={profile.C10_capacity_Ah}Ah, pulse_current=C/{IMPULSE_CURRENT_RATE_DIVISOR}="
              f"{pulse_current_A:.3f}A, sample_interval_s={SAMPLE_INTERVAL_S}, "
              f"duration_s={IMPULSE_DURATION_S}")

        u_before = dmm_v.read_voltage()
        print(f"U_batt (pulzus elott): {u_before:.3f}V")

        # Elozetes safety-kapuk -- az OcvSocController production mintajat kovetve.
        # R9-B2: a szolgaltatas a friss pulzusadatok teljesitmenyet is ellenorzi.
        sr = safety.check_battery_voltage(u_before)
        if not sr.is_ok:
            print(f"SAFETY FAULT (check_battery_voltage): {sr.fault} -- {sr.message}")
            return
        sr = safety.check_load_power(u_before, pulse_current_A)
        if not sr.is_ok:
            print(f"SAFETY FAULT (check_load_power): {sr.fault} -- {sr.message}")
            return
        print("Safety-kapuk: OK (check_battery_voltage, check_load_power).")

        service = PulseMeasurementService(
            load,
            dmm_v,
            terminate_voltage_V=profile.terminate_voltage_pack_V,
            safety=safety,
            max_voltage_V=profile.batt_absolute_max_V,
            max_load_power_W=safety.max_load_power_W,
            chunk_sample_count=3,
            sample_interval_s=SAMPLE_INTERVAL_S,
        )

        print(f"[SZANDEKOS] Nincs precharge/relax -- csak a modszer: "
              f"begin_pulse() -> step_chunk()* -> finalize().")
        t0 = time.monotonic()
        started = service.begin_pulse(pulse_current_A=pulse_current_A, duration_s=IMPULSE_DURATION_S)
        print(f"  [t={time.monotonic()-t0:6.2f}s] begin_pulse() -> started={started}")

        if started:
            chunk_n = 0
            more = True
            while more:
                more = service.step_chunk()
                chunk_n += 1
                elapsed = time.monotonic() - t0
                print(f"  [t={elapsed:6.2f}s] chunk #{chunk_n} lekerdezve, "
                      f"minta_szam={len(service._samples)}, more={more}")

        result = service.finalize()
        print()
        print(f"=== EREDMENY === (t={time.monotonic()-t0:.2f}s)")
        print(f"  started={result.started}  skipped_low_voltage={result.skipped_low_voltage}  "
              f"cutoff_during_pulse={result.cutoff_during_pulse}")
        print(f"  pulse_current_degraded={result.pulse_current_degraded}  "
              f"has_invalid_resistance={result.has_invalid_resistance}")
        print(f"  v_baseline_V={result.v_baseline_V:.4f}  "
              f"pulse_current_set_A={result.pulse_current_set_A:.4f}  "
              f"pulse_current_measured_mean_A={result.pulse_current_measured_mean_A:.4f}")
        print(f"  pulse_duration_set_s={result.pulse_duration_set_s:.2f}  "
              f"pulse_duration_actual_s={result.pulse_duration_actual_s:.2f}")
        print(f"  synchronization_quality={result.synchronization_quality}  "
              f"method_origin={result.method_origin}")
        print(f"  removed_Ah={result.removed_Ah:.6f}  removed_Wh={result.removed_Wh:.6f}")
        print()
        for target in (1.0, 10.0, 30.0):
            p = result.result_at(target)
            if p is None:
                print(f"  t={target}s: NINCS EREDMENY (nem szerepel a results dict-ben)")
                continue
            print(
                f"  t={target}s: reached={p.reached} within_tolerance={p.within_tolerance} "
                f"actual_time_s={p.actual_time_s} time_error_s={p.time_error_s} "
                f"U={p.voltage_V} I={p.current_A} dU={p.delta_voltage_V} dI={p.delta_current_A} "
                f"R={p.resistance_ohm} Rb={p.resistance_mOhm} mOhm"
            )
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
        print("Minden muszer lezarva, Load OFF, PSU OFF.")


if __name__ == "__main__":
    main()
