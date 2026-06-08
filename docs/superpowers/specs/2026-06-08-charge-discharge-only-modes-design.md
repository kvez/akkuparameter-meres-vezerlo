# Design: CHARGE_ONLY / DISCHARGE_ONLY módok + GUI bővítés
**Dátum:** 2026-06-08
**Státusz:** Jóváhagyva

## Összefoglaló

Hat párhuzamos fejlesztés egy csomagban:
1. Logo fix — dedikált fejlécsáv a tabok felett
2. C10 kapacitás mező — label átnevezés, logika változatlan
3. CHARGE_ONLY teszttípus — CC/CV/Taper + rövid relax, majd leáll
4. DISCHARGE_ONLY teszttípus — CC kisütés, majd leáll
5. Kisütési profilok — 3 preset gomb (C/5 / C/10 / C/20) + szabad áram + végfeszültség
6. Töltőáram felülírás — SpinBox + "számított: X.XX A" felirat, PSU-limit clamp

## Architekturális döntések

- **BatteryProfile változatlan** — a `nominal_capacity_Ah` már most is C10-ként van kezelve
  (`effective_max_charge_A = 0.25 × nominal_capacity_Ah` = FIAMM adatlap szerinti érték).
  Csak a GUI label változik.
- **RelaxController változatlan** — a `RelaxConfig(min_relax_s=...)` már parametrizálható.
  CHARACTERIZATION: 7200s, CHARGE_ONLY: `relax_after_charge_s` (default 600s).
- **SafetyManager, Logger, Worker, InstrumentManager** — érintetlen.
- **PSU áramlimit** mindig érvényes: INDEPENDENT 1.5A, PARALLEL 3.0A, SERIES 1.5A.
  Az override sem léphet túl rajta — a ChargeController és a GUI egyaránt kényszeríti.

## Érintett fájlok

| Fájl | Változás típusa |
|------|----------------|
| `Prog/src/test_runner.py` | 2 új TestType + 2 új TestPlan |
| `Prog/src/discharge_controller.py` | `terminate_voltage_V_override` a DischargeConfig-ban |
| `Prog/src/charge_controller.py` | `charge_current_A_override` a ChargeConfig-ban |
| `Prog/gui/panels/config_panel.py` | SessionConfig + 2 új GroupBox + preset gombok |
| `Prog/gui/main_window.py` | fejlécsáv + `_build_runner` bővítés |

## Backend részletek

### test_runner.py

```python
class TestType(Enum):
    CHARACTERIZATION = "CHARACTERIZATION"
    BQ_LEARNING_PHYSICAL = "BQ_LEARNING_PHYSICAL"
    OCV_SOC_CHARACTERIZATION = "OCV_SOC_CHARACTERIZATION"
    CHARGE_ONLY = "CHARGE_ONLY"       # ÚJ
    DISCHARGE_ONLY = "DISCHARGE_ONLY" # ÚJ

class TestPlan:
    @staticmethod
    def charge_only() -> "TestPlan":
        return TestPlan(
            test_type=TestType.CHARGE_ONLY,
            steps=(
                TestStep(StepKind.CHARGE, "charge"),
                TestStep(StepKind.RELAX,  "relax_after_charge"),
            ),
        )

    @staticmethod
    def discharge_only() -> "TestPlan":
        return TestPlan(
            test_type=TestType.DISCHARGE_ONLY,
            steps=(
                TestStep(StepKind.DISCHARGE, "discharge"),
            ),
        )
```

### discharge_controller.py — DischargeConfig

```python
@dataclass
class DischargeConfig:
    discharge_current_A: float = 0.0
    terminate_voltage_V_override: float = 0.0   # ÚJ; 0 = profil default
    max_discharge_time_s: float = 86400.0
    max_discharge_Ah_factor: float = 1.10
```

Felhasználás a controllerben:
```python
terminate_V = (
    self._config.terminate_voltage_V_override
    if self._config.terminate_voltage_V_override > 0
    else self._profile.terminate_voltage_pack_V
)
```

### charge_controller.py — ChargeConfig

```python
@dataclass
class ChargeConfig:
    charge_current_A_override: float = 0.0   # ÚJ; 0 = auto profile
    # ... többi mező változatlan
```

Felhasználás `_run_psu_preset()`-ben:
```python
psu_hw_max_A = {
    PsuMode.INDEPENDENT: 1.5,
    PsuMode.PARALLEL: 3.0,
    PsuMode.SERIES: 1.5,
}[self._safety.psu_mode]

if self._config.charge_current_A_override > 0:
    charge_A = min(self._config.charge_current_A_override, psu_hw_max_A)
else:
    charge_A = min(self._profile.effective_max_charge_A, psu_hw_max_A)
```

### _build_runner() bővítés

```python
# Kisütési kontroller
def _make_discharge_ctrl():
    return DischargeController(
        psu, load, dmm_v, dmm_t, profile, safety,
        DischargeConfig(
            discharge_current_A=cfg.discharge_current_A,
            terminate_voltage_V_override=cfg.discharge_terminate_voltage_V,
        ),
    )

# Töltési kontroller
def _make_charge_ctrl():
    return ChargeController(
        psu, load, dmm_v, dmm_t, profile, safety,
        ChargeConfig(
            charge_current_A_override=cfg.charge_current_A_override,
            taper_hold_s=cfg.taper_hold_s,
        ),
    )

# Relax kontroller — CHARGE_ONLY esetén rövid relax
def _make_relax_ctrl():
    relax_s = cfg.relax_after_charge_s  # CHARGE_ONLY: 600s; CHARACTERIZATION: 7200s
    rc = RelaxController(dmm_v, RelaxConfig(min_relax_s=relax_s))
    rc.on_event = lambda ev: logger.log_event(...)
    return rc

# TestPlan kiválasztás
if cfg.test_type == "CHARGE_ONLY":
    test_plan = TestPlan.charge_only()
elif cfg.test_type == "DISCHARGE_ONLY":
    test_plan = TestPlan.discharge_only()
elif cfg.test_type == "CHARACTERIZATION":
    test_plan = TestPlan.characterization()
elif cfg.test_type == "OCV_SOC_CHARACTERIZATION":
    test_plan = TestPlan.ocv_soc_characterization()
else:
    test_plan = TestPlan.bq_learning_physical()
```

**Megjegyzés:** CHARACTERIZATION-nál a relax idő a default 7200s marad (RelaxConfig default),
CHARGE_ONLY esetén `cfg.relax_after_charge_s` kerül átadásra.
A `_make_relax_ctrl` factory ezt az eltérést kezeli: a factory closure zárja be a megfelelő értéket.

## GUI részletek

### main_window.py — fejlécsáv

```python
# setCentralWidget(self._tabs) helyett:
container = QWidget()
vbox = QVBoxLayout(container)
vbox.setContentsMargins(0, 0, 0, 0)
vbox.setSpacing(0)

header = QWidget()
header.setFixedHeight(72)
hbox = QHBoxLayout(header)
# logo (scaledToHeight 64) + "Akkuteszter — Labor műszerfal" QLabel
vbox.addWidget(header)
vbox.addWidget(self._tabs)
self.setCentralWidget(container)
# cornerWidget törlése
```

### config_panel.py — SessionConfig bővítés

```python
@dataclass
class SessionConfig:
    # Meglévő mezők (változatlan értékek, csak label változik a GUI-ban)
    nominal_capacity_ah: float = 0.0   # C10 kapacitás

    # ÚJ mezők
    relax_after_charge_s: float = 600.0
    charge_current_A_override: float = 0.0
    discharge_current_A: float = 0.0
    discharge_terminate_voltage_V: float = 0.0
```

### config_panel.py — "Akkumulátor" GroupBox

- `"Névleges kapacitás:"` → `"C10 kapacitás (Ah):"`
- Tooltip: `"Az adatlap szerinti 10 órás kapacitás. C-ráta számítás alapja (0.25×C10 = max töltőáram)."`

### config_panel.py — "Teszt" GroupBox bővítés

- `test_type_combo`: hozzáadva `"CHARGE_ONLY"`, `"DISCHARGE_ONLY"`
- Új sor: `"Relax töltés után:"` — `QDoubleSpinBox`, 60–7200s, alapérték 600s, suffix ` s`
- **`discharge_rate_combo` ELTÁVOLÍTVA** — a szerepét az új "Kisütési paraméterek" GroupBox veszi át.
  A `SessionConfig.discharge_rate_divisor` mező megmarad fallback-ként (ha `discharge_current_A == 0`),
  de a GUI-ból nem szerkeszthető közvetlenül — a preset gombok implicit kezelik.
- Az OCV-SOC lépés sor megmarad (csak OCV_SOC módhoz releváns, de nem rejtjük el — YAGNI)

### config_panel.py — "Töltési paraméterek" GroupBox (ÚJ)

```
Töltőáram:  [SpinBox 0.10–max A, lépés 0.01]  A    "számított: 1.44 A"
```

Logika:
- `_psu_hw_max_A()`: `3.0` ha PARALLEL, egyébként `1.5`
- SpinBox max = `_psu_hw_max_A()`
- "számított" felirat = `min(nominal_capacity_ah × 0.25, _psu_hw_max_A())`
- Frissítés trigger: PSU mód combo + C10 kapacitás spinbox `valueChanged`
- PSU mód váltáskor ha SpinBox.value() > új max → `setValue(új max)`
- SpinBox alapértéke induláskor = számított érték

### config_panel.py — "Kisütési paraméterek" GroupBox (ÚJ)

```
Preset:         [C/5]  [C/10]  [C/20]
Áram:           [SpinBox 0.0–60.0 A, lépés 0.01]  A
Végfeszültség:  [SpinBox 0.0–60.0 V, lépés 0.01]  V
```

Preset gomb hatások:

| Gomb | Áram képlete | Végfeszültség |
|------|-------------|---------------|
| C/5  | C10 ÷ 5     | 1.80 V/cella  |
| C/10 | C10 ÷ 10    | 1.80 V/cella  |
| C/20 | C10 ÷ 20    | 1.75 V/cella  |

- 0.0 A az áram mezőben = auto (C10 / `discharge_rate_divisor`, visszafelé kompatibilis)
- 0.0 V a végfeszültség mezőben = profil default (1.80V/cella)
- A preset gombok a C10 kapacitás spinbox aktuális értékéből számolnak

### config_panel.py — validate() bővítés

```python
# Végfeszültség ellenőrzés (ha be van állítva)
if self.discharge_terminate_voltage_V > 0:
    cell_count = _PROFILE_DEFAULTS.get(
        self.battery_profile_name, {}
    ).get("cell_count", 6)
    min_v = cell_count * 1.60
    if self.discharge_terminate_voltage_V < min_v:
        errors.append(
            f"Végfeszültség ({self.discharge_terminate_voltage_V:.2f}V) "
            f"< 1.60V/cella minimum ({min_v:.2f}V)"
        )

# Töltőáram override ellenőrzés
if self.charge_current_A_override > 0:
    psu_max = 3.0 if self.psu_mode == "PARALLEL" else 1.5
    if self.charge_current_A_override > psu_max:
        errors.append(
            f"Töltőáram ({self.charge_current_A_override:.2f}A) "
            f"> PSU {self.psu_mode} limit ({psu_max:.1f}A)"
        )
```

## Tesztelési terv

1. **Unit tesztek** (meglévő struktúra kiterjesztése):
   - `TestPlan.charge_only()` és `TestPlan.discharge_only()` lépéseinek ellenőrzése
   - `DischargeConfig.terminate_voltage_V_override` — kontroller a helyes feszültséget használja
   - `ChargeConfig.charge_current_A_override` — PSU limit fölötti érték clampelődik
   - `SessionConfig.validate()` — végfeszültség és töltőáram határok

2. **Integrációs tesztek** (mock driverekkel):
   - CHARGE_ONLY: CHARGE_DONE → RELAX_DONE → TestResult.status == "DONE"
   - DISCHARGE_ONLY: DISCHARGE_DONE → TestResult.status == "DONE"

3. **GUI tesztek** (SessionConfig szintjén):
   - PSU mód váltás → SpinBox max frissül + clamp
   - C10 értékváltozás → "számított" felirat frissül

## Nem változik

- `BatteryProfile` mezők és számítások
- `SafetyManager`, `Logger`, `Worker`, `InstrumentManager`
- CHARACTERIZATION, BQ_LEARNING_PHYSICAL, OCV_SOC_CHARACTERIZATION flow
- A meglévő 462 teszt nem törik — csak additive változások
