# Rev2 Safety Hotfixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 8 safety and accuracy issues (K1–K4, V1–V3, E5) identified in rev2.txt before first active HW test with battery.

**Architecture:** Nincs új fájl — meglévő modulok célzott módosítása. Task A (1–4): ChargeController/DischargeController safety; Task B (5): integrációs időalap; Task C (6–8): infrastruktúra. Minden change TDD-sorrendben.

**Tech Stack:** Python 3.11, pytest, unittest.mock

---

## Érintett fájlok

| Fájl | Feladat |
|------|---------|
| `Prog/src/charge_controller.py` | 1, 2, 3, 4 |
| `Prog/src/discharge_controller.py` | 4 |
| `Prog/src/test_runner.py` | 5, 8 |
| `Prog/src/logger.py` | 5, 6 |
| `Prog/src/instrument_manager.py` | 7 |
| `Prog/tests/mock_drivers/mock_psu.py` | 3 |
| `Prog/tests/test_charge_controller.py` | 1, 2, 3, 4 |
| `Prog/tests/test_discharge_controller.py` | 4 |
| `Prog/tests/test_test_runner.py` | 5, 8 |
| `Prog/tests/test_logger.py` | 5, 6 |
| `Prog/tests/test_instrument_manager.py` | 7 |

---

## Task 1 — K2: `_check_charge_limits()` helper (max Ah / max time minden töltési fázisban)

**Fájlok:**
- Modify: `Prog/src/charge_controller.py`
- Modify: `Prog/tests/test_charge_controller.py`

**Háttér:**
`MAX_CHARGE_AH_REACHED` és `MAX_CHARGE_TIME_REACHED` csak `_run_charge_cc()`-ben van. CV és TAPER fázisban nincs ellenőrzés. Új helper: `_check_charge_limits() -> bool`.

- [x] **Step 1: Írj failing teszteket**

Hozzáadd a `Prog/tests/test_charge_controller.py` végéhez:

```python
class TestChargeLimitsAllPhases:
    """K2: max_charge_Ah és max_charge_time minden töltési fázisban ellenőrzött."""

    def _advance_to_cv(self, dmm_voltage_V=14.4):
        """Helper: controller CHARGE_CV_DMM_CONTROL állapotba hozva."""
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=dmm_voltage_V)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV (u_batt=14.4 >= 14.3)
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL, ctrl.state
        return ctrl

    def test_max_charge_ah_triggers_fault_in_cv_phase(self):
        ctrl = self._advance_to_cv()
        ctrl._integrator.accumulated_charge_Ah = (
            ctrl._profile.nominal_capacity_Ah * 1.21
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "MAX_CHARGE_AH" in ctrl.fault_reason

    def test_max_charge_time_triggers_fault_in_cv_phase(self):
        ctrl = self._advance_to_cv()
        ctrl._elapsed_s = ctrl._config.max_charge_time_s + 1.0
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "MAX_CHARGE_TIME" in ctrl.fault_reason

    def test_max_charge_ah_triggers_fault_in_taper_phase(self):
        ctrl = self._advance_to_cv()
        ctrl._state = ChargeState.TAPER_HOLD
        ctrl._integrator.accumulated_charge_Ah = (
            ctrl._profile.nominal_capacity_Ah * 1.21
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "MAX_CHARGE_AH" in ctrl.fault_reason

    def test_max_charge_time_triggers_fault_in_taper_phase(self):
        ctrl = self._advance_to_cv()
        ctrl._state = ChargeState.TAPER_HOLD
        ctrl._elapsed_s = ctrl._config.max_charge_time_s + 1.0
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "MAX_CHARGE_TIME" in ctrl.fault_reason

    def test_cc_limits_still_work(self):
        """K2 nem töri el a meglévő CC limitet."""
        ctrl, *_ = make_controller(dmm_voltage_V=12.5)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC
        ctrl._integrator.accumulated_charge_Ah = (
            ctrl._profile.nominal_capacity_Ah * 1.21
        )
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
```

- [x] **Step 2: Futtasd a teszteket — ellenőrizd a FAIL-t**

```
python -m pytest Prog/tests/test_charge_controller.py::TestChargeLimitsAllPhases -v
```

Elvárt: 4× FAIL (a CC teszt PASS lehet a meglévő kóddal).

- [x] **Step 3: Implementáld `_check_charge_limits()`-t a `charge_controller.py`-ban**

A `_run_charge_cc()` metódus ELŐTT add hozzá:

```python
def _check_charge_limits(self) -> bool:
    """K2: Ah és idő limit ellenőrzés. True = limit elérve, emergency_stop meghívva."""
    if self._elapsed_s > self._config.max_charge_time_s:
        self.emergency_stop("MAX_CHARGE_TIME_REACHED")
        return True
    if self._integrator.accumulated_charge_Ah > (
        self._profile.nominal_capacity_Ah * self._config.max_charge_Ah_factor
    ):
        self.emergency_stop("MAX_CHARGE_AH_REACHED")
        return True
    return False
```

- [x] **Step 4: Módosítsd `_run_charge_cc()`-t — használja a helpért**

Cseréld a jelenlegi két limit-blokk:
```python
        if self._elapsed_s > self._config.max_charge_time_s:
            self.emergency_stop("MAX_CHARGE_TIME_REACHED")
            return
        if self._integrator.accumulated_charge_Ah > (
            self._profile.nominal_capacity_Ah * self._config.max_charge_Ah_factor
        ):
            self.emergency_stop("MAX_CHARGE_AH_REACHED")
            return
```
erre:
```python
        if self._check_charge_limits():
            return
```

- [x] **Step 5: Add hozzá `_run_charge_cv()`-hez**

```python
    def _run_charge_cv(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        self._regulate_cv()
        self._integrate(dt_s, signed_current_A=self._i_charge, source="PSU_READBACK")

        if self._check_charge_limits():
            return

        if self._check_taper_condition():
            self._state = ChargeState.TAPER_HOLD
```

- [x] **Step 6: Add hozzá `_run_taper_hold()`-hoz**

```python
    def _run_taper_hold(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        self._integrate(dt_s, signed_current_A=self._i_charge, source="PSU_READBACK")

        if self._check_charge_limits():
            return

        if not self._check_taper_condition():
            self._taper_timer_s = 0.0
            self._state = ChargeState.CHARGE_CV_DMM_CONTROL
            return

        self._taper_timer_s += dt_s
        if self._taper_timer_s >= self._config.taper_hold_s:
            self._psu.output_off()
            self._state = ChargeState.CHARGE_DONE
```

- [x] **Step 7: Futtasd az összes tesztet**

```
python -m pytest Prog/tests/test_charge_controller.py -v
```

Elvárt: minden teszt PASS.

- [x] **Step 8: Commit**

```
git add Prog/src/charge_controller.py Prog/tests/test_charge_controller.py
git commit -m "fix: K2 — _check_charge_limits() minden töltési fázisban (CC/CV/TAPER)"
```

---

## Task 2 — K3: `_check_series_safety()` — series_drop és diode_power bekötése

**Fájlok:**
- Modify: `Prog/src/charge_controller.py`
- Modify: `Prog/tests/test_charge_controller.py`

**Háttér:**
`SafetyManager.check_series_drop()` és `check_diode_power()` definiált de soha nem hívódik. `_check_series_safety()` helper: PSU feszültség readback + u_drop számítás + safety check. Series drop → fault; diode power → warning flag.

- [x] **Step 1: Írj failing teszteket**

Hozzáadd a `Prog/tests/test_charge_controller.py` végéhez:

```python
class TestSeriesSafety:
    """K3: series_drop és diode_power safety check bekötve töltés közben."""

    def _advance_to_cc(self, psu_voltage_V=13.0, dmm_voltage_V=12.5):
        ctrl, psu, load, dmm = make_controller(
            dmm_voltage_V=dmm_voltage_V,
            psu_current_A=1.5,
        )
        psu.voltage_V = psu_voltage_V
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        assert ctrl.state == ChargeState.CHARGE_CC
        return ctrl, psu, dmm

    def test_series_drop_above_fault_triggers_fault(self):
        """u_psu - u_batt > fault_series_drop_V (1.25V) → SERIES_DROP_TOO_HIGH fault."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.voltage_V = 12.5 + 1.30  # u_drop = 1.30V > 1.25V
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.FAULT
        assert "SERIES_DROP_TOO_HIGH" in ctrl.fault_reason

    def test_series_drop_below_fault_no_fault(self):
        """u_drop = 0.85V < 1.25V → nincs fault."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.voltage_V = 12.5 + 0.85
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC

    def test_negative_drop_no_fault(self):
        """u_psu < u_batt (pl. PSU kikapcsolva) → nincs false fault."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5)
        psu.voltage_V = 12.0  # alatt
        ctrl.advance(dt_s=1.0)
        assert ctrl.state != ChargeState.FAULT

    def test_diode_power_warning_stored(self):
        """Diode power magas: warning_code beállítva, nincs fault."""
        ctrl, psu, dmm = self._advance_to_cc(dmm_voltage_V=12.5, psu_voltage_V=12.5)
        # 1.5A @ 1.10V = 1.65W > 1.0W diode_power_warning_W (alapértelmezett)
        # SafetyManager default: diode_power_warning_W=2.0 → ehhez 2.0/1.5 = 1.33V kell
        psu.voltage_V = 12.5 + 1.10  # 1.5A × 1.10V = 1.65W < 2.0W → no warning yet
        # Állítsuk be a safety manager warningot alacsonyabb küszöbre:
        ctrl._safety.diode_power_warning_W = 1.5
        psu.voltage_V = 12.5 + 1.10  # 1.5A × 1.10V = 1.65W > 1.5W → warning
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC  # nincs fault
        assert ctrl.last_warning_code != ""
```

- [x] **Step 2: Futtasd a teszteket — ellenőrizd a FAIL-t**

```
python -m pytest Prog/tests/test_charge_controller.py::TestSeriesSafety -v
```

Elvárt: mind FAIL (nincs `_check_series_safety()` és `last_warning_code` még).

- [x] **Step 3: Adj hozzá `_last_warning_code`-t és `last_warning_code` propertyt**

A `ChargeController.__init__`-ben, a `self._dmm_valid` után:

```python
        self._dmm_valid: bool = True
        self._last_warning_code: str = ""
```

A `last_integration_source` property után:

```python
    @property
    def last_warning_code(self) -> str:
        return self._last_warning_code
```

- [x] **Step 4: Adj hozzá `_read_psu_voltage()` és `_check_series_safety()` metódusokat**

A `_read_psu_current()` metódus mellé (a `# Segédek` szekció végéhez):

```python
    def _read_psu_voltage(self) -> Optional[float]:
        try:
            return self._psu.measure_output_voltage()
        except Exception:
            return None

    def _check_series_safety(self) -> bool:
        """K3: series_drop és diode_power ellenőrzés. True = fault triggerelt."""
        u_psu = self._read_psu_voltage()
        if u_psu is None or self._u_batt <= 0:
            return False
        u_drop_V = u_psu - self._u_batt
        if u_drop_V < 0:
            return False  # PSU feszültség akkuszint alatt — skip

        drop_result = self._safety.check_series_drop(u_drop_V)
        if drop_result.fault is not None:
            self.emergency_stop(drop_result.fault.name)
            return True

        power_result = self._safety.check_diode_power(self._i_charge, u_drop_V)
        if power_result.warning is not None:
            self._last_warning_code = power_result.warning.name

        return False
```

- [x] **Step 5: Töröld a `_last_warning_code`-t az `advance()` elején, hívd a safety check-et CC és CV-ben**

Az `advance()` elején, az `if self._state == ChargeState.FAULT` sor elé:

```python
        self._last_warning_code = ""
```

A `_run_charge_cc()`-ben, a `_check_charge_limits()` blokk után:

```python
        if self._check_charge_limits():
            return

        if self._check_series_safety():
            return
```

A `_run_charge_cv()`-ben, a `_check_charge_limits()` blokk után:

```python
        if self._check_charge_limits():
            return

        if self._check_series_safety():
            return
```

- [x] **Step 6: Futtasd az összes tesztet**

```
python -m pytest Prog/tests/test_charge_controller.py -v
```

Elvárt: minden teszt PASS.

- [x] **Step 7: Commit**

```
git add Prog/src/charge_controller.py Prog/tests/test_charge_controller.py
git commit -m "fix: K3 — series_drop fault és diode_power warning bekötve charge controller futó körbe"
```

---

## Task 3 — V1: PSU current readback hiba → `PSU_COMM_LOST` fault (ne 0.0A)

**Fájlok:**
- Modify: `Prog/src/charge_controller.py`
- Modify: `Prog/tests/mock_drivers/mock_psu.py`
- Modify: `Prog/tests/test_charge_controller.py`

**Háttér:**
`_read_psu_current()` exception esetén `return 0.0` → hamis taper feltétel → CHARGE_DONE. Fix: `emergency_stop("PSU_COMM_LOST")` hívás, majd FAULT guard a hívó fázisokban.

- [x] **Step 1: Add `simulate_current_readback_timeout` flaget a MockPSU-hoz**

`Prog/tests/mock_drivers/mock_psu.py`-ban, a `simulate_timeout: bool = False` sor után:

```python
    simulate_current_readback_timeout: bool = False
```

A `measure_output_current()` metódusban:

```python
    def measure_output_current(self) -> float:
        self.calls.append("measure_output_current()")
        if self.simulate_timeout or self.simulate_current_readback_timeout:
            raise InstrumentTimeoutError("MockPSU: simulated read timeout")
        return self.current_A
```

- [x] **Step 2: Írj failing tesztet**

Hozzáadd a `Prog/tests/test_charge_controller.py` végéhez:

```python
class TestPsuCommLost:
    """V1: PSU current readback hiba → PSU_COMM_LOST fault, nem CHARGE_DONE."""

    def test_psu_current_failure_in_cv_phase_triggers_fault_not_charge_done(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=14.4, psu_current_A=1.5)
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → PSU_PRESET
        ctrl.advance(dt_s=1.0)  # PSU_PRESET → CHARGE_CC
        ctrl.advance(dt_s=1.0)  # CHARGE_CC → CHARGE_CV
        assert ctrl.state == ChargeState.CHARGE_CV_DMM_CONTROL

        psu.simulate_current_readback_timeout = True
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "PSU_COMM_LOST"

    def test_psu_current_failure_in_cc_phase_triggers_fault(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5, psu_current_A=1.5)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert ctrl.state == ChargeState.CHARGE_CC

        psu.simulate_current_readback_timeout = True
        ctrl.advance(dt_s=1.0)

        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "PSU_COMM_LOST"
```

- [x] **Step 3: Futtasd a teszteket — ellenőrizd a FAIL-t**

```
python -m pytest Prog/tests/test_charge_controller.py::TestPsuCommLost -v
```

Elvárt: FAIL (jelenlegi kód CHARGE_DONE vagy CHARGE_CC marad, nem FAULT).

- [x] **Step 4: Módosítsd `_read_psu_current()`-t**

```python
    def _read_psu_current(self) -> float:
        try:
            return self._psu.measure_output_current()
        except Exception:
            self.emergency_stop("PSU_COMM_LOST")
            return 0.0  # hívó a FAULT állapotot ellenőrzi
```

- [x] **Step 5: Add FAULT guard-ot a hívó fázisokhoz**

`_run_charge_cc()` elején, az integrate hívás előtt:

```python
    def _run_charge_cc(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        if self._state == ChargeState.FAULT:
            return
        self._integrate(...)
        ...
```

`_run_charge_cv()` elején:

```python
    def _run_charge_cv(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        if self._state == ChargeState.FAULT:
            return
        self._regulate_cv()
        ...
```

`_run_taper_hold()` elején:

```python
    def _run_taper_hold(self, dt_s: float) -> None:
        self._i_charge = self._read_psu_current()
        if self._state == ChargeState.FAULT:
            return
        self._integrate(...)
        ...
```

- [x] **Step 6: Futtasd az összes tesztet**

```
python -m pytest Prog/tests/test_charge_controller.py -v
```

Elvárt: minden teszt PASS.

- [x] **Step 7: Commit**

```
git add Prog/src/charge_controller.py Prog/tests/mock_drivers/mock_psu.py Prog/tests/test_charge_controller.py
git commit -m "fix: V1 — PSU current readback hiba PSU_COMM_LOST faultot triggerel, nem 0.0A-t"
```

---

## Task 4 — V4: PRECHECK DMM guard (ChargeController + DischargeController)

**Fájlok:**
- Modify: `Prog/src/charge_controller.py`
- Modify: `Prog/src/discharge_controller.py`
- Modify: `Prog/tests/test_charge_controller.py`
- Modify: `Prog/tests/test_discharge_controller.py`

**Háttér:**
Ha DMM az első ciklusban nem olvasható, `_u_batt=0.0` → PRECHECK `DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED` faultot ad. Fix: explicit DMM validity check PRECHECK elején.

- [x] **Step 1: Írj failing teszteket**

Hozzáadd a `Prog/tests/test_charge_controller.py` végéhez:

```python
class TestPrecheckDmmGuard:
    """V4: PRECHECK DMM hiba → DMM_FEEDBACK_LOST, nem DEEPLY_DISCHARGED."""

    def test_dmm_failure_at_precheck_gives_dmm_fault(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        dmm.dmm_valid = False  # DMM read failure szimulálása
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → kell fault legyen
        assert ctrl.state == ChargeState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"

    def test_dmm_failure_not_misleading_deeply_discharged(self):
        ctrl, psu, load, dmm = make_controller(dmm_voltage_V=12.5)
        dmm.dmm_valid = False
        ctrl.advance(dt_s=1.0)
        ctrl.advance(dt_s=1.0)
        assert "DEEPLY_DISCHARGED" not in ctrl.fault_reason
```

Hozzáadd a `Prog/tests/test_discharge_controller.py` végéhez:

```python
class TestDischargePrecheckDmmGuard:
    """V4: DischargeController PRECHECK DMM hiba → DMM_FEEDBACK_LOST."""

    def test_dmm_failure_at_precheck_gives_dmm_fault(self):
        ctrl, psu, load, dmm = make_discharge_controller(dmm_voltage_V=12.5)
        dmm.dmm_valid = False
        ctrl.advance(dt_s=1.0)  # INIT → PRECHECK
        ctrl.advance(dt_s=1.0)  # PRECHECK → fault
        assert ctrl.state == DischargeState.FAULT
        assert ctrl.fault_reason == "DMM_FEEDBACK_LOST"
```

- [x] **Step 2: Futtasd a teszteket — ellenőrizd a FAIL-t**

```
python -m pytest Prog/tests/test_charge_controller.py::TestPrecheckDmmGuard Prog/tests/test_discharge_controller.py::TestDischargePrecheckDmmGuard -v
```

- [x] **Step 3: Módosítsd `charge_controller.py:_run_precheck()`-t**

```python
    def _run_precheck(self) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return

        mode_result = self._safety.check_psu_mode_compatibility()
        if mode_result.fault is not None:
            self.emergency_stop(mode_result.fault.name)
            return

        voltage_result = self._safety.check_precheck_voltage(self._u_batt)
        if voltage_result.fault is not None:
            self.emergency_stop(voltage_result.fault.name)
            return

        self._state = ChargeState.PSU_PRESET
```

- [x] **Step 4: Módosítsd `discharge_controller.py:_run_precheck()`-t**

```python
    def _run_precheck(self) -> None:
        if not self._dmm_valid:
            self.emergency_stop("DMM_FEEDBACK_LOST")
            return

        voltage_result = self._safety.check_precheck_voltage(self._u_batt)
        if voltage_result.fault is not None:
            self.emergency_stop(voltage_result.fault.name)
            return
        self._psu.all_outputs_off()
        self._state = DischargeState.DISCHARGE_CC_SETUP
```

- [x] **Step 5: Futtasd az összes tesztet**

```
python -m pytest Prog/tests/test_charge_controller.py Prog/tests/test_discharge_controller.py -v
```

Elvárt: minden teszt PASS.

- [x] **Step 6: Commit**

```
git add Prog/src/charge_controller.py Prog/src/discharge_controller.py Prog/tests/test_charge_controller.py Prog/tests/test_discharge_controller.py
git commit -m "fix: V4 — PRECHECK DMM hiba explicit DMM_FEEDBACK_LOST fault (nem DEEPLY_DISCHARGED)"
```

---

## Task 5 — K1+V3: Valós dt_s integrációban + adaptív sleep + logger wall-clock commit

**Fájlok:**
- Modify: `Prog/src/test_runner.py`
- Modify: `Prog/src/logger.py`
- Modify: `Prog/tests/test_test_runner.py`
- Modify: `Prog/tests/test_logger.py`

**Háttér:**
K1: `advance(runner_tick_s)` névleges értéket kap, valódi ciklus idő I/O+sleep. Fix: `perf_counter()` alapú `last_tick_time` + adaptív sleep. V3: logger `_elapsed_since_commit` kumulatív elapsed_s-t adja össze. Fix: `time.monotonic()` alapú wall-clock commit timer.

- [x] **Step 1: Írj failing tesztet a valós dt_s-hez**

Hozzáadd a `Prog/tests/test_test_runner.py` végéhez:

```python
from unittest.mock import patch, MagicMock
import time as _time_module


class TestRealDtIntegration:
    """K1: _run_step() valós dt_s-t ad az advance()-nek, nem nominálisát."""

    def _make_minimal_runner(self, tick_s=2.0):
        """Minimális TestRunner stub kontrollerrel."""
        from Prog.src.test_runner import TestRunner, TestRunnerConfig
        from Prog.tests.test_test_runner import make_runner  # meglévő helper
        # Ha nincs make_runner, inline:
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.safety import SafetyManager, PsuMode
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import Logger, LogConfig
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        import tempfile
        from pathlib import Path

        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG20721",
            nominal_capacity_Ah=7.0, cell_count=6,
        )
        psu = MockPSU(); load = MockLoad(); dmm_v = MockDMM(); dmm_t = MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        tmpdir = Path(tempfile.mkdtemp())
        logger = Logger(tmpdir, LogConfig())
        cfg = TestRunnerConfig(runner_tick_s=tick_s, sleep_enabled=False)
        return runner, logger, tmpdir

    def test_advance_receives_actual_elapsed_not_nominal(self):
        """Perf counter mock: tényleges dt eltérő nominálistól → advance() ténylegesét kapja."""
        from Prog.src.test_runner import TestRunner, TestRunnerConfig, TestPlan
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.safety import SafetyManager, PsuMode
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.src.logger import Logger, LogConfig
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.src.relax_controller import RelaxController, RelaxConfig, RelaxState
        import tempfile
        from pathlib import Path

        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG20721",
            nominal_capacity_Ah=7.0, cell_count=6,
        )
        psu = MockPSU(); load = MockLoad()
        dmm_v = MockDMM(voltage_V=12.5); dmm_t = MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        tmpdir = Path(tempfile.mkdtemp())
        logger = Logger(tmpdir, LogConfig())
        cfg = TestRunnerConfig(runner_tick_s=2.0, sleep_enabled=False)

        # DtRecorder: rögzíti a kapott dt_s értékeket, 2 ciklus után kész
        recorded_dt = []

        class DtRecordingRelax:
            state = RelaxState.RELAXING
            _count = 0
            def advance(self, dt_s):
                recorded_dt.append(dt_s)
                self._count += 1
                if self._count >= 2:
                    self.state = RelaxState.RELAX_DONE
                return self.state
            def reset(self): pass

        relax = DtRecordingRelax()
        runner = TestRunner(im, safety, logger, profile, cfg,
                            charge_controller=None,
                            discharge_controller=None,
                            relax_controller=relax)

        # perf_counter: első hívás = 0.0, majd +2.4s increment (szimulálja az I/O overhead-et)
        counter_values = [0.0, 2.4, 4.8, 7.2, 9.6]
        counter_idx = [0]
        def mock_perf():
            val = counter_values[counter_idx[0]]
            counter_idx[0] += 1
            return val

        from Prog.src.test_runner import TestStep, StepKind, TestPlan
        step = TestStep(StepKind.RELAX, "relax_test")
        plan = TestPlan(test_type=None, steps=(step,))

        with patch('Prog.src.test_runner.time') as mock_time:
            mock_time.perf_counter.side_effect = mock_perf
            mock_time.sleep = MagicMock()
            runner.run(plan)

        # Első dt: 2.4 - 0.0 = 2.4s (nem 2.0s nominal)
        assert len(recorded_dt) >= 1
        assert abs(recorded_dt[0] - 2.4) < 0.01, f"Elvárt ~2.4, kapott {recorded_dt[0]}"
        logger.close()
```

**Megjegyzés:** Ez a teszt a `Prog.src.test_runner.time` modul patchelésével működik. Ha a `test_runner.py`-ban `import time` van, akkor `patch('Prog.src.test_runner.time')`.

- [x] **Step 2: Írj failing tesztet a logger wall-clock commithoz**

Hozzáadd a `Prog/tests/test_logger.py` végéhez:

```python
class TestLoggerCommitTiming:
    """V3: Logger SQLite commit wall-clock alapú, nem kumulatív elapsed_s."""

    def test_commit_not_triggered_on_every_sample_after_first(self):
        """Nagy elapsed_s értékek ne okozzanak per-sample commitot."""
        import tempfile
        from pathlib import Path
        from Prog.src.logger import Logger, LogConfig

        tmpdir = Path(tempfile.mkdtemp())
        cfg = LogConfig(sqlite_commit_interval_s=10.0)
        logger = Logger(tmpdir, cfg)

        commit_count = [0]
        original_commit = logger._commit_sqlite
        def counting_commit():
            commit_count[0] += 1
            original_commit()
        logger._commit_sqlite = counting_commit

        # 5 minta hozzáadása nagy elapsed_s értékekkel
        for i in range(5):
            sample = {col: None for col in __import__('Prog.src.logger', fromlist=['CSV_COLUMNS']).CSV_COLUMNS}
            sample['elapsed_s'] = float((i + 1) * 3600)  # 1h, 2h, ... — kumulatív
            logger.log_sample(sample)

        # 5 mintánál az 5 × 3600s > 10s minden esetben → régi kód mindegyiknél commitolt
        # Új kód: 5 minta <<< 10 másodperc real wall time → legfeljebb 1 commit
        assert commit_count[0] <= 1, f"Túl sok commit: {commit_count[0]}"
        logger.close()
```

- [x] **Step 3: Futtasd a teszteket — ellenőrizd a FAIL-t**

```
python -m pytest Prog/tests/test_test_runner.py::TestRealDtIntegration Prog/tests/test_logger.py::TestLoggerCommitTiming -v
```

- [x] **Step 4: Módosítsd `test_runner.py:_run_step()`-et**

A `while not self._is_finished(controller):` blokk előtt:

```python
        _last_tick_t = time.perf_counter() - self._config.runner_tick_s
```

A while blokkon belül, a meglévő `controller.advance(self._config.runner_tick_s)` sort cseréld:

```python
            _t_now = time.perf_counter()
            actual_dt_s = _t_now - _last_tick_t
            _last_tick_t = _t_now
            controller.advance(actual_dt_s)
```

A `time.sleep()` sort cseréld:

```python
            if self._config.sleep_enabled:
                _elapsed = time.perf_counter() - _last_tick_t
                time.sleep(max(0.0, self._config.runner_tick_s - _elapsed))
```

- [x] **Step 5: Módosítsd `logger.py` wall-clock commit timerre**

Add hozzá az importokhoz:

```python
import time
```

Az `__init__`-ben töröld az `_elapsed_since_commit = 0.0` sort, add helyette:

```python
        self._last_commit_wall_t: float = time.monotonic()
```

A `log_sample()` metódusban cseréld a commit-trigger blokkot:

```python
    def log_sample(self, sample: dict) -> None:
        row = {col: sample.get(col, "") for col in CSV_COLUMNS}
        self._csv_writer.writerow(row)
        self._pending_rows.append(tuple(str(row[c]) if row[c] is not None else "" for c in CSV_COLUMNS))

        if time.monotonic() - self._last_commit_wall_t >= self._config.sqlite_commit_interval_s:
            self._commit_sqlite()
```

A `_commit_sqlite()`-ban töröld az `self._elapsed_since_commit = 0.0` sort, add:

```python
        self._last_commit_wall_t = time.monotonic()
```

- [x] **Step 6: Futtasd az összes tesztet**

```
python -m pytest Prog/tests/ -v
```

Elvárt: minden teszt PASS.

- [x] **Step 7: Commit**

```
git add Prog/src/test_runner.py Prog/src/logger.py Prog/tests/test_test_runner.py Prog/tests/test_logger.py
git commit -m "fix: K1+V3 — valós dt_s perf_counter alapon + adaptív sleep + logger wall-clock commit"
```

---

## Task 6 — K4: Atomi `checkpoint.json` write (`os.replace`)

**Fájlok:**
- Modify: `Prog/src/logger.py`
- Modify: `Prog/tests/test_logger.py`

**Háttér:**
`Path.write_text()` nem atomi Windowson. Temp fájl + `os.replace()` biztosítja, hogy egy félbeszakadt write után a checkpoint.json valid marad.

- [x] **Step 1: Írj failing tesztet**

Hozzáadd a `Prog/tests/test_logger.py` végéhez:

```python
class TestAtomicCheckpoint:
    """K4: checkpoint.json write atomi — temp fájl + os.replace."""

    def test_checkpoint_valid_json_after_write(self):
        import tempfile, json
        from pathlib import Path
        from Prog.src.logger import Logger, LogConfig

        tmpdir = Path(tempfile.mkdtemp())
        logger = Logger(tmpdir, LogConfig())
        state = {"status": "RUNNING", "step": "charge", "charge_ah": 1.23}
        logger.write_checkpoint(state)

        content = (tmpdir / "checkpoint.json").read_text(encoding="utf-8")
        parsed = json.loads(content)
        assert parsed["status"] == "RUNNING"
        assert parsed["charge_ah"] == pytest.approx(1.23)
        logger.close()

    def test_no_tmp_file_left_after_write(self):
        """Temp fájl ne maradjon a könyvtárban írás után."""
        import tempfile
        from pathlib import Path
        from Prog.src.logger import Logger, LogConfig

        tmpdir = Path(tempfile.mkdtemp())
        logger = Logger(tmpdir, LogConfig())
        logger.write_checkpoint({"status": "TEST"})

        tmp_files = list(tmpdir.glob("*.tmp"))
        assert tmp_files == [], f"Maradt temp fájl: {tmp_files}"
        logger.close()
```

- [x] **Step 2: Futtasd a teszteket**

```
python -m pytest Prog/tests/test_logger.py::TestAtomicCheckpoint -v
```

Az első teszt valószínűleg PASS (a jelenlegi kód is helyes JSON-t ír). A második teszt PASS is, mert nincs temp fájl generálva. Az igazi biztosíték a `os.replace` path lesz. Folytasd.

- [x] **Step 3: Módosítsd `logger.py:write_checkpoint()`-ot**

Add hozzá az importokhoz (a fájl elejére):

```python
import os
```

A `write_checkpoint()` metódus:

```python
    def write_checkpoint(self, state: dict) -> None:
        tmp = self._checkpoint_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._checkpoint_path)
```

- [x] **Step 4: Futtasd az összes tesztet**

```
python -m pytest Prog/tests/test_logger.py -v
```

- [x] **Step 5: Commit**

```
git add Prog/src/logger.py Prog/tests/test_logger.py
git commit -m "fix: K4 — checkpoint.json atomi write (temp + os.replace)"
```

---

## Task 7 — V2: `connect_all()` rollback + `disconnect_all()`

**Fájlok:**
- Modify: `Prog/src/instrument_manager.py`
- Modify: `Prog/tests/test_instrument_manager.py`

**Háttér:**
Részleges `connect_all()` failure esetén a már csatlakoztatott eszközök open VISA resource-ban maradnak. Fix: rollback — disconnect minden sikeresen kapcsolódott eszközön.

- [x] **Step 1: Írj failing teszteket**

Nézd meg a `Prog/tests/test_instrument_manager.py` tartalmát, majd add hozzá:

```python
class TestConnectAllRollback:
    """V2: connect_all() részleges failure esetén rollback."""

    def test_partial_connect_failure_rollbacks_connected_instruments(self):
        from Prog.src.instrument_manager import InstrumentManager, InstrumentConfig
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.src.exceptions import InstrumentTimeoutError

        psu = MockPSU()
        load = MockLoad(raise_on_connect=True)  # Load connect hibázik
        dmm_v = MockDMM()
        dmm_t = MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        cfg = InstrumentConfig(
            psu_resource="USB::PSU",
            load_resource="USB::LOAD",
            dmm_voltage_resource="TCPIP::DMM_V",
            dmm_temperature_resource="TCPIP::DMM_T",
        )

        with pytest.raises(Exception):
            im.connect_all(cfg)

        # PSU-t disconnectelni kellett a rollback során
        assert psu.called("disconnect"), f"PSU calls: {psu.calls}"

    def test_disconnect_all_calls_disconnect_on_all(self):
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM

        psu = MockPSU(); load = MockLoad(); dmm_v = MockDMM(); dmm_t = MockDMM()
        im = InstrumentManager(psu, load, dmm_v, dmm_t)
        im.disconnect_all()
        for inst in (psu, load, dmm_v, dmm_t):
            assert inst.called("disconnect"), f"{inst} disconnect nem hívódott"
```

**Megjegyzés:** A `MockLoad`-ban szükség van `raise_on_connect: bool = False` flagre. Ellenőrizd, hogy megvan-e; ha nem, add hozzá a `Prog/tests/mock_drivers/mock_load.py`-ba hasonlóan a MockPSU mintájára.

- [x] **Step 2: Futtasd a teszteket — ellenőrizd a FAIL-t**

```
python -m pytest Prog/tests/test_instrument_manager.py::TestConnectAllRollback -v
```

- [x] **Step 3: Módosítsd `instrument_manager.py`-t**

```python
    def connect_all(self, config: InstrumentConfig) -> None:
        instruments = [
            (self._psu,    config.psu_resource),
            (self._load,   config.load_resource),
            (self._dmm_v,  config.dmm_voltage_resource),
            (self._dmm_t,  config.dmm_temperature_resource),
        ]
        connected = []
        try:
            for inst, resource in instruments:
                inst.connect(resource)
                connected.append(inst)
        except Exception:
            for inst in reversed(connected):
                try:
                    inst.disconnect()
                except Exception:
                    pass
            raise

    def disconnect_all(self) -> None:
        """Sorrend: DMM_T → DMM_V → Load → PSU (fordított connect sorrend)."""
        for inst in (self._dmm_t, self._dmm_v, self._load, self._psu):
            try:
                inst.disconnect()
            except Exception:
                pass
```

- [x] **Step 4: Ellenőrizd, hogy a MockLoad-ban van-e `raise_on_connect`**

Ha nincs, add hozzá a `Prog/tests/mock_drivers/mock_load.py`-ban:

```python
    raise_on_connect: bool = False
```

és a `connect()` metódusban:

```python
    def connect(self, resource: str) -> None:
        self.calls.append(f"connect({resource!r})")
        if self.raise_on_connect:
            raise InstrumentTimeoutError("MockLoad: simulated connect failure")
```

- [x] **Step 5: Futtasd az összes tesztet**

```
python -m pytest Prog/tests/ -v
```

- [x] **Step 6: Commit**

```
git add Prog/src/instrument_manager.py Prog/tests/test_instrument_manager.py Prog/tests/mock_drivers/mock_load.py
git commit -m "fix: V2 — connect_all() rollback részleges failure esetén + disconnect_all()"
```

---

## Task 8 — E5: `Logger.close()` terminális állapotokban

**Fájlok:**
- Modify: `Prog/src/test_runner.py`
- Modify: `Prog/tests/test_test_runner.py`

**Háttér:**
`_emergency_stop()` és `_graceful_stop()` `flush_all()`-t hív, de `close()`-t nem. DONE path sem hívja. A fájlhandle-ek a GC-re maradnak. `Logger.close()` már tartalmaz `flush_all()`-t, tehát helyettesíteni lehet.

- [x] **Step 1: Írj failing tesztet**

Hozzáadd a `Prog/tests/test_test_runner.py` végéhez:

```python
class TestLoggerCloseOnTermination:
    """E5: Logger.close() terminális állapotokban hívódik."""

    def _run_to_done(self):
        """Helper: minimális TestRunner DONE-ig futtatva. Visszaadja a logger mockat."""
        from unittest.mock import MagicMock, patch
        from Prog.src.test_runner import TestRunner, TestRunnerConfig, TestPlan, TestStep, StepKind
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.safety import SafetyManager, PsuMode
        from Prog.src.instrument_manager import InstrumentManager
        from Prog.tests.mock_drivers.mock_psu import MockPSU
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.src.relax_controller import RelaxController, RelaxState
        import tempfile
        from pathlib import Path
        from Prog.src.logger import Logger, LogConfig

        profile = BatteryProfile(
            battery_name="T", manufacturer="F", model="FG20721",
            nominal_capacity_Ah=7.0, cell_count=6,
        )
        im = InstrumentManager(MockPSU(), MockLoad(), MockDMM(), MockDMM())
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        tmpdir = Path(tempfile.mkdtemp())
        real_logger = Logger(tmpdir, LogConfig())
        logger_mock = MagicMock(wraps=real_logger)

        class ImmediateRelax:
            state = RelaxState.RELAX_DONE
            def advance(self, dt_s): return self.state
            def reset(self): pass

        cfg = TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False)
        runner = TestRunner(im, safety, logger_mock, profile, cfg,
                            charge_controller=None, discharge_controller=None,
                            relax_controller=ImmediateRelax())

        step = TestStep(StepKind.RELAX, "relax")
        plan = TestPlan(test_type=None, steps=(step,))
        runner.run(plan)
        return logger_mock, real_logger

    def test_logger_close_called_on_done(self):
        logger_mock, real_logger = self._run_to_done()
        logger_mock.close.assert_called()
        real_logger.close()
```

- [x] **Step 2: Futtasd a tesztet — ellenőrizd a FAIL-t**

```
python -m pytest Prog/tests/test_test_runner.py::TestLoggerCloseOnTermination -v
```

- [x] **Step 3: Módosítsd `test_runner.py` terminális ágait**

`_emergency_stop()`-ban cseréld `self._logger.flush_all()`-t:

```python
        self._logger.close()
```

`_graceful_stop()`-ban ugyanígy:

```python
        self._logger.close()
```

A `run()` DONE ágában (a `return TestResult(status="DONE", ...)` előtt):

```python
        self.status = "DONE"
        self._logger.close()
        return TestResult(...)
```

**Megjegyzés:** CHECKPOINT_STOPPED esetén (`_run_manual_checkpoint()`) a `flush_all()` marad — a session folytatható, a logger nem zárható le.

- [x] **Step 4: Futtasd az összes tesztet**

```
python -m pytest Prog/tests/ -v
```

- [x] **Step 5: Commit**

```
git add Prog/src/test_runner.py Prog/tests/test_test_runner.py
git commit -m "fix: E5 — Logger.close() meghívása DONE/FAULT/STOPPED terminális állapotokban"
```

---

## Záróteszt — teljes suite

- [ ] **Futtasd a teljes tesztsuitét**

```
python -m pytest Prog/tests/ -v
python -m compileall Prog
python -m ruff check Prog
python -m mypy Prog
```

Elvárt: 0 FAIL, 0 error. Ha mypy vagy ruff hibát jelez, javítsd a típusannotációkat és a style problémákat.

- [ ] **Frissítsd a memory-t és commitolj**

```
git log --oneline -8
```

Összefoglaló commit ha minden task kész:
```
git commit --allow-empty -m "chore: rev2 safety hotfixes kész — K1-K4 + V1-V3 + E5"
```

---

## Végrehajtás után ellenőrzendő

| # | Ellenőrzés | Módszer |
|---|-----------|---------|
| K1 | dt_s nem állandó 2.0 | Teszt: perf_counter mock |
| K2 | Max Ah/time CV-ben is fault | Teszt: accumulated_charge_Ah > 1.20× |
| K3 | u_drop > 1.25V → FAULT | Teszt: psu.voltage_V = u_batt + 1.30 |
| V1 | PSU readback fail → FAULT | Teszt: simulate_current_readback_timeout |
| V4 | DMM hiba precheck → DMM_FEEDBACK_LOST | Teszt: dmm.dmm_valid = False |
| K4 | Checkpoint temp fájl nem marad | Teszt: glob("*.tmp") |
| V2 | Részleges connect → rollback | Teszt: psu disconnect called |
| E5 | close() hívódik DONE-nál | Teszt: mock.close.assert_called() |
