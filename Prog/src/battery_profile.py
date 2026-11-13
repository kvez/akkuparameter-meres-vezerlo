from dataclasses import dataclass
from decimal import Decimal
from math import isfinite

from Prog.src.exceptions import ProfileValidationError

# D9/F-07 (2026-09-13, Riportok/ui_policy_audit_20260913.md): a kemény
# safety felső határ V/cellánként korábban KIZÁRÓLAG a
# `batt_absolute_max_V_per_cell` property törzsében élt literálként —
# a GUI-nak (config_panel.py, readonly "charge_safety_limits" sor) erre
# a SAJÁT konstansra van szüksége, hogy ne duplikálja a számot egy
# második, kézzel karbantartott helyen (DFAM-017-elv). Nulla
# viselkedésváltozás: a property VÁLTOZATLANUL ezt a konstanst adja
# vissza, csak a forrás most modul-szinten, importálhatóan él. L. a
# property docstringje a forrás/indoklás teljes szövegéért.
BATT_ABSOLUTE_MAX_V_PER_CELL = 2.425

# Q-26 / Q-44 / Q-48: felhasznaloi toltesi cel-plafon, nem kezeloi konfiguracio.
# A FIAMM hideg oldali emelese megengedett eljaras, de a fixture-re
# adott 2,400 V/cella korlat szigorubb; az abszolut safety-limit megmarad.
CHARGE_MAX_V_PER_CELL = 2.400


# GOV-029 / Q-34: a profil önmagában nem bizonyít adatlapi C20-rátát.
NORMAL_DISCHARGE_POLICY_ID = "GOV-029/Q-34:B16a-v1"


@dataclass(frozen=True)
class DischargeRateOrigin:
    """Belső eredet-snapshot; nem kezelői felmentés és nem TL-flag."""
    rate_policy: str = "UNKNOWN"
    capacity_basis: str = "UNKNOWN"
    capacity_Ah: float = 0.0


UNKNOWN_DISCHARGE_RATE_ORIGIN = DischargeRateOrigin()


def discharge_rate_origin_for_mode(
    test_type: str, requested: DischargeRateOrigin, nominal_capacity_Ah: float,
    discharge_rate_divisor: int,
) -> DischargeRateOrigin:
    """Az adott fogyasztó tényleges bázisa; a rejtett GUI-draft nem eredet."""
    if test_type in {"DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL"}:
        return requested
    if test_type == "CHEMID_CHARACTERIZATION_PBA":
        return DischargeRateOrigin("C/20", "PROFILE_C10_BASE_Ah", nominal_capacity_Ah)
    if test_type == "OCV_SOC_CHARACTERIZATION":
        return DischargeRateOrigin(
            f"C/{discharge_rate_divisor}", "PROFILE_C10_BASE_Ah", nominal_capacity_Ah,
        )
    if test_type == "GOLDEN_GG":
        return DischargeRateOrigin("CUSTOM", "SYSTEM_RATE_CURRENT_A")
    return DischargeRateOrigin()


def normal_discharge_minimum_per_cell(
    *, manufacturer: str = "", model: str = "",
    current_A: float = 0.0, origin: DischargeRateOrigin = UNKNOWN_DISCHARGE_RATE_ORIGIN,
) -> float:
    """FIAMM FG_FOLDER_EN Rev.11.2025, FG20721: 20H/7,2Ah/1,75VPC.
    C10 és más/bizonytalan/CUSTOM eredet: GOV-029 szerint 1,80 V/cella.
    A /20 osztó és a numerikus áramegyezés külön-külön nem bizonyíték.
    """
    # A NOMINAL_C10_CAPACITY a meglévő UI-enum neve; itt az exact FG20721
    # adatlap 7,2 Ah-s C20-bázisa az eredet, nem az enum elnevezése.
    if (
        manufacturer == "FIAMM" and model == "FG20721"
        and origin.rate_policy == "C/20"
        and origin.capacity_basis == "NOMINAL_C10_CAPACITY"
        and origin.capacity_Ah == 7.2
        and current_A == origin.capacity_Ah / 20
    ):
        return 1.75
    return 1.80


def validate_normal_discharge(
    profile: "BatteryProfile", current_A: float, cutoff_V: float,
    origin: DischargeRateOrigin = UNKNOWN_DISCHARGE_RATE_ORIGIN,
) -> float:
    """A kért értéket elutasítjuk; nincs clamp vagy csendes javítás."""
    minimum = normal_discharge_minimum_per_cell(
        manufacturer=profile.manufacturer, model=profile.model,
        current_A=current_A, origin=origin,
    )
    if origin.capacity_Ah != 0.0 and (
        not isfinite(origin.capacity_Ah)
        or (
            origin.capacity_basis == "NOMINAL_C10_CAPACITY"
            and origin.capacity_Ah != profile.nominal_capacity_Ah
        )
    ):
        raise ValueError("GOV-029: a kapacitásbázis eredete nem egyezik a profillal")
    if not isfinite(current_A) or current_A <= 0.0:
        raise ValueError(f"GOV-029: érvénytelen kisütőáram: {current_A!r}")
    if not isfinite(cutoff_V) or cutoff_V < minimum * profile.cell_count:
        raise ValueError(
            f"GOV-029: végfeszültség {cutoff_V!r} V < "
            f"{minimum * profile.cell_count:.3f} V; eredet={origin!r}"
        )
    return minimum * profile.cell_count


@dataclass
class BatteryProfile:
    battery_name: str
    manufacturer: str
    model: str
    nominal_voltage_V: float = 12.0
    cell_count: int = 6
    nominal_capacity_Ah: float = 0.0
    chemistry: str = "AGM_VRLA"
    profile_source: str = ""
    charge_voltage_per_cell_V: float = 2.40
    terminate_voltage_per_cell_V: float = 1.80
    float_voltage_per_cell_V: float = 2.27
    C10_capacity_Ah: float | None = None   # R16-B7: a BQ-design/learning explicit C10-et követel; csak a régi, nem BQ fogyasztók használnak névleges fallbacket.
    # Új mező (governance-v2 kör 2, IEC 60896-21 DC belső ellenállás mód
    # bevezetése): a 10 órás kisütési áram [A], AZ ADOTT AKKUMODELL SAJÁT,
    # jóváhagyott gyártói C10 kisütési táblázatából (10 h, 1,80 V/cella),
    # NEM a nominal_capacity_Ah/10-ből számolva -- ez a project-oldali
    # döntés FÜGGETLEN a BAT-CAP-001 REVIEW STOP-tól (nominal_capacity_Ah
    # C10 vagy C20 bázisa, l. C10_capacity_Ah fenti kommentje): akármelyik
    # olvasat legyen is végül helyes, egy KÜLÖN, közvetlenül a gyártói C10
    # táblázatból eredő mező pontosabb, mint bármilyen /10 levezetés.
    # Forrás/módszertan: Folyamatok/tervek/
    # AGM_VRLA_belsoellenallas_IEC60896_ACIR_HRM10.txt 3-4. szakasz, FG20721
    # példával (eredetileg I10≈0,62 A becslés -- 2026-09-12: primer FIAMM
    # adatlap alapján I10=0,67 A-ra pontosítva, l. CLAIM_LEDGER.yaml
    # BAT-FG20721-I10-001). Csak az IEC_DC_INTERNAL_RESISTANCE teszt módhoz
    # kötelező (Ia=4×I10, Ib=20×I10 terhelési pontok forrása) -- más módoknál
    # nem használt, None maradhat.
    i10_discharge_current_A: float | None = None
    max_charge_current_A: float | None = None
    taper_current_A: float | None = None
    temp_comp_mV_per_cell_per_degC: float = -2.5
    test_temperature_min_C: float = 15.0
    test_temperature_max_C: float = 30.0

    def __post_init__(self):
        self._validate()

    def _validate(self) -> None:
        if not self.model or not self.model.strip():
            raise ProfileValidationError(
                f"BatteryProfile.model mező kötelező (pl. 'FG20721'). "
                f"Kapott: {self.model!r}"
            )
        if self.nominal_capacity_Ah <= 0:
            raise ProfileValidationError(
                f"nominal_capacity_Ah > 0 kötelező. Kapott: {self.nominal_capacity_Ah}"
            )
        if not (0.1 <= self.nominal_capacity_Ah <= 1000):
            raise ProfileValidationError(
                f"nominal_capacity_Ah tartomány: 0.1–1000 Ah. "
                f"Kapott: {self.nominal_capacity_Ah}"
            )
        if not (2.00 <= self.charge_voltage_per_cell_V <= 2.60):
            raise ProfileValidationError(
                f"charge_voltage_per_cell_V tartomány: 2.00–2.60 V. "
                f"Kapott: {self.charge_voltage_per_cell_V}"
            )
        minimum = normal_discharge_minimum_per_cell()
        if not (minimum <= self.terminate_voltage_per_cell_V <= 2.00):
            raise ProfileValidationError(
                f"terminate_voltage_per_cell_V tartomány: {minimum:.2f}–2.00 V (GOV-029). "
                f"Kapott: {self.terminate_voltage_per_cell_V}"
            )
        if not (1 <= self.cell_count <= 24):
            raise ProfileValidationError(
                f"cell_count tartomány: 1–24. Kapott: {self.cell_count}"
            )
        if self.C10_capacity_Ah is not None:
            if not (0.1 <= self.C10_capacity_Ah <= 1000):
                raise ProfileValidationError(
                    f"C10_capacity_Ah tartomány: 0.1–1000 Ah. Kapott: {self.C10_capacity_Ah}"
                )
            if self.C10_capacity_Ah > self.nominal_capacity_Ah:
                raise ProfileValidationError(
                    f"C10_capacity_Ah ({self.C10_capacity_Ah}) nem lehet nagyobb "
                    f"a nominal_capacity_Ah ({self.nominal_capacity_Ah}) értéknél. "
                    f"C10 ≤ C20 (gyorsabb kisütés → kisebb kapacitás)."
                )
        if self.i10_discharge_current_A is not None:
            if self.i10_discharge_current_A <= 0:
                raise ProfileValidationError(
                    f"i10_discharge_current_A > 0 kötelező, ha meg van adva. "
                    f"Kapott: {self.i10_discharge_current_A}"
                )
            # Plauzibilitási felső korlát: egy tipikus AGM/VRLA C10 kisütési
            # ráta kb. 0,08-0,12×nominal_capacity_Ah (l. FG20721 példa,
            # 2026-09-12-i primer adatlap-ertek: 0,67 A / 7,2 Ah ≈ 0,093) --
            # MAX verifier lelet (governance-v2
            # kör 2, F7): a korábbi korlát (i10 > nominal_capacity_Ah, azaz
            # akár 1×C is átment volna) implauzibilisan laza volt ehhez
            # képest, és a kiszámolt Ib=20×I10 terhelőáram-parancsot ELLENŐRZÉS
            # NÉLKÜL küldi ki a load felé (a safety power/voltage-check csak
            # az input_on() UTÁN, a MÉRT áramra fut, l. iec_dc_resistance_
            # controller.py::_run_load_1/2 kommentje) -- egy nagyságrendekkel
            # túl nagy I10 (pl. Ah/10 elfelejtve, vagy mAh/A keverés) a
            # ténylegesen leadható tartományban is irreálisan nagy Ib
            # parancsot generálna. 0,5×nominal_capacity_Ah bőséges (kb.
            # 5x-6x) tartalékot hagy a tipikus C10 ráta felett, mégis
            # elkapja a nagyságrendi adatbeviteli hibákat.
            if self.i10_discharge_current_A > 0.5 * self.nominal_capacity_Ah:
                raise ProfileValidationError(
                    f"i10_discharge_current_A ({self.i10_discharge_current_A} A) "
                    f"implauzibilisan nagy a nominal_capacity_Ah "
                    f"({self.nominal_capacity_Ah} Ah) mellett (egy tipikus C10 "
                    f"ráta kb. 0,08-0,12×kapacitás) -- ellenőrizd, hogy "
                    f"a modell saját C10 kisütési táblázatából (10 h, 1,80 V/cella) "
                    f"származik, nem Ah/10 közelítésből vagy mértékegység-hibából."
                )

    # ------------------------------------------------------------------ #
    # Safety határok                                                       #
    # ------------------------------------------------------------------ #

    @property
    def batt_absolute_max_V_per_cell(self) -> float:
        """
        Kemény safety felső határ V/cellánként.
        Forrás: mérnöki döntés (nem gyári adatlap-érték) — a FIAMM Engineering
        Manual 2,40 V/cella boost célfeszültsége fölötti ~25 mV/cella biztonsági
        tartalék. Eredet: Folyamatok/döntések/d7.txt (GUI-ból nem módosítható
        safety limitek listája) + Folyamatok/tervek/implementacios_terv_v1.2.1.txt
        [N1] (hőkompenzáció clamp ehhez igazítva, korábbi 2.50V/cella helyett) +
        Folyamatok/külső-review-k/rev1.2.txt (a limit eredeti bevezetése és
        indoklása: "normál boost fölötti safety határ").
        """
        return BATT_ABSOLUTE_MAX_V_PER_CELL

    @property
    def batt_absolute_max_V(self) -> float:
        return round(self.batt_absolute_max_V_per_cell * self.cell_count, 6)

    # ------------------------------------------------------------------ #
    # Pack feszültségek                                                    #
    # ------------------------------------------------------------------ #

    @property
    def charge_voltage_pack_V(self) -> float:
        return round(min(CHARGE_MAX_V_PER_CELL, self.charge_voltage_per_cell_V) * self.cell_count, 6)

    @property
    def terminate_voltage_pack_V(self) -> float:
        return self.terminate_voltage_per_cell_V * self.cell_count

    @property
    def float_voltage_pack_V(self) -> float:
        return self.float_voltage_per_cell_V * self.cell_count

    # ------------------------------------------------------------------ #
    # C-ráta számítás [R2]                                                 #
    # FIAMM Engineering Manual: 0.25 C10 max charge, 0.03 C10 taper stop. #
    # FRISSÍTÉS (2026-09-07, governance-v2 review-kör 1, CF-11f/CF-49,    #
    # BAT-CAP-001): a korábbi "FGH/FGHL: nominal=C10" feltevés HAMIS — a  #
    # FIAMM FGH/FGHL katalógus (FIAMM-FGH-FGHL-FOLDER) is KÜLÖN 10H/20H   #
    # kapacitásoszlopot ad, eltérő értékekkel (pl. 12FGH36: 7.6/9.0 Ah).  #
    # R16-B7: a névleges rate-bázis külön mezője továbbra sem létezik.     #
    # A forrásolt FG20721 GUI-profil explicit C10-et kap; BQ-design és    #
    # learning hiányzó C10-nél blokkol. Csak a régi, nem BQ fogyasztók     #
    # használják a _c10_base_Ah névleges fallbackjét. Ez nem oldja fel a   #
    # többi modell rate-bázisának CF-49/BAT-CAP-001 forráskapuját.         #
    # ------------------------------------------------------------------ #

    @property
    def _c10_base_Ah(self) -> float:
        """Korábbi, nem BQ fogyasztók kapacitásbázisa; a fenti CF-49 történeti.
        R16-B7: BQ-design/learning csak require_c10_capacity_Ah()-t használ;
        a forrásolt FG20721 BQ-profil C10-adatát a GUI külön tölti ki.
        Ez a megőrzött fallback nem igazol névleges C10-bázist."""
        return self.C10_capacity_Ah if self.C10_capacity_Ah is not None else self.nominal_capacity_Ah

    def require_c10_capacity_Ah(self) -> float:
        """Q-9: BQ-design és learning nem használhat névleges fallbacket."""
        value = self.C10_capacity_Ah
        if value is None or not isfinite(value) or value <= 0:
            raise ProfileValidationError("SOURCE_REQUIRED: explicit C10_capacity_Ah szükséges")
        return value

    @property
    def fg20721_parallel_count(self) -> int:
        """FG_FOLDER_EN Rev.11.2025 p2: exact FG20721, 12 V, C20=7,2 Ah."""
        if (
            self.manufacturer != "FIAMM" or self.model != "FG20721"
            or (self.nominal_voltage_V, self.cell_count) not in {(12.0, 6), (24.0, 12)}
        ):
            raise ProfileValidationError("SOURCE_REQUIRED: csak FG20721 12/24 V forrásolt")
        count = self.nominal_capacity_Ah / 7.2
        if not isfinite(count) or count < 1 or abs(count - round(count)) > 1e-9:
            raise ProfileValidationError("SOURCE_REQUIRED: FG20721 C20 = 7,2 Ah × párhuzamos szám")
        return round(count)

    @property
    def fg20721_c10_capacity_Ah(self) -> float:
        """FG_FOLDER_EN Rev.11.2025 p2, FG20721 10H/1,80 VPC: 6,7 Ah."""
        return 6.7 * self.fg20721_parallel_count

    @property
    def qmax_cell0_initial_mAh(self) -> int:
        """Q-23 PROJECT_POLICY: C20; eltér a Q-9 C10 design-bázisától."""
        value = 7200 * self.fg20721_parallel_count
        if value > 32767:
            raise ProfileValidationError("Q-22: Qmax kívül a nem-X10 I2 tartományon")
        return value

    @property
    def design_energy_scale(self) -> int:
        """Q-9 PROJECT_POLICY; SLUSB55B Rev.B p17 Table 10."""
        return 10

    @property
    def pack_configuration_x10(self) -> int:
        """Q-22: WP-X10 halasztva; SLUSB55B Rev.B p20 Table 12 bit7."""
        return 0

    @property
    def load_mode(self) -> int:
        """SLUSB55B Rev.B p16: az alapértelmezett állandóáram-modell."""
        return 0

    @property
    def load_select(self) -> int:
        """SLUSB55B Rev.B p17 Table 8: 1 = az aktuális ciklus addigi átlagárama."""
        return 1

    @property
    def design_energy_cWh(self) -> int:
        """Q-9: teljes pack névleges energiája, C10; Table 20 p39 I2."""
        # A decimális végpontot a bináris float szorzási hibája nem tolhatja túl.
        raw = Decimal(str(self.require_c10_capacity_Ah())) * Decimal(str(self.nominal_voltage_V)) * 100
        if not raw.is_finite() or not 0 < raw <= 32767:
            raise ProfileValidationError("Q-22: Design Energy kívül a nem-X10 cWh tartományon")
        return round(raw)

    @property
    def bq_learning_discharge_current_A(self) -> float:
        """Q-9: a C/10 learning-áram csak explicit C10-bázisból képezhető."""
        return self.require_c10_capacity_Ah() / 10

    @property
    def design_capacity_mAh(self) -> int:
        """BQ34Z110 Data Flash 'Design Capacity' (SC48/off21) elvárt értéke.

        Forrás: Folyamatok/tervek/TRADITIONAL_LEARNING_BQ_PROVISIONING_AUDIT_2026-08-24.md
        3.5 szakasz (FIAMM AGM Engineering Manual alapján) — a Design Capacity a C10
        kapacitásból, NEM a nominal_capacity_Ah-ból (ami FG-sorozatnál C20) származik.
        """
        raw = Decimal(str(self.require_c10_capacity_Ah())) * 1000
        if not 0 < raw <= 32767:
            raise ProfileValidationError("Q-22: Design Capacity kívül a nem-X10 mAh tartományon")
        return round(raw)

    @property
    def C5_discharge_current_A(self) -> float:
        """Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
        Adatintegritás P2 lelet: ugyanaz a hibaosztály, mint a
        C10_discharge_current_A release-audit 2.9 fixje fent — korábban
        nominal_capacity_Ah/5-öt adott, NEM _c10_base_Ah/5-öt. FG-sorozatnál
        (nominal=C20), ha a C10_capacity_Ah külön meg van adva (BAT-002
        javítása után), ez a deklaráltnál magasabb kisütőáramot eredményezne
        — jelenleg gyakorlatban maszkolt (BAT-002 nyitva, a GUI sosem tölti
        ki C10_capacity_Ah-t, tehát _c10_base_Ah == nominal_capacity_Ah
        mindig igaz éles session-ben), de preventíven javítva, ugyanazzal az
        indoklással, mint C10_discharge_current_A-nál."""
        return self._c10_base_Ah / 5

    @property
    def C10_discharge_current_A(self) -> float:
        """Kisütőáram C/10 rátán (A) — NEM azonos az effective_max_charge_A-val!
        Release-audit 2026-08-23 (2.9): korábban nominal_capacity_Ah/10-et
        adott, NEM _c10_base_Ah/10-et — inkonzisztens a testvér
        effective_max_charge_A/effective_taper_A property-kkel, amik
        helyesen _c10_base_Ah-t használják. FG-sorozatnál (C20-nominal),
        ha a C10_capacity_Ah külön meg van adva (BAT-002 javítása után),
        ez korábban a deklaráltnál magasabb BQ golden-image tanulási
        kisütőáramot eredményezett volna."""
        return self._c10_base_Ah / 10

    @property
    def C20_discharge_current_A(self) -> float:
        """Régi, nem BQ C10/20 segédérték; explicit C10 hiányában névleges
        fallbacket használ. Nem azonos a modell adatlapi C20-áramával.
        R16-B7 óta a BQ learning külön, kötelező C10-bázist használ."""
        return self._c10_base_Ah / 20

    @property
    def effective_max_charge_A(self) -> float:
        """FIAMM: 0.25 × C10_capacity_Ah [A] (ha nincs C10: fallback=nominal)"""
        if self.max_charge_current_A is not None:
            return self.max_charge_current_A
        return 0.25 * self._c10_base_Ah

    @property
    def effective_taper_A(self) -> float:
        """FIAMM: 0.03 × C10_capacity_Ah [A] (ha nincs C10: fallback=nominal)"""
        if self.taper_current_A is not None:
            return self.taper_current_A
        return 0.03 * self._c10_base_Ah

    # ------------------------------------------------------------------ #
    # Hőkompenzáció [N1]                                                   #
    # Q-26/Q-44: clamp [2.30, CHARGE_MAX_V_PER_CELL] V/cella              #
    # ------------------------------------------------------------------ #

    def compensated_charge_voltage_V(self, temperature_C: float) -> float:
        """Hokompenzalt toltofeszultseg (pack, V), Q-26/Q-44 plafonnal.
        A FIAMM linearis -2,5 mV/cella/C gorbe hideg oldali emeleset a
        szigorubb felhasznaloi 2,400 V/cella korlat vagja; melegben a gorbe
        valtozatlan. A 2,425 V/cella abszolut safety-limit kulon megmarad.

        [SW-P3-4, Kor 18 Batch C14, DFAM-042]
        Az abszolut safety-hatar dokumentalt mernoki dontes; a toltesi
        felso clampet a szigorubb Q-26/Q-44 korlat adja. Az
        ALSÓ határ (2.30 V/cella) NEM — bare literál, forrás/döntés-
        dokumentáció nélkül a repóban (CLAIM_LEDGER.yaml BAT-TEMPCOMP-
        CLAMP-001, SOURCE_REQUIRED). Ez a floor MELEG hőmérsékleten
        CSENDBEN FELEMELHETI a számított/konfigurált kimenetet — nincs
        event/log/quality-flag, ami ezt jelezné. A jelenlegi FIAMM-
        validált -20..+50°C tartományban ez valószínűleg ritkán/soha nem
        aktiválódik, de ez NEM bizonyított, csak valószínűsített —
        HALASZTVA, nem javítva ebben a körben.
        """
        corrected = self.charge_voltage_per_cell_V + (
            self.temp_comp_mV_per_cell_per_degC / 1000.0
        ) * (temperature_C - 20.0)
        corrected = max(2.30, min(CHARGE_MAX_V_PER_CELL, self.batt_absolute_max_V_per_cell, corrected))
        return round(corrected * self.cell_count, 6)
