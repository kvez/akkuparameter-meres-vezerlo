"""
bq_consistency_gate.py — Block 2 (2026-08-25, Traditional Learning BQ34Z110
provisioning /goal): PROFILE↔SESSION↔BQ DATA FLASH READBACK konzisztencia-gate.

Forrás: Folyamatok/tervek/TRADITIONAL_LEARNING_BQ_PROVISIONING_AUDIT_2026-08-24.md
GAP #1/#6 — a battery profile legyen az egyetlen igazságforrás, amiből a learning
csak akkor indulhat, ha a BQ34Z110-re ténylegesen programozott Data Flash mezők
konzisztensek a helyi profillal.

Architektúrális határ (CLAUDE.md 9. szakasz): tiszta logika, nincs PySide6, nincs
közvetlen transport-hívás — a Block 1 `Bq34z110DataFlashService`-t használja
mező-olvasásra. Minta: `session_recovery.py::evaluate_resume()` (enum döntés +
evidence-dataclass), de ott EGZAKT egyenlőség-alapú a hash-összevetés, itt MEZŐ-
SZINTŰ, numerikus TOLERANCIA-alapú összehasonlítás kell, mert a DF-értékeket egy
külső (nem ez az app) programozási lépés írta be, és kerekítési eltérés várható.

Kimaradó mezők (tudatos döntés):
- `qmax_cell0_initial_mAh`: a learning ALATT szándékosan változik (ez maga a
  tanulás eredménye) — nem stabil "elvárt" érték, nem gate-elhető.
- `cell_bl_set_volt_mV`/`cell_bh_set_volt_mV`: Block 3 saját, read-modify-restore
  tranzakciós kezelésű mezői — nincs fix "elvárt" értékük (a Block 3 a chipről
  ténylegesen kiolvasott eredeti értéket menti/állítja vissza, nem egy konstanst).

Konzervatív döntés (nincs "degraded, de folytatódik" köztes szint): bármelyik
vizsgált mező toleranciasávon kívül esése BLOKKOLJA az indulást — ez a CLAUDE.md
10.7 szakasz szellemével összhangban a biztonságosabb alapállás, amíg fizikai
HIL-tapasztalat nem indokolja egy finomabb, mezőnkénti súlyozás bevezetését.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from Prog.src.battery_profile import BatteryProfile
from Prog.src.bq_data_flash_service import Bq34z110DataFlashService
from Prog.src.exceptions import ProfileValidationError, TestAbortedByUser

# Vizsgált mezők — l. modul-docstring a kimaradó mezők indoklásáért.
#
# SCF-007 (2026-09-10, felhasználói döntés): korábban KIZÁRÓLAG a T1-T2
# (0-10°C) Cell Charge Voltage mezőt ellenőrizte a gate -- a projekt labor
# teszt-hőmérséklet ablaka (15-30°C) ténylegesen a T2-T3 (10-45°C) sávba
# esik, tehát a T1-T2 önmagában nem volt a működésileg releváns mező. A
# felhasználó úgy döntött, hogy az Op Config B JEITA-bit gyári alapértéken
# (engedélyezve) marad, és MINDHÁROM sávot (T1-T2/T2-T3/T3-T4) programozni
# és ellenőrizni kell -- l. CLAIM_LEDGER.yaml BQ-JEITA-001.
GATE_FIELDS: tuple[str, ...] = (
    "design_capacity_mAh",
    "design_energy_cWh",
    "design_energy_scale",
    "pack_configuration",
    "load_mode",
    "load_select",
    "cell_design_voltage_mV",
    "cell_charge_voltage_t1t2_mV",
    "cell_charge_voltage_t2t3_mV",
    "cell_charge_voltage_t3t4_mV",
    "taper_current_mA",
    "cell_terminate_voltage_mV",
    "number_of_series_cells",
)

# Mérnöki feltételezés, dokumentált, felülírható (nincs gyári forrás a pontos
# kerekítési/programozási toleranciára) — a `number_of_series_cells` KIVÉTEL:
# nulla tolerancia, mert ez egy safety-kritikus topológiai mező (rossz cellaszám
# esetén a BQ teljesen más pack-feszültséget feltételez, mint a valós).
FIELD_TOLERANCES: dict[str, int] = {
    "design_capacity_mAh": 50,
    "design_energy_cWh": 0,
    "design_energy_scale": 0,
    "pack_configuration": 0,
    "load_mode": 0,
    "load_select": 0,
    "cell_design_voltage_mV": 10,
    "cell_charge_voltage_t1t2_mV": 10,
    "cell_charge_voltage_t2t3_mV": 10,
    "cell_charge_voltage_t3t4_mV": 10,
    "taper_current_mA": 5,
    "cell_terminate_voltage_mV": 10,
    "number_of_series_cells": 0,
}

# DF FIELD MAP: Cell Design Voltage ólomsav kémiánál fix 2000mV, nem
# profil-eredetű (l. bq_data_flash_service.py FIELD_REGISTRY source_note).
_CELL_DESIGN_VOLTAGE_MV = 2000

# BQ34Z110 JEITA hőmérséklet-sávhatárok (SLUSB55B Table 20, subclass=48,
# offset=37-40, VERIFIED_PRIMARY, gyári alapérték): T1=0°C, T2=10°C,
# T3=45°C, T4=55°C. Felhasználói döntés (2026-09-10): minden JEITA charge-
# voltage mezőhöz egy reprezentatív hőmérsékletet választunk.
#
# [pontosítás, MAX verifier N3, 2026-09-11] EZ NEM egységesen "a sáv
# geometriai közepe" minden sávra -- T1-T2 és T3-T4 valóban a sáv közepét
# használja (a folytonos FIAMM-görbe 3 diszkrét BQ34Z110-lépcsőre
# bontásának kiegyensúlyozott közelítéseként), DE T2-T3 a FIAMM 20°C-os
# REFERENCIAPONTJÁT használja (NEM a sáv 10-45°C geometriai közepét, ami
# 27,5°C lenne) -- mert ez a pont MAGA a FIAMM adatlap kalibrációs
# alapja, ahol a kompenzáció definíció szerint nulla.
_JEITA_T1T2_MIDPOINT_C = 5.0    # (0+10)/2
_JEITA_T2T3_MIDPOINT_C = 20.0   # a T2-T3 sáv (10-45°C) TARTALMAZZA a FIAMM
                                 # 20°C referenciapontot -- nincs extra
                                 # kompenzacio; a Q-26/Q-44 plafon itt is ervenyes
                                 # (NEM a sáv geometriai közepe, 27,5°C -- l.
                                 # fenti pontosítás)
_JEITA_T3T4_MIDPOINT_C = 50.0   # (45+55)/2 -- MEGJEGYZÉS (MAX verifier P3-F,
                                 # 2026-09-10): a FIAMM float/boost temp-comp
                                 # görbe a saját forrása szerint (Engineering_
                                 # Manual_AGM_EN.txt 879. sor) "-20 and +50°C"
                                 # tartományra validált -- az 50°C midpoint
                                 # ÉPP a validált tartomány szélén van, az
                                 # 55°C-ig terjedő T3-T4 sáv felső fele (50-
                                 # 55°C) FIAMM-validáláson KÍVÜLI extrapoláció.
                                 # A Q-26/Q-44 szerinti 2.40V/2.30V clamp
                                 # (l. compensated_charge_voltage_V()) itt is
                                 # érvényesül, tehát ez nem safety-kockázat,
                                 # de a pontosság 50°C fölött nem gyártói
                                 # forrással igazolt -- HIL_PENDING.


def _jeita_band_charge_voltage_mV(profile: BatteryProfile, midpoint_C: float) -> int:
    """[regresszió, MAX verifier P1-A, 2026-09-10] EZ a metódus KORÁBBAN
    saját, duplikált temp-comp számítást végzett (hardkódolt -2,5 mV/cella/
    °C állandóval, clamp NÉLKÜL) -- ez KÉT hibát okozott: (1) figyelmen
    kívül hagyta, hogy `temp_comp_mV_per_cell_per_degC` profilonként
    KONFIGURÁLHATÓ mező (`battery_profile.py:33`), nem projekt-szintű
    állandó (DFAM: duplikált config-forrás -- ha egy profil eltérő
    együtthatót kap, a töltő és a gate csendben szétcsúszna); (2) NEM
    alkalmazta a `batt_absolute_max_V_per_cell` (2,425 V/cella) kemény
    safety-plafon clampet -- a T1-T2 (hideg, 5°C) sávra ez egy olyan
    DF-elvárást (2438 mV) eredményezett, ami MEGHALADJA a projekt saját,
    GUI-ból nem módosítható kemény safety-plafonját (2425 mV), tehát a
    gate egy azzal ütköző értéket várt volna el. Javítás: a MEGLÉVŐ, már
    clampelt `profile.compensated_charge_voltage_V()`-t hívjuk --
    EGYETLEN igazságforrás a FIAMM hőmérséklet-kompenzációra ÉS a
    safety-clampre, nincs duplikált/szétcsúszható másolat.

    Q-26/Q-44/Q-48 (2026-11-12): a kozos profilfuggveny felso toltesi
    cel-clampje 2400 mV/cella. Q-48 (2) szerint ez a termek golden image
    JEITA hideg savjaban is ervenyes. A FIAMM hideg oldali emelesehez
    kepest ez felhasznaloi szigoritas, nem gyartoi eljaras.
    A PSU-celt minden TempCompMode mellett korlatozza a kozos homersekleti
    plafon; MONITOR_ONLY/OFF meleg celja is csokkenhet. A T-kiesesi policy
    marad. A DMM_V BATTERY_OVERVOLTAGE kapuja valtozatlanul 2425 mV/cella;
    kulon 2400 mV-os FAULT-kapu nincs. A BQ JEITA DF-elvaras nem kozvetlen
    PSU-setpoint-masolat; a regi 2425 mV-os hideg DF-et a konzisztenciakapu
    a kulon HIL-ujraprovisionalasig blokkolja, automatikus DF-iras nelkul."""
    pack_V = profile.compensated_charge_voltage_V(midpoint_C)
    return round(pack_V / profile.cell_count * 1000)


class ProvisioningGateDecision(Enum):
    CONSISTENT = "CONSISTENT"
    BLOCKED_BQ_DF_READBACK_FAILED = "BLOCKED_BQ_DF_READBACK_FAILED"
    BLOCKED_CONFIGURATION_MISMATCH = "BLOCKED_CONFIGURATION_MISMATCH"


@dataclass(frozen=True)
class DfFieldComparison:
    field_name: str
    profile_expected: int
    bq_readback: int | None
    unit: str
    tolerance: int
    within_tolerance: bool


@dataclass(frozen=True)
class ProvisioningConsistencyReport:
    decision: ProvisioningGateDecision
    reason: str
    comparisons: tuple[DfFieldComparison, ...]

    @property
    def consistent(self) -> bool:
        return self.decision is ProvisioningGateDecision.CONSISTENT


def build_expected_df_values(profile: BatteryProfile) -> dict[str, int]:
    """A profilból származtatott, a BQ34Z110 Data Flash-én elvárt mezőértékek.

    [regresszió, SCF-007, 2026-09-10] a `cell_charge_voltage_t1t2_mV` elvárt
    értéke KORÁBBAN feltétel nélkül `round(profile.charge_voltage_per_cell_V
    * 1000)` volt -- azaz a 20°C-os FIAMM referenciaértéket várta el a HIDEG
    (0-10°C) sávra is, hőmérséklet-kompenzáció nélkül. Ez KÉTSZERESEN
    pontatlan volt: (1) T1-T2 nem is a labor működési hőmérsékletére (15-
    30°C, ami T2-T3-ba esik) releváns mező, (2) a hidegebb sávnak a FIAMM
    -2,5 mV/cella/C formula szerint a hideg ertek magasabb lenne.
    Q-26/Q-44 A ezt a felhasznaloi 2,400 V/cella plafonnal szigoritja.
    A kozos profilformula 5/20/50 C reprezentativ pontokon ervenyes;
    a 20 C-os referenciara is vonatkozik a toltesi plafon."""
    if Decimal(str(profile.nominal_capacity_Ah)) * 1000 > 32767:
        raise ProfileValidationError("Q-22: a C20/Qmax-bázis kívül a nem-X10 mAh tartományon")
    return {
        "design_capacity_mAh": profile.design_capacity_mAh,
        "design_energy_cWh": profile.design_energy_cWh,
        "design_energy_scale": profile.design_energy_scale,
        "pack_configuration": profile.pack_configuration_x10,
        "load_mode": profile.load_mode,
        "load_select": profile.load_select,
        "cell_design_voltage_mV": _CELL_DESIGN_VOLTAGE_MV,
        "cell_charge_voltage_t1t2_mV": _jeita_band_charge_voltage_mV(
            profile, _JEITA_T1T2_MIDPOINT_C
        ),
        "cell_charge_voltage_t2t3_mV": _jeita_band_charge_voltage_mV(
            profile, _JEITA_T2T3_MIDPOINT_C
        ),
        "cell_charge_voltage_t3t4_mV": _jeita_band_charge_voltage_mV(
            profile, _JEITA_T3T4_MIDPOINT_C
        ),
        "taper_current_mA": round(profile.effective_taper_A * 1000),
        "cell_terminate_voltage_mV": round(profile.terminate_voltage_per_cell_V * 1000),
        "number_of_series_cells": profile.cell_count,
    }


def build_initial_df_values(profile: BatteryProfile) -> dict[str, int]:
    """Kezdeti DF-elvárás; a tanult Qmax nem része a stabil kapunak."""
    values = build_expected_df_values(profile)
    # A teljes konfigurációs szó nem profiladat: véletlenül se írjuk felül.
    del values["pack_configuration"]
    values["qmax_cell0_initial_mAh"] = profile.qmax_cell0_initial_mAh
    return values


def evaluate_provisioning_consistency(
    profile: BatteryProfile, df_service: Bq34z110DataFlashService,
) -> ProvisioningConsistencyReport:
    """PROFILE↔BQ DATA FLASH READBACK összevetés. ELŐFELTÉTEL (mint
    `Bq34z110DataFlashService`): a hívó MÁR UNSEALED állapotba hozta a chipet —
    ez a függvény NEM hív unseal()/seal()-t."""
    try:
        expected = build_expected_df_values(profile)
    except ProfileValidationError as exc:
        return ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH,
            reason=f"Érvénytelen BQ-profil: {exc}",
            comparisons=(),
        )

    try:
        readback = df_service.read_fields(GATE_FIELDS)
    except TestAbortedByUser:
        raise
    except Exception as exc:
        return ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.BLOCKED_BQ_DF_READBACK_FAILED,
            reason=f"BQ Data Flash olvasás sikertelen: {exc}",
            comparisons=(),
        )

    comparisons: list[DfFieldComparison] = []
    all_within_tolerance = True
    for field_name in GATE_FIELDS:
        expected_value = expected[field_name]
        tolerance = FIELD_TOLERANCES[field_name]
        actual_value = readback[field_name].value
        # Csak az X10 bit kapuzott; a többi Pack Configuration bit megmarad.
        compared_value = (actual_value >> 7) & 1 if field_name == "pack_configuration" else actual_value
        within = abs(compared_value - expected_value) <= tolerance
        if not within:
            all_within_tolerance = False
        comparisons.append(DfFieldComparison(
            field_name=field_name,
            profile_expected=expected_value,
            bq_readback=compared_value,
            unit="bit" if field_name == "pack_configuration" else readback[field_name].spec.unit,
            tolerance=tolerance,
            within_tolerance=within,
        ))

    if all_within_tolerance:
        return ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.CONSISTENT,
            reason="minden vizsgált DF-mező a profil-elvárt tolerancián belül",
            comparisons=tuple(comparisons),
        )

    mismatched = [c.field_name for c in comparisons if not c.within_tolerance]
    return ProvisioningConsistencyReport(
        decision=ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH,
        reason=f"eltérő mező(k) a toleranciasávon kívül: {', '.join(mismatched)}",
        comparisons=tuple(comparisons),
    )


__all__ = [
    "FIELD_TOLERANCES",
    "GATE_FIELDS",
    "DfFieldComparison",
    "ProvisioningConsistencyReport",
    "ProvisioningGateDecision",
    "build_expected_df_values",
    "build_initial_df_values",
    "evaluate_provisioning_consistency",
]
