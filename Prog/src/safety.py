from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from math import isfinite
from typing import ClassVar

from Prog.src.battery_profile import (
    UNKNOWN_DISCHARGE_RATE_ORIGIN,
    BatteryProfile,
    DischargeRateOrigin,
    validate_normal_discharge,
)

# D9/F-07 (2026-09-13, Riportok/ui_policy_audit_20260913.md): a kisütési
# runaway-backstop (idő+Ah) korábban KIZÁRÓLAG a SafetyManager.
# max_discharge_time_s/max_discharge_Ah_factor mezők alapérték-
# literáljaiként élt -- a GUI-nak (config_panel.py, readonly
# "discharge_safety_limits" sor) erre a SAJÁT konstansra van szüksége,
# hogy ne duplikálja a számpárt egy második, kézzel karbantartott helyen
# (ugyanaz a DFAM-017-elv, mint BATT_ABSOLUTE_MAX_V_PER_CELL-nél,
# battery_profile.py). Nulla viselkedésváltozás: a mezők alapértéke
# VÁLTOZATLAN, csak a forrás most modul-szinten, importálhatóan él.
MAX_DISCHARGE_TIME_S = 86400.0
MAX_DISCHARGE_AH_FACTOR = 1.10


class PsuMode(Enum):
    INDEPENDENT = "INDEPENDENT"
    PARALLEL = "PARALLEL"
    SERIES = "SERIES"


class TempCompMode(Enum):
    OFF = "OFF"
    MONITOR_ONLY = "MONITOR_ONLY"
    ENABLED = "ENABLED"


class FaultCode(Enum):
    PSU_MODE_INCOMPATIBLE_WITH_PACK_VOLTAGE = auto()
    DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED = auto()
    REVERSE_POLARITY_OR_WIRING_FAULT = auto()
    BATTERY_OVERVOLTAGE = auto()
    BATTERY_UNDERVOLTAGE = auto()
    TEMPERATURE_MONITOR_LOST_CRITICAL = auto()
    INTEGRATION_FALLBACK_TOO_LONG = auto()
    CONCURRENT_PSU_LOAD_ON = auto()
    DMM_FEEDBACK_LOST = auto()
    SERIES_DROP_TOO_HIGH = auto()
    LOAD_POWER_LIMIT = auto()
    MAX_CHARGE_AH_REACHED = auto()
    MAX_CHARGE_TIME_REACHED = auto()
    PSU_COMM_LOST = auto()
    LOAD_COMM_LOST = auto()
    BATTERY_TEMPERATURE_RISE_OVER_AMBIENT = auto()  # CHG-004
    CHARGE_CURRENT_LIMIT_EXCEEDED = auto()  # R9-B1: profilmaximum
    BATTERY_OVERTEMPERATURE = auto()  # R11-B4: PROJECT_POLICY
    DISCHARGE_CONFIG_INVALID = auto()  # Q-46: validacio, nem akku-alulfeszultseg


class WarningCode(Enum):
    PSU_SERIES_MODE_UNUSUAL_FOR_12V_PACK = auto()
    TEMPERATURE_OUT_OF_RANGE = auto()
    TEMPERATURE_DMM_LOST = auto()
    HEADROOM_APPROACHING = auto()
    REDUCED_CHARGE_CURRENT_DUE_TO_PSU_LIMIT = auto()
    PSU_LEAKAGE_SUSPECTED = auto()
    INTEGRATION_FALLBACK_TO_SETPOINT = auto()
    PSU_OUTPUT_STATE_UNCERTAIN = auto()
    LOAD_INPUT_STATE_UNCERTAIN = auto()
    PSU_VOLTAGE_READBACK_LOST = auto()
    SERIES_DROP_HIGH = auto()
    DIODE_POWER_HIGH = auto()
    DIODE_POWER_TOO_HIGH = auto()
    LOAD_CURRENT_SETPOINT_NOT_REACHED = auto()
    PSU_CHARGE_CURRENT_SETPOINT_NOT_REACHED = auto()
    NORMAL_DISCHARGE_CUTOFF_OVERSHOOT = auto()


@dataclass
class SafetyResult:
    fault: FaultCode | None = None
    warning: WarningCode | None = None
    message: str = ""

    @property
    def is_ok(self) -> bool:
        return self.fault is None


@dataclass
class SafetyManager:
    # R11-B4 / Q-14: PROJECT_POLICY, nem gyartoi hatar (2026-11-04).
    BATTERY_OVERTEMPERATURE_C: ClassVar[float] = 60.0

    profile: BatteryProfile
    psu_mode: PsuMode
    temp_comp_mode: TempCompMode = TempCompMode.MONITOR_ONLY
    temperature_dmm_fault_timeout_s: float = 60.0
    diode_power_warning_W: float = 2.0
    diode_power_critical_warning_W: float = 3.0
    warning_series_drop_V: float = 1.20
    fault_series_drop_V: float = 1.50
    fault_series_drop_V_bq: float = 3.50
    is_bq_mode: bool = False
    # Teljes kódbázis review 5. kör (2026-08-29), Szoftver/Architektúra +
    # Hardver/Műszervezérlés P2 lelet (dedup): korábban check_load_power()
    # egy HÍVÁSONKÉNTI paraméter-defaultként (250.0) tartotta ezt az
    # értéket, teljesen függetlenül a standalone DischargeController saját,
    # ténylegesen konfigurálható DischargeConfig.max_load_power_W
    # mezőjétől (l. test_discharge_controller.py: max_load_power_W=15.0
    # egy konkrét teszthez) — a két érték ma számszerűen egyezik, de két
    # független forrásból, nincs mechanizmus egy alacsonyabb limit
    # egységes átvételére. Mostantól EZ az egyetlen forrás — a standalone
    # DischargeController is ezt olvassa (l. _run_cc()), a beágyazott
    # kisütési fázisok (chemid/golden_gg/traditional_learning/ocv_soc)
    # pedig a check_load_power() alapértékén (ugyanez a mező) keresztül.
    max_load_power_W: float = 250.0     # Keithley 2380-120-60: 120V/60A/250W hardver max
    # Teljes kódbázis review 8. kör (2026-08-30), Szoftver/Architektúra P2
    # lelet: a kisütési runaway-backstop (idő+Ah) korábban a standalone
    # DischargeController.DischargeConfig-ban KONFIGURÁLHATÓ mezőként élt,
    # DE a beágyazott kisütési fázisok (chemid/golden_gg/traditional_
    # learning) mindegyike egy KÜLÖN, PRIVÁT, hardkódolt modul-konstansként
    # duplikálta ugyanezt az értékpárt (86400.0/1.10) — nem konfigurálható,
    # nincs config-szintű expozíció. Ugyanaz a hibaosztály, mint a
    # max_load_power_W fenti konszolidációja (5. kör) — mostantól EZ az
    # egyetlen forrás mind a 4 kisütési útvonalhoz (standalone +
    # a 3 beágyazott controller + OcvSocController STEP_DISCHARGE idő-
    # limitje, ami csak az idő-komponenst használja).
    max_discharge_time_s: float = MAX_DISCHARGE_TIME_S
    max_discharge_Ah_factor: float = MAX_DISCHARGE_AH_FACTOR
    # Review-kör 4 (2026-09-09), Hardver/Műszervezérlés HW4-02 (P1,
    # DFAM-001 sibling_safety_gate_omission -- l. a PSU-oldali testvér-
    # gate max_load_power_W/check_load_power() mintáját fent): a Load
    # CC-módú setpointja és a ténylegesen mért árama korábban sehol nem
    # került összehasonlításra -- egy elmaradó áram (védelmi latch, CC-
    # tartomány-elutasítás, csendes SCPI-elutasítás) fizikailag hihető,
    # de HAMIS adatfolyamként jelent volna meg. PROJECT_POLICY érték (NEM
    # gyártói specifikáció -- a 2380 nem ad "setpoint elérve" toleranciát,
    # ez egy szoftver-oldali regulációs/hihetőségi kapu, nem műszer-
    # pontossági limit): 10% relatív eltérés, jóval lazább, mint a 2380
    # CC-pontossága, de elég szűk ahhoz, hogy egy teljesen elmaradt vagy
    # az előző szinten ragadt áramot (a failure scenario szerint akár
    # 0 A vs a kért Ib=20×I10) biztosan elkapjon.
    load_current_setpoint_tolerance_factor: float = 0.10
    # Kor 18, Batch C7 R17-10 (DFAM-001 + DFAM-019 inverz alak, l.
    # DEFECT_FAMILY_REGISTRY.yaml "R17-10a GOVERNANCE-KORREKCIO"): a
    # check_load_current_setpoint() PSU-oldali parja. A REGISZTRALT
    # allitas, hogy a PSU-oldal mar kapott analog gate-et
    # (REDUCED_CHARGE_CURRENT_DUE_TO_PSU_LIMIT), HAMIS volt -- az egy
    # konfiguracio-idobeni setpoint-csokkentes, NEM egy futasidoben
    # mert-vs-setpoint hihetosegi kapu. PROJECT_POLICY, NEM gyartoi
    # specifikacio, l. CLAIM_LEDGER.yaml PROJ-PSU-TOL-001.
    psu_charge_current_setpoint_tolerance_factor: float = 0.10

    def check_psu_mode_compatibility(self) -> SafetyResult:
        """
        [N3] PSU mód és pack feszültség kompatibilitás ellenőrzése.

        Szabályok (implementáld):
          - 24V pack (cell_count >= 12): SERIES mód kötelező.
            INDEPENDENT vagy PARALLEL → FaultCode.PSU_MODE_INCOMPATIBLE_WITH_PACK_VOLTAGE
          - 12V pack (cell_count <= 6) + SERIES mód:
            → WarningCode.PSU_SERIES_MODE_UNUSUAL_FOR_12V_PACK
            (nem tiltott, de szokatlan és kezelői figyelmeztető dialógus szükséges)
          - Minden más kombináció: SafetyResult() (fault=None, warning=None)

        Returns:
            SafetyResult — fault ha indítás tiltott, warning ha csak figyelmeztetés
        """
        if self.profile.cell_count >= 12 and self.psu_mode != PsuMode.SERIES:
            return SafetyResult(
                fault=FaultCode.PSU_MODE_INCOMPATIBLE_WITH_PACK_VOLTAGE,
                message=(
                    f"{self.profile.cell_count} cellás / 24V+ pack csak SERIES "
                    f"PSU módban indítható. Aktuális mód: {self.psu_mode.value}"
                ),
            )

        if self.profile.cell_count <= 6 and self.psu_mode == PsuMode.SERIES:
            return SafetyResult(
                warning=WarningCode.PSU_SERIES_MODE_UNUSUAL_FOR_12V_PACK,
                message=(
                    f"{self.profile.cell_count} cellás / 12V pack SERIES PSU módban "
                    f"szokatlan; kezelői megerősítés szükséges."
                ),
            )

        return SafetyResult()

    def check_precheck_voltage(
        self, measured_voltage_V: float, *, _traditional_learning: bool = False,
    ) -> SafetyResult:
        """
        [N6] Precheck: mért akkufeszültség ésszerűségi ellenőrzése.

        12V pack (cell_count=6):
          < 10.5 V: FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED
          10.5–batt_absolute_max_V: OK
          > batt_absolute_max_V: FaultCode.BATTERY_OVERVOLTAGE

        24V pack (cell_count=12): határok kétszerese (21.0 / batt_absolute_max_V).

        Mérnöki feltételezés (nincs gyári forrás a pontos 1.75V/cella értékre,
        l. CLAUDE.md §10.7): a küszöb cell_count-tal skálázódik.
        """
        # Teljes kódbázis review 20. kör (2026-09-06), Hardver/Műszervezérlés
        # P1 lelet: korábban bináris (6 vs. minden-más) elágazás volt, ami a
        # két shippelt profilra (6/12 cella) véletlenül helyes eredményt adott,
        # de MINDEN MÁS cell_count-ra hibás — 7-11 cellánál túl magas (jó
        # akkut is elutasítana), 13-24 cellánál túl alacsony küszöböt adna
        # (VESZÉLYES IRÁNY: egy ténylegesen mélykisült, nagyobb cellaszámú
        # packet "OK"-nak minősítene). A BatteryProfile.cell_count validációja
        # (1-24) ezt nem akadályozza meg. A profil már tartalmaz helyesen
        # skálázódó testvér-mintát (terminate_voltage_pack_V = per_cell ×
        # cell_count) — ezt a mintát követve explicit skálázva.
        # Q-42 PROJECT_POLICY: belso TLC-kontextus, nem kezelo konfiguracio.
        _DEEP_DISCHARGE_THRESHOLD_V_PER_CELL = 1.70 if _traditional_learning else 1.75
        deep_discharge_threshold_V = (
            _DEEP_DISCHARGE_THRESHOLD_V_PER_CELL * self.profile.cell_count
        )

        # SAFE-009: negatív/nulla feszültség fizikailag nem lehet egy csatlakoztatott
        # akku esete — fordított polaritásra vagy hibás/hiányzó bekötésre utal,
        # nem szabad mélykisült akkunak minősíteni (ami helytelenül automatikus
        # DEEPLY_DISCHARGED útvonalra terelné).
        if measured_voltage_V <= 0.0:
            return SafetyResult(
                fault=FaultCode.REVERSE_POLARITY_OR_WIRING_FAULT,
                message=f"Precheck: fizikailag ésszerűtlen feszültség "
                        f"{measured_voltage_V:.2f}V — fordított polaritás vagy "
                        f"hibás/hiányzó bekötés gyanús."
            )
        if measured_voltage_V > self.profile.batt_absolute_max_V:
            return SafetyResult(
                fault=FaultCode.BATTERY_OVERVOLTAGE,
                message=f"Precheck: feszültség {measured_voltage_V:.2f}V > "
                        f"max {self.profile.batt_absolute_max_V:.2f}V"
            )
        if measured_voltage_V < deep_discharge_threshold_V:
            return SafetyResult(
                fault=FaultCode.DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED,
                message=f"Precheck: mélykisült akku {measured_voltage_V:.2f}V < "
                        f"{deep_discharge_threshold_V:.2f}V. "
                        f"Recovery mód nincs implementálva."
            )
        return SafetyResult()

    def check_battery_overtemperature(
        self, battery_temperature_C: float | None, temperature_valid: bool,
    ) -> SafetyResult:
        """R11-B4: friss, ervenyes meres; modfuggetlen PROJECT_POLICY."""
        if (
            temperature_valid
            and battery_temperature_C is not None
            and isfinite(battery_temperature_C)
            and battery_temperature_C >= self.BATTERY_OVERTEMPERATURE_C
        ):
            return SafetyResult(
                fault=FaultCode.BATTERY_OVERTEMPERATURE,
                message=(
                    f"Akkuhomerseklet {battery_temperature_C:.1f} C >= "
                    f"{self.BATTERY_OVERTEMPERATURE_C:.1f} C (PROJECT_POLICY)"
                ),
            )
        return SafetyResult()

    def check_test_temperature_range(self, battery_temperature_C: float) -> SafetyResult:
        """
        Teljes kódbázis review 20. kör (2026-09-06), Metrológia/
        Adatintegritás P2 lelet: a BatteryProfile.test_temperature_min_C/
        max_C mezőket a GUI ténylegesen betölti a YAML-ból, de semmi nem
        vetette össze a mért akkufeszültség-hőmérséklettel — a
        WarningCode.TEMPERATURE_OUT_OF_RANGE enum-tag sosem került
        ténylegesen kiadásra. Egy a FIAMM AGM Engineering Manual szerinti
        névleges (15-30°C) ablakon kívül futtatott kapacitásteszt emiatt
        semmilyen quality-jelzőt nem kapott, holott a mért kapacitás
        hőmérsékletfüggése ezen a tartományon kívül jelentős lehet
        (CLAUDE.md §12 "minden fontos eredmény kapjon quality/minősítést").

        NEM fault — csak a mert kapacitas osszehasonlithatosaganak
        figyelmeztetese. A kulon abszolut leallitasi kapu:
        check_battery_overtemperature() (R11-B4, PROJECT_POLICY).
        """
        if battery_temperature_C < self.profile.test_temperature_min_C:
            return SafetyResult(
                warning=WarningCode.TEMPERATURE_OUT_OF_RANGE,
                message=(
                    f"Akkuhőmérséklet {battery_temperature_C:.1f}°C < "
                    f"{self.profile.test_temperature_min_C:.1f}°C (a profil "
                    f"deklarált tesztelési tartománya alatt)."
                ),
            )
        if battery_temperature_C > self.profile.test_temperature_max_C:
            return SafetyResult(
                warning=WarningCode.TEMPERATURE_OUT_OF_RANGE,
                message=(
                    f"Akkuhőmérséklet {battery_temperature_C:.1f}°C > "
                    f"{self.profile.test_temperature_max_C:.1f}°C (a profil "
                    f"deklarált tesztelési tartománya felett)."
                ),
            )
        return SafetyResult()

    # R11-B1: a kiesesi elozmeny a kozos SafetyManager elettartamaig el.
    temperature_dmm_fault_s: float = field(default=0.0, init=False)

    def record_temperature_dmm_read(self, *, valid: bool, dt_s: float) -> None:
        if valid:
            self.temperature_dmm_fault_s = 0.0
        else:
            self.temperature_dmm_fault_s += dt_s

    def check_temperature_dmm_fault(self, elapsed_fault_s: float) -> SafetyResult:
        """
        [N2] Hőmérséklet-DMM kiesés kezelése mód szerint.

        ENABLED:      azonnali FAULT_FATAL (elapsed_fault_s bármennyi)
        MONITOR_ONLY: 0..timeout → WARNING; >= timeout → FAULT_FATAL
        OFF:          nincs fault, nincs warning
        """
        if self.temp_comp_mode == TempCompMode.OFF:
            return SafetyResult()

        if self.temp_comp_mode == TempCompMode.ENABLED:
            if elapsed_fault_s > 0.0:
                return SafetyResult(
                    fault=FaultCode.TEMPERATURE_MONITOR_LOST_CRITICAL,
                    message="Temp DMM kiesés ENABLED módban — azonnali leállítás"
                )
            return SafetyResult()

        # MONITOR_ONLY
        if elapsed_fault_s >= self.temperature_dmm_fault_timeout_s:
            return SafetyResult(
                fault=FaultCode.TEMPERATURE_MONITOR_LOST_CRITICAL,
                message=f"Temp DMM kiesés > {self.temperature_dmm_fault_timeout_s:.0f}s"
            )
        if elapsed_fault_s > 0.0:
            return SafetyResult(
                warning=WarningCode.TEMPERATURE_DMM_LOST,
                message=f"Temp DMM kiesés {elapsed_fault_s:.0f}s"
            )
        return SafetyResult()

    def check_ambient_temperature_rise(
        self,
        battery_temperature_C: float,
        ambient_temperature_source: str,
        ambient_temperature_C: float | None,
    ) -> SafetyResult:
        """
        CHG-004: FIAMM temperature-rise termination — battery_temp > ambient + 10°C.

        Csak akkor érvényesíthető, ha van tényleges (nem közelítő) ambient
        referencia: ambient_temperature_source == MANUAL vagy EXTERNAL_SENSOR,
        és ambient_temperature_C ténylegesen meg van adva. Egyéb esetben
        (UNKNOWN, vagy INITIAL_BATTERY_APPROX — ez utóbbi jelen körben nem
        implementált, l. gap-mátrix) a feltétel NEM kényszeríthető ki — ez nem
        csendes hiba, hanem a hívónak (charge_controller/riport) kell jeleznie
        a bizonyítottsági szint hiányát (MEASUREMENT_LIMITATIONS).
        """
        if ambient_temperature_source not in ("MANUAL", "EXTERNAL_SENSOR"):
            return SafetyResult()
        if ambient_temperature_C is None:
            return SafetyResult()
        if battery_temperature_C > ambient_temperature_C + 10.0:
            return SafetyResult(
                fault=FaultCode.BATTERY_TEMPERATURE_RISE_OVER_AMBIENT,
                message=(
                    f"Akkuhőmérséklet {battery_temperature_C:.1f}°C > "
                    f"ambient {ambient_temperature_C:.1f}°C + 10°C — FIAMM "
                    f"temperature-rise termination."
                ),
            )
        return SafetyResult()

    def check_battery_voltage(self, measured_V: float) -> SafetyResult:
        """Futó mérés közben: BATTERY_OVERVOLTAGE ellenőrzés."""
        # SAFE-009: aktív töltés/kisütés közben is ki kell szűrni a fizikailag
        # ésszerűtlen (negatív/nulla) feszültséget — enélkül a hibás bekötés
        # csendben "OK" (nem overvoltage) eredményt adna.
        if measured_V <= 0.0:
            return SafetyResult(
                fault=FaultCode.REVERSE_POLARITY_OR_WIRING_FAULT,
                message=f"U_batt {measured_V:.3f}V fizikailag ésszerűtlen — "
                        f"fordított polaritás vagy hibás/hiányzó bekötés gyanús."
            )
        if measured_V > self.profile.batt_absolute_max_V:
            return SafetyResult(
                fault=FaultCode.BATTERY_OVERVOLTAGE,
                message=f"U_batt {measured_V:.3f}V > max {self.profile.batt_absolute_max_V:.3f}V"
            )
        return SafetyResult()

    def check_normal_discharge(
        self, measured_V: float, current_A: float, cutoff_V: float,
        origin: DischargeRateOrigin = UNKNOWN_DISCHARGE_RATE_ORIGIN,
        *, first_cutoff_crossing: bool = False,
    ) -> SafetyResult:
        """Szigorú setup/mintakapu; Q-35 szerint csak az első cutoff-minta zárhat rendesen.
        Az egyszeri engedélyt a normál controller fogyasztja el; IEC/pulzus nem kapja meg.
        """
        if not isfinite(measured_V):
            return SafetyResult(fault=FaultCode.DMM_FEEDBACK_LOST,
                                message="GOV-029: nem véges feszültségminta")
        voltage = self.check_battery_voltage(measured_V)
        if voltage.fault is not None:
            return voltage
        try:
            minimum_V = validate_normal_discharge(self.profile, current_A, cutoff_V, origin)
        except ValueError as exc:
            return SafetyResult(fault=FaultCode.DISCHARGE_CONFIG_INVALID, message=str(exc))
        if measured_V < minimum_V:
            if first_cutoff_crossing:
                return SafetyResult(
                    warning=WarningCode.NORMAL_DISCHARGE_CUTOFF_OVERSHOOT,
                    message=(
                        f"GOV-029/Q-35: U_batt={measured_V:.6f} V; "
                        f"minimum={minimum_V:.6f} V; "
                        f"overshoot={minimum_V - measured_V:.6f} V"
                    ),
                )
            return SafetyResult(
                fault=FaultCode.BATTERY_UNDERVOLTAGE,
                message=f"GOV-029: U_batt={measured_V:.6f} V < {minimum_V:.6f} V",
            )
        return SafetyResult()

    def enforce_normal_discharge(
        self, measured_V: float, current_A: float, cutoff_V: float,
        stop: Callable[[str], None],
        on_event: Callable[[dict], None] | None = None,
        origin: DischargeRateOrigin = UNKNOWN_DISCHARGE_RATE_ORIGIN,
        *, first_cutoff_crossing: bool = False,
    ) -> bool:
        result = self.check_normal_discharge(
            measured_V, current_A, cutoff_V, origin,
            first_cutoff_crossing=first_cutoff_crossing,
        )
        if result.fault is None:
            if result.warning == WarningCode.NORMAL_DISCHARGE_CUTOFF_OVERSHOOT:
                # Callback nélkül sem maradhat csendes a diszkrét mintavétel túllövése.
                try:
                    logging.getLogger(__name__).warning(result.message)
                    if on_event is not None:
                        minimum_V = validate_normal_discharge(self.profile, current_A, cutoff_V, origin)
                        on_event({
                            "event_code": result.warning.name, "event_message": result.message,
                            "severity": "WARNING", "measured_V": measured_V,
                            "minimum_V": minimum_V, "overshoot_V": minimum_V - measured_V,
                        })
                except BaseException:
                    # A dobó naplózó se hagyja bekapcsolva a terhelést.
                    stop("DISCHARGE_OVERSHOOT_EVENT_FAILED")
                    raise
            return True
        # A controller saját terminális cleanupja: Load OFF -> PSU ALL OFF.
        # Az esemény callbackje csak a lekapcsolás és a FAULT után futhat.
        stop(result.fault.name)
        # Callback nélkül is megmarad a részletes hiba; a cleanup már lefutott.
        logging.getLogger(__name__).error(result.message)
        if result.fault in (FaultCode.BATTERY_UNDERVOLTAGE, FaultCode.DISCHARGE_CONFIG_INVALID) and on_event is not None:
            on_event({
                "event_code": result.fault.name, "event_message": result.message,
                "severity": "CRITICAL",
            })
        return False

    def check_diode_power(
        self, charge_current_A: float, u_drop_V: float
    ) -> SafetyResult:
        """[BY550] Dióda disszipáció ellenőrzés."""
        p_diode = abs(charge_current_A) * u_drop_V
        if p_diode > self.diode_power_critical_warning_W:
            return SafetyResult(
                warning=WarningCode.DIODE_POWER_TOO_HIGH,
                message=f"Dióda disszipáció {p_diode:.2f}W > {self.diode_power_critical_warning_W:.1f}W"
            )
        if p_diode > self.diode_power_warning_W:
            return SafetyResult(
                warning=WarningCode.DIODE_POWER_HIGH,
                message=f"Dióda disszipáció {p_diode:.2f}W > {self.diode_power_warning_W:.1f}W"
            )
        return SafetyResult()

    def check_load_power(
        self, measured_voltage_V: float, measured_current_A: float,
        max_load_power_W: float | None = None,
    ) -> SafetyResult:
        """Keithley 2380-120-60 hardver-teljesítménykorlát ellenőrzés
        (120V/60A/250W — l. self.max_load_power_W azonos alapértéke).

        Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow P1
        lelet: a standalone DischargeController._run_cc() ezt minden
        ticken ellenőrzi (mostantól szintén EZEN a SafetyManager rétegen
        át, l. self.max_load_power_W kommentje) — a beágyazott (inline)
        kisütési fázisok (chemid/golden_gg/traditional_learning/
        ocv_soc_controller.py) eddig egyike sem tartalmazta. Ez a metódus
        a hiányzó ellenőrzés egységes, újrafelhasználható formája ezekhez
        a hívókhoz.

        `max_load_power_W` explicit megadható egy híváshoz eltérő
        (pl. teszt-specifikus) limit érvényesítéséhez — alapértelmezés
        (`None`) esetén a self.max_load_power_W-t használja, ami az
        EGYETLEN kanonikus forrás minden hívóhoz."""
        limit_W = self.max_load_power_W if max_load_power_W is None else max_load_power_W
        p_load = abs(measured_voltage_V * measured_current_A)
        if p_load > limit_W:
            return SafetyResult(
                fault=FaultCode.LOAD_POWER_LIMIT,
                message=f"Load power {p_load:.1f}W > max {limit_W:.1f}W"
            )
        return SafetyResult()

    def check_load_current_setpoint(
        self, measured_current_A: float, setpoint_current_A: float,
        tolerance_factor: float | None = None,
    ) -> SafetyResult:
        """HW4-02 (review-kör 4, P1): a Load CC-módú setpointja és a
        ténylegesen mért árama közti relatív eltérés ellenőrzése -- a
        `check_load_power()` mintáját követve, egységes, újrafelhasználható
        formában a mind a 8 hívóhelyhez (standalone DischargeController +
        7 beágyazott/service-szintű kisütési/terhelési fázis).

        `setpoint_current_A <= 0` esetén nincs mit ellenőrizni (nincs
        aktív setpoint) -- SafetyResult() üresen tér vissza, elkerülve a
        nullával osztást.

        `tolerance_factor` explicit megadható egy híváshoz eltérő limithez
        (pl. teszt) -- alapértelmezés (`None`) esetén a self.load_current_
        setpoint_tolerance_factor-t használja, ami az EGYETLEN kanonikus
        forrás minden hívóhoz."""
        if setpoint_current_A <= 0:
            return SafetyResult()
        factor = (
            self.load_current_setpoint_tolerance_factor
            if tolerance_factor is None else tolerance_factor
        )
        relative_deviation = abs(measured_current_A - setpoint_current_A) / setpoint_current_A
        if relative_deviation > factor:
            return SafetyResult(
                warning=WarningCode.LOAD_CURRENT_SETPOINT_NOT_REACHED,
                message=(
                    f"Load mért áram {measured_current_A:.3f}A eltér a "
                    f"setpointtól ({setpoint_current_A:.3f}A) — relatív "
                    f"eltérés {relative_deviation * 100:.1f}% > "
                    f"{factor * 100:.0f}% tolerancia."
                ),
            )
        return SafetyResult()

    def check_psu_charge_current_setpoint(
        self, measured_current_A: float, setpoint_current_A: float,
        compliance_headroom_exhausted: bool,
        tolerance_factor: float | None = None,
    ) -> SafetyResult:
        """Kor 18, Batch C7 R17-10: a check_load_current_setpoint()
        PSU-oldali parja -- a HW4-02 failure scenario tolteshez (a PSU
        csendben NEM adja a parancsolt aramot, a mert adatfolyam
        fizikailag hihetonek, de HAMISNAK tunik).

        `compliance_headroom_exhausted` KOTELEZO, nem opcionalis: CC
        fazisban a mert aram LEGITIMEN elmaradhat a setponttol, amig a
        charge_controller.py::_maybe_escalate_cc_compliance() meg tudja
        emelni a compliance-feszultseget a plafonig -- ez a normal rampa,
        NEM hiba. Csak a plafon ELERESE utan jelent a hiany valodi,
        nem-javithato szolgaltatasi elmaradast. Enelkul a kapu MINDEN
        normal CC-ticken tuzelne, a defektet, amit celoz, reprodukalva.

        CSAK WARNING, sosem fault -- a Load-oldali testver elvet
        koveteve. A `tolerance_factor` 10% NEM gyartoi specifikacio, l.
        CLAIM_LEDGER.yaml PROJ-PSU-TOL-001."""
        if setpoint_current_A <= 0 or not compliance_headroom_exhausted:
            return SafetyResult()
        factor = (
            self.psu_charge_current_setpoint_tolerance_factor
            if tolerance_factor is None else tolerance_factor
        )
        relative_deviation = abs(measured_current_A - setpoint_current_A) / setpoint_current_A
        if relative_deviation > factor:
            return SafetyResult(
                warning=WarningCode.PSU_CHARGE_CURRENT_SETPOINT_NOT_REACHED,
                message=(
                    f"PSU mért töltőáram {measured_current_A:.3f}A eltér a "
                    f"setponttól ({setpoint_current_A:.3f}A) a compliance-"
                    f"plafon elérése UTÁN — relatív eltérés "
                    f"{relative_deviation * 100:.1f}% > {factor * 100:.0f}%."
                ),
            )
        return SafetyResult()

    def check_concurrent_psu_load(
        self, psu_commanded_on: bool, load_commanded_on: bool
    ) -> SafetyResult:
        """PSU ON + Load ON egyszerre nem megengedett — hardware conflict detektálás."""
        if psu_commanded_on and load_commanded_on:
            return SafetyResult(
                fault=FaultCode.CONCURRENT_PSU_LOAD_ON,
                message="PSU és Load egyszerre aktív — hardware conflict!"
            )
        return SafetyResult()

    def check_series_drop(self, u_drop_V: float) -> SafetyResult:
        """[BY550] Soros dióda feszültségesés kétszintű ellenőrzés."""
        active_fault_limit = self.fault_series_drop_V_bq if self.is_bq_mode else self.fault_series_drop_V
        if u_drop_V > active_fault_limit:
            return SafetyResult(
                fault=FaultCode.SERIES_DROP_TOO_HIGH,
                message=f"U_drop {u_drop_V:.3f}V > fault limit {active_fault_limit:.2f}V"
            )
        if u_drop_V > self.warning_series_drop_V:
            return SafetyResult(
                warning=WarningCode.SERIES_DROP_HIGH,
                message=f"U_drop {u_drop_V:.3f}V > warning limit {self.warning_series_drop_V:.2f}V"
            )
        return SafetyResult()
