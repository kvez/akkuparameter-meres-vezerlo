# TestRunner — FÁZIS 5 design spec

**Dátum:** 2026-05-31  
**Döntési előzmények:** d4.txt, d5.txt, d6.txt  
**Állapot:** Jóváhagyott

---

## 1. Célkitűzés

A `Prog/src/test_runner.py` a rendszer Application/Controller rétege: összeköti a meglévő állapotgépeket (ChargeController, DischargeController, RelaxController), a loggert, a SafetyManagert és az InstrumentManagert, és egy teljes mérési ciklust hajt végre.

**Nem tartalmaz:** GUI-kódot, QThread-et, asyncio-t, saját main()-t, saját threadet.  
**Kívülről hívható:** CLI-ből, unit tesztből, és FÁZIS 6-ban GUI worker thread-ből.

---

## 2. Adatstruktúrák

```python
class TestType(Enum):
    CHARACTERIZATION = "CHARACTERIZATION"
    BQ_LEARNING_PHYSICAL = "BQ_LEARNING_PHYSICAL"

class StepKind(Enum):
    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"
    RELAX = "RELAX"
    MANUAL_CHECKPOINT = "MANUAL_CHECKPOINT"

@dataclass(frozen=True)
class TestStep:
    kind: StepKind
    label: str          # pl. "charge_1", "relax_after_discharge" — loghoz, riporthoz

@dataclass(frozen=True)
class TestPlan:
    test_type: TestType
    steps: list[TestStep]

    @staticmethod
    def characterization() -> "TestPlan":
        # [CHARGE, RELAX, DISCHARGE, RELAX]

    @staticmethod
    def bq_learning_physical() -> "TestPlan":
        # [CHARGE, RELAX, DISCHARGE, RELAX] × 2 + [MANUAL_CHECKPOINT]

@dataclass
class TestResult:
    status: str              # "DONE" / "FAULT" / "STOPPED"
    reason: str = ""
    total_charge_ah: float = 0.0
    total_discharge_ah: float = 0.0

@dataclass
class TestRunnerConfig:
    runner_tick_s: float = 2.0
    test_name: str = "unnamed"
    sleep_enabled: bool = True   # False unit tesztekben
```

**Indoklások:**
- `frozen=True` a TestStep/TestPlan-on: futás közben nem módosítható véletlenül; GUI és runner egyszerre olvashatja race condition nélkül.
- Enum értékek explicit string-ek: logban, CSV-ben, checkpoint JSON-ban olvashatók, sorrend-függetlenek.
- `status: str` (nem Enum): egyszerűbb, FÁZIS 5-höz elegendő.
- `sleep_enabled`: a runner nem tudja, hogy mock vagy valós hardver — a hívó dönti el.

---

## 3. TestRunner osztály

### __init__ paraméterek

```python
class TestRunner:
    def __init__(
        self,
        instrument_manager: InstrumentManager,
        safety: SafetyManager,
        logger: Logger,
        profile: BatteryProfile,
        config: TestRunnerConfig,
        charge_controller: ChargeController,
        discharge_controller: DischargeController,
        relax_controller: RelaxController,
    ): ...
```

A kontrollerek előre inicializálva érkeznek kívülről. A TestRunner nem példányosít kontrollert — ez megkönnyíti a tesztelést és a GUI integrációt.

### Publikus API

```python
def run(self, test_plan: TestPlan) -> TestResult   # blokkoló
def request_stop(self) -> None                     # graceful: lépések között / RELAX alatt hat
def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP") -> None  # azonnali
```

### Belső állapot

```python
self.stop_requested: bool = False
self.emergency_stop_requested: bool = False
self.emergency_stop_reason: str = ""
self.current_step: TestStep | None = None
self.status: str = "IDLE"          # "IDLE" / "RUNNING" / "DONE" / "FAULT" / "STOPPED"
self._total_charge_ah: float = 0.0
self._total_discharge_ah: float = 0.0
```

---

## 4. Kontroll-folyam

### run()

```
status = "RUNNING"
for step in test_plan.steps:
    ① emergency_stop_requested? → _emergency_stop()
    ② stop_requested?           → _graceful_stop()   ← lépések KÖZÖTT
    ③ step_result = _run_step(step)
    ④ step_result.status == "FAULT"   → _emergency_stop(step_result.reason)
    ⑤ step_result.status == "STOPPED" → return step_result
return TestResult("DONE", total_charge_ah=..., total_discharge_ah=...)
```

### _run_step()

```
MANUAL_CHECKPOINT: → _run_manual_checkpoint()  (külön ág, nincs controller)

controller = _controller_for_step(step)
while not _is_finished(controller):
    ① emergency_stop_requested? → _emergency_stop()               ← MINDIG azonnali
    ② stop_requested AND _step_can_be_gracefully_interrupted(step)?
       → _graceful_stop()                                          ← csak RELAX/MANUAL_CHECKPOINT
    ③ controller.advance(config.runner_tick_s)
    ④ sample = _build_sample(step, controller)
    ⑤ logger.log_sample(sample)   # SQLite commit időzítését a Logger kezeli belsőleg
    ⑥ logger.flush_all()          # CSV flush; minden tick-ben (FÁZIS 5 egyszerűsítés)
    ⑦ logger.write_checkpoint({"status": "RUNNING", "step": step.label, ...})  # periódikusan
    ⑧ _controller_faulted(controller)? → return TestResult("FAULT", reason)
    ⑨ sleep_enabled → time.sleep(runner_tick_s)
return TestResult("DONE")
```

### Stop-szabályok összefoglalva

| Lépés                | stop_requested      | emergency_stop_requested |
|----------------------|---------------------|--------------------------|
| CHARGE               | ✗ (lépés végéig)   | ✓ azonnali               |
| DISCHARGE            | ✗ (lépés végéig)   | ✓ azonnali               |
| RELAX                | ✓ megszakítható     | ✓ azonnali               |
| MANUAL_CHECKPOINT    | ✓ megszakítható     | ✓ azonnali               |

**Indoklás:** CHARGE/DISCHARGE félbehagyása rendezetlen akkuállapotot és érvénytelen kapacitásadatot okozna. RELAX és MANUAL_CHECKPOINT esetén nincs aktív energiaút — biztonságosan megszakítható.

---

## 5. _build_sample()

A TestRunner hívja az `InstrumentManager.read_all()`-t, majd összeállítja a Logger számára szükséges dict-et a `CSV_COLUMNS` alapján.

Forrás-mező mapping:

| Csoport | Forrás |
|---------|--------|
| timestamp_iso, elapsed_s | datetime.now(), futásidő számítás |
| test_name, step_name, state | config.test_name, step.label, controller.state.value |
| battery_voltage_V, psu_readback_*, load_readback_* | instrument_manager.read_all() |
| accumulated_charge_Ah, accumulated_discharge_Ah | controller tulajdonságai |
| integration_valid, fault_reason | controller tulajdonságai |
| isolation_state | "PSU_OUTPUT_OFF_ONLY" (konstans, [R1]) |
| u_drop_V | psu_readback_voltage_V - battery_voltage_V |
| diode_power_W | u_drop_V × psu_readback_current_A |

Hiányzó mező: `None` (a Logger kezeli).

---

## 6. _emergency_stop() / _graceful_stop()

```
_emergency_stop(reason):
    status = "FAULT"
    instrument_manager.safe_all_off()   # LOAD OFF → PSU OFF ([R1] sorrendben)
    logger.log_event("EMERGENCY_STOP", reason, is_critical=True)
    logger.write_checkpoint({"status": "FAULT", "reason": reason})
    logger.flush_all()
    return TestResult("FAULT", reason)

_graceful_stop(reason):
    status = "STOPPED"
    instrument_manager.safe_all_off()
    logger.log_event("GRACEFUL_STOP", reason)
    logger.write_checkpoint({"status": "STOPPED", "reason": reason})
    logger.flush_all()
    return TestResult("STOPPED", reason)
```

**safe_all_off() idempotens** — ha a controller már meghívta, a dupla hívás nem okoz hibát ([R20]).

---

## 7. _run_manual_checkpoint()

```
_run_manual_checkpoint(step):
    logger.log_event("MANUAL_BQ_CHECKPOINT_REACHED", step.label)
    logger.write_checkpoint({"status": "MANUAL_CHECKPOINT", "step": step.label})
    logger.flush_all()
    return TestResult(status="STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
                      total_charge_ah=..., total_discharge_ah=...)
```

FÁZIS 5-ben nincs CLI input — a futás leáll a checkpoint után. FÁZIS 6-ban a GUI vehet át irányítást.

---

## 8. Belső segédmetódusok

```python
def _controller_for_step(step: TestStep)          # StepKind → controller objektum
def _is_finished(controller) -> bool               # DONE/FAULT/SAFE_OFF állapot → True
def _controller_faulted(controller) -> bool        # FAULT/SAFE_OFF → True
def _step_can_be_gracefully_interrupted(step) -> bool  # RELAX, MANUAL_CHECKPOINT → True
```

---

## 9. TestPlan gyártómetódusok — lépéssorrend

**CHARACTERIZATION:**
```
CHARGE       "charge"
RELAX        "relax_after_charge"
DISCHARGE    "discharge"
RELAX        "relax_after_discharge"
```

**BQ_LEARNING_PHYSICAL:**
```
CHARGE       "charge_1"
RELAX        "relax_after_charge_1"
DISCHARGE    "discharge_1"
RELAX        "relax_after_discharge_1"
CHARGE       "charge_2"
RELAX        "relax_after_charge_2"
DISCHARGE    "discharge_2"
RELAX        "relax_after_discharge_2"
MANUAL_CHECKPOINT  "manual_bq_checkpoint"
```

---

## 10. Tesztelési stratégia

Mock driverekkel futtatható, GUI/thread nélkül:

```python
runner = TestRunner(
    instrument_manager=MockInstrumentManager(...),
    safety=SafetyManager(profile, PsuMode.INDEPENDENT),
    logger=Logger(tmp_dir, LogConfig()),
    profile=profile,
    config=TestRunnerConfig(runner_tick_s=1.0, sleep_enabled=False),
    charge_controller=ChargeController(...mock...),
    discharge_controller=DischargeController(...mock...),
    relax_controller=RelaxController(...mock...),
)
result = runner.run(TestPlan.characterization())
assert result.status == "DONE"
```

Tesztelendő esetek:
- Normál CHARACTERIZATION → DONE, Ah > 0
- Normál BQ_LEARNING_PHYSICAL → STOPPED (MANUAL_CHECKPOINT_REACHED)
- emergency_stop CHARGE közben → FAULT, instruments OFF
- stop_requested CHARGE közben → nem hat, végigfut
- stop_requested RELAX közben → azonnal STOPPED
- DMM feedback lost (MockDMM timeout) → FAULT
- Unhandled exception → FAULT, instruments OFF

---

## 11. Fájlok

| Fájl | Tartalom |
|------|---------|
| `Prog/src/test_runner.py` | TestType, StepKind, TestStep, TestPlan, TestResult, TestRunnerConfig, TestRunner |
| `Prog/tests/test_test_runner.py` | Mock-alapú unit tesztek |
