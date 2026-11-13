"""
IecDcResistanceController — IEC 60896-21 §6.3 szerinti kétpontos DC
belső ellenállás (Ri) és becsült rövidzárási áram (Isc) mérési
állapotgép.

Forrás (review-kör 5, F5-2, SOURCE_REQUIRED -- l. CLAIM_LEDGER.yaml
IEC-RI-001, SOURCE_CONFLICTS.md SCF-013): Folyamatok/tervek/
AGM_VRLA_belsoellenallas_IEC60896_ACIR_HRM10.txt -- ez egy PROJEKT-ÍRTA
magyar nyelvű összefoglaló az IEC 60896-21:2004 Clause 6.3-ról ("Short-
circuit current and d.c. internal resistance"), NEM maga a szabvány
szövege. [PONTOSÍTVA, Kör 21 F17/SRC21-05:] a repóban (git-követetlen,
l. .gitignore:8 `Leírások/`) létezik egy `Leírások/akku_adatlapok/
IEC-60896-21-2004.pdf`, DE ez egy iTeh Standards "Document Preview"
kivonat, amely csak az 1-3. szakaszt (Scope/Normative references/
Definitions) tartalmazza -- a 6. szakasz ("Test methods", ezen belül a
6.3 alszakasz) NINCS benne (ellenőrizve: pdftotext, a kinyert szöveg a
3.36-os definícióig tart, nyomtatott 19. oldal -- l. SOURCE_REGISTRY.yaml
IEC-60896-21-2004-PREVIEW, SOURCE_CONFLICTS.md SCF-013 korrekció; a TOC
szerinti 6. szakasz 33. oldala nem érhető el). A fájl emiatt TOVÁBBRA sem
tartalmaz oldal-/bekezdés-
szintű verbatim idézetet a 6.3 szakaszból -- ez NEM oldja fel a lenti
SOURCE_REQUIRED státuszt. Az alábbi eljárás és
konstansok emiatt SOURCE_REQUIRED minősítésűek, amíg a szabvány szövege
elő nem kerül -- NE módosítsd őket "konzervatívabb" irányba anélkül,
hogy a forrásállapotot feloldanád (CLAUDE.md §3/.claude/rules/
00-source-authority.md). A FG20721 I10=0,67A munkapélda (2026-09-12,
CLAIM_LEDGER.yaml BAT-FG20721-I10-001: primer FIAMM adatlap, FG_FOLDER_EN.pdf,
10H/1.80VPC/25°C kapacitásoszlop=6,7Ah -- pontosítva a korábbi, projekt-derivált
összefoglalóból vett ~0,62A becsléshez képest) a FIAMM saját kisütési
táblázatából jön (forrásolt, l. battery_profile.py).
Ez a mód FÜGGETLEN, ÚJ mérési módszer a meglévő OCV_SOC_CHARACTERIZATION
1s/10s/30s pulzus-alapú belső ellenállás becslésétől (l.
pulse_measurement_service.py) — az IEC módszer NEM impulzus, hanem két,
eltérő, sustained (folyamatos) DC terhelőáram-szinten vett mérési pont;
a két módszer eredménye NEM ugyanaz a fizikai mennyiség, és a projekt
külön eredmény-típusként kezeli őket (l. a forrás 12. szakasza:
IEC_DC_INTERNAL_RESISTANCE vs. PULSE_RESISTANCE vs. AC_INTERNAL_
RESISTANCE_1KHZ).

Mérési eljárás (a fenti projekt-derivált összefoglalóból, SOURCE_REQUIRED):
  1. Előfeltétel: teljesen feltöltött akku.
  2. Ia = 4×I10 alkalmazva; 20 s-nál Ua/Ia rögzítve; legkésőbb 25 s-nál
     Load OFF.
  3. Load OFF, 5 perc nyitott áramköri pihenés, nincs visszatöltés.
  4. Ib = 20×I10 alkalmazva; 5 s-nál Ub/Ib rögzítve; Load OFF.
  5. Ri = (Ua − Ub) / (Ib − Ia); Isc = (Ua×Ib − Ub×Ia) / (Ua − Ub).

I10 forrása: BatteryProfile.i10_discharge_current_A — KÖTELEZŐ, a
konkrét akkumodell saját, jóváhagyott C10 kisütési táblázatából (10 h,
1,80 V/cella) megadva, NEM nominal_capacity_Ah/10-ből számolva (l.
battery_profile.py mező-docstringje, BAT-CAP-001 C10≠C20 elve).

[R1] Nincs relay hívás — emergency_stop() sorrendje Load OFF → PSU OFF,
mint minden más controlleren.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from Prog.drivers.device_load import INPUT_ACTIVATION_ACK_TIMEOUT_S
from Prog.src.battery_profile import BatteryProfile
from Prog.src.exceptions import (
    ActivationStopHook,
    ConfigurationError,
    TestAbortedByUser,
)
from Prog.src.integrator import Integrator
from Prog.src.pulse_measurement_service import SYNC_QUALITY_SOFTWARE_TICK_TIMED
from Prog.src.safety import FaultCode, SafetyManager, SafetyResult, TempCompMode

if TYPE_CHECKING:
    from Prog.src.charge_controller import ChargeController

# MAX verifier lelet (governance-v2 kör 2, F1) -- a discharge_controller.py
# _RB_TIME_TOLERANCE_S azonos mintáját követve: mekkora eltérés a nominális
# mintavételi időponttól (sample_delay_1_s/2_s) számít degradáltnak.
_IEC_TIME_TOLERANCE_S = 0.5
_LOGGER = logging.getLogger(__name__)


class IecDcResistanceState(Enum):
    INIT = "INIT"
    PRECHARGE = "PRECHARGE"
    PRECHARGE_RELAX = "PRECHARGE_RELAX"
    LOAD_1 = "LOAD_1"
    OPEN_CIRCUIT_REST = "OPEN_CIRCUIT_REST"
    LOAD_2 = "LOAD_2"
    COMPUTE_RESULT = "COMPUTE_RESULT"
    DONE = "DONE"
    FAULT = "FAULT"


_TERMINAL_STATES = (IecDcResistanceState.DONE, IecDcResistanceState.FAULT)


@dataclass
class IecDcResistanceConfig:
    """A projekt-derivált összefoglaló (SOURCE_REQUIRED, l. modul docstring
    F5-2/IEC-RI-001) minden időzítési paramétert rögzít -- ezek NEM szabad
    GUI-paraméterek, a config csak tesztelhetőség/injektálhatóság miatt
    dataclass."""

    # Első terhelési pont: Ia=4×I10, mintavétel 20 s-nál, kemény cutoff-
    # backstop 25 s-nál (a forrás 5. szakasza szerint -- SOURCE_REQUIRED).
    sample_delay_1_s: float = 20.0
    cutoff_1_s: float = 25.0
    # Második terhelési pont: Ib=20×I10, mintavétel 5 s-nál.
    #
    # R16-B22 / Q-36 D3=A: a runner sajat monoton IEC-blokkot futtat,
    # igy a 2/5/30 s kulso tick nem tolja el az 5 s-os mintapontot.
    # A kesleltetett, nem megszakithato I/O idobelyege es degraded jelzese
    # tovabbra is megmarad; az IEC normativ forrasallapota nem valtozik.
    sample_delay_2_s: float = 5.0
    # Nyitott áramköri pihenés a két pont között.
    rest_duration_s: float = 300.0
    # PROJECT_POLICY (NEM IEC-előírás -- a forrás nem specifikál settle-
    # időt a teljes feltöltés UTÁN, csak magát az "teljesen feltöltött"
    # előfeltételt): a projekt már meglévő, más módoknál is használt
    # konzervatív post-charge relax értékét követi (l. PROJ-RELAX-001,
    # golden_gg_controller.py relax_after_charge_s / ocv_soc_controller.py
    # relax_default_s -- mindegyik 7200s/2h), hogy a frissen feltöltött
    # akku feszültsége settle-ödjön a terhelési pontok mérése előtt.
    relax_before_measurement_s: float = 7200.0

    def __post_init__(self) -> None:
        if self.sample_delay_1_s <= 0 or self.cutoff_1_s <= self.sample_delay_1_s:
            raise ValueError(
                f"cutoff_1_s ({self.cutoff_1_s}) > sample_delay_1_s "
                f"({self.sample_delay_1_s}) > 0 kötelező."
            )
        if self.sample_delay_2_s <= 0:
            raise ValueError(f"sample_delay_2_s > 0 kötelező. Kapott: {self.sample_delay_2_s}")
        if self.rest_duration_s <= 0:
            raise ValueError(f"rest_duration_s > 0 kötelező. Kapott: {self.rest_duration_s}")
        if self.relax_before_measurement_s < 0:
            raise ValueError(
                f"relax_before_measurement_s >= 0 kötelező. "
                f"Kapott: {self.relax_before_measurement_s}"
            )


class IecDcResistanceController:
    """IEC_DC_INTERNAL_RESISTANCE állapotgép."""

    def __init__(
        self,
        psu,
        load,
        dmm_voltage,
        dmm_temperature,
        profile: BatteryProfile,
        safety: SafetyManager,
        config: IecDcResistanceConfig,
        charge_config=None,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if profile.i10_discharge_current_A is None:
            raise ConfigurationError(
                "IEC_DC_INTERNAL_RESISTANCE mód: profile.i10_discharge_current_A "
                "hiányzik -- ez a mérés a IEC 60896-21 §6.3 szerinti Ia=4×I10/"
                "Ib=20×I10 terhelési pontokhoz KÖTELEZŐ, a konkrét akkumodell "
                "saját C10 kisütési táblázatából (10h, 1,80V/cella) megadva. "
                "Nem származtatható automatikusan nominal_capacity_Ah-ból."
            )
        self._psu = psu
        self._load = load
        self._dmm_v = dmm_voltage
        self._dmm_t = dmm_temperature
        self._profile = profile
        self._safety = safety
        self._config = config
        # R16-B12: a mérés közben érkező leállítást az indítás előtt látni kell.
        self.handoff_stop_requested: Callable[[], bool] | None = None
        self._time_source = time_source
        self._iec_block_ready = False
        self._iec_block_active = False
        self._iec_block_halted = False
        self._iec_off_attempted = False
        self._iec_owner: object | None = None
        self._iec_window: dict = {}
        self._iec_windows: list[dict] = []
        self._iec_block_warnings: dict[str, int] = {}
        self._pulse_observed_extra_s = 0.0
        from Prog.src.charge_controller import ChargeConfig
        self._charge_config = charge_config or ChargeConfig()

        i10 = profile.i10_discharge_current_A
        self._ia_A = 4.0 * i10
        self._ib_A = 20.0 * i10

        self._state = IecDcResistanceState.INIT
        # CF-19 (governance-v2 kör 2, DFAM-023) -- l. ocv_soc_controller.py
        # azonos hivatkozású phase_elapsed_s/_enter() docstringje. Ez a
        # controller MAGÁT a phase_elapsed_s-t használja a LOAD_1/LOAD_2/
        # OPEN_CIRCUIT_REST/PRECHARGE_RELAX fázisok belső időzítőjeként is
        # (nincs külön, duplikált számláló) -- minden `_enter()` hívás
        # nullázza, pontosan ott, ahol egy új fázis-időzítésnek kezdődnie
        # kell.
        self._phase_elapsed_s: float = 0.0
        self._fault_reason: str = ""
        # MAX verifier lelet (governance-v2 kör 2, F5, DFAM-011/012 -- l.
        # ocv_soc_controller.py::relax_dmm_fault_total azonos hivatkozású
        # docstringje): session-szintű, fázisváltáskor NEM nullázódó
        # számláló a PRECHARGE_RELAX/OPEN_CIRCUIT_REST alatti DMM-
        # kiesésekre -- a testvér controllerek mintáját követve.
        self._relax_dmm_fault_total: int = 0

        self._charge_ctrl: ChargeController | None = None
        self._load_1_started = False
        self._load_2_started = False
        # METRO-R20-1 (Kör 20 Batch E1, DFAM-001 + DFAM-035-rokon) -- ez a
        # blokk a governance-v2 kör 2 F1 KORRIGÁLT változata. A
        # `_phase_elapsed_s` az ÁLLAPOTBA LÉPÉS óta telt időt méri, ami a
        # setup-tick miatt (az `input_on()` csak az ELSŐ LOAD_1/LOAD_2
        # advance()-hívásban fut le) egy `dt_s`-nyivel TÖBBET mutat, mint
        # amióta a terhelés ténylegesen fel van kapcsolva.
        #
        # A `_load_on_elapsed_s` a TÉNYLEGES terhelési időt hordozza, de
        # NEM a hívó `dt_s`-einek összegzésével -- az a tick-HATÁRTÓL
        # számolna (test_runner.py:1315: actual_dt_s = tick-start -
        # tick-start), és a setup-tick `input_on()` ELŐTTI előtagját
        # (ε = 2 DMM-olvasás + 4-5 SCPI-írás) beleszámolná --, hanem KÉT
        # monoton időbélyeg KÜLÖNBSÉGEKÉNT:
        #   `_load_on_t0_s`        = a saját IEC-blokkban az ON-parancs ELŐTT;
        #                            a legacy advance()-úton a sikeres ON UTÁN
        #   `_t_voltage_sample_s`  = a feszültségolvasás VISSZATÉRÉSI bélyege.
        # A saját blokk ON-/OFF-parancsát és visszatérését külön rögzíti.
        # Az I/U-pár aszinkron: a záró V-mintához az előző iteráció mért
        # árama tartozik. A 0,5 s-os tűrés a V-mintapont célidőtől való
        # eltérését minősíti; az áramminta korát nem minősíti.
        # Az `_time_source` injektálható a determinisztikus tesztekhez.
        #
        # MARADÓ, KÓDDAL NEM MEGSZÜNTETHETŐ KORLÁT (őszintén jelezve,
        # rules/20-metrology.md "ne állíts nagyobb pontosságot"): a
        # tényleges mérési pillanat a DMM apertúra (NPLC) KÖZEPE, a
        # bélyeg viszont az olvasás VISSZATÉRÉSEKOR készül -- ez ~NPLC/2
        # rendszeres eltolás, amit csak műszer-oldali timestamp tüntetne
        # el. Ezért a `synchronization_quality` VÁLTOZATLANUL
        # SOFTWARE_TICK_TIMED marad (NEM "upgrade"-elhető).
        #
        # A saját blokk 25 s-os backstopja ugyanebből az ON-parancs előtti
        # horgonyból számol; a legacy advance()-út fázisidőt használ.
        self._load_on_elapsed_s: float = 0.0
        # MAX verifier korrekció (Kör 20 Batch E1): a run-ág a
        # `_load_1_started`/`_load_2_started` kapu miatt csak a sikeres
        # setup UTÁN fut, tehát a különbség SOHA nem lehet nagyságrendileg
        # hibás -- de NEM garantáltan nem-negatív: ha az ELSŐ mérő tick
        # `read_voltage()`-e dob, `_t_voltage_sample_s` a setup-tick
        # bélyege marad, és a különbség legrosszabb esetben `-ε`
        # (negatív) lehet. Ez az érték azonban SOHA nem jut mintavételbe
        # és sehol nincs exportálva, mert a run-ág első lépése
        # (`if not self._dmm_valid: emergency_stop(...)`) ugyanezen a
        # ticken kilép, MIELŐTT a sample-feltétel kiértékelődne -- l.
        # `_t_voltage_sample_s` alábbi kommentje.
        self._load_on_t0_s: float = 0.0
        # Sikertelen feszültségolvasásnál NEM frissül. Stale bélyeg SOHA
        # nem juthat a `_t_sample_*_s`-be, mert a run-ág eleje
        # (`if not self._dmm_valid: emergency_stop("DMM_FEEDBACK_LOST")`)
        # a mintavétel ELŐTT kilép.
        self._t_voltage_sample_s: float = 0.0
        self._timing_degraded: bool = False
        self._t_sample_1_s: float | None = None
        self._t_sample_2_s: float | None = None

        self._u_batt: float = 0.0
        self._dmm_valid: bool = True
        # Kör 19 Batch D5 (F19-S2, DFAM-001 4. controller-előfordulás -- l.
        # ocv_soc_controller.py::_dmm_valid_before_tick azonos nevű mezőjének
        # docstringje az eredeti indoklásért, BATCH 4.2b F-2/R17-14 mintáját
        # követve): a PRECHARGE_RELAX/OPEN_CIRCUIT_REST alatt korábban MINDEN
        # ticken tüzelt az IEC_RELAX_DMM_FAULT esemény, amíg a DMM-kiesés
        # tartott -- alapértelmezett relax_before_measurement_s=7200s mellett
        # akár ~3600 azonos esemény ürítette volna ki az EventLogWidget
        # 500-soros gyűrűjét egyetlen tartós dropout alatt. Ez az "előző tick
        # érvényessége" jelző teszi lehetővé, hogy csak az ÁTMENETKOR
        # (érvényes -> érvénytelen) tüzeljünk -- a _relax_dmm_fault_total
        # számláló továbbra is MINDEN ticken nő, csak az esemény-spam szűnik
        # meg.
        self._dmm_valid_before_tick: bool = True
        self._battery_temperature_C: float = 20.0
        # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás NEW-2
        # (DFAM-024 2. előfordulás + DFAM-021) -- l. chemid_controller.py/
        # golden_gg_controller.py azonos nevű mezőjének docstringje
        # ugyanerről a session-szintű, tick-alapú számlálóról. Itt
        # emellett a MÉRÉSI PILLANAT (LOAD_1/LOAD_2 mintavétel) érvényessége
        # is közvetlenül nyomon követhető _temp_dmm_valid-dal, mert az IEC
        # eredmény KÉT KÜLÖN mintavételi pillanatot fed le (LOAD_1/Ua és
        # LOAD_2/Ub, egymástól a rest_duration_s + egy teljes terhelési
        # ciklus választja el) -- ezért KÉT KÜLÖN hőmérséklet-pár kell
        # (M4-06 review-kör-4: korábban csak az 1. ponthoz volt rögzítve,
        # l. _measurement_temperature_2_C/_valid lent).
        self._temp_dmm_fault_ticks: int = 0
        self._temp_dmm_valid: bool = True
        self._last_warning_code: str = ""
        # MAX verifier (review-kör 4, HW4-02, 329c1a3) N1 lelete -- l.
        # discharge_controller.py azonos hivatkozású mező docstringje.
        # Közös számláló mind a LOAD_1 (Ia), mind a LOAD_2 (Ib)
        # mintavételre -- a két fázis egy sessionben egyszer fut, nincs
        # szükség külön-külön szétbontásra.
        self._load_current_setpoint_warning_ticks: int = 0
        self._temperature_out_of_range_count: int = 0
        # Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        # l. discharge_controller.py::i_load_measured_A/load_current_
        # readback_valid property docstringje -- ugyanaz a tick-scoped
        # validity-flag minta, közös mind a LOAD_1 (Ia), mind a LOAD_2
        # (Ib) fázisra.
        self._i_load_measured_A: float | None = None
        self._load_current_readback_valid: bool | None = None

        self._energy_integrator = Integrator()
        self._last_integration_source: str = "ZERO"  # R6-B3, l. last_integration_source

        self._ua_V: float | None = None
        self._ia_measured_A: float | None = None
        self._ub_V: float | None = None
        self._ib_measured_A: float | None = None
        self._measurement_temperature_C: float | None = None
        self._measurement_temperature_valid: bool = True
        self._measurement_temperature_2_C: float | None = None
        self._measurement_temperature_2_valid: bool = True
        self._result: dict | None = None

        self.on_result: Callable[[dict], None] | None = None
        self.on_event: Callable[[dict], None] | None = None

    # ------------------------------------------------------------------ #
    # Nyilvános tulajdonságok                                              #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> IecDcResistanceState:
        return self._state

    @property
    def phase_elapsed_s(self) -> float:
        """CF-19 -- l. ocv_soc_controller.py::phase_elapsed_s azonos
        hivatkozású docstringje."""
        return self._phase_elapsed_s

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    @property
    def relax_dmm_fault_total(self) -> int:
        """L. ocv_soc_controller.py::relax_dmm_fault_total azonos
        hivatkozású docstringje -- main_window.py::_write_report()
        ebből olvassa ki a session-szintű összeget."""
        return self._relax_dmm_fault_total

    @property
    def temp_dmm_fault_ticks(self) -> int:
        """Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás NEW-2
        -- l. self._temp_dmm_fault_ticks docstringje __init__-ben (a
        chemid_controller.py/golden_gg_controller.py azonos property-jének
        testvére). L. main_window.py::_write_report()
        iec_dc_resistance_temp_dmm_fault_ticks mezője. Review-kör 3
        (2026-09-09), MAX verifier (a306e7e764e3a717b) P2-1, DFAM-001
        további előfordulása: a beágyazott ChargeController (PRECHARGE
        alatt) saját összegét is hozzáadja, a temperature_out_of_range_
        count property mintáját követve."""
        return self._temp_dmm_fault_ticks + (
            self._charge_ctrl.temp_dmm_fault_ticks
            if self._charge_ctrl is not None else 0
        )

    @property
    def load_current_setpoint_warning_ticks(self) -> int:
        """MAX verifier (review-kör 4, HW4-02) N1 lelete -- l.
        discharge_controller.py azonos property docstringje. Közös
        számláló a LOAD_1/LOAD_2 mintavételre -- l. a mező __init__-beli
        docstringje."""
        return self._load_current_setpoint_warning_ticks

    @property
    def result(self) -> dict | None:
        """A COMPUTE_RESULT-ban kiszámolt Ri/Isc eredmény, vagy None,
        amíg a mérés nem fejeződött be."""
        if self._result is None and self._iec_windows and (
            self._iec_block_halted or self._state == IecDcResistanceState.FAULT
        ):
            return {
                "load_windows": [dict(window) for window in self._iec_windows],
                "quality": "INVALID_MEASUREMENT_MISSING",
                "timing_degraded": self._timing_degraded,
            }
        return self._result

    @property
    def charge_config(self):
        return self._charge_config

    @property
    def accumulated_charge_Ah(self) -> float:
        return self._energy_integrator.accumulated_charge_Ah

    @property
    def accumulated_discharge_Ah(self) -> float:
        return self._energy_integrator.accumulated_discharge_Ah

    @property
    def i_load_measured_A(self) -> float | None:
        """Teljes kódbázis review 2. kör (2026-09-15, R2-06, 12. writer-kör):
        l. discharge_controller.py::i_load_measured_A property docstringje.
        Közös mind a LOAD_1 (Ia), mind a LOAD_2 (Ib) fázisra."""
        return self._i_load_measured_A

    @property
    def load_current_readback_valid(self) -> bool | None:
        """L. i_load_measured_A docstringje."""
        return self._load_current_readback_valid

    @property
    def accumulated_charge_Wh(self) -> float:
        return self._energy_integrator.accumulated_charge_Wh

    @property
    def accumulated_discharge_Wh(self) -> float:
        return self._energy_integrator.accumulated_discharge_Wh

    @property
    def extreme_dt_s_count(self) -> int:
        return self._energy_integrator.extreme_dt_s_count

    @property
    def max_observed_dt_s(self) -> float:
        return self._energy_integrator.max_observed_dt_s

    @property
    def u_psu_set_V(self) -> float:
        return self._charge_ctrl.u_psu_set_V if self._charge_ctrl is not None else 0.0

    @property
    def i_psu_set_A(self) -> float:
        return self._charge_ctrl.i_psu_set_A if self._charge_ctrl is not None else 0.0

    @property
    def i_psu_measured_A(self) -> float | None:
        """Review-kör 6 (2026-09-15), C-08a — l. ChargeController.
        i_psu_measured_A/psu_current_readback_valid docstringje. `None`,
        ha nincs beágyazott ChargeController (test_runner.py ekkor a
        nyers read_all() readbackre esik vissza)."""
        return self._charge_ctrl.i_psu_measured_A if self._charge_ctrl is not None else None

    @property
    def psu_current_readback_valid(self) -> bool | None:
        """Review-kör 6 (2026-09-15), C-08a — l. i_psu_measured_A fenti
        testvér-property docstringje."""
        return self._charge_ctrl.psu_current_readback_valid if self._charge_ctrl is not None else None

    @property
    def u_psu_measured_V(self) -> float | None:
        """Review-kör 6 (2026-09-15), C-08b — l. ChargeController.
        u_psu_measured_V/psu_voltage_readback_valid docstringje."""
        return self._charge_ctrl.u_psu_measured_V if self._charge_ctrl is not None else None

    @property
    def psu_voltage_readback_valid(self) -> bool | None:
        """Review-kör 6 (2026-09-15), C-08b — l. u_psu_measured_V fenti
        testvér-property docstringje."""
        return self._charge_ctrl.psu_voltage_readback_valid if self._charge_ctrl is not None else None

    @property
    def last_integration_source(self) -> str:
        # R6-B3 (R6-metrology-01, DFAM-001): a kompozit SAJAT, tick-scoped
        # rogzitett erteke, nem a gyermek-ChargeController elo proxyja.
        # Szerzodes: (1) advance() eleje: "ZERO" (kompozit tick eleji
        # reset); (2) kisutesi integralasi pont: "LOAD_READBACK" (mert
        # nulla aramnal is); (3) toltesi ag: a gyermek MEGORZOTT (sticky)
        # forrasanak atvetele a gyermek advance()-a UTAN, az elengedese
        # ELOTT. A (3) altal masolt "PSU_READBACK" onmagaban NEM bizonyit
        # friss integralast vagy nem nulla deltat ezen a ticken.
        # (4) IEC ON-ablak áramminta nélkül: "UNAVAILABLE", nem mért nulla.
        return self._last_integration_source

    @property
    def taper_timer_s(self) -> float:
        return self._charge_ctrl.taper_timer_s if self._charge_ctrl is not None else 0.0

    @property
    def last_fast_loop_extra_s(self) -> float:
        """Review-kör 5, Metrológia M5-01 (P1) -- ugyanaz a hiányzó proxy-
        property minta, mint fent a taper_timer_s-nél. A TestRunner ezt a
        beágyazott ChargeController fast-loop mechanizmusa (l.
        charge_controller.py azonos property docstringje) által MÁR
        elintegrált extra időt olvassa ki, hogy a KÖVETKEZŐ tick dt_s-éből
        levonja (elkerülve a duplán-integrálást a PRECHARGE fázisban)."""
        return self._charge_ctrl.last_fast_loop_extra_s if self._charge_ctrl is not None else 0.0

    @property
    def i_load_set_A(self) -> float:
        if self._state == IecDcResistanceState.LOAD_1:
            return self._ia_A
        if self._state == IecDcResistanceState.LOAD_2:
            return self._ib_A
        return 0.0

    @property
    def u_batt_V(self) -> float:
        return self._u_batt

    @property
    def battery_temperature_C(self) -> float:
        return self._battery_temperature_C

    @property
    def dmm_valid(self) -> bool:
        return self._dmm_valid

    @property
    def temp_dmm_valid(self) -> bool:
        """Review-kör 3 (2026-09-09), friss Metrológia M3-01 (P1, DFAM-001+
        DFAM-018) -- l. discharge_controller.py azonos property docstringje
        a dmm_valid (feszültség) testvér-mintájáról. Ugyanaz a mező, amit a
        result["temperature_valid"] a mérési pillanatra befagyaszt."""
        return self._temp_dmm_valid

    @property
    def last_warning_code(self) -> str:
        if self._charge_ctrl is not None and self._charge_ctrl.last_warning_code:
            return self._charge_ctrl.last_warning_code
        return self._last_warning_code

    @property
    def temperature_out_of_range_count(self) -> int:
        base = self._temperature_out_of_range_count
        if self._charge_ctrl is not None:
            base += self._charge_ctrl.temperature_out_of_range_count
        return base

    # ------------------------------------------------------------------ #
    # CF-19: egységes állapotváltás-helper                                 #
    # ------------------------------------------------------------------ #

    def _enter(self, state: IecDcResistanceState) -> None:
        """L. ocv_soc_controller.py::_enter() azonos hivatkozású docstringje."""
        self._state = state
        self._phase_elapsed_s = 0.0

    # ------------------------------------------------------------------ #
    # Fő advance() — egy vezérlési ciklus                                  #
    # ------------------------------------------------------------------ #

    def advance(self, dt_s: float) -> IecDcResistanceState:
        # R2-06 (2026-09-15): tick-scoped validity flag -- MINDEN korai-
        # return és safety-kapu ELŐTT nullázva (l. feedback_new_tick_
        # scoped_validity_flag_needs_explicit_reset.md).
        self._i_load_measured_A = None
        self._load_current_readback_valid = None
        # R6-B3: kompozit tick eleji reset (l. last_integration_source);
        # a kisutesi integralasi pont, ill. a toltesi ag atvetele irja felul.
        self._last_integration_source = "ZERO"

        if self._state in _TERMINAL_STATES or self._iec_block_halted:
            return self._state
        if self._iec_block_ready and self._state in (
            IecDcResistanceState.LOAD_1, IecDcResistanceState.LOAD_2,
        ):
            # R16-B22: az IEC-blokk az ON-t es az elso mintat is maga hajtja.
            return self._state

        self._phase_elapsed_s += dt_s
        self._last_warning_code = ""

        # PRECHARGE alatt a beágyazott ChargeController az egyetlen DMM-
        # olvasási tulajdonos (CLAUDE.md §9) -- minden más állapotban ez a
        # réteg olvas.
        if self._state != IecDcResistanceState.PRECHARGE:
            self._dmm_valid_before_tick = self._dmm_valid
            self._dmm_valid = self._read_dmm(dt_s)
            t_result = self._check_temperature_dmm_fault()
            if t_result.fault is not None:
                self.emergency_stop(t_result.fault.name)
                return self._state
            if t_result.warning is not None:
                self._last_warning_code = t_result.warning.name

            # R11-B4: az aktiv kisutes ugyanazt a kozos kaput hasznalja.
            if self._state in (IecDcResistanceState.LOAD_1, IecDcResistanceState.LOAD_2):
                overtemp = self._safety.check_battery_overtemperature(
                    self._battery_temperature_C, self._temp_dmm_valid,
                )
                if overtemp.fault is not None:
                    self.emergency_stop(overtemp.fault.name)
                    return self._state

            range_result = self._check_test_temperature_range()
            if range_result.warning is not None and t_result.warning is None:
                self._last_warning_code = range_result.warning.name

        if self._state == IecDcResistanceState.INIT:
            self._enter_precharge()
        elif self._state == IecDcResistanceState.PRECHARGE:
            self._run_precharge(dt_s)
        elif self._state == IecDcResistanceState.PRECHARGE_RELAX:
            self._run_precharge_relax(dt_s)
        elif self._state == IecDcResistanceState.LOAD_1:
            self._run_load_1(dt_s)
        elif self._state == IecDcResistanceState.OPEN_CIRCUIT_REST:
            self._run_open_circuit_rest(dt_s)
        elif self._state == IecDcResistanceState.LOAD_2:
            self._run_load_2(dt_s)
        elif self._state == IecDcResistanceState.COMPUTE_RESULT:
            self._run_compute_result()

        else:
            # Review-kör 5, Session/Workflow S5-05 (P3, DFAM-004 sibling) --
            # l. charge_controller.py::advance() azonos hivatkozású
            # kommentje ugyanerről a hibaosztályról. A _TERMINAL_STATES-t
            # (DONE/FAULT) a függvény ELEJÉN lévő korai return már lefedi.
            raise ValueError(f"Unhandled IecDcResistanceState in advance(): {self._state!r}")

        return self._state

    # ------------------------------------------------------------------ #
    # Vészleállítás [R1] — LOAD OFF → PSU OFF, relay soha                  #
    # ------------------------------------------------------------------ #

    def emergency_stop(self, reason: str) -> None:
        if self._iec_block_active:
            self._fault_reason = (
                self._fault_reason + " | " + reason if self._fault_reason else reason
            )
            self._enter(IecDcResistanceState.FAULT)
            self._iec_off()
            return
        off_failures: list[str] = []
        try:
            self._load.input_off()
        except Exception as exc:
            off_failures.append(f"LOAD_OFF_FAILED:{exc}")
        try:
            self._psu.all_outputs_off()
        except Exception as exc:
            off_failures.append(f"PSU_OFF_FAILED:{exc}")
        self._fault_reason = reason if not off_failures else reason + " | " + " | ".join(off_failures)
        self._enter(IecDcResistanceState.FAULT)

    # ------------------------------------------------------------------ #
    # Állapot implementációk                                               #
    # ------------------------------------------------------------------ #

    def _enter_precharge(self) -> None:
        """INIT → PRECHARGE: az IEC 60896-21 §6.3 előfeltétele (teljesen
        feltöltött akku) -- ugyanaz a beágyazott ChargeController minta,
        mint OcvSocController/GoldenGgController/stb."""
        from Prog.src.charge_controller import ChargeController
        self._charge_ctrl = ChargeController(
            self._psu, self._load, self._dmm_v, self._dmm_t,
            self._profile, self._safety, self._charge_config,
        )
        self._enter(IecDcResistanceState.PRECHARGE)

    def _run_precharge(self, dt_s: float) -> None:
        from Prog.src.charge_controller import ChargeState
        assert self._charge_ctrl is not None
        # R1 B8a, F-20 (DFAM-035): a gyermek integrátorának állása az
        # advance() ELŐTT -- l. lent az add_charge_delta() kommentjét.
        charge_Ah_before = self._charge_ctrl.accumulated_charge_Ah
        charge_Wh_before = self._charge_ctrl.accumulated_charge_Wh
        try:
            # R16-B12: a beágyazott töltő is a runner élő leállításjelét figyeli.
            self._charge_ctrl.handoff_stop_requested = self.handoff_stop_requested
            charge_state = self._charge_ctrl.advance(dt_s)
        except BaseException:
            # R2-B5 (R2-session-02, DFAM-033): kivételes kilépéskor (pl.
            # KeyboardInterrupt a fast-loop alvásában) a gyermek által MÁR
            # elintegrált delta és a dt-diagnosztika egyszer itt kerül át --
            # csak property-olvasás és aritmetika, I/O nincs; az eredeti
            # kivétel csupasz raise-zel változatlanul továbbmegy. NEM
            # finally: a normál út lent adja át, finally-val duplázna.
            self._energy_integrator.add_charge_delta(
                self._charge_ctrl.accumulated_charge_Ah - charge_Ah_before,
                self._charge_ctrl.accumulated_charge_Wh - charge_Wh_before,
                dt_s + self._charge_ctrl.last_fast_loop_extra_s,
            )
            raise

        self._dmm_valid = self._charge_ctrl.dmm_valid
        # Review-kör 3 (2026-09-09), MAX verifier (a3f0e6dcf93588360) P2-1:
        # a feszültség-ikret (fenti sor) mindig átvezettük a beágyazott
        # ChargeControllerből, a hőmérsékletit nem -- emiatt a
        # temp_dmm_valid property PRECHARGE alatt (ahol a saját
        # _read_dmm() ki van kapcsolva) hamisan True maradt egy
        # ténylegesen halott hőmérséklet-DMM mellett is.
        self._temp_dmm_valid = self._charge_ctrl.temp_dmm_valid
        self._u_batt = self._charge_ctrl.u_batt_V
        self._battery_temperature_C = self._charge_ctrl.battery_temperature_C

        # R1 B8a, F-20 (DFAM-035, RECURRING; a C-06 fix utódja) -- l.
        # ocv_soc_controller.py::_run_precharge() azonos hivatkozású
        # kommentje: a session-szintű Ah/Wh a beágyazott ChargeController
        # EZEN a ticken (fast-loop résztickjeivel együtt) elintegrált
        # deltáját veszi át -- a belső integrál az EGYETLEN numerikus
        # forrás. A FAULT és a CHARGE_DONE ág (gyermek eldobása) ELŐTT.
        self._energy_integrator.add_charge_delta(
            self._charge_ctrl.accumulated_charge_Ah - charge_Ah_before,
            self._charge_ctrl.accumulated_charge_Wh - charge_Wh_before,
            dt_s + self._charge_ctrl.last_fast_loop_extra_s,
        )
        # R6-B3: a gyermek megorzott forrasanak atvetele (nem friss-integralas-
        # bizonyitek), a gyermek elengedese ELOTT.
        self._last_integration_source = self._charge_ctrl.last_integration_source

        if charge_state == ChargeState.FAULT:
            self.emergency_stop(self._charge_ctrl.fault_reason or "PRECHARGE_FAULT")
        elif charge_state == ChargeState.CHARGE_DONE:
            self._temperature_out_of_range_count += self._charge_ctrl.temperature_out_of_range_count
            # Review-kör 3 (2026-09-09), MAX verifier (a306e7e764e3a717b)
            # P2-1 -- l. temp_dmm_fault_ticks property docstringje, ugyanaz
            # a carry-over minta, mint a fenti temperature_out_of_range_
            # count-nál.
            self._temp_dmm_fault_ticks += self._charge_ctrl.temp_dmm_fault_ticks
            self._charge_ctrl = None
            self._enter(IecDcResistanceState.PRECHARGE_RELAX)

    def _run_precharge_relax(self, dt_s: float) -> None:
        """Settle a friss teljes töltés után, a terhelési pontok mérése
        előtt (PROJECT_POLICY, l. IecDcResistanceConfig.
        relax_before_measurement_s docstringje -- NEM IEC-előírás)."""
        if not self._dmm_valid:
            # MAX verifier lelet (governance-v2 kör 2, F5, DFAM-011/012
            # -- l. ocv_soc_controller.py::_run_relax_phase() azonos
            # hivatkozású kommentje): a DMM-kiesés eddig sem élő
            # eseményt, sem session-szintű számlálót nem kapott.
            self._relax_dmm_fault_total += 1
            # Kör 19 Batch D5 (F19-S2): csak az ÁTMENETKOR (érvényes ->
            # érvénytelen) tüzelünk, l. _dmm_valid_before_tick docstringje --
            # a számláló fent változatlanul minden ticken nő.
            if self._dmm_valid_before_tick and self.on_event is not None:
                self.on_event({
                    "event_code": "IEC_RELAX_DMM_FAULT",
                    "event_message": (
                        "DMM olvasási hiba a töltés utáni relax alatt "
                        f"(hiba #{self._relax_dmm_fault_total})"
                    ),
                })
        self._energy_integrator.add_sample(0.0, self._u_batt, dt_s)
        if self._phase_elapsed_s >= self._config.relax_before_measurement_s:
            self._enter(IecDcResistanceState.LOAD_1)

    def _run_load_1(self, dt_s: float) -> None:
        """Az IEC első terhelési pontjának legacy advance()-útja.

        Ia=4×I10; a monoton ON-visszatéréshez mért V-mintabélyeg célja
        sample_delay_1_s, utána azonnali Load OFF. Ez az út csak külső
        híváskor halad; a runner a saját órájú run_pulse_block()-ot használja.
        Ott az ON-parancs ELŐTTI közös horgonyhoz tartozik a 20 s-os cél
        és a 25 s-os cutoff, a külső runner-ticktől függetlenül.

        Mindkét úton IEC_LOAD_1_CUTOFF_DEADLINE_EXCEEDED FAULT állítja
        le a cutoffot túllépő terhelést; ilyen minta nem fogadható el Ua-ként.
        A saját blokk nem hosszabbítja az ON-ablakot mintapótlásért.
        A fizikai kapcsolási késés és a nem megszakítható I/O HIL_PENDING;
        az IEC 60896-21 §6.3 normatív megfelelősége SOURCE_REQUIRED.

        A legacy setup-tick az ON után visszatér. Konfiguráció és ON előtt
        DMM-, feszültség-, setpoint-teljesítmény- és egyidejűség-kapu fut.
        """
        # GOV-029/Q-34: setup és minden terhelt minta; Q-35 kivétel nélkül.
        if self._dmm_valid and not self._safety.enforce_normal_discharge(
            self._u_batt, self._ia_A, self._profile.terminate_voltage_pack_V,
            self.emergency_stop, self.on_event,
        ):
            return
        if not self._load_1_started:
            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return
            v_result = self._safety.check_battery_voltage(self._u_batt)
            if v_result.fault is not None:
                self.emergency_stop(v_result.fault.name)
                return
            power_result = self._safety.check_load_power(self._u_batt, self._ia_A)
            if power_result.fault is not None:
                self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
                return
            concurrent_result = self._safety.check_concurrent_psu_load(
                psu_commanded_on=self._psu.output_commanded_on,
                load_commanded_on=True,
            )
            if concurrent_result.fault is not None:
                self.emergency_stop(concurrent_result.fault.name)
                return
            # R16-B12: a tick-eleji DMM-olvasás alatt is érkezhet leállítás.
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                return
            try:
                self._psu.all_outputs_off()
                self._load.set_mode_cc()
                self._load.set_current(self._ia_A)
                self._load.input_on(stop_requested=self.handoff_stop_requested)
            except TestAbortedByUser:
                # Nyugta nélkül nincs időbélyeg vagy sikeres terhelési szint.
                return
            except Exception as exc:
                self.emergency_stop(f"IEC_LOAD_1_SETUP_FAILED: {exc}")
                return
            self._load_1_started = True
            self._load_on_t0_s = self._time_source()
            self._load_on_elapsed_s = 0.0
            return

        self._load_on_elapsed_s = self._t_voltage_sample_s - self._load_on_t0_s

        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        try:
            i_load = self._load.measure_current()
            self._i_load_measured_A = i_load
            self._load_current_readback_valid = True
        except Exception:
            self._load_current_readback_valid = False
            self.emergency_stop("LOAD_COMM_LOST")
            return

        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        power_result = self._safety.check_load_power(self._u_batt, i_load)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return
        v_result = self._safety.check_battery_voltage(self._u_batt)
        if v_result.fault is not None:
            self.emergency_stop(v_result.fault.name)
            return

        # HW4-02 (review-kör 4, P1, DFAM-001) -- l. discharge_controller.py::
        # _run_cc() azonos hivatkozású kommentje/SafetyManager.check_load_
        # current_setpoint() docstringje. Ia setpoint (LOAD_1). MAX
        # verifier N1 lelete: a tick-számláló a _last_warning_code
        # slot-tól FÜGGETLENÜL nő.
        setpoint_result = self._safety.check_load_current_setpoint(i_load, self._ia_A)
        if setpoint_result.warning is not None:
            self._load_current_setpoint_warning_ticks += 1
            if self._last_warning_code == "":
                self._last_warning_code = setpoint_result.warning.name

        self._energy_integrator.add_sample(-i_load, self._u_batt, dt_s)
        self._last_integration_source = "LOAD_READBACK"

        # Forrás hard-deadline (§5. szakasz): a terhelőáramot LEGKÉSŐBB
        # 25 s után meg kell szakítani -- ez egy REDUNDÁNS biztonsági
        # backstop; a normál útvonalon a mintavétel (lent) mindig
        # sample_delay_1_s-nél zárja le a fázist, jóval cutoff_1_s előtt.
        # A `_phase_elapsed_s`-t (állapotba lépés óta, ezért mindig >= a
        # `_load_on_elapsed_s`-nél) használja referenciának -- ez
        # KONZERVATÍVABB (korábban old ki), nem gyengíti a safety-t.
        if self._phase_elapsed_s > self._config.cutoff_1_s:
            self.emergency_stop("IEC_LOAD_1_CUTOFF_DEADLINE_EXCEEDED")
            return

        if self._load_on_elapsed_s >= self._config.sample_delay_1_s:
            self._ua_V = self._u_batt
            self._ia_measured_A = i_load
            self._measurement_temperature_C = self._battery_temperature_C
            # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás
            # NEW-2: a fenti temperature_C korábban akkor is quality="OK"-
            # ként exportálódott, ha a hőmérséklet-DMM éppen ekkor (vagy
            # korábban, stale-en) hibázott -- self._temp_dmm_valid a
            # LEGUTÓBBI olvasási kísérlet tényleges kimenetét tükrözi
            # (l. _read_dmm()), itt fagyasztjuk be a mintavétel pillanatára.
            self._measurement_temperature_valid = self._temp_dmm_valid
            self._t_sample_1_s = self._load_on_elapsed_s
            if abs(self._load_on_elapsed_s - self._config.sample_delay_1_s) > _IEC_TIME_TOLERANCE_S:
                self._timing_degraded = True
            try:
                self._load.input_off()
            except Exception as exc:
                self.emergency_stop(f"IEC_LOAD_1_OFF_FAILED: {exc}")
                return
            self._enter(IecDcResistanceState.OPEN_CIRCUIT_REST)

    def _run_open_circuit_rest(self, dt_s: float) -> None:
        """5 perc nyitott áramköri pihenés, nincs visszatöltés (Load OFF
        és PSU OFF már megtörtént a LOAD_1 lezárásakor/emergency_stop()
        útján)."""
        if not self._dmm_valid:
            self._relax_dmm_fault_total += 1
            # Kör 19 Batch D5 (F19-S2): l. _run_precharge_relax() azonos
            # hivatkozású kommentje ugyanerről az átmenet-alapú kapuról.
            if self._dmm_valid_before_tick and self.on_event is not None:
                self.on_event({
                    "event_code": "IEC_RELAX_DMM_FAULT",
                    "event_message": (
                        "DMM olvasási hiba a nyitott áramköri pihenő alatt "
                        f"(hiba #{self._relax_dmm_fault_total})"
                    ),
                })
        self._energy_integrator.add_sample(0.0, self._u_batt, dt_s)
        if self._phase_elapsed_s >= self._config.rest_duration_s:
            self._enter(IecDcResistanceState.LOAD_2)

    def _run_load_2(self, dt_s: float) -> None:
        """IEC 60896-21 §6.3, 2. terhelési pont: Ib=20×I10, mintavétel
        Ub/Ib a load-on pillanata óta eltelt sample_delay_2_s-nél, majd
        Load OFF. L. _run_load_1() docstringje ugyanerről a load-on
        elapsed idő mintáról (MAX verifier F1) ÉS a pre-actuation safety-
        kapukról (MAX verifier 2. kör V1)."""
        # GOV-029/Q-34: setup és minden terhelt minta; Q-35 kivétel nélkül.
        if self._dmm_valid and not self._safety.enforce_normal_discharge(
            self._u_batt, self._ib_A, self._profile.terminate_voltage_pack_V,
            self.emergency_stop, self.on_event,
        ):
            return
        if not self._load_2_started:
            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return
            v_result = self._safety.check_battery_voltage(self._u_batt)
            if v_result.fault is not None:
                self.emergency_stop(v_result.fault.name)
                return
            power_result = self._safety.check_load_power(self._u_batt, self._ib_A)
            if power_result.fault is not None:
                self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
                return
            concurrent_result = self._safety.check_concurrent_psu_load(
                psu_commanded_on=self._psu.output_commanded_on,
                load_commanded_on=True,
            )
            if concurrent_result.fault is not None:
                self.emergency_stop(concurrent_result.fault.name)
                return
            # R16-B12: a tick-eleji DMM-olvasás alatt is érkezhet leállítás.
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                return
            try:
                self._psu.all_outputs_off()
                self._load.set_mode_cc()
                self._load.set_current(self._ib_A)
                self._load.input_on(stop_requested=self.handoff_stop_requested)
            except TestAbortedByUser:
                # Nyugta nélkül nincs időbélyeg vagy sikeres terhelési szint.
                return
            except Exception as exc:
                self.emergency_stop(f"IEC_LOAD_2_SETUP_FAILED: {exc}")
                return
            self._load_2_started = True
            self._load_on_t0_s = self._time_source()
            self._load_on_elapsed_s = 0.0
            return

        self._load_on_elapsed_s = self._t_voltage_sample_s - self._load_on_t0_s

        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return
        try:
            i_load = self._load.measure_current()
            self._i_load_measured_A = i_load
            self._load_current_readback_valid = True
        except Exception:
            self._load_current_readback_valid = False
            self.emergency_stop("LOAD_COMM_LOST")
            return

        concurrent_result = self._safety.check_concurrent_psu_load(
            psu_commanded_on=self._psu.output_commanded_on,
            load_commanded_on=self._load.input_commanded_on,
        )
        if concurrent_result.fault is not None:
            self.emergency_stop(concurrent_result.fault.name)
            return
        power_result = self._safety.check_load_power(self._u_batt, i_load)
        if power_result.fault is not None:
            self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
            return
        v_result = self._safety.check_battery_voltage(self._u_batt)
        if v_result.fault is not None:
            self.emergency_stop(v_result.fault.name)
            return

        # HW4-02 (review-kör 4, P1, DFAM-001) -- l. discharge_controller.py::
        # _run_cc() azonos hivatkozású kommentje/SafetyManager.check_load_
        # current_setpoint() docstringje. Ib setpoint (LOAD_2). MAX
        # verifier N1 lelete: a tick-számláló a _last_warning_code
        # slot-tól FÜGGETLENÜL nő.
        setpoint_result = self._safety.check_load_current_setpoint(i_load, self._ib_A)
        if setpoint_result.warning is not None:
            self._load_current_setpoint_warning_ticks += 1
            if self._last_warning_code == "":
                self._last_warning_code = setpoint_result.warning.name

        self._energy_integrator.add_sample(-i_load, self._u_batt, dt_s)
        self._last_integration_source = "LOAD_READBACK"

        if self._load_on_elapsed_s >= self._config.sample_delay_2_s:
            self._ub_V = self._u_batt
            self._ib_measured_A = i_load
            self._measurement_temperature_2_C = self._battery_temperature_C
            # M4-06 (review-kör-4): l. _run_load_1() azonos mintavételi
            # pillanatra befagyasztott temperature_valid kommentje -- a
            # Ub-mérés a LOAD_1-hez képest rest_duration_s + egy teljes
            # terhelési ciklus múlva történik, saját hőmérséklet-mintavétel
            # és érvényesség-jelzés nélkül a mező hamisan sugallhatta, hogy
            # az 1. ponton olvasott hőmérséklet a 2. pontra is érvényes.
            self._measurement_temperature_2_valid = self._temp_dmm_valid
            self._t_sample_2_s = self._load_on_elapsed_s
            if abs(self._load_on_elapsed_s - self._config.sample_delay_2_s) > _IEC_TIME_TOLERANCE_S:
                self._timing_degraded = True
            try:
                self._load.input_off()
            except Exception as exc:
                self.emergency_stop(f"IEC_LOAD_2_OFF_FAILED: {exc}")
                return
            self._enter(IecDcResistanceState.COMPUTE_RESULT)

    def _run_compute_result(self) -> None:
        """Ri = (Ua-Ub)/(Ib-Ia); Isc = (Ua×Ib-Ub×Ia)/(Ua-Ub) -- a projekt-
        derivált összefoglaló (IEC 60896-21 §6.3 alapján, SOURCE_REQUIRED,
        l. modul docstring F5-2/IEC-RI-001) képletei."""
        ua, ia = self._ua_V, self._ia_measured_A
        ub, ib = self._ub_V, self._ib_measured_A
        quality = "OK"
        ri_ohm: float | None = None
        isc_A: float | None = None

        if ua is None or ub is None or ia is None or ib is None:
            quality = "INVALID_MEASUREMENT_MISSING"
        elif ib <= ia:
            # MAX verifier lelet (governance-v2 kör 2, F2): a forrás szerint
            # Ib=20×I10 > Ia=4×I10 -- ha a MÉRT áramok ezt nem teljesítik
            # (pl. a load a beállított 20×I10-et nem tudta leadni és a
            # sajátkorlátjára állt be, vagy egy leolvasási hiba miatt
            # Ib==Ia), a Ri/Isc nevezője (Ib-Ia) nulla vagy negatív lenne --
            # ez korábban VAGY ZeroDivisionError-t dobott (ib==ia, kiszökött
            # az advance()-ból, UNHANDLED_EXCEPTION-né lett a runnerben), VAGY
            # egy fizikailag lehetetlen NEGATÍV Ri/Isc-t irt "OK" quality-vel
            # a report.json-ba (ib<ia). Ezt az ELLENŐRZÉST kell ELŐBB
            # elvégezni, mint az ua<=ub guardot (lent), mert az áramhiba
            # önmagában, a feszültségektől függetlenül érvényteleníti a
            # mérést.
            quality = "INVALID_LOAD_CURRENT_PAIR"
        elif ua <= ub:
            # Fizikailag implauzibilis: Ib > Ia (nagyobb terhelőáram), tehát
            # a feszültségnek ALACSONYABBNAK kell lennie a 2. pontban
            # (Ub < Ua) -- ha nem, a mérés nem forrás-konform, Ri/Isc nem
            # számolható megbízhatóan (elkerüli a nulla/negatív osztást is,
            # l. Isc képlet nevezője (Ua-Ub)).
            quality = "INVALID_RESISTANCE"
        else:
            ri_ohm = (ua - ub) / (ib - ia)
            isc_A = (ua * ib - ub * ia) / (ua - ub)

        self._result = {
            "Ua_V": ua,
            "Ia_A": ia,
            "Ub_V": ub,
            "Ib_A": ib,
            "Ri_ohm": ri_ohm,
            "Isc_A": isc_A,
            "i10_source_A": self._profile.i10_discharge_current_A,
            "temperature_C": self._measurement_temperature_C,
            # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás
            # NEW-2: False, ha a fenti temperature_C a mintavétel
            # pillanatában sikertelen (stale/default) hőmérséklet-DMM-
            # olvasásból származott -- l. _read_dmm()/self._temp_dmm_valid.
            "temperature_valid": self._measurement_temperature_valid,
            "method_origin": "IEC_60896-21_6.3",
            "quality": quality,
            # MAX verifier lelet (governance-v2 kör 2, F1) -- a testvér
            # discharge_controller.py rb_timing_degraded/synchronization_
            # quality mintáját követve: a mintavétel software-tick-timed,
            # ezt a mezők ŐSZINTÉN jelzik, nem állítanak nagyobb
            # pontosságot, mint amit a runner_tick_s alátámaszt (CLAUDE.md
            # §7). A `timing_degraded` NEM írja felül a fenti `quality`-t
            # (ugyanaz az elv, mint az `rb_timing_degraded`-nél).
            "load_windows": list(self._iec_windows),
            "t_sample_1_s": self._t_sample_1_s,
            "t_sample_2_s": self._t_sample_2_s,
            "timing_degraded": self._timing_degraded,
            "synchronization_quality": SYNC_QUALITY_SOFTWARE_TICK_TIMED,
            # M4-06 (review-kör-4): a Ub (2. pont) saját hőmérséklet-
            # mintavétele -- a fenti temperature_C/temperature_valid
            # KIZÁRÓLAG az Ua (1. pont) mintavételi pillanatára vonatkozik.
            "temperature_2_C": self._measurement_temperature_2_C,
            "temperature_2_valid": self._measurement_temperature_2_valid,
        }
        if self.on_result is not None:
            self.on_result(self._result)
        self._enter(IecDcResistanceState.DONE)

    def _check_temperature_dmm_fault(self) -> SafetyResult:
        # A sikertelen belépési mérés nulla eltelt idővel is azonnali hiba
        # ENABLED módban; a kiesés időtartamát nem helyettesítjük kitalált dt-vel.
        if not self._temp_dmm_valid and self._safety.temp_comp_mode == TempCompMode.ENABLED:
            return SafetyResult(fault=FaultCode.TEMPERATURE_MONITOR_LOST_CRITICAL)
        return self._safety.check_temperature_dmm_fault(self._safety.temperature_dmm_fault_s)

    def _check_test_temperature_range(self) -> SafetyResult:
        # Minden friss, érvényes mérés egyszer számít, a legacy tickkel azonosan.
        if not self._temp_dmm_valid:
            return SafetyResult()
        result = self._safety.check_test_temperature_range(self._battery_temperature_C)
        if result.warning is not None:
            self._temperature_out_of_range_count += 1
        return result

    def prepare_pulse_block(self) -> None:
        """R16-B22: szuk IEC-adapter; nincs uj worker vagy scheduler."""
        self._pulse_observed_extra_s = 0.0
        self._iec_block_ready = self._state in (
            IecDcResistanceState.LOAD_1, IecDcResistanceState.LOAD_2,
        )

    @property
    def last_pulse_observed_extra_s(self) -> float:
        return self._pulse_observed_extra_s

    def take_pulse_warnings(self) -> dict[str, int]:
        """A runner kivételes lezáráskor is egyszer veszi át a blokk warningjait."""
        warnings, self._iec_block_warnings = self._iec_block_warnings, {}
        return warnings

    def _iec_target_due(self) -> bool:
        on = self._iec_window.get("on_command_s")
        return on is not None and self._time_source() >= on + self._iec_window["target_s"] - 1e-9

    def _iec_off(self) -> None:
        """Mindket OFF-kiserlet lefut, BaseException mellett is; egyszer."""
        if self._iec_off_attempted:
            return
        self._iec_off_attempted = True
        fatal: BaseException | None = None
        for label, operation in (
            ("LOAD", self._load.input_off), ("PSU", self._psu.all_outputs_off),
        ):
            if label == "LOAD":
                self._iec_window["off_command_s"] = self._time_source()
            try:
                operation()
            except BaseException as exc:
                _LOGGER.exception("IEC %s OFF sikertelen", label)
                reason = f"IEC_{label}_OFF_FAILED:{exc}"
                self._fault_reason = (
                    self._fault_reason + " | " + reason if self._fault_reason else reason
                )
                self._enter(IecDcResistanceState.FAULT)
                if not isinstance(exc, Exception) and fatal is None:
                    fatal = exc
            finally:
                if label == "LOAD":
                    self._iec_window["off_return_s"] = self._time_source()
        if fatal is not None:
            raise fatal

    def run_pulse_block(
        self, poll_fault: Callable[[bool, Callable[[], bool]], str | None],
    ) -> dict[str, int] | None:
        """A runner szalan fut; minden I/O elott elo stop/nyugta/deadline."""
        if not self._iec_block_ready or self._iec_block_halted or self._state not in (
            IecDcResistanceState.LOAD_1, IecDcResistanceState.LOAD_2,
        ):
            return None
        self._iec_block_ready = False
        phase = self._state
        first = phase == IecDcResistanceState.LOAD_1
        target = self._config.sample_delay_1_s if first else self._config.sample_delay_2_s
        current_set = self._ia_A if first else self._ib_A
        self._iec_window = {
            "phase": phase.name, "target_s": target,
            "on_command_s": None, "on_return_s": None,
            "off_command_s": None, "off_return_s": None,
        }
        self._iec_windows.append(self._iec_window)
        self._iec_off_attempted = False
        self._iec_block_active = True
        started = self._time_source()
        last_temperature = started
        raw: tuple[float, float, float | None] | None = None
        last_sample: tuple[float, float, float] | None = None
        integrated_until: float | None = None
        reached = False
        tail_permitted = False
        first_poll = True
        ack_pending = False
        final_voltage_due = False
        warnings = self._iec_block_warnings = {}
        setup_io = False
        voltage_io_s = temperature_io_s = input_io_s = current_io_s = poll_io_s = 0.0
        block_error: BaseException | None = None

        def record_error(exc: BaseException, reason: str | None) -> None:
            nonlocal block_error
            # Csak a blokkban elkapott kivétel számít; külső except nem.
            # Az első továbbadandó hiba után a cleanup már nem cserélhet okot.
            if block_error is None:
                block_error = exc
            if reason is not None:
                self._iec_block_halted = True
                detail = f"{reason}:{exc}"
                self._fault_reason = (
                    self._fault_reason + " | " + detail if self._fault_reason else detail
                )
            self._enter(IecDcResistanceState.FAULT)

        def cleanup(operation: Callable[[], None], reason: str | None) -> None:
            try:
                operation()
            except BaseException as exc:
                _LOGGER.exception("IEC lezárási hiba: %s", reason)
                record_error(exc, reason)

        def check_cleanup_stop() -> None:
            if self.handoff_stop_requested is not None and self.handoff_stop_requested():
                self._iec_block_halted = True

        def reserve_voltage(next_io_s: float) -> bool:
            nonlocal final_voltage_due
            # A legutóbbi I/O-időkből előretekintünk; mintapótlásért nem
            # indítunk új nem-V I/O-t, és az ON-ablakot sem hosszabbítjuk.
            remaining = self._load_on_t0_s + target - self._time_source()
            final_voltage_due = remaining <= next_io_s + voltage_io_s + 0.02 + 1e-9
            return final_voltage_due

        def allow() -> bool:
            nonlocal ack_pending
            if self._state == IecDcResistanceState.FAULT:
                return False
            stopped = self.handoff_stop_requested is not None and self.handoff_stop_requested()
            concurrent = self._safety.check_concurrent_psu_load(
                psu_commanded_on=self._psu.output_commanded_on,
                load_commanded_on=self._load.input_commanded_on,
            )
            if concurrent.fault is not None:
                self._iec_block_halted |= stopped
                self.emergency_stop(concurrent.fault.name)
                return False
            if stopped:
                self._iec_block_halted = True
                self._iec_off()
                return False
            on = self._iec_window["on_command_s"]
            if on is not None:
                now = self._time_source()
                # A közös poll is nyugtázhat: a következő I/O előtti kapu
                # még a határidőn belül átveszi a driver lezárt ablakát.
                if (
                    ack_pending and now - on < INPUT_ACTIVATION_ACK_TIMEOUT_S
                    and self._load.input_commanded_on
                    and getattr(self._load, "input_on_commanded_at", None) is None
                ):
                    ack_pending = False
                    self._iec_window["ack_return_s"] = now
                # A lejárati snapshot továbbra is minden új poll előtt dönt.
                if (
                    ack_pending and now - on >= INPUT_ACTIVATION_ACK_TIMEOUT_S
                ):
                    self.emergency_stop(
                        "SCPI_CRITICAL_ERROR[Load]: INPUT_STATE_MISMATCH: "
                        "commanded=ON actual=OFF | ON_ACK_EXPIRED",
                    )
                    return False
                if first and now - on >= self._config.cutoff_1_s - 1e-9:
                    self.emergency_stop("IEC_LOAD_1_CUTOFF_DEADLINE_EXCEEDED")
                    return False
                if self._iec_target_due() and not final_voltage_due:
                    self._iec_off()
                    return False
            return not self._iec_off_attempted

        def validate_raw() -> None:
            nonlocal raw, last_sample, integrated_until, reached, tail_permitted
            if raw is None:
                return
            voltage, stamp, measured = raw
            raw = None
            # Hibás új nyers adatot a korábbi minta becsült farka sem fedhet le.
            tail_permitted = False
            self._u_batt = voltage
            self._t_voltage_sample_s = stamp
            self._iec_window["last_voltage_return_s"] = stamp
            # NaN/Inf nem lehet érvényes DMM-adat (a lenti Load ON előtti kapu erre épül).
            self._dmm_valid = math.isfinite(voltage)
            # Az OFF-hiba vagy stop sem ugorhatja at a mar visszatert nyers kaput.
            if not math.isfinite(voltage):
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return
            if not self._safety.enforce_normal_discharge(
                voltage, current_set, self._profile.terminate_voltage_pack_V,
                self.emergency_stop, self.on_event,
            ):
                return
            check = self._safety.check_battery_voltage(voltage)
            if check.fault is not None:
                self.emergency_stop(check.fault.name)
                return
            # A cleanup FAULT/STOP állapota nem érvényteleníti a visszatért
            # mérést; az adat saját feszültség-, alsó- és teljesítménykapuja dönt.
            if measured is None:
                return
            check = self._safety.check_load_power(voltage, measured)
            if check.fault is not None:
                self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
                return
            on = self._iec_window["on_command_s"]
            if on is None:
                return
            previous = on if integrated_until is None else integrated_until
            # A nyers V-bélyeg auditadat; OFF után már nincs kisütési intervallum.
            off_return = self._iec_window["off_return_s"]
            end = min(stamp, off_return) if off_return is not None else stamp
            self._energy_integrator.add_sample(-measured, voltage, max(0.0, end - previous))
            self._last_integration_source = "LOAD_READBACK"
            integrated_until = end
            last_sample = (voltage, stamp, measured)
            tail_permitted = True
            self._load_on_elapsed_s = stamp - on
            if self._load_on_elapsed_s >= target - 1e-9:
                reached = True

        try:
            if not allow():
                return warnings
            owner = object()
            self._dmm_v.acquire_measurement(owner)
            self._iec_owner = owner
            if not allow():
                return warnings
            io_started = self._time_source()
            try:
                raw = (self._dmm_v.read_voltage(owner=owner), self._time_source(), None)
            except BaseException as exc:
                tail_permitted = False
                self._dmm_valid = False
                if isinstance(exc, TestAbortedByUser) or not isinstance(exc, Exception):
                    raise
                _LOGGER.exception("IEC kezdeti feszultsegolvasasi hiba")
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return warnings
            voltage_io_s = self._time_source() - io_started
            if not allow():
                return warnings
            validate_raw()
            if not allow():
                return warnings
            io_started = self._time_source()
            try:
                self._battery_temperature_C = self._dmm_t.read_temperature()
                if not math.isfinite(self._battery_temperature_C):
                    raise ValueError("Nem véges IEC-hőmérséklet")
                self._temp_dmm_valid = True
            except BaseException as exc:
                self._temp_dmm_valid = False
                self._temp_dmm_fault_ticks += 1
                if isinstance(exc, TestAbortedByUser) or not isinstance(exc, Exception):
                    raise
                _LOGGER.exception("IEC kezdeti homersekletolvasasi hiba")
            temperature_io_s = self._time_source() - io_started
            last_temperature = io_started
            self._safety.record_temperature_dmm_read(valid=self._temp_dmm_valid, dt_s=0.0)
            temperature_fault = self._check_temperature_dmm_fault()
            if temperature_fault.fault is not None:
                self.emergency_stop(temperature_fault.fault.name)
                return warnings
            overtemp = self._safety.check_battery_overtemperature(
                self._battery_temperature_C, self._temp_dmm_valid,
            )
            if overtemp.fault is not None:
                self.emergency_stop(overtemp.fault.name)
                return warnings
            for check in (temperature_fault, self._check_test_temperature_range()):
                if check.warning is not None:
                    self._last_warning_code = check.warning.name
                    warnings[check.warning.name] = 1
            if not allow():
                return warnings
            power = self._safety.check_load_power(self._u_batt, current_set)
            if power.fault is not None:
                self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
                return warnings
            for operation in (
                self._psu.all_outputs_off, self._load.set_mode_cc,
                lambda: self._load.set_current(current_set),
            ):
                if not allow():
                    return warnings
                setup_io = True
                operation()
                setup_io = False
            if not allow():
                return warnings
            concurrent = self._safety.check_concurrent_psu_load(
                psu_commanded_on=self._psu.output_commanded_on, load_commanded_on=True,
            )
            if concurrent.fault is not None:
                self.emergency_stop(concurrent.fault.name)
                return warnings
            # DFAM-001 (R1 B3-4 szerkezeti kapu): Load ON előtt explicit DMM-érvényesség.
            if not self._dmm_valid:
                self.emergency_stop("DMM_FEEDBACK_LOST")
                return warnings
            ack_pending = True
            # Egyetlen horgony az ON-parancs ELŐTT: célminta, backstop és energia.
            self._iec_window["on_command_s"] = self._time_source()
            self._load_on_t0_s = self._iec_window["on_command_s"]
            setup_io = True
            try:
                self._load.input_on(wait_for_ack=False, stop_requested=self.handoff_stop_requested)
            finally:
                self._iec_window["on_return_s"] = self._time_source()
            setup_io = False
            setattr(self, "_load_1_started" if first else "_load_2_started", True)
            # Az ON visszateresi hataran lejart ablakot a kovetkezo poll elott zarjuk.
            while allow():
                if final_voltage_due:
                    remaining = self._load_on_t0_s + target - self._time_source()
                    if remaining > 1e-9:
                        # Rövid, saját órájú várakozás; minden körben élő kapu.
                        time.sleep(min(0.02, remaining))
                        continue
                    # Visszatérési V-bélyeg, az előző iteráció mért áramával.
                    # A célhoz mért 0,5 s-os tűrés csak a V-mintapontot minősíti.
                    measured = last_sample[2] if last_sample is not None else None
                    try:
                        raw = (self._dmm_v.read_voltage(owner=owner), self._time_source(), measured)
                    except BaseException as exc:
                        tail_permitted = False
                        self._dmm_valid = False
                        if isinstance(exc, TestAbortedByUser) or not isinstance(exc, Exception):
                            raise
                        _LOGGER.exception("IEC vegso feszultsegolvasasi hiba")
                        self.emergency_stop("DMM_FEEDBACK_LOST")
                        break
                    final_voltage_due = False
                    allow()
                    validate_raw()
                    break
                if ack_pending:
                    if reserve_voltage(input_io_s):
                        continue
                    io_started = self._time_source()
                    setup_io = True
                    actual = self._load.read_input_state()
                    setup_io = False
                    input_io_s = self._time_source() - io_started
                    # A driver a pozitiv readbacknel torolheti a sajat jelzojet;
                    # a helyi, meg nyitott ablaknak ettol meg le kellett zarnie.
                    if not allow():
                        break
                    if actual:
                        ack_pending = False
                        self._iec_window["ack_return_s"] = self._time_source()
                if reserve_voltage(poll_io_s):
                    continue
                io_started = self._time_source()
                reason = poll_fault(first_poll, allow)
                poll_io_s = self._time_source() - io_started
                first_poll = False
                if reason is not None:
                    if self._state != IecDcResistanceState.FAULT:
                        self.emergency_stop(reason)
                    break
                if not allow():
                    break
                if ack_pending:
                    # Nyitott nyugtaablakban INP? és közös poll élvez elsőbbséget;
                    # lassú T/V nem tolhatja ki a következő nyugtakísérletet.
                    time.sleep(0.02)
                    continue
                now = self._time_source()
                if now - last_temperature >= 0.1:
                    if reserve_voltage(temperature_io_s):
                        continue
                    dt = now - last_temperature
                    last_temperature = now
                    try:
                        self._battery_temperature_C = self._dmm_t.read_temperature()
                        if not math.isfinite(self._battery_temperature_C):
                            raise ValueError("Nem véges IEC-hőmérséklet")
                        self._temp_dmm_valid = True
                    except BaseException as exc:
                        self._temp_dmm_valid = False
                        self._temp_dmm_fault_ticks += 1
                        if isinstance(exc, TestAbortedByUser) or not isinstance(exc, Exception):
                            raise
                        _LOGGER.exception("IEC homersekletolvasasi hiba")
                    temperature_io_s = self._time_source() - now
                    self._safety.record_temperature_dmm_read(valid=self._temp_dmm_valid, dt_s=dt)
                    for temperature_check in (
                        self._check_temperature_dmm_fault,
                        lambda: self._safety.check_battery_overtemperature(
                            self._battery_temperature_C, self._temp_dmm_valid,
                        ),
                        self._check_test_temperature_range,
                    ):
                        check = temperature_check()
                        if check.fault is not None:
                            self.emergency_stop(check.fault.name)
                            break
                        if check.warning is not None:
                            self._last_warning_code = check.warning.name
                            warnings[check.warning.name] = 1
                    if not allow():
                        break
                if reserve_voltage(current_io_s):
                    continue
                io_started = self._time_source()
                tail_before_current = tail_permitted
                tail_permitted = False
                self._load_current_readback_valid = False
                try:
                    measured = self._load.measure_current()
                except BaseException as exc:
                    self._load_current_readback_valid = False
                    if isinstance(exc, TestAbortedByUser) or not isinstance(exc, Exception):
                        raise
                    _LOGGER.exception("IEC aramolvasasi hiba")
                    self.emergency_stop("LOAD_COMM_LOST")
                    break
                current_io_s = self._time_source() - io_started
                self._i_load_measured_A = measured
                self._load_current_readback_valid = math.isfinite(measured)
                if not self._load_current_readback_valid:
                    self.emergency_stop("LOAD_COMM_LOST")
                    break
                # A friss áram safety-kapuja minden további I/O és warning előtt fut.
                power = self._safety.check_load_power(self._u_batt, measured)
                if power.fault is not None:
                    self.emergency_stop("LOAD_POWER_LIMIT_EXCEEDED")
                    break
                tail_permitted = tail_before_current
                if not allow():
                    break
                check = self._safety.check_load_current_setpoint(measured, current_set)
                if check.warning is not None:
                    # A teljes blokk egy külső tick: a sűrű belső mérés nem
                    # sokszorozhatja meg a session tick-alapú számlálóit.
                    if check.warning.name not in warnings:
                        self._load_current_setpoint_warning_ticks += 1
                    self._last_warning_code = check.warning.name
                    warnings[check.warning.name] = 1
                io_started = self._time_source()
                try:
                    raw = (self._dmm_v.read_voltage(owner=owner), self._time_source(), measured)
                except BaseException as exc:
                    tail_permitted = False
                    self._dmm_valid = False
                    if isinstance(exc, TestAbortedByUser) or not isinstance(exc, Exception):
                        raise
                    _LOGGER.exception("IEC feszultsegolvasasi hiba")
                    self.emergency_stop("DMM_FEEDBACK_LOST")
                    break
                voltage_io_s = self._time_source() - io_started
                # OFF az ujabb T/poll/I/O elott, de a nyers also kapu mindig lefut.
                permitted = allow()
                validate_raw()
                if not permitted or not allow():
                    break
                remaining = self._load_on_t0_s + target - self._time_source()
                reserve_voltage(0.0)
                time.sleep(min(0.02, max(0.0, remaining)))
        except TestAbortedByUser as exc:
            stop_hook = self.handoff_stop_requested
            try:
                stopped = (stop_hook is not None and stop_hook()) or (
                    isinstance(stop_hook, ActivationStopHook) and stop_hook.activation_interrupted
                )
            except BaseException as hook_error:
                _LOGGER.exception("IEC abort utáni stop-lekérdezési hiba")
                self._fault_reason = self._fault_reason or f"UNHANDLED_EXCEPTION: {exc}"
                record_error(exc, None)
                record_error(hook_error, "IEC_STOP_HOOK_FAILED")
            else:
                if stopped:
                    self._iec_block_halted = True
                else:
                    self._fault_reason = self._fault_reason or f"UNHANDLED_EXCEPTION: {exc}"
                    record_error(exc, None)
        except Exception as exc:
            origin = "SETUP_FAILED" if setup_io else "IO_FAILED"
            _LOGGER.exception("IEC %s %s", phase.name, origin)
            reason = f"IEC_{phase.name}_{origin}: {exc}"
            cleanup(lambda: self.emergency_stop(reason), None)
        except BaseException as exc:
            _LOGGER.exception("IEC %s megszakítás", phase.name)
            self._fault_reason = self._fault_reason or f"IEC_{phase.name}_IO_FAILED:{exc}"
            record_error(exc, None)
        finally:
            # Független lezárási lépések: egyetlen kivételprioritás, nincs
            # egymásba ágyazott finally vagy a hívó except-állapotának olvasása.
            cleanup(self._iec_off, None)
            cleanup(validate_raw, f"IEC_{phase.name}_IO_FAILED")

            def check_cutoff() -> None:
                on = self._iec_window["on_command_s"]
                off = self._iec_window["off_command_s"]
                if (
                    first and on is not None
                    and (
                        (off is not None and off - on > self._config.cutoff_1_s)
                        or (last_sample is not None and last_sample[1] - on > self._config.cutoff_1_s)
                    )
                    and "IEC_LOAD_1_CUTOFF_DEADLINE_EXCEEDED" not in self._fault_reason
                ):
                    # Futó I/O átléphette a backstopot: energia megmaradhat,
                    # de Ua nem fogadható el, LOAD_2 nem indulhat.
                    self.emergency_stop("IEC_LOAD_1_CUTOFF_DEADLINE_EXCEEDED")

            def account_tail() -> None:
                # A runner az elszámolt ablak végét még release előtt átveszi.
                off_return = self._iec_window["off_return_s"]
                if off_return is not None:
                    self._pulse_observed_extra_s = max(0.0, off_return - started)
                # A nulla könyvelt energia áramminta nélkül nem bizonyított nulla.
                if self._iec_window["on_command_s"] is not None and last_sample is None:
                    self._last_integration_source = "UNAVAILABLE"
                # Az I/V-engedélytől külön T-kapu csak sikeres, véges
                # T-méréssel nyílik újra; I/V-validálás nem oldhatja fel.
                if (
                    last_sample is not None and tail_permitted
                    and self._temp_dmm_valid and off_return is not None
                ):
                    voltage, stamp, measured = last_sample
                    tail = max(0.0, off_return - stamp)
                    self._energy_integrator.add_sample(-measured, voltage, tail)
                    self._iec_window["tail_estimated_s"] = tail

            def release() -> None:
                if self._iec_owner is not None:
                    self._dmm_v.release_measurement(self._iec_owner)
                    self._iec_owner = None

            def audit_window() -> None:
                self._iec_window["fault_reason"] = self._fault_reason
                self._iec_window["stop_requested"] = self._iec_block_halted

            def finish_window() -> None:
                on = self._iec_window["on_command_s"]
                off = self._iec_window["off_command_s"]
                self._iec_window["command_error_s"] = (
                    off - on - target if on is not None and off is not None else None
                )
                self._iec_window["physical_timing"] = "HIL_PENDING"
                audit_window()

            def emit_window() -> None:
                if self.on_event is not None:
                    self.on_event({
                        "event_code": "IEC_LOAD_WINDOW",
                        "event_message": str(self._iec_window) + (
                            "; energy=UNAVAILABLE: no measured current; Ah/Wh excludes this ON window"
                            if self._last_integration_source == "UNAVAILABLE" else ""
                        ),
                    })

            cleanup(check_cutoff, f"IEC_{phase.name}_IO_FAILED")
            cleanup(account_tail, f"IEC_{phase.name}_IO_FAILED")
            cleanup(release, "IEC_DMM_RELEASE_FAILED")
            self._iec_block_active = False
            cleanup(check_cleanup_stop, "IEC_STOP_HOOK_FAILED")
            cleanup(finish_window, f"IEC_{phase.name}_IO_FAILED")
            cleanup(emit_window, "IEC_WINDOW_EVENT_FAILED")
            cleanup(check_cleanup_stop, "IEC_STOP_HOOK_FAILED")
            cleanup(audit_window, f"IEC_{phase.name}_IO_FAILED")
            if block_error is not None:
                raise block_error
            if self._state != IecDcResistanceState.FAULT and not self._iec_block_halted:
                self._timing_degraded |= not reached
                if reached and last_sample is not None:
                    voltage, stamp, measured = last_sample
                    elapsed = stamp - self._load_on_t0_s
                    self._timing_degraded |= abs(elapsed - target) > _IEC_TIME_TOLERANCE_S
                    if first:
                        self._ua_V, self._ia_measured_A, self._t_sample_1_s = voltage, measured, elapsed
                        self._measurement_temperature_C = self._battery_temperature_C
                        self._measurement_temperature_valid = self._temp_dmm_valid
                    else:
                        self._ub_V, self._ib_measured_A, self._t_sample_2_s = voltage, measured, elapsed
                        self._measurement_temperature_2_C = self._battery_temperature_C
                        self._measurement_temperature_2_valid = self._temp_dmm_valid
                self._enter(
                    IecDcResistanceState.OPEN_CIRCUIT_REST if first
                    else IecDcResistanceState.COMPUTE_RESULT,
                )
        return warnings

    def _read_dmm(self, dt_s: float) -> bool:
        """L. ocv_soc_controller.py::_read_dmm() azonos hivatkozású
        docstringje."""
        try:
            self._u_batt = (
                self._dmm_v.read_voltage(owner=self._iec_owner)
                if self._iec_owner is not None else self._dmm_v.read_voltage()
            )
            self._t_voltage_sample_s = self._time_source()
            voltage_ok = True
        except Exception:
            voltage_ok = False

        try:
            self._battery_temperature_C = self._dmm_t.read_temperature()
            self._safety.record_temperature_dmm_read(valid=True, dt_s=dt_s)
            self._temp_dmm_valid = True
        except Exception:
            self._safety.record_temperature_dmm_read(valid=False, dt_s=dt_s)
            self._temp_dmm_fault_ticks += 1
            self._temp_dmm_valid = False

        return voltage_ok


__all__ = [
    "IecDcResistanceConfig",
    "IecDcResistanceController",
    "IecDcResistanceState",
]
