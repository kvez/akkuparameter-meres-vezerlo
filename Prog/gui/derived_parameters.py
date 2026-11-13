"""
SUP02 — dinamikus, üzemmód-függő GUI és reaktív paraméterkezelés.
Forrás: kiegeszito_szabalyok_02_dinamikus_gui_parameterkezeles_v1_0_20260723.txt

Pure Python reaktív derived-parameter engine — NINCS PySide6 import
(SUP02-ARCH-001/002, SUP02-DERIVE-006: "A QLabel és QSpinBox nem lehet az
üzleti logika elsődleges adatforrása"). A GUI ebből az objektumból renderel,
nem fordítva.

Kulcselvek:
  - SUP02-MODEL-001/002/003: minden származtatott paraméter value+source+policy+
    dependencies+validation_state+revision-t tárol; a 0.0 SOSEM "auto" sentinel —
    mindenhol Optional[float] + explicit policy enum.
  - SUP02-REACT-001/002/003/004: egyetlen központi recompute(), batch_update()
    context manager a signal-vihar ellen, config_revision minden sikeres
    recompute-nál nő.
  - SUP02-POLICY-003/VALID-002: nincs csendes hardware-clamp — ha a kért érték
    meghaladja a hardver-limitet, CONFIG_INCOMPATIBLE validation_state (blokkoló),
    a kért és a limit érték egyaránt látható marad.
  - SUP02-CONFIG-001: mód-specifikus draft megőrzés módváltáskor.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Optional

from Prog.gui.ui_policy import is_group_visible
from Prog.src.battery_profile import DischargeRateOrigin, normal_discharge_minimum_per_cell


# ------------------------------------------------------------------ #
# SUP02-MODEL-001/002: engedélyezett forrás- és validációs állapotok  #
# ------------------------------------------------------------------ #

class ParamSource(Enum):
    PROFILE_DEFAULT = "PROFILE_DEFAULT"
    DERIVED_PRESET = "DERIVED_PRESET"
    USER_OVERRIDE = "USER_OVERRIDE"
    LOADED_SESSION = "LOADED_SESSION"
    HARDWARE_LIMIT = "HARDWARE_LIMIT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ValidationState(Enum):
    OK = "OK"
    WARNING = "WARNING"
    CONFIG_INCOMPATIBLE = "CONFIG_INCOMPATIBLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# ------------------------------------------------------------------ #
# SUP02-POLICY-*: explicit policy selectorok (nem egyszeri gombos     #
# számbeírás — l. SUP02-POLICY-001).                                  #
# ------------------------------------------------------------------ #

class DischargeRatePolicy(Enum):
    PROFILE_DEFAULT = "PROFILE_DEFAULT"
    C5 = "C/5"
    C10 = "C/10"
    C20 = "C/20"
    CUSTOM = "CUSTOM"


class TerminateVoltagePolicy(Enum):
    PROFILE_DEFAULT = "PROFILE_DEFAULT"
    V_1_80_PER_CELL = "1.80_V_PER_CELL"
    V_1_75_PER_CELL = "1.75_V_PER_CELL"
    CUSTOM = "CUSTOM"


class ChargeCurrentPolicy(Enum):
    PROFILE_MAX = "PROFILE_MAX"
    C_RATE_PRESET = "C_RATE_PRESET"
    CUSTOM = "CUSTOM"


class PulseCurrentPolicy(Enum):
    C5 = "C/5"
    C10 = "C/10"
    CUSTOM = "CUSTOM"


class CapacityBasisPolicy(Enum):
    MEASURED_USABLE_CAPACITY = "MEASURED_USABLE_CAPACITY"
    NOMINAL_C10_CAPACITY = "NOMINAL_C10_CAPACITY"


_DISCHARGE_RATE_DIVISOR: dict[DischargeRatePolicy, int] = {
    DischargeRatePolicy.C5: 5,
    DischargeRatePolicy.C10: 10,
    DischargeRatePolicy.C20: 20,
}

_PULSE_RATE_DIVISOR: dict[PulseCurrentPolicy, int] = {
    PulseCurrentPolicy.C5: 5,
    PulseCurrentPolicy.C10: 10,
}

_TERMINATE_V_PER_CELL: dict[TerminateVoltagePolicy, float] = {
    TerminateVoltagePolicy.V_1_80_PER_CELL: 1.80,
    TerminateVoltagePolicy.V_1_75_PER_CELL: 1.75,
}

# Kör 20 Batch E5 (SW20-2, DFAM-017): a profil-YAML `terminate_voltage_
# per_cell_V` HIÁNYA esetén érvényes FIAMM alapérték. UGYANAZ az érték,
# mint main_window.py::_build_runner() `profile_yaml.get(..., 1.80)`
# fallbackje -- innen importálva, hogy ne legyen két, kézzel szinkronban
# tartott literál. A fenti _TERMINATE_V_PER_CELL[V_1_80_PER_CELL] 1.80-a
# NEM ugyanez a fogalom: az EXPLICIT kezelői preset, nem profil-alapérték
# -- a numerikus egybeesés a közös FIAMM/IEC 1,80 V/cella referenciából
# adódik, DE a két konstans FOGALMILAG külön koncepció, nem közös kódforrás
# (nem szabad összevonni, ha a kettő valaha eltérne).
DEFAULT_TERMINATE_V_PER_CELL: float = 1.80

# R8-B3: 2380-120-60 CC-tartomany, 2380-120-60-900-01 Rev. A, 4. fejezet, p47.
LOAD_HW_MAX_CURRENT_A: float = 60.0

# SUP02-ARCH-004: PSU hardver-limit egy helyen (a charge_controller.py saját,
# vezérlési célú másolatát NEM váltja ki — az a biztonsági döntési út marad;
# ez a GUI-oldali előnézet/validáció közös forrása).
PSU_HW_MAX_CURRENT_A: dict[str, float] = {
    "INDEPENDENT": 1.5,
    "PARALLEL": 3.0,
    "SERIES": 1.5,
}

# Governance-v2 review-kör 1 (CF-15/DFAM-017 3. előfordulás + DFAM-004,
# batch 4.5): ugyanaz a SUP02-ARCH-004 minta, mint fent a current-limitre —
# main_window.py korábban egy SAJÁT, function-scoped _PSU_MODE_MAX_VOLTAGE_V
# dict-et tartott ugyanezekkel az értékekkel (2. forrás). A
# charge_controller.py::_advance_series() saját psu_mode_max dict-je itt is
# a biztonsági döntési út marad (ugyanaz a KEEP_AS_IS indoklás, mint a
# current-limitnél).
PSU_HW_MAX_VOLTAGE_V: dict[str, float] = {
    "INDEPENDENT": 30.0,
    "PARALLEL": 30.0,
    "SERIES": 60.0,
}

# Governance-v2 review-kör 1 (CF-15/DFAM-017 3. előfordulás, batch 4.5):
# korábban 3 külön helyen élt ugyanez a dict, csekély eltéréssel (config_
# panel.py::_PROFILE_DEFAULTS és main_window.py::_PROFILE_META_FALLBACK is
# hordozott battery_name/manufacturer mezőket, amikből csak a manufacturer-t
# olvasta valaha a main_window.py-beli YAML-fallback — a battery_name/
# config_panel.py-beli manufacturer sosem lett kiolvasva, holt adat volt).
# Ez a public (nem _-prefixes) verzió az egyetlen kanonikus forrás — l.
# PROFILE_META.get(name)-t hívó összes hely.
PROFILE_META: dict[str, dict] = {
    "FIAMM_12V": {"cell_count": 6, "nominal_voltage_V": 12.0, "manufacturer": "FIAMM"},
    "FIAMM_24V": {"cell_count": 12, "nominal_voltage_V": 24.0, "manufacturer": "FIAMM"},
}


# ------------------------------------------------------------------ #
# SUP02-ARCH-004: formula registry — tiszta, egy-helyen-élő függvények #
# ------------------------------------------------------------------ #

def compute_discharge_current_A(capacity_basis_Ah: float, divisor: int) -> float:
    return capacity_basis_Ah / divisor


def compute_terminate_voltage_pack_V(cell_count: int, v_per_cell: float) -> float:
    return cell_count * v_per_cell


def compute_max_charge_current_A(c10_capacity_Ah: float) -> float:
    return 0.25 * c10_capacity_Ah


def compute_taper_current_A(c10_capacity_Ah: float) -> float:
    return 0.03 * c10_capacity_Ah


def compute_pulse_current_A(capacity_basis_Ah: float, divisor: int) -> float:
    return capacity_basis_Ah / divisor


@dataclass
class DerivedParam:
    """SUP02-MODEL-001: minden származtatott paraméter egységes leírója."""
    value: Optional[float]
    source: ParamSource
    policy: str
    dependencies: tuple[str, ...] = ()
    validation_state: ValidationState = ValidationState.OK
    revision: int = 0


class DynamicSessionModel:
    """
    SUP02 reaktív session-konfigurációs modell — egyetlen "source of truth"
    (SUP02-DERIVE-006). A ConfigPanel ebből olvas, ebbe ír vissza felhasználói
    inputot, és sosem számol közvetlenül widget-szövegből.
    """

    def __init__(
        self,
        battery_profile_name: str = "FIAMM_12V",
        c10_capacity_Ah: float = 0.0,
        test_type: str = "CHARACTERIZATION",
        psu_mode: str = "INDEPENDENT",
    ) -> None:
        meta = PROFILE_META.get(battery_profile_name, PROFILE_META["FIAMM_12V"])
        self.battery_profile_name = battery_profile_name
        self.battery_model = ""
        self.cell_count: int = meta["cell_count"]
        self.nominal_voltage_V: float = meta["nominal_voltage_V"]
        self.c10_capacity_Ah = c10_capacity_Ah
        self.measured_capacity_Ah: Optional[float] = None
        # Kör 20 Batch E5 (SW20-2, P2, DFAM-001 + DFAM-017 -- a lenti
        # max_charge_current_A_profile_override PONTOS mintája ugyanerről a
        # hibaosztályról): ez a PROFILE_DEFAULT terminate-policy V/cella
        # alapja. Korábban HARDKÓDOLT 1.80 volt, miközben a TÉNYLEGESEN
        # alkalmazott cutoff-ot a BatteryProfile.terminate_voltage_pack_V
        # (main_window.py::_build_runner(), profil-YAML
        # `terminate_voltage_per_cell_V`) adja. A config_panel.py::
        # _apply_profile_yaml_overrides() tölti fel a VALÓS YAML-ból; ez az
        # érték csak a feltöltés ELŐTTI konstrukciós ablakban érvényes.
        self.terminate_voltage_per_cell_default_V: float = DEFAULT_TERMINATE_V_PER_CELL

        self.psu_mode = psu_mode
        # SUP02-MODEL-003: nincs 0.0 auto sentinel — a wiring_confirmed külön,
        # explicit bool flag, nem numerikus mező.
        self.wiring_confirmed: bool = False
        self.psu_mode_source: ParamSource = ParamSource.DERIVED_PRESET

        self.test_type = test_type

        self.discharge_rate_policy: DischargeRatePolicy = DischargeRatePolicy.C5
        self.discharge_current_custom_A: Optional[float] = None

        self.charge_current_policy: ChargeCurrentPolicy = ChargeCurrentPolicy.PROFILE_MAX
        self.charge_current_custom_A: Optional[float] = None
        # Review-kör 4/5 GUI F6 (P2, BAT-001/SRC-003/CFG-001 sibling -- l.
        # config_panel.py::_refresh_i10_source_label() pontos mintája
        # ugyanerről a hibaosztályról): PROFILE_MAX policy esetén ez a
        # modell korábban MINDIG a generikus 0,25×C10 képletet mutatta a
        # Start-összegzőben, míg a TÉNYLEGESEN alkalmazott áram
        # (charge_controller.py::_run_precheck() -> BatteryProfile.
        # effective_max_charge_A) egy profil-YAML `max_charge_current_A`
        # felülbírálást ELSŐBBSÉGGEL használ, ha az adott profilban be van
        # állítva -- ma mindkét gyári profilban (FIAMM_12V/24V.yaml) null,
        # tehát 0 viselkedésváltozás, de egy jövőbeli profil-YAML-szerkesztés
        # (kód nélkül!) csendben eltérő Start-összegzőt eredményezne. A
        # config_panel.py::_on_profile_changed() tölti fel a VALÓS YAML-ból.
        self.max_charge_current_A_profile_override: Optional[float] = None

        # Kör 20 Batch E5 (SW20-2): a max_charge_current_A_profile_override
        # testvére a taper-oldalon (BatteryProfile.effective_taper_A tükre).
        # FIGYELEM, SZÁNDÉKOS (DFAM-026, ne jelentsd újra findingként): a
        # modell `taper_current_A` derived értékének MA NINCS éles
        # fogyasztója -- a CV->TAPER_HOLD kapu (charge_controller.py:1227-1230)
        # és a BQ DF `taper_current_mA` (bq_consistency_gate.py:194) egyaránt
        # a BatteryProfile.effective_taper_A-t használja. A mező itt a
        # modell belső konzisztenciáját tartja fenn, hogy egy JÖVŐBELI
        # fogyasztó ne a generikus képletet kapja meg.
        self.taper_current_A_profile_override: Optional[float] = None

        self.terminate_voltage_policy: TerminateVoltagePolicy = TerminateVoltagePolicy.PROFILE_DEFAULT
        self.terminate_voltage_custom_V: Optional[float] = None

        self.pulse_current_policy: PulseCurrentPolicy = PulseCurrentPolicy.C5
        self.pulse_current_custom_A: Optional[float] = None

        self.capacity_basis_policy: CapacityBasisPolicy = CapacityBasisPolicy.NOMINAL_C10_CAPACITY

        # SUP02-REACT-004: revision-számláló.
        self.config_revision: int = 0
        self._last_validated_revision: int = -1
        self._last_validation_state: ValidationState = ValidationState.NOT_APPLICABLE

        # SUP02-REACT-002: batch update guard — a köztes recompute-okat elnyomja.
        self._batch_depth: int = 0

        # SUP02-CONFIG-001: mód-specifikus draftok.
        self._drafts: dict[str, dict] = {}

        self._derived: dict[str, DerivedParam] = {}
        self.recompute()

    # ------------------------------------------------------------------ #
    # SUP02-GRID-003 megfelelő: kapacitásalap                             #
    # ------------------------------------------------------------------ #

    @property
    def capacity_basis_Ah(self) -> float:
        if (
            self.capacity_basis_policy == CapacityBasisPolicy.MEASURED_USABLE_CAPACITY
            and self.measured_capacity_Ah is not None
            and self.measured_capacity_Ah > 0
        ):
            return self.measured_capacity_Ah
        return self.c10_capacity_Ah

    @property
    def capacity_basis_quality(self) -> str:
        """SUP02-POLICY-006: measured hiányában nominal fallback csak explicit
        minősítéssel — sosem csendben."""
        if (
            self.capacity_basis_policy == CapacityBasisPolicy.MEASURED_USABLE_CAPACITY
            and self.measured_capacity_Ah is not None
            and self.measured_capacity_Ah > 0
        ):
            return "MEASURED_CAPACITY_BASED"
        return "NOMINAL_CAPACITY_BASED_SOC"

    @property
    def measured_capacity_available(self) -> bool:
        return self.measured_capacity_Ah is not None and self.measured_capacity_Ah > 0

    # ------------------------------------------------------------------ #
    # Batch update (SUP02-REACT-002)                                      #
    # ------------------------------------------------------------------ #

    def batch_update(self) -> "_BatchGuard":
        return _BatchGuard(self)

    def _maybe_recompute(self) -> None:
        if self._batch_depth == 0:
            self.recompute()

    # ------------------------------------------------------------------ #
    # SUP02-REACT-001: központi recompute — MINDEN származtatott érték     #
    # ------------------------------------------------------------------ #

    def recompute(self) -> None:
        self._recompute_discharge_current()
        self._recompute_charge_current()
        self._recompute_terminate_voltage()
        self._recompute_taper_current()
        self._recompute_pulse_current()
        self.config_revision += 1

    def _recompute_discharge_current(self) -> None:
        policy = self.discharge_rate_policy
        if policy == DischargeRatePolicy.CUSTOM:
            value = self.discharge_current_custom_A
            source = ParamSource.USER_OVERRIDE
        elif policy == DischargeRatePolicy.PROFILE_DEFAULT:
            value = None
            source = ParamSource.NOT_APPLICABLE
        else:
            divisor = _DISCHARGE_RATE_DIVISOR[policy]
            value = compute_discharge_current_A(self.capacity_basis_Ah, divisor)
            source = ParamSource.DERIVED_PRESET
        validation_state = ValidationState.OK
        if policy != DischargeRatePolicy.PROFILE_DEFAULT and (
            value is None or not isfinite(value) or value <= 0.0
            or value > LOAD_HW_MAX_CURRENT_A
        ):
            validation_state = ValidationState.CONFIG_INCOMPATIBLE
        self._derived["discharge_current_A"] = DerivedParam(
            value=value, source=source, policy=policy.value,
            dependencies=("capacity_basis_Ah", "discharge_rate_policy"),
            validation_state=validation_state,
            revision=self.config_revision,
        )

    def _recompute_charge_current(self) -> None:
        policy = self.charge_current_policy
        hw_max = PSU_HW_MAX_CURRENT_A.get(self.psu_mode, 1.5)

        if policy == ChargeCurrentPolicy.CUSTOM:
            requested = self.charge_current_custom_A
            source = ParamSource.USER_OVERRIDE
        elif self.max_charge_current_A_profile_override is not None:
            # Review-kör 4/5 GUI F6 (P2) -- l. max_charge_current_A_
            # profile_override docstringje fent: a BatteryProfile.
            # effective_max_charge_A pontos tükre, ELSŐBBSÉGGEL a generikus
            # képlet előtt.
            requested = self.max_charge_current_A_profile_override
            source = ParamSource.DERIVED_PRESET
        else:
            # PROFILE_MAX és C_RATE_PRESET egyaránt a FIAMM 0.25×C10 képletet
            # használja jelenleg (SUP01-C_RATE_PRESET külön ráta-választó
            # bevezetése nem P0/P1 — l. gap-mátrix nyitott pont).
            requested = compute_max_charge_current_A(self.c10_capacity_Ah)
            source = ParamSource.DERIVED_PRESET

        # SUP02-POLICY-003/VALID-002: TILOS csendben clampelni — ha a kért
        # érték meghaladja a hardverlimitet, CONFIG_INCOMPATIBLE (blokkoló).
        validation_state = ValidationState.OK
        if requested is not None and (
            requested > hw_max + 1e-9
            or requested > self.charge_current_profile_limit_A
        ):
            validation_state = ValidationState.CONFIG_INCOMPATIBLE

        self._derived["requested_charge_current_A"] = DerivedParam(
            value=requested, source=source, policy=policy.value,
            dependencies=("c10_capacity_Ah", "charge_current_policy", "psu_mode"),
            validation_state=validation_state,
            revision=self.config_revision,
        )
        self._derived["charge_current_hardware_limit_A"] = DerivedParam(
            value=hw_max, source=ParamSource.HARDWARE_LIMIT, policy="PSU_MODE_LIMIT",
            dependencies=("psu_mode",), revision=self.config_revision,
        )

    def _recompute_terminate_voltage(self) -> None:
        policy = self.terminate_voltage_policy
        if policy == TerminateVoltagePolicy.CUSTOM:
            value = self.terminate_voltage_custom_V
            source = ParamSource.USER_OVERRIDE
        else:
            v_per_cell = _TERMINATE_V_PER_CELL.get(
                policy, self.terminate_voltage_per_cell_default_V
            )
            value = compute_terminate_voltage_pack_V(self.cell_count, v_per_cell)
            source = (
                ParamSource.PROFILE_DEFAULT
                if policy == TerminateVoltagePolicy.PROFILE_DEFAULT
                else ParamSource.DERIVED_PRESET
            )
        minimum = normal_discharge_minimum_per_cell(
            manufacturer=PROFILE_META.get(self.battery_profile_name, {}).get("manufacturer", ""),
            model=self.battery_model, current_A=self.discharge_current_A or 0.0,
            origin=self.discharge_rate_origin,
        ) * self.cell_count
        invalid = value is None or not isfinite(value) or value < minimum
        self._derived["terminate_voltage_pack_V"] = DerivedParam(
            value=value, source=source, policy=policy.value,
            dependencies=("cell_count", "terminate_voltage_policy", "discharge_rate_origin"),
            validation_state=ValidationState.CONFIG_INCOMPATIBLE if invalid else ValidationState.OK,
            revision=self.config_revision,
        )

    def _recompute_taper_current(self) -> None:
        if self.taper_current_A_profile_override is not None:
            # Kör 20 Batch E5 (SW20-2) -- a BatteryProfile.effective_taper_A
            # pontos tükre, ELSŐBBSÉGGEL a generikus képlet előtt.
            value = self.taper_current_A_profile_override
            policy = "PROFILE_YAML_OVERRIDE"
        else:
            value = compute_taper_current_A(self.c10_capacity_Ah)
            policy = "FIAMM_0.03xC10"
        self._derived["taper_current_A"] = DerivedParam(
            value=value, source=ParamSource.DERIVED_PRESET, policy=policy,
            dependencies=("c10_capacity_Ah",), revision=self.config_revision,
        )

    def _recompute_pulse_current(self) -> None:
        policy = self.pulse_current_policy
        if policy == PulseCurrentPolicy.CUSTOM:
            value = self.pulse_current_custom_A
            source = ParamSource.USER_OVERRIDE
        else:
            divisor = _PULSE_RATE_DIVISOR[policy]
            value = compute_pulse_current_A(self.capacity_basis_Ah, divisor)
            source = ParamSource.DERIVED_PRESET
        self._derived["pulse_current_A"] = DerivedParam(
            value=value, source=source, policy=policy.value,
            dependencies=("capacity_basis_Ah", "pulse_current_policy"),
            revision=self.config_revision,
        )

    # ------------------------------------------------------------------ #
    # Publikus derived-érték elérők                                       #
    # ------------------------------------------------------------------ #

    def get(self, name: str) -> DerivedParam:
        return self._derived[name]

    @property
    def discharge_current_A(self) -> Optional[float]:
        return self._derived["discharge_current_A"].value

    @property
    def is_discharge_current_incompatible(self) -> bool:
        return (
            self._derived["discharge_current_A"].validation_state
            == ValidationState.CONFIG_INCOMPATIBLE
        )

    @property
    def requested_charge_current_A(self) -> Optional[float]:
        return self._derived["requested_charge_current_A"].value

    @property
    def charge_current_profile_limit_A(self) -> float:
        # R9-B1: a profil effektív maximumának GUI-oldali tükre.
        if self.max_charge_current_A_profile_override is not None:
            return self.max_charge_current_A_profile_override
        return compute_max_charge_current_A(self.c10_capacity_Ah)

    @property
    def charge_current_hardware_limit_A(self) -> float:
        return self._derived["charge_current_hardware_limit_A"].value  # type: ignore[return-value]

    @property
    def is_charge_current_incompatible(self) -> bool:
        return (
            self._derived["requested_charge_current_A"].validation_state
            == ValidationState.CONFIG_INCOMPATIBLE
        )

    @property
    def terminate_voltage_pack_V(self) -> Optional[float]:
        return self._derived["terminate_voltage_pack_V"].value

    @property
    def taper_current_A(self) -> Optional[float]:
        return self._derived["taper_current_A"].value

    @property
    def pulse_current_A(self) -> Optional[float]:
        return self._derived["pulse_current_A"].value

    # ------------------------------------------------------------------ #
    # Setterek — mindegyik a központi recompute()-ot hívja (batch alatt   #
    # elnyomva, l. batch_update()).                                       #
    # ------------------------------------------------------------------ #

    @property
    def discharge_rate_origin(self) -> DischargeRateOrigin:
        return DischargeRateOrigin(
            self.discharge_rate_policy.value, self.capacity_basis_policy.value,
            self.capacity_basis_Ah,
        )

    def set_battery_model(self, model: str) -> None:
        self.battery_model = model.strip()
        self._maybe_recompute()

    def set_battery_profile(self, name: str) -> None:
        """SUP02-DERIVE-001/REACT-006: profilváltás — pack-feszültségek
        frissülnek, wiring confirmation MINDIG reset, 24V→SERIES ajánlás."""
        meta = PROFILE_META.get(name, PROFILE_META["FIAMM_12V"])
        self.battery_profile_name = name
        self.cell_count = meta["cell_count"]
        self.nominal_voltage_V = meta["nominal_voltage_V"]
        self.wiring_confirmed = False  # SUP02-REACT-006: mindig reset

        if name == "FIAMM_24V":
            self.psu_mode = "SERIES"
            self.psu_mode_source = ParamSource.DERIVED_PRESET
        elif self.psu_mode_source != ParamSource.USER_OVERRIDE:
            # visszaváltás 12V-re: alapértelmezett INDEPENDENT ajánlás,
            # kivéve ha a felhasználó explicit (USER_OVERRIDE) PARALLEL/SERIES-t
            # választott — az USER_OVERRIDE-ot csak akkor tartjuk meg, ha az
            # egyébként is valid marad (12V+SERIES csak warning, nem fault).
            self.psu_mode = "INDEPENDENT"

        self._maybe_recompute()

    def set_max_charge_current_A_profile_override(self, value: Optional[float]) -> None:
        """Review-kör 4/5 GUI F6 (P2) -- l. max_charge_current_A_profile_
        override docstringje. A hívó (config_panel.py::_on_profile_changed())
        minden profilváltáskor hívja, a TÉNYLEGES YAML-ból olvasott értékkel
        (vagy None-nal, ha a profil nem ad meg felülbírálást) -- nem
        `set_battery_profile()` része, mert az a PROFILE_META fallback
        dict-et használja, ami NEM tartalmazza ezt a mezőt (BAT-001/
        SRC-003/CFG-001: a versioned YAML az egyetlen hiteles forrás)."""
        self.max_charge_current_A_profile_override = value
        self._maybe_recompute()

    def set_terminate_voltage_per_cell_default_V(self, value: Optional[float]) -> None:
        """Kör 20 Batch E5 (SW20-2) -- a set_max_charge_current_A_profile_
        override() pontos mintája. A hívó (config_panel.py::
        _apply_profile_yaml_overrides()) minden profilváltáskor hívja, a
        TÉNYLEGES profil-YAML `terminate_voltage_per_cell_V` értékével.
        None (a profil nem adja meg / nincs YAML-fájl) -> a FIAMM
        alapérték, PONTOSAN ugyanaz a fallback-szemantika, mint
        main_window.py::_build_runner() `.get("terminate_voltage_per_cell_V",
        DEFAULT_TERMINATE_V_PER_CELL)`-jé -- SOSEM az előző profil értékének
        megtartása. Nem a set_battery_profile() része, mert az a PROFILE_META
        fallback dict-et használja, ami ezt a mezőt NEM tartalmazza
        (BAT-001/SRC-003/CFG-001: a versioned YAML az egyetlen hiteles
        forrás)."""
        self.terminate_voltage_per_cell_default_V = (
            DEFAULT_TERMINATE_V_PER_CELL if value is None else value
        )
        self._maybe_recompute()

    def set_taper_current_A_profile_override(self, value: Optional[float]) -> None:
        """Kör 20 Batch E5 (SW20-2) -- l. a taper_current_A_profile_override
        mező docstringjét (DFAM-026: ma nincs éles fogyasztó)."""
        self.taper_current_A_profile_override = value
        self._maybe_recompute()

    def set_psu_mode(self, mode: str, *, user_override: bool = True) -> None:
        """SUP02-DERIVE-003: PSU-mód váltás — wiring confirmation MINDIG reset."""
        self.psu_mode = mode
        self.psu_mode_source = (
            ParamSource.USER_OVERRIDE if user_override else ParamSource.DERIVED_PRESET
        )
        self.wiring_confirmed = False
        self._maybe_recompute()

    def set_capacity(self, c10_capacity_Ah: float) -> None:
        """SUP02-DERIVE-002: kapacitásváltozás → minden AUTO/PRESET áram frissül;
        CUSTOM értékek megmaradnak (csak revalidálódnak)."""
        self.c10_capacity_Ah = c10_capacity_Ah
        self._maybe_recompute()

    def set_measured_capacity(self, measured_Ah: Optional[float]) -> None:
        self.measured_capacity_Ah = measured_Ah
        self._maybe_recompute()

    def set_capacity_basis_policy(self, policy: CapacityBasisPolicy) -> None:
        if policy == CapacityBasisPolicy.MEASURED_USABLE_CAPACITY and not self.measured_capacity_available:
            # SUP02-POLICY-006: measured opció csak akkor választható, ha van
            # mért érték — enélkül a hívó felelőssége, hogy ne ajánlja fel.
            policy = CapacityBasisPolicy.NOMINAL_C10_CAPACITY
        self.capacity_basis_policy = policy
        self._maybe_recompute()

    def set_discharge_rate_policy(
        self, policy: DischargeRatePolicy, custom_A: Optional[float] = None
    ) -> None:
        self.discharge_rate_policy = policy
        if policy == DischargeRatePolicy.CUSTOM and custom_A is not None:
            self.discharge_current_custom_A = custom_A
        self._maybe_recompute()

    def set_charge_current_policy(
        self, policy: ChargeCurrentPolicy, custom_A: Optional[float] = None
    ) -> None:
        self.charge_current_policy = policy
        if policy == ChargeCurrentPolicy.CUSTOM and custom_A is not None:
            self.charge_current_custom_A = custom_A
        self._maybe_recompute()

    def set_terminate_voltage_policy(
        self, policy: TerminateVoltagePolicy, custom_V: Optional[float] = None
    ) -> None:
        self.terminate_voltage_policy = policy
        if policy == TerminateVoltagePolicy.CUSTOM and custom_V is not None:
            self.terminate_voltage_custom_V = custom_V
        self._maybe_recompute()

    def set_pulse_current_policy(
        self, policy: PulseCurrentPolicy, custom_A: Optional[float] = None
    ) -> None:
        self.pulse_current_policy = policy
        if policy == PulseCurrentPolicy.CUSTOM and custom_A is not None:
            self.pulse_current_custom_A = custom_A
        self._maybe_recompute()

    # ------------------------------------------------------------------ #
    # SUP02-CONFIG-001: mód-specifikus draft megőrzés módváltáskor         #
    # ------------------------------------------------------------------ #

    def set_test_type(self, new_test_type: str) -> None:
        self._drafts[self.test_type] = self._capture_mode_draft()
        self.test_type = new_test_type
        if new_test_type in self._drafts:
            self._restore_mode_draft(self._drafts[new_test_type])
        self._maybe_recompute()

    def _capture_mode_draft(self) -> dict:
        return {
            "discharge_rate_policy": self.discharge_rate_policy,
            "discharge_current_custom_A": self.discharge_current_custom_A,
            "charge_current_policy": self.charge_current_policy,
            "charge_current_custom_A": self.charge_current_custom_A,
            "terminate_voltage_policy": self.terminate_voltage_policy,
            "terminate_voltage_custom_V": self.terminate_voltage_custom_V,
            "pulse_current_policy": self.pulse_current_policy,
            "pulse_current_custom_A": self.pulse_current_custom_A,
        }

    def _restore_mode_draft(self, draft: dict) -> None:
        for key, value in draft.items():
            setattr(self, key, value)

    # ------------------------------------------------------------------ #
    # Validáció (SUP02-VALID-*) — csak az aktív módhoz releváns mezők      #
    # ------------------------------------------------------------------ #

    def validate_effective_config(self) -> ValidationState:
        state = ValidationState.OK
        if is_group_visible(self.test_type, "charge_current") and self.is_charge_current_incompatible:
            state = ValidationState.CONFIG_INCOMPATIBLE
        if is_group_visible(self.test_type, "discharge_rate") and self.is_discharge_current_incompatible:
            state = ValidationState.CONFIG_INCOMPATIBLE
        profile_cutoff = self.terminate_voltage_per_cell_default_V
        if not isfinite(profile_cutoff) or profile_cutoff < normal_discharge_minimum_per_cell():
            state = ValidationState.CONFIG_INCOMPATIBLE
        if (
            is_group_visible(self.test_type, "terminate_voltage")
            and self.get("terminate_voltage_pack_V").validation_state
            == ValidationState.CONFIG_INCOMPATIBLE
        ):
            state = ValidationState.CONFIG_INCOMPATIBLE
        self._last_validated_revision = self.config_revision
        self._last_validation_state = state
        return state

    def is_start_allowed(self) -> bool:
        """SUP02-REACT-004/TEST-010: csak friss (nem stale) és validált
        revision mellett engedélyezett a Start."""
        return (
            self._batch_depth == 0
            and self._last_validated_revision == self.config_revision
            and self._last_validation_state != ValidationState.CONFIG_INCOMPATIBLE
        )

    # ------------------------------------------------------------------ #
    # SUP02-UI-008/CONFIG-003: effective (csak látható) config kinyerése   #
    # ------------------------------------------------------------------ #

    def effective_config(self) -> dict:
        """Csak az AKTÍV módban látható csoportok kerülnek bele — rejtett
        mező sosem jelenik meg itt, tehát a session_config sem kaphatja meg."""
        config: dict = {
            "test_type": self.test_type,
            "battery_profile_name": self.battery_profile_name,
            "cell_count": self.cell_count,
            "psu_mode": self.psu_mode,
            "wiring_confirmed": self.wiring_confirmed,
            "config_revision": self.config_revision,
        }
        if is_group_visible(self.test_type, "charge_current"):
            config["requested_charge_current_A"] = self.requested_charge_current_A
            config["charge_current_policy"] = self.charge_current_policy.value
        if is_group_visible(self.test_type, "discharge_rate"):
            config["discharge_current_A"] = self.discharge_current_A
            config["discharge_rate_policy"] = self.discharge_rate_policy.value
        if is_group_visible(self.test_type, "terminate_voltage"):
            config["terminate_voltage_pack_V"] = self.terminate_voltage_pack_V
        if is_group_visible(self.test_type, "pulse_current"):
            config["pulse_current_A"] = self.pulse_current_A
        if is_group_visible(self.test_type, "capacity_basis"):
            config["capacity_basis_Ah"] = self.capacity_basis_Ah
            config["capacity_basis_quality"] = self.capacity_basis_quality
        return config

    def snapshot(self) -> dict:
        """SUP02-CONFIG-003: immutable start snapshot — csak akkor hívható
        érdemben, ha is_start_allowed() igaz (a hívó felelőssége ellenőrizni)."""
        return {
            "effective_config": self.effective_config(),
            "revision": self.config_revision,
            "validation_state": self._last_validation_state.value,
        }

    def build_summary(self) -> dict:
        """SUP02-VALID-005/UI-001: Start előtti effective summary — a
        felhasználó ebből lát mindent, mielőtt elindítja a mérést."""
        warnings: list[str] = []
        blocking_errors: list[str] = []
        custom_overrides: list[str] = []

        requested_charge_A = self.requested_charge_current_A
        if requested_charge_A is not None and is_group_visible(self.test_type, "charge_current"):
            if requested_charge_A > self.charge_current_profile_limit_A:
                blocking_errors.append(
                    f"CONFIG_INCOMPATIBLE: kért töltőáram "
                    f"{requested_charge_A} A > profilmaximum "
                    f"{self.charge_current_profile_limit_A} A"
                )
            if requested_charge_A > self.charge_current_hardware_limit_A + 1e-9:
                blocking_errors.append(
                    f"CONFIG_INCOMPATIBLE: kért töltőáram "
                    f"{requested_charge_A} A > PSU {self.psu_mode} limit "
                    f"{self.charge_current_hardware_limit_A} A"
                )

        if self.is_discharge_current_incompatible and is_group_visible(self.test_type, "discharge_rate"):
            blocking_errors.append(
                f"CONFIG_INCOMPATIBLE: kért kisütőáram "
                f"{self.discharge_current_A} A > Load limit {LOAD_HW_MAX_CURRENT_A} A"
            )

        # Review-kör 2 (2026-09-09), friss GUI/UX F7 + Szoftver/Architektúra
        # F6 (DFAM-026 declared_policy_without_consumer): a fenti "warnings"
        # lista korábban SZERKEZETILEG soha nem kapott append-et -- a Start
        # előtti összegzőben (_format_start_summary(), ami MÁR renderelte
        # ezt a kulcsot, ha nem üres) ez a csatorna holt volt. Az EGYETLEN
        # ma, Start-időben (SafetyManager/BatteryProfile felépítése ELŐTT)
        # kiértékelhető safety-figyelmeztetés a SafetyManager::check_psu_
        # mode_compatibility() 12V-pack+SERIES ága -- ugyanaz a feltétel és
        # üzenet, csak a cell_count/psu_mode mezőkből, amik itt már
        # rendelkezésre állnak (nem hoz létre új SafetyManager-példányt).
        if self.cell_count <= 6 and self.psu_mode == "SERIES":
            warnings.append(
                f"{self.cell_count} cellás / 12V pack SERIES PSU módban "
                f"szokatlan; kezelői megerősítés szükséges."
            )

        # R1 B8c, F-24 (P2, DFAM-026, D-B8-3 b): csak az aktív módban
        # LÁTHATÓ csoport mezője listázható -- a CUSTOM policy módváltáskor
        # draft nélkül átöröklődik (set_test_type()), és a rejtett, a
        # célmódban hatástalan értéket a Start-összegző korábban "egyedi"
        # beállításként mutatta. Ugyanaz a mező->csoport leképezés, mint az
        # effective_config()-ban.
        for name, group in (
            ("discharge_current_A", "discharge_rate"),
            ("requested_charge_current_A", "charge_current"),
            ("terminate_voltage_pack_V", "terminate_voltage"),
            ("pulse_current_A", "pulse_current"),
        ):
            if not is_group_visible(self.test_type, group):
                continue
            dp = self._derived.get(name)
            if dp is not None and dp.source == ParamSource.USER_OVERRIDE:
                custom_overrides.append(name)

        summary: dict = {
            "test_type": self.test_type,
            "battery_profile_name": self.battery_profile_name,
            "cell_count": self.cell_count,
            "psu_mode": self.psu_mode,
            "wiring_confirmed": self.wiring_confirmed,
            "warnings": warnings,
            "blocking_errors": blocking_errors,
            "custom_overrides": custom_overrides,
        }
        summary.update(self.effective_config())
        return summary


class _BatchGuard:
    """SUP02-REACT-002: batch update context manager — YAML betöltés,
    profilváltás és tesztmódváltás alatt csak EGYETLEN végső recompute fusson."""

    def __init__(self, model: DynamicSessionModel) -> None:
        self._model = model

    def __enter__(self) -> DynamicSessionModel:
        self._model._batch_depth += 1
        return self._model

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._model._batch_depth -= 1
        if self._model._batch_depth == 0:
            self._model.recompute()
