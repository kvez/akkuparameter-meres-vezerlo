# CHARGE_ONLY / DISCHARGE_ONLY módok + GUI bővítés — Implementációs terv

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CHARGE_ONLY és DISCHARGE_ONLY teszttípusok hozzáadása; töltőáram és kisütési profil (áram + végfeszültség) konfigurálhatóvá tétele; dedikált logó fejlécsáv.

**Architecture:** Additive változások — 2 új TestType/TestPlan a test_runner-ben, 1-1 override mező a DischargeConfig/ChargeConfig-ban, 4 új SessionConfig mező, 2 új GroupBox a ConfigPanel-ben, fejlécsáv a MainWindow-ban. Meglévő 462 teszt érintetlen marad.

**Tech Stack:** Python 3.11+, PySide6, pytest. Futtatás: `python -m pytest Prog/tests -q`

---

## Fájlstruktúra

| Fájl | Változás |
|------|----------|
| `Prog/src/discharge_controller.py` | `DischargeConfig.terminate_voltage_V_override` hozzáadása; `_run_cc()` override logika |
| `Prog/src/charge_controller.py` | `ChargeConfig.charge_current_A_override` hozzáadása; `_run_psu_preset()` override logika |
| `Prog/src/test_runner.py` | `TestType.CHARGE_ONLY`, `TestType.DISCHARGE_ONLY`; `TestPlan.charge_only()`, `TestPlan.discharge_only()` |
| `Prog/gui/panels/config_panel.py` | `SessionConfig` 4 új mező; `validate()` bővítés; GUI: label + 2 új GroupBox + preset gombok; `discharge_rate_combo` eltávolítva |
| `Prog/gui/main_window.py` | fejlécsáv; `_build_runner()` bővítés |
| `Prog/tests/test_discharge_controller.py` | 2 új teszt osztály |
| `Prog/tests/test_charge_controller.py` | 1 új teszt osztály |
| `Prog/tests/test_test_runner.py` | 2 új teszt |
| `Prog/tests/gui/test_session_config.py` | 3 új teszt |

---

## Task 1: DischargeConfig — terminate_voltage_V_override

**Files:**
- Modify: `Prog/src/discharge_controller.py:28-32` (DischargeConfig), `:217` (_run_cc)
- Test: `Prog/tests/test_discharge_controller.py`

- [ ] **1.1 Írj failing tesztet**

`Prog/tests/test_discharge_controller.py` végére add hozzá:

```python
class TestDischargeTerminateOverride:
    def test_uses_override_voltage_instead_of_profile(self):
        """terminate_voltage_V_override=11.5V esetén 11.5V-nál áll meg, nem 10.8V-nál."""
        profile = make_profile()  # terminate = 6 × 1.80 = 10.80V
        psu = MockPSU(voltage_V=0.0, current_A=0.0)
        load = MockLoad(voltage_V=12.0)
        dmm_v = MockDMM(voltage_V=12.0)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = DischargeConfig(terminate_voltage_V_override=11.5)
        ctrl = DischargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        for _ in range(3):
            ctrl.advance(dt_s=1.0)

        # 11.4V < override 11.5V → le kell állni
        dmm_v.voltage_V = 11.4
        load.voltage_V = 11.4
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_DONE

    def test_zero_override_uses_profile_default(self):
        """terminate_voltage_V_override=0 esetén a profil 1.80V/cella értéke érvényes."""
        profile = make_profile()  # 6 × 1.80 = 10.80V
        psu = MockPSU(voltage_V=0.0, current_A=0.0)
        load = MockLoad(voltage_V=12.0)
        dmm_v = MockDMM(voltage_V=12.0)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = DischargeConfig(terminate_voltage_V_override=0.0)
        ctrl = DischargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        for _ in range(3):
            ctrl.advance(dt_s=1.0)

        # 11.4V > profile 10.80V → ne álljon meg
        dmm_v.voltage_V = 11.4
        load.voltage_V = 11.4
        ctrl.advance(dt_s=1.0)
        assert ctrl.state != DischargeState.DISCHARGE_DONE

        # 10.75V < profile 10.80V → álljon meg
        dmm_v.voltage_V = 10.75
        load.voltage_V = 10.75
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == DischargeState.DISCHARGE_DONE
```

- [ ] **1.2 Futtasd, ellenőrizd hogy FAIL**

```
python -m pytest Prog/tests/test_discharge_controller.py::TestDischargeTerminateOverride -v
```
Várt: `FAILED` — `AttributeError: 'DischargeConfig' object has no attribute 'terminate_voltage_V_override'`

- [ ] **1.3 Implementáld a DischargeConfig mezőt**

`Prog/src/discharge_controller.py`, `DischargeConfig` dataclass (sor ~28):

```python
@dataclass
class DischargeConfig:
    discharge_current_A: float = 0.0
    terminate_voltage_V_override: float = 0.0   # 0 = profil default (1.80V/cella)
    max_discharge_time_s: float = 86400.0
    max_discharge_Ah_factor: float = 1.10
```

- [ ] **1.4 Frissítsd a _run_cc() terminate logikát**

`Prog/src/discharge_controller.py`, `_run_cc()` metódusban (sor ~217):

```python
        terminate_V = (
            self._config.terminate_voltage_V_override
            if self._config.terminate_voltage_V_override > 0
            else self._profile.terminate_voltage_pack_V
        )
```

- [ ] **1.5 Futtasd, ellenőrizd hogy PASS**

```
python -m pytest Prog/tests/test_discharge_controller.py -v
```
Várt: mind PASS (a meglévő tesztek is)

- [ ] **1.6 Commit**

```bash
git add Prog/src/discharge_controller.py Prog/tests/test_discharge_controller.py
git commit -m "feat: DischargeConfig.terminate_voltage_V_override — kisütési végfeszültség override"
```

---

## Task 2: ChargeConfig — charge_current_A_override

**Files:**
- Modify: `Prog/src/charge_controller.py` (ChargeConfig, `_run_psu_preset`)
- Test: `Prog/tests/test_charge_controller.py`

- [ ] **2.1 Írj failing tesztet**

`Prog/tests/test_charge_controller.py` végére add hozzá (keress egy `make_charge_controller` segédfüggvényt — ha nincs, nézd meg a meglévő teszteket és kövesd a mintát):

```python
class TestChargeCurrentOverride:
    def test_override_current_used_when_set(self):
        """charge_current_A_override=1.2A esetén a PSU 1.2A-t kap, nem profile.effective_max_charge_A-t.

        MockPSU.calls stringeket tárol ("set_output_current(1.2)"),
        de psu.current_A értéke frissül set_output_current() hívásakor — ezt ellenőrizzük.
        """
        from Prog.src.charge_controller import ChargeController, ChargeConfig
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.safety import SafetyManager, PsuMode
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG20721",
            nominal_capacity_Ah=7.0, cell_count=6, nominal_voltage_V=12.0,
        )
        # profile.effective_max_charge_A = 0.25 × 7.0 = 1.75A → PSU clamp: 1.5A
        # override = 1.2A → psu.current_A = 1.2A kell a PSU_PRESET után
        psu = MockPSU(voltage_V=13.0, current_A=1.0)
        load = MockLoad(voltage_V=13.0)
        dmm_v = MockDMM(voltage_V=13.0)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = ChargeConfig(charge_current_A_override=1.2)
        ctrl = ChargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        # 3 advance: INIT→PRECHECK→PSU_PRESET→CHARGE_CC
        for _ in range(3):
            ctrl.advance(dt_s=1.0)

        # MockPSU.set_output_current(i) beállítja self.current_A = i
        assert psu.called("set_output_current"), "set_output_current nem lett meghívva"
        assert abs(psu.current_A - 1.2) < 0.01, (
            f"PSU current = {psu.current_A:.3f}A, várt: 1.2A"
        )

    def test_override_clamped_to_psu_hw_max(self):
        """charge_current_A_override=2.0A INDEPENDENT módban clampelődik 1.5A-re."""
        from Prog.src.charge_controller import ChargeController, ChargeConfig
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.safety import SafetyManager, PsuMode
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG20721",
            nominal_capacity_Ah=7.0, cell_count=6, nominal_voltage_V=12.0,
        )
        psu = MockPSU(voltage_V=13.0, current_A=1.0)
        load = MockLoad(voltage_V=13.0)
        dmm_v = MockDMM(voltage_V=13.0)
        dmm_t = MockDMM(temperature_C=22.0)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        cfg = ChargeConfig(charge_current_A_override=2.0)  # > 1.5A INDEPENDENT limit
        ctrl = ChargeController(psu, load, dmm_v, dmm_t, profile, safety, cfg)

        for _ in range(3):
            ctrl.advance(dt_s=1.0)

        # 2.0A override → clampelve 1.5A-re (INDEPENDENT hw limit)
        assert psu.current_A <= 1.5, (
            f"PSU current = {psu.current_A:.3f}A, PSU limit 1.5A-t lépi túl"
        )
```

- [ ] **2.2 Futtasd, ellenőrizd hogy FAIL**

```
python -m pytest Prog/tests/test_charge_controller.py::TestChargeCurrentOverride -v
```
Várt: FAIL — `ChargeConfig` nem ismeri a `charge_current_A_override` mezőt.

- [ ] **2.4 Implementáld a ChargeConfig mezőt**

`Prog/src/charge_controller.py`, `ChargeConfig` dataclass után (sor ~32):

```python
@dataclass
class ChargeConfig:
    charge_current_A_override: float = 0.0   # 0 = auto (profile.effective_max_charge_A)
    deadband_V: float = 0.010
    max_step_up_V: float = 0.050
    max_step_down_V: float = 0.500
    cv_entry_margin_V: float = 0.100
    max_expected_series_drop_V: float = 0.90
    taper_hold_s: float = 600.0
    taper_current_tolerance_factor: float = 1.05
    cv_voltage_tolerance_V_per_cell: float = 0.003
    max_charge_time_s: float = 86400.0
    max_charge_Ah_factor: float = 1.20
    temperature_dmm_fault_timeout_s: float = 60.0
```

- [ ] **2.5 Frissítsd a _run_psu_preset() áramlogikát**

`Prog/src/charge_controller.py`, `_run_psu_preset()` metódusban (a `psu_hw_max_A` sor után):

```python
        psu_hw_max_A = {
            PsuMode.INDEPENDENT: 1.5,
            PsuMode.PARALLEL: 3.0,
            PsuMode.SERIES: 1.5,
        }.get(self._safety.psu_mode, 1.5)
        if self._config.charge_current_A_override > 0:
            charge_A = min(self._config.charge_current_A_override, psu_hw_max_A)
        else:
            charge_A = min(self._profile.effective_max_charge_A, psu_hw_max_A)
        self._psu.set_output_current(charge_A)
```

- [ ] **2.6 Futtasd a teljes teszt suitot**

```
python -m pytest Prog/tests -q
```
Várt: mind PASS (meglévő tesztek + 2 új)

- [ ] **2.7 Commit**

```bash
git add Prog/src/charge_controller.py Prog/tests/test_charge_controller.py
git commit -m "feat: ChargeConfig.charge_current_A_override — töltőáram override, PSU limit kényszerítve"
```

---

## Task 3: TestRunner — CHARGE_ONLY és DISCHARGE_ONLY

**Files:**
- Modify: `Prog/src/test_runner.py` (TestType enum, TestPlan class)
- Test: `Prog/tests/test_test_runner.py`

- [ ] **3.1 Írj failing tesztet**

`Prog/tests/test_test_runner.py` végére:

```python
class TestNewTestPlans:
    def test_charge_only_plan_has_charge_and_relax_steps(self):
        plan = TestPlan.charge_only()
        assert plan.test_type == TestType.CHARGE_ONLY
        assert len(plan.steps) == 2
        assert plan.steps[0].kind == StepKind.CHARGE
        assert plan.steps[1].kind == StepKind.RELAX

    def test_discharge_only_plan_has_single_discharge_step(self):
        plan = TestPlan.discharge_only()
        assert plan.test_type == TestType.DISCHARGE_ONLY
        assert len(plan.steps) == 1
        assert plan.steps[0].kind == StepKind.DISCHARGE
```

- [ ] **3.2 Futtasd, ellenőrizd hogy FAIL**

```
python -m pytest Prog/tests/test_test_runner.py::TestNewTestPlans -v
```
Várt: FAIL — `AttributeError: CHARGE_ONLY`

- [ ] **3.3 Implementáld a TestType enum értékeket**

`Prog/src/test_runner.py`, `TestType` enum-ban:

```python
class TestType(Enum):
    CHARACTERIZATION = "CHARACTERIZATION"
    BQ_LEARNING_PHYSICAL = "BQ_LEARNING_PHYSICAL"
    OCV_SOC_CHARACTERIZATION = "OCV_SOC_CHARACTERIZATION"
    CHARGE_ONLY = "CHARGE_ONLY"
    DISCHARGE_ONLY = "DISCHARGE_ONLY"
```

- [ ] **3.4 Implementáld a TestPlan metódusokat**

`Prog/src/test_runner.py`, `TestPlan` classban az `ocv_soc_characterization()` után:

```python
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

- [ ] **3.5 Futtasd**

```
python -m pytest Prog/tests/test_test_runner.py -v
```
Várt: mind PASS

- [ ] **3.6 Commit**

```bash
git add Prog/src/test_runner.py Prog/tests/test_test_runner.py
git commit -m "feat: TestType + TestPlan — CHARGE_ONLY és DISCHARGE_ONLY teszttípusok"
```

---

## Task 4: SessionConfig — új mezők + validate()

**Files:**
- Modify: `Prog/gui/panels/config_panel.py` (SessionConfig dataclass, validate())
- Test: `Prog/tests/gui/test_session_config.py`

- [ ] **4.1 Írj failing tesztet**

`Prog/tests/gui/test_session_config.py` végére:

```python
class TestSessionConfigNewFields:
    def _base_config(self) -> SessionConfig:
        """Minimálisan valid SessionConfig az új mezőkkel."""
        return SessionConfig(
            battery_profile_name="FIAMM_12V",
            battery_model="FG20721",
            nominal_capacity_ah=7.0,
            psu_resource="USB::TEST",
            load_resource="USB::TEST",
            dmm_voltage_resource="TCPIP::1.2.3.4::INSTR",
            dmm_temperature_resource="TCPIP::1.2.3.5::INSTR",
        )

    def test_new_fields_have_defaults(self):
        cfg = self._base_config()
        assert cfg.relax_after_charge_s == 600.0
        assert cfg.charge_current_A_override == 0.0
        assert cfg.discharge_current_A == 0.0
        assert cfg.discharge_terminate_voltage_V == 0.0

    def test_discharge_terminate_voltage_below_min_fails(self):
        """1.60V/cella minimum: 6 cella → 9.60V. 9.0V alatt hibát kell adni."""
        cfg = self._base_config()
        cfg.discharge_terminate_voltage_V = 9.0  # < 6 × 1.60 = 9.60V
        errors = cfg.validate()
        assert any("Végfeszültség" in e for e in errors)

    def test_discharge_terminate_voltage_zero_is_valid(self):
        """0.0V = profil default, nem hiba."""
        cfg = self._base_config()
        cfg.discharge_terminate_voltage_V = 0.0
        errors = cfg.validate()
        assert not any("Végfeszültség" in e for e in errors)

    def test_charge_current_override_above_psu_limit_fails(self):
        """INDEPENDENT módban 1.5A max. 2.0A override hibát kell adjon."""
        cfg = self._base_config()
        cfg.psu_mode = "INDEPENDENT"
        cfg.charge_current_A_override = 2.0
        errors = cfg.validate()
        assert any("Töltőáram" in e for e in errors)

    def test_charge_current_override_zero_is_valid(self):
        """0.0A = auto számított, nem hiba."""
        cfg = self._base_config()
        cfg.charge_current_A_override = 0.0
        errors = cfg.validate()
        assert not any("Töltőáram" in e for e in errors)
```

- [ ] **4.2 Futtasd, ellenőrizd hogy FAIL**

```
python -m pytest Prog/tests/gui/test_session_config.py::TestSessionConfigNewFields -v
```
Várt: FAIL — `SessionConfig` nem ismeri az új mezőket

- [ ] **4.3 Implementáld az új SessionConfig mezőket**

`Prog/gui/panels/config_panel.py`, `SessionConfig` dataclass-ban, a `temperature_compensation_mode` sor után:

```python
    # Töltési paraméterek
    relax_after_charge_s: float = 600.0
    charge_current_A_override: float = 0.0   # 0 = auto (C10 × 0.25, PSU limitre clampelve)

    # Kisütési paraméterek
    discharge_current_A: float = 0.0          # 0 = auto (C10 / discharge_rate_divisor)
    discharge_terminate_voltage_V: float = 0.0 # 0 = profil default (1.80V/cella)
```

- [ ] **4.4 Bővítsd a validate() metódust**

`Prog/gui/panels/config_panel.py`, `validate()` végén, a meglévő visszatérési sor előtt:

```python
        # Kisütési végfeszültség minimum ellenőrzés (csak ha be van állítva)
        if self.discharge_terminate_voltage_V > 0:
            cell_count = _PROFILE_DEFAULTS.get(
                self.battery_profile_name, {}
            ).get("cell_count", 6)
            min_v = cell_count * 1.60
            if self.discharge_terminate_voltage_V < min_v:
                errors.append(
                    f"Végfeszültség ({self.discharge_terminate_voltage_V:.2f}V)"
                    f" < 1.60V/cella minimum ({min_v:.2f}V)"
                )

        # Töltőáram override PSU limit ellenőrzés (csak ha be van állítva)
        if self.charge_current_A_override > 0:
            psu_max = 3.0 if self.psu_mode == "PARALLEL" else 1.5
            if self.charge_current_A_override > psu_max:
                errors.append(
                    f"Töltőáram ({self.charge_current_A_override:.2f}A)"
                    f" > PSU {self.psu_mode} limit ({psu_max:.1f}A)"
                )
```

- [ ] **4.5 Futtasd**

```
python -m pytest Prog/tests/gui/test_session_config.py -v
```
Várt: mind PASS

- [ ] **4.6 Commit**

```bash
git add Prog/gui/panels/config_panel.py Prog/tests/gui/test_session_config.py
git commit -m "feat: SessionConfig — relax_after_charge_s, charge_current_A_override, discharge paraméterek + validate"
```

---

## Task 5: main_window.py — fejlécsáv + _build_runner bővítés

**Files:**
- Modify: `Prog/gui/main_window.py`

Nincs új teszt ebben a taskban — a `_build_runner` változások a meglévő integrációs teszteket nem érintik (mock drivereket használnak), a fejlécsáv vizuális változás.

- [ ] **5.1 Cseréld le a setCentralWidget és cornerWidget logikát**

`Prog/gui/main_window.py`, `__init__` metódusban töröld ezeket a sorokat:

```python
        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)
```

Helyettük (és a logo blokk előtt, de a `_tabs` létrehozása után):

```python
        self._tabs = QTabWidget()

        # Dedikált fejlécsáv a tabok felett
        _container = QWidget()
        _vbox = QVBoxLayout(_container)
        _vbox.setContentsMargins(0, 0, 0, 0)
        _vbox.setSpacing(0)

        _header = QWidget()
        _header.setFixedHeight(72)
        _hbox = QHBoxLayout(_header)
        _hbox.setContentsMargins(8, 4, 8, 4)

        logo_path = app_paths.resources_dir() / "psnd.png"
        if logo_path.exists():
            self.setWindowIcon(QIcon(str(logo_path)))
            _logo_pix = QPixmap(str(logo_path)).scaledToHeight(
                60, Qt.TransformationMode.SmoothTransformation
            )
            _logo_lbl = QLabel()
            _logo_lbl.setPixmap(_logo_pix)
            _logo_lbl.setToolTip("PSND Elektronika")
            _hbox.addWidget(_logo_lbl)

        _title_lbl = QLabel("Akkuteszter — Labor műszerfal")
        _title_font = _title_lbl.font()
        _title_font.setBold(True)
        _title_font.setPointSize(12)
        _title_lbl.setFont(_title_font)
        _hbox.addWidget(_title_lbl)
        _hbox.addStretch()

        _vbox.addWidget(_header)
        _vbox.addWidget(self._tabs)
        self.setCentralWidget(_container)
```

Töröld a `__init__` végéről az eredeti logo blokkot (a `logo_path = ...` kezdetű részt, ami `self._tabs.setCornerWidget`-et hív).

- [ ] **5.2 Frissítsd a _make_relax_ctrl factory-t a _build_runner()-ben**

`Prog/gui/main_window.py`, `_build_runner()` metódusban keresd meg a `_make_relax_ctrl` definícióját és cseréld le:

```python
        def _make_relax_ctrl():
            # CHARGE_ONLY: rövid relax (cfg.relax_after_charge_s, default 600s)
            # CHARACTERIZATION / egyéb: hosszú relax (RelaxConfig default 7200s)
            relax_s = (
                cfg.relax_after_charge_s
                if cfg.test_type == "CHARGE_ONLY"
                else RelaxConfig().min_relax_s
            )
            rc = RelaxController(dmm_v, RelaxConfig(min_relax_s=relax_s))
            rc.on_event = lambda ev: logger.log_event(
                ev.get("event_code", "RELAX_EVENT"),
                ev.get("event_message", ""),
            )
            return rc
```

Add az importhoz: `from Prog.src.relax_controller import RelaxController, RelaxConfig` — ez már szerepel, ellenőrizd.

- [ ] **5.3 Frissítsd a _make_charge_ctrl factory-t**

```python
        def _make_charge_ctrl():
            return ChargeController(
                psu, load, dmm_v, dmm_t, profile, safety,
                ChargeConfig(
                    charge_current_A_override=cfg.charge_current_A_override,
                    taper_hold_s=cfg.taper_hold_s,
                ),
            )
```

- [ ] **5.4 Frissítsd a _make_discharge_ctrl factory-t**

```python
        def _make_discharge_ctrl():
            discharge_A = (
                cfg.discharge_current_A
                if cfg.discharge_current_A > 0
                else profile.nominal_capacity_Ah / cfg.discharge_rate_divisor
            )
            return DischargeController(
                psu, load, dmm_v, dmm_t, profile, safety,
                DischargeConfig(
                    discharge_current_A=discharge_A,
                    terminate_voltage_V_override=cfg.discharge_terminate_voltage_V,
                ),
            )
```

- [ ] **5.5 Frissítsd a TestPlan kiválasztást**

A `_build_runner()` végén keresd meg az `if cfg.test_type == "CHARACTERIZATION":` blokkot és cseréld le:

```python
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
        return runner, test_plan
```

- [ ] **5.6 Ellenőrizd compileall-lal**

```
python -m compileall Prog/gui/main_window.py
```
Várt: `Compiling...` hibák nélkül.

- [ ] **5.7 Futtasd a teljes teszt suitot**

```
python -m pytest Prog/tests -q
```
Várt: mind PASS (462+ teszt)

- [ ] **5.8 Commit**

```bash
git add Prog/gui/main_window.py
git commit -m "feat: fejlécsáv logóval + _build_runner CHARGE_ONLY/DISCHARGE_ONLY + override paraméterek"
```

---

## Task 6: ConfigPanel GUI — új GroupBox-ok + preset gombok

**Files:**
- Modify: `Prog/gui/panels/config_panel.py` (ConfigPanel._build_ui, get_session_config)

- [ ] **6.1 Add import a QHBoxLayout-hoz**

`Prog/gui/panels/config_panel.py` importokban ellenőrizd, hogy `QHBoxLayout` szerepel a PySide6 importban. Ha nem:

```python
from PySide6.QtWidgets import (
    QWidget, QFormLayout, QLineEdit, QDoubleSpinBox, QComboBox,
    QCheckBox, QGroupBox, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QScrollArea,
)
```

- [ ] **6.2 Frissítsd az "Akkumulátor" GroupBox C10 labelét**

`Prog/gui/panels/config_panel.py`, `_build_ui()` metódusban:

```python
        self._capacity_spin = QDoubleSpinBox()
        self._capacity_spin.setRange(0.1, 1000.0)
        self._capacity_spin.setDecimals(1)
        self._capacity_spin.setSuffix(" Ah")
        self._capacity_spin.setToolTip(
            "Az adatlap szerinti 10 órás kapacitás. "
            "C-ráta számítás alapja (0.25×C10 = max töltőáram)."
        )
        batt_form.addRow("C10 kapacitás:", self._capacity_spin)
```

- [ ] **6.3 Frissítsd a "Teszt" GroupBox-ot**

A meglévő `test_form` részben:
- Add hozzá `"CHARGE_ONLY"` és `"DISCHARGE_ONLY"` értékeket a `test_type_combo`-hoz
- **Töröld** a `self._discharge_rate_combo` sort és a hozzá tartozó `test_form.addRow` sort
- Add hozzá a relax mezőt a taper sor után:

```python
        self._test_type_combo.addItems([
            "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL",
            "OCV_SOC_CHARACTERIZATION", "CHARGE_ONLY", "DISCHARGE_ONLY",
        ])
        # ... (taper_spin megmarad) ...

        self._relax_charge_spin = QDoubleSpinBox()
        self._relax_charge_spin.setRange(60.0, 7200.0)
        self._relax_charge_spin.setDecimals(0)
        self._relax_charge_spin.setValue(600.0)
        self._relax_charge_spin.setSuffix(" s")
        self._relax_charge_spin.setToolTip("Pihenési idő töltés után (CHARGE_ONLY módban)")
        test_form.addRow("Relax töltés után:", self._relax_charge_spin)
```

- [ ] **6.4 Add hozzá a "Töltési paraméterek" GroupBox-ot**

A `test_box` `root.addWidget` sora után, a hőkompenzáció box előtt:

```python
        # --- Töltési paraméterek ---
        charge_box = QGroupBox("Töltési paraméterek")
        charge_form = QFormLayout(charge_box)

        charge_row = QWidget()
        charge_hbox = QHBoxLayout(charge_row)
        charge_hbox.setContentsMargins(0, 0, 0, 0)

        self._charge_current_spin = QDoubleSpinBox()
        self._charge_current_spin.setRange(0.10, 1.5)   # default: INDEPENDENT limit
        self._charge_current_spin.setDecimals(2)
        self._charge_current_spin.setSuffix(" A")
        self._charge_current_spin.setToolTip(
            "0 = automatikus (C10 × 0.25, PSU limitre clampelve)\n"
            "Kézzel beírt értéket vesz át — soha nem lépi túl a PSU mód limitét."
        )
        charge_hbox.addWidget(self._charge_current_spin)

        self._charge_current_calc_label = QLabel("számított: — A")
        charge_hbox.addWidget(self._charge_current_calc_label)

        charge_form.addRow("Töltőáram:", charge_row)
        root.addWidget(charge_box)

        # Frissítés trigger: kapacitás vagy PSU mód változásakor
        self._capacity_spin.valueChanged.connect(self._update_charge_current_display)
        self._psu_mode_combo.currentTextChanged.connect(self._update_charge_current_display)
```

- [ ] **6.5 Add hozzá a "Kisütési paraméterek" GroupBox-ot**

A `charge_box` `root.addWidget` sora után:

```python
        # --- Kisütési paraméterek ---
        discharge_box = QGroupBox("Kisütési paraméterek")
        discharge_layout = QVBoxLayout(discharge_box)

        preset_row = QWidget()
        preset_hbox = QHBoxLayout(preset_row)
        preset_hbox.setContentsMargins(0, 0, 0, 0)
        preset_hbox.addWidget(QLabel("Preset:"))
        btn_c5  = QPushButton("C/5")
        btn_c10 = QPushButton("C/10")
        btn_c20 = QPushButton("C/20")
        btn_c5.clicked.connect(lambda: self._apply_discharge_preset(5,  1.80))
        btn_c10.clicked.connect(lambda: self._apply_discharge_preset(10, 1.80))
        btn_c20.clicked.connect(lambda: self._apply_discharge_preset(20, 1.75))
        preset_hbox.addWidget(btn_c5)
        preset_hbox.addWidget(btn_c10)
        preset_hbox.addWidget(btn_c20)
        preset_hbox.addStretch()
        discharge_layout.addWidget(preset_row)

        discharge_form = QFormLayout()

        self._discharge_current_spin = QDoubleSpinBox()
        self._discharge_current_spin.setRange(0.0, 60.0)
        self._discharge_current_spin.setDecimals(2)
        self._discharge_current_spin.setSuffix(" A")
        self._discharge_current_spin.setToolTip("0 = automatikus (C10 / ráta). Preset gombok töltik ki.")
        discharge_form.addRow("Áram:", self._discharge_current_spin)

        self._discharge_terminate_spin = QDoubleSpinBox()
        self._discharge_terminate_spin.setRange(0.0, 60.0)
        self._discharge_terminate_spin.setDecimals(2)
        self._discharge_terminate_spin.setSuffix(" V")
        self._discharge_terminate_spin.setToolTip("0 = profil default (1.80V/cella). Preset gombok töltik ki.")
        discharge_form.addRow("Végfeszültség:", self._discharge_terminate_spin)

        discharge_layout.addLayout(discharge_form)
        root.addWidget(discharge_box)
```

- [ ] **6.6 Implementáld a segédmetódusokat**

`Prog/gui/panels/config_panel.py`-ban, a `_on_psu_mode_changed` után:

```python
    def _psu_hw_max_A(self) -> float:
        return 3.0 if self._psu_mode_combo.currentText() == "PARALLEL" else 1.5

    def _update_charge_current_display(self) -> None:
        c10 = self._capacity_spin.value()
        psu_max = self._psu_hw_max_A()
        calc_A = min(c10 * 0.25, psu_max)
        self._charge_current_calc_label.setText(f"számított: {calc_A:.2f} A")
        self._charge_current_spin.setMaximum(psu_max)
        if self._charge_current_spin.value() > psu_max:
            self._charge_current_spin.setValue(psu_max)
        # Ha még az alapértéken volt (0.10), állítsd a számított értékre
        if self._charge_current_spin.value() <= 0.10:
            self._charge_current_spin.setValue(calc_A)

    def _apply_discharge_preset(self, divisor: int, terminate_V_per_cell: float) -> None:
        c10 = self._capacity_spin.value()
        cell_count = int(self._cell_count_label.text() or "6")
        self._discharge_current_spin.setValue(round(c10 / divisor, 2))
        self._discharge_terminate_spin.setValue(
            round(terminate_V_per_cell * cell_count, 2)
        )
```

- [ ] **6.7 Hívd meg az _update_charge_current_display-t _load_yaml() után**

`Prog/gui/panels/config_panel.py`, `__init__` metódusban, `self._load_yaml()` sor után:

```python
        self._update_charge_current_display()
```

- [ ] **6.8 Frissítsd a get_session_config() metódust**

A meglévő `return SessionConfig(...)` blokkban töröld a `discharge_rate_divisor=discharge_divisor` sort (a `discharge_rate_map` logikával együtt), és add hozzá az új mezőket:

```python
    def get_session_config(self) -> SessionConfig:
        return SessionConfig(
            battery_profile_name=self._profile_combo.currentText(),
            battery_model=self._model_edit.text().strip(),
            nominal_capacity_ah=self._capacity_spin.value(),
            sample_id=self._sample_id_edit.text().strip(),
            psu_resource=self._psu_res_edit.text().strip(),
            load_resource=self._load_res_edit.text().strip(),
            dmm_voltage_resource=self._dmm_v_res_edit.text().strip(),
            dmm_temperature_resource=self._dmm_t_res_edit.text().strip(),
            psu_mode=self._psu_mode_combo.currentText(),
            hardware_wiring_confirmed=self._wiring_confirmed_cb.isChecked(),
            test_type=self._test_type_combo.currentText(),
            runner_tick_s=self._tick_spin.value(),
            taper_hold_s=self._taper_spin.value(),
            relax_after_charge_s=self._relax_charge_spin.value(),
            discharge_rate_divisor=5,   # fallback, nem szerkeszthető közvetlenül
            ocv_soc_step_percent=self._ocv_soc_step_spin.value(),
            temperature_compensation_mode=self._temp_comp_combo.currentText(),
            charge_current_A_override=self._charge_current_spin.value(),
            discharge_current_A=self._discharge_current_spin.value(),
            discharge_terminate_voltage_V=self._discharge_terminate_spin.value(),
        )
```

- [ ] **6.9 Compileall + teljes teszt suite**

```
python -m compileall Prog
python -m pytest Prog/tests -q
```
Várt: 0 syntax hiba, mind PASS

- [ ] **6.10 Commit**

```bash
git add Prog/gui/panels/config_panel.py
git commit -m "feat: ConfigPanel — C10 label, töltési/kisütési paraméter GroupBox-ok, preset gombok"
```

---

## Task 7: Végső ellenőrzés

- [ ] **7.1 Teljes teszt suite zöld**

```
python -m pytest Prog/tests -v --tb=short 2>&1 | tail -20
```
Várt: `X passed, 0 failed`

- [ ] **7.2 Ruff + mypy**

```
python -m ruff check Prog
python -m mypy Prog --ignore-missing-imports 2>&1 | tail -10
```
Várt: 0 hiba mindkettőnél

- [ ] **7.3 compileall**

```
python -m compileall Prog -q
```

- [ ] **7.4 Memória frissítés**

Frissítsd a `~/.claude/projects/.../memory/project_summary.md` Státusz szekcióját:
- Új modulok: CHARGE_ONLY/DISCHARGE_ONLY TestPlan, terminate_voltage_V_override, charge_current_A_override
- GUI: fejlécsáv, preset gombok, töltési/kisütési GroupBox-ok
- Következő: exe újrabuild, éles tesztelés
