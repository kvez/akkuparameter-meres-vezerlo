"""R13: SCPI inventory through production drivers; JSON/CSV candidate golden.

Measured durations include VISA/USB/LAN/GPIB transport latency. They are NOT
physical switching times. A write duration ends at write() return; query RTT
ends at response receipt; an atomic ;*OPC? RTT includes command completion.
SYST:ERR? is checked after each PROGRAM MESSAGE, including readbacks, not
between units inside an atomic production message. It is not queried
recursively after SYST:ERR? itself. Diagnostic queries use the same session
without Device Clear, so INIT/trace capture is not aborted by instrumentation.
Production messages, clear calls and order remain visible in the transcript.

No-energy means PSU/Load energy is off and DUT/battery disconnected. DMM
resistance/RTD measurements still use the meter's internal excitation.
A uses the fixed fixture declared by --fixture-state; no runtime prompts:
DMM_T=NTC10K;DMM_V=OPEN;PSU=OPEN;LOAD=OPEN;BATTERY=DISCONNECTED
DMM_V and DMM_T are separate 34465A meters. The NTC remains connected for the
PT100 command-acceptance case; that reading is explicitly not temperature
evidence. NTC is read before PT100; subsequent NTC readings are not
metrologically assessable because PT100 excitation may cause self-heating.
NTC conversion uses production R25=10000 ohm, B=3950 K, NPLC=10.
After final safe-off, the identified, distinct thermal meter is restored to
NTC resistance mode with CONF?/RES:NPLC? verification, also after failures.
Missing identities or duplicate IDs prohibit configuring an uncertain meter.
Resource arguments default to Prog/config/local_config.yaml. Outputs go only
to --output-dir.
Timing and configuration results are candidates until physical review.
Energized runs require a separate flag AND typed, runtime confirmation.
Use isolated lab fixtures, never a battery; PSU and Load are never on together.
Combined PSU modes require runtime wiring confirmation and bounded readback
of BOTH channel setpoints before ON. No driver retry or uncertain-command
retry is allowed. Poll deadlines are checked between synchronous calls; the
VISA timeout continues to bound each I/O call. Repetition occurs only after a
completely successful case.
A candidate is scoped to its phase, mode, firmware, arguments and fixtures;
conditional fallback branches are inventoried, never forced on real hardware.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import inspect
import json
import logging
import math
import platform
import re
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from Prog.drivers import device_dmm, device_load, device_psu

logger = logging.getLogger(__name__)
LIMITATION = "Includes VISA/interface latency; not physical switching time."
CLASSES = {
    "load": device_load.Keithley2380Load,
    "psu": device_psu.Keithley2220PSU,
    "dmm_voltage": device_dmm.Keysight34465ADMM,
    "dmm_temperature": device_dmm.Keysight34465ADMM,
}
MODELS = {"load": "2380-120-60", "psu": "2220-30-1",
          "dmm_voltage": "34465A", "dmm_temperature": "34465A"}
FIXTURE_STATE = "DMM_T=NTC10K;DMM_V=OPEN;PSU=OPEN;LOAD=OPEN;BATTERY=DISCONNECTED"
LOCAL_CONFIG = _ROOT / "Prog" / "config" / "local_config.yaml"
NTC_PARAMETERS = (10000.0, 3950.0, 10.0)  # main_window.py bring-up parameters
TOKEN = {"no-energy": "DUT_DISCONNECTED", "energized": "ENERGIZE_ISOLATED_FIXTURE"}
# Readback-szoveg: rovid SCPI-forma -> elfogadott pontos valaszok (rovid/hosszu).
SCPI_LONG_FORMS = {
    "FIX": {"FIX", "FIXED"},
    "CURR": {"CURR", "CURRENT"},
    "VOLT": {"VOLT", "VOLTAGE"},
    "NEV": {"NEV", "NEVER"},
    "IMM": {"IMM", "IMMEDIATE"},
    "TIM": {"TIM", "TIMER"},
}


class InventoryError(RuntimeError):
    """Stop this run; never retry an uncertain operation."""


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or abs(number) >= device_dmm.OVERLOAD_THRESHOLD:
        raise InventoryError(f"Invalid numeric readback: {value!r}")
    return number


def boolean(raw: str) -> bool:
    token = raw.strip().upper()
    if token in ("ON", "ENABLED"):
        return True
    if token in ("OFF", "DISABLED"):
        return False
    number = finite(token)
    if number not in (0.0, 1.0):
        raise InventoryError(f"Invalid state: {raw!r}")
    return bool(number)


# Independent safety policy: deliberately NOT inferred from driver source/inventory.
ENERGY_METHODS = frozenset({
    "output_on", "input_on", "configure_transient_pulse", "fire_transient_pulse",
    "begin_pulse", "fire_pulse",
})
RESET_NOTE = ("not executed: may reset active protection settings, "
              "e.g. 2220 VOLT:LIM, 2380 CURR:PROT")
NTC_AFTER_PT100 = ("not metrologically assessable: preceding PT100 excitation "
                  "may cause NTC self-heating; no cooldown has been validated")
# All retained readbacks are documented; none is optional or error-tolerant.
# Local primary manuals, with text-extract line references:
# 2220S-907-01B: prog.txt 1433-1435, 1539-1589, 1660-1676, 1717-1719,
# 2007-2015 (current/voltage, mode, timer, output recall, regulation).
# 2380-120-60-900-01 Rev A: 5638-5771, 5814-6025, 6455 ff.
# (trace, input guards, function, current/range, voltage-on).
# 34460-90901 Ed.7: SCPI reference, Voltage/Resistance/Temperature,
# Sample and Trigger subsystems; CONF? p.244/245, SAMP:TIM? p.311,
# temperature reference p.438. Firmware acceptance remains HIL_PENDING.
# The production PSU connect no longer queries OUTP:PON? (R13-B4,
# R13-UD-PSU-PON-REMOVE); the timer safety gate stays mandatory.


def scpi_headers(message: str) -> list[tuple[str, str]]:
    """Include raw and SCPI-relative headers; common commands keep the path."""
    result = []
    parent = ""
    for part in message.upper().split(";"):
        fields = part.strip().split(None, 1)
        if not fields:
            continue
        raw = fields[0]
        header = raw.lstrip(":")
        argument = fields[1].strip() if len(fields) == 2 else ""
        result.append((header, argument))
        if header.startswith("*"):
            continue
        resolved = header if raw.startswith(":") or not parent else parent + ":" + header
        if resolved != header:
            result.append((resolved, argument))
        parent = resolved.rsplit(":", 1)[0] if ":" in resolved else ""
    return result


def energizes(message: str) -> bool:
    for header, argument in scpi_headers(message):
        if re.fullmatch(r"FORC(?:E)?:TRIG(?:GER)?|\*TRG", header):
            return True
        if re.fullmatch(
                r"(?:SOUR(?:CE)?:)?(?:OUTP(?:UT)?(?::(?:STAT(?:E)?|ENAB(?:LE)?))?"
                r"|INP(?:UT)?(?::STAT(?:E)?)?|TRAN(?:SIENT)?(?::STAT(?:E)?)?)", header):
            if argument == "ON":
                return True
            try:
                if float(argument) == 1.0:
                    return True
            except ValueError:
                pass
    return False


def resets_settings(message: str) -> bool:
    return any(re.fullmatch(r"\*RST|\*RCL|SYST(?:EM)?:PRES(?:ET)?", header)
               for header, _ in scpi_headers(message))


def energy_method(method: str) -> bool:
    return method in ENERGY_METHODS or bool(re.fullmatch(r"(?:begin|fire)_.*pulse.*", method))

def source_inventory() -> list[dict[str, Any]]:
    """AST inventory, including zero-I/O helpers; not execution evidence."""
    result: list[dict[str, Any]] = []
    for key, cls in CLASSES.items():
        path = Path(inspect.getfile(cls))
        source_bytes = path.read_bytes()
        text = source_bytes.decode("utf-8")
        tree = ast.parse(text)
        digest = hashlib.sha256(source_bytes).hexdigest()
        for node in tree.body:
            nodes = node.body if isinstance(node, ast.ClassDef) else [node]
            for method in nodes:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                expressions = [
                    ast.unparse(call) for call in ast.walk(method)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr in ("write", "query", "_write", "_query",
                                           "_apply_config_atomic", "_apply_atomic_query")
                ]
                result.append({
                    "instrument": key, "method": method.name, "line": method.lineno,
                    "source_sha256": digest, "io_expressions": expressions,
                    "evidence": "source_only",
                    "scope": ("conditional_or_parameterized" if expressions else
                              "no_direct_scpi_or_delegates"),
                    "execution_note": (RESET_NOTE if method.name == "reset" else
                                       "source-only in A: energy activation prohibited"
                                       if energy_method(method.name) else
                                       "execution depends on phase and fixture"),
                })
    return result


def summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    for row in records:
        if row["kind"] in ("write", "query") and row["exception"] is None:
            key = (row["instrument"], row["case"], row["role"],
                   row["kind"], row["message"])
            groups[key].append(row["elapsed_s"])
    result = []
    for key, samples in sorted(groups.items()):
        ordered = sorted(samples)
        result.append(dict(zip(
            ("instrument", "case", "role", "kind", "message"), key, strict=True),
            n=len(samples), min_s=min(samples), median_s=statistics.median(samples),
            max_s=max(samples), p95_s=ordered[math.ceil(0.95 * len(ordered)) - 1],
            p95_method="nearest_rank"))
    return result


class Session:
    """Resource proxy; records actual driver I/O, never synthesizes success."""

    def __init__(self, owner: Runner, name: str, raw: Any) -> None:
        self.owner, self.name, self.raw = owner, name, raw

    @property
    def timeout(self) -> Any:
        return self.raw.timeout

    @timeout.setter
    def timeout(self, value: Any) -> None:
        self.raw.timeout = value

    def clear(self) -> None:
        # Device Clear is an interface action, not an SCPI write.
        self.owner.record(self.name, self.raw, "clear", "", "driver")

    def close(self) -> None:
        self.owner.record(self.name, self.raw, "close", "", "driver")

    def write(self, message: str) -> Any:
        return self.io("write", message)

    def query(self, message: str) -> str:
        return str(self.io("query", message))

    def io(self, kind: str, message: str, role: str = "driver") -> Any:
        owner = self.owner
        if energizes(message) and (owner.args.phase != "energized"
                                   or not owner.confirmed or owner.cleaning):
            owner.halted = True
            raise InventoryError("ON blocked by phase/confirmation/cleanup gate")
        if owner.args.phase == "no-energy" and resets_settings(message):
            owner.halted = True
            raise InventoryError(RESET_NOTE)
        if owner.halted and not owner.cleaning:
            raise InventoryError("Run halted; no further production command")
        value, row = owner.record(self.name, self.raw, kind, message, role)
        if message == "*IDN?" and owner.identity_matches(self.name, str(value)):
            owner.verified.add(self.name)
        if kind == "query" and "*OPC?" in message.upper() and value.strip() != "1":
            owner.halted = True
            raise InventoryError(f"Unexpected OPC response: {value!r}")
        if message.strip().upper() == "SYST:ERR?":
            try:
                owner.check_error_response(str(value))
            except InventoryError:
                row["scpi_error"] = str(value)
                raise
        else:
            # Deliberately bypass proxy recursion AND driver Device Clear.
            # This is added diagnostics, not a duplicated production operation.
            err, error_row = owner.record(
                self.name, self.raw, "query", "SYST:ERR?", "error")
            row["error_record_id"] = error_row["id"]
            try:
                owner.check_error_response(str(err))
            except InventoryError:
                error_row["scpi_error"] = str(err)
                raise
        return value


class ExistingRM:
    def __init__(self, sessions: dict[str, Session]) -> None:
        self.sessions = sessions

    def open_resource(self, address: str) -> Session:
        return self.sessions[address]


class Runner:
    def __init__(self, args: argparse.Namespace, rm: Any,
                 confirm: Callable[[str], str] = input) -> None:
        self.args, self.rm, self.confirm = args, rm, confirm
        self.devices: dict[str, Any] = {
            name: cls(timeout_ms=args.timeout_ms, max_retries=1)
            for name, cls in CLASSES.items()
        }
        self.sessions: dict[str, Session] = {}
        self.records: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.transitions: list[dict[str, Any]] = []
        self.identities: dict[str, str] = {}
        self.ch2_voltage_v = 0.0
        self.verified: set[str] = set()
        self.case = "startup"
        self.iteration = 0
        self.halted = False
        self.cleaning = False
        self.confirmed = False
        self.failure: str | None = None
        self.cleanup_failures: list[str] = []
        self.dmm_state: dict[str, dict[str, Any]] = {
            name: {"mode": "unknown", "nplc": None, "range_V": None}
            for name in ("dmm_voltage", "dmm_temperature")
        }
        self.measurements: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.pt100_attempted = False
        self.psu_restore: dict[str, Any] = {"status": "not_attempted_identity_gate"}
        self.psu_off_verified = False
        self.not_executed: list[dict[str, str]] = []
        self.safe_off_status: dict[str, dict[str, str]] = {
            name: {"status": "safe_off_not_attempted", "reason": "identity_gate_not_passed"}
            for name in ("load", "psu")
        }
        self.thermal_restore_allowed = False
        self.thermal_restore: dict[str, Any] = {"status": "not_attempted_identity_gate"}
        self.inventory = source_inventory()

    def record(self, name: str, raw: Any, kind: str,
               message: str, role: str) -> tuple[Any, dict[str, Any]]:
        row = {
            "id": len(self.records), "instrument": name, "case": self.case,
            "iteration": self.iteration, "role": role, "kind": kind,
            "message": message, "utc": utc(), "start_perf_s": time.perf_counter(),
            "timeout_ms": raw.timeout, "response_raw": None, "response_length": None,
            "exception": None, "dmm_state": dict(self.dmm_state.get(name, {})),
            "error_record_id": None, "scpi_error": None,
        }
        self.records.append(row)
        try:
            value = getattr(raw, kind)(message) if kind in ("write", "query") else getattr(raw, kind)()
            row["elapsed_s"] = time.perf_counter() - row["start_perf_s"]
            if kind == "query":
                row["response_raw"] = str(value)
                row["response_length"] = len(str(value))
            return value, row
        except BaseException as exc:
            row["elapsed_s"] = time.perf_counter() - row["start_perf_s"]
            self.halted = True
            row["exception"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            row["timing_type"] = ("write_return" if kind == "write" else
                                  "opc_completion_rtt" if "*OPC?" in message else
                                  "query_rtt" if kind == "query" else "interface_call")

    def check_error_response(self, raw: str) -> None:
        try:
            code = int(raw.strip().split(",", 1)[0])
        except ValueError as exc:
            self.halted = True
            raise InventoryError(f"Malformed SYST:ERR?: {raw!r}") from exc
        if code != 0:
            self.halted = True
            raise InventoryError(f"SCPI error: {raw!r}")

    def ask(self, token: str, explanation: str) -> None:
        if self.confirm(f"{explanation}\nType {token}: ").strip() != token:
            raise InventoryError("Runtime confirmation refused")

    def invoke(self, name: str, method: str, *values: Any) -> Any:
        if energy_method(method) and (self.args.phase != "energized"
                                      or not self.confirmed or self.cleaning):
            self.halted = True
            raise InventoryError(f"Energy method blocked: {name}.{method}")
        if self.args.phase == "no-energy" and method == "reset":
            self.halted = True
            raise InventoryError(RESET_NOTE)
        self.calls.append({"instrument": name, "method": method, "case": self.case,
                           "iteration": self.iteration, "args": values, "utc": utc()})
        if name == "load" and method == "input_on":
            # R16-B10F1: az ON-nyugtát a saját, határidős wait_state poll igazolja.
            value = self.devices[name].input_on(*values, wait_for_ack=False)
        else:
            value = getattr(self.devices[name], method)(*values)
        if self.halted and not self.cleaning:
            raise InventoryError("Driver swallowed an I/O error; stopping")
        return value

    def query(self, name: str, message: str) -> str:
        # Additional readbacks only. All inventory operations use invoke().
        return str(self.sessions[name].io("query", message, "readback"))

    def expect(self, name: str, message: str, expected: Any) -> str:
        raw = self.query(name, message)
        if isinstance(expected, bool):
            ok = boolean(raw) == expected
        elif isinstance(expected, (int, float)):
            ok = math.isclose(finite(raw), expected, rel_tol=1e-5, abs_tol=1e-8)
        else:
            # HIL 2026-11-05: a 2380 a SOUR:FUNC:MODE? kerdesre "FIXED"-et ad.
            # Rovid es hosszu SCPI-forma pontos egyezessel, prefix nelkul.
            allowed = SCPI_LONG_FORMS.get(str(expected).upper(), {str(expected).upper()})
            ok = raw.strip().strip('"').upper() in allowed
        if not ok:
            self.halted = True
            raise InventoryError(f"{name} {message}: {raw!r}, expected {expected!r}")
        return raw

    def case_call(self, name: str, method: str, *values: Any,
                  readbacks: tuple[tuple[str, Any], ...] = ()) -> None:
        self.case = f"{name}.{method}:{values!r}"
        for iteration in range(self.args.repeat):
            self.iteration = iteration
            self.invoke(name, method, *values)
            for message, expected in readbacks:
                self.expect(name, message, expected)

    def wait_state(self, name: str, target: bool, started: float) -> None:
        deadline = started + self.args.poll_max_s
        method = "read_input_state" if name == "load" else "read_output_state"
        while True:
            first = len(self.records)
            actual = self.invoke(name, method)
            observed = max(r["start_perf_s"] + r["elapsed_s"] for r in self.records[first:]
                           if r["kind"] == "query" and r["role"] == "driver")
            now = time.perf_counter()
            if actual is target and now <= deadline:
                self.transitions.append({
                    "instrument": name, "case": self.case, "target": target,
                    "utc": utc(), "elapsed_s": observed - started,
                    "limitation": LIMITATION,
                })
                return
            if now >= deadline:
                raise InventoryError(f"{name}: state polling deadline exceeded")
            time.sleep(min(self.args.poll_interval_s, deadline - now))

    def off(self) -> list[str]:
        failures: list[str] = []
        started_at: dict[str, float] = {}
        previous = self.cleaning
        self.cleaning = True
        first_record = len(self.records)
        self.psu_off_verified = False
        command_ok: set[str] = set()
        try:
            # Send BOTH OFFs before polling; a failed Load OFF cannot block PSU OFF.
            for name, method in (("load", "input_off"), ("psu", "all_outputs_off")):
                if name not in self.verified:
                    continue
                self.safe_off_status[name] = {"status": "attempted", "reason": "identity_verified"}
                started_at[name] = time.perf_counter()
                first = len(self.records)
                try:
                    self.invoke(name, method)
                    command_ok.add(name)
                except BaseException as exc:
                    logger.exception("Safe-off command failed: %s", name)
                    failures.append(f"{name} safe-off: {type(exc).__name__}: {exc}")
                finally:
                    started_at[name] = self.command_start(
                        name, first, started_at[name], on=False)
            for name, started in started_at.items():
                try:
                    self.wait_state(name, False, started)
                    if name == "psu" and name in command_ok:
                        self.psu_off_verified = not any(
                            r["exception"] or r["scpi_error"]
                            for r in self.records[first_record:] if r["instrument"] == "psu")
                    if name == "load":
                        self.expect("load", "TRAN?", False)
                except BaseException as exc:
                    logger.exception("OFF readback failed: %s", name)
                    failures.append(f"{name} OFF readback: {type(exc).__name__}: {exc}")
            if any(r["exception"] or r["scpi_error"] for r in self.records[first_record:]):
                failures.append("Safe-off I/O/SCPI error (including swallowed driver errors)")
        finally:
            self.cleaning = previous
        return failures

    def require_off(self) -> None:
        failures = self.off()
        if failures:
            self.cleanup_failures.extend(failures)
            raise InventoryError("; ".join(failures))

    def identity_matches(self, name: str, identity: str) -> bool:
        parts = [p.strip().upper() for p in identity.split(",")]
        manufacturers = (("KEYSIGHT TECHNOLOGIES", "AGILENT TECHNOLOGIES")
                         if name.startswith("dmm_") else ("KEITHLEY", "KEITHLEY INSTRUMENTS",
                                               "KEITHLEY INSTRUMENTS INC."))
        return len(parts) >= 4 and parts[0] in manufacturers and parts[1] == MODELS[name]

    def command_start(self, name: str, first: int, default: float,
                      *, on: bool) -> float:
        # Anchor to actual resource I/O, after production Device Clear.
        for row in self.records[first:]:
            if row["instrument"] != name or row["kind"] not in ("write", "query"):
                continue
            units = [unit.lstrip(":") for unit in row["message"].split(";")]
            needle = ("OUTP ON" if name == "psu" else "INP ON") if on else (
                "OUTP OFF" if name == "psu" else "INP OFF")
            if needle in units or (not on and "OUTP:ENAB OFF" in units):
                return float(row["start_perf_s"])
        return default

    def open(self) -> None:
        addresses = {name: getattr(self.args, name) for name in CLASSES}
        if len(set(addresses.values())) != len(addresses):
            raise InventoryError("Instrument resources must be distinct")
        # R15-B1: identify both energy instruments even after a peer failure.
        errors: list[BaseException] = []
        previous = self.cleaning
        try:
            for name in CLASSES:
                self.cleaning = bool(errors)
                try:
                    raw = self.rm.open_resource(addresses[name])
                    session = Session(self, name, raw)
                    self.sessions[name] = session
                    raw.timeout = self.args.timeout_ms
                    device = self.devices[name]
                    device._resource = session
                    identity = self.invoke(name, "idn")
                    self.identities[name] = identity
                    if not self.identity_matches(name, identity):
                        raise InventoryError(f"{name}: identity mismatch: {identity!r}")
                    self.verified.add(name)
                except BaseException as exc:
                    errors.append(exc)
                    logger.exception("Identity/open failed: %s", name)
        finally:
            self.cleaning = previous
        if errors:
            raise errors[0]
        seen: dict[str, str] = {}
        for name, identity in self.identities.items():
            normalized = ",".join(part.strip().upper() for part in identity.split(","))
            if normalized in seen:
                raise InventoryError(f"Duplicate IDN for {seen[normalized]} and {name}: {identity!r}")
            seen[normalized] = name
        self.thermal_restore_allowed = True
        mapping = ExistingRM({addresses[n]: s for n, s in self.sessions.items()})
        self.case = "initial_safe_off"
        self.require_off()
        try:
            for name in CLASSES:
                if errors and name not in ("load", "psu"):
                    break
                self.cleaning = bool(errors)  # OFF-only recovery; ON remains blocked.
                self.case = f"{name}.connect"
                first = len(self.records)
                try:
                    self.devices[name].connect(addresses[name], rm=mapping)
                    if any(r["exception"] or r["scpi_error"] for r in self.records[first:]):
                        raise InventoryError("Connection diagnostics failed")
                    if name == "load":
                        for message, expected in (("INP:SHOR?", False), ("INP:TIM?", False),
                                                  ("VOLT:ON?", 0.0), ("SOUR:FUNC:MODE?", "FIX")):
                            self.expect(name, message, expected)
                    if name == "psu":
                        for channel in ("CH1", "CH2"):
                            self.expect(name, f"INST:SEL {channel};:OUTP:TIM:STAT?", False)
                        self.invoke(name, "_select_ch1")
                except BaseException as exc:
                    errors.append(exc)
                    logger.exception("Connect failed: %s", name)
        finally:
            self.cleaning = previous
        if errors:
            raise errors[0]
        self.require_off()

    def configure_dcv(self) -> None:
        self.dmm_state["dmm_voltage"] = {"mode": "DCV_configuring", "nplc": None, "range_V": None}
        self.case_call("dmm_voltage", "configure_dcv", 10.0, 1.0, readbacks=(
            ("VOLT:DC:NPLC?", 1.0), ("VOLT:DC:RANG?", 10.0),
            ("VOLT:DC:RANG:AUTO?", False), ("TRIG:SOUR?", "IMM"),
            ("TRIG:COUN?", 1), ("SAMP:COUN?", 1), ("SAMP:SOUR?", "IMM")))
        self.dmm_state["dmm_voltage"] = {"mode": "DCV", "nplc": 1.0, "range_V": 10.0,
                                         "basis": "readback"}

    def no_energy(self) -> None:
        for name in CLASSES:
            self.require_off()
            self.case_call(name, "clear_status")
            self.case_call(name, "check_error")
            self.case_call(name, "idn")
            self.case_call(name, "safe_off")
        psu_modes = (("set_mode_independent", "OFF"),
                     ("set_mode_series", "SER"), ("set_mode_parallel", "PAR"))
        for method, expected in psu_modes:
            self.require_off()
            self.case_call("psu", method)
            for iteration in range(self.args.repeat):
                self.iteration = iteration
                if self.invoke("psu", "query_combination_mode") != expected:
                    raise InventoryError("Unexpected PSU combination mode")
            self.case_call("psu", "set_output_voltage", self.args.voltage_v,
                           readbacks=(("SOUR:VOLT?", self.args.voltage_v),))
            self.case_call("psu", "set_output_current", self.args.current_a,
                           readbacks=(("SOUR:CURR?", self.args.current_a),))
            self.case_call("psu", "measure_output_voltage")
            self.case_call("psu", "measure_output_current")
        self.case_call("psu", "set_mode_independent")
        self.case_call("psu", "output_off")
        self.case_call("psu", "read_output_state")
        self.case_call("psu", "read_regulation_state")
        self.case_call("load", "set_mode_cc", readbacks=(("SOUR:FUNC?", "CURR"),))
        self.case_call("load", "set_current", self.args.current_a, readbacks=(
            ("SOUR:CURR?", self.args.current_a), ("CURR:RANG?", self.args.current_a)))
        # R15-B1: configuration readback, not proof of the physical LOW/HIGH range.
        self.case = "load.trace_storage_disable"
        self.sessions["load"].io("query", "TRAC:FEED:CONT NEV;*OPC?", "preparation")
        self.expect("load", "TRAC:FEED:CONT?", "NEV")
        self.not_executed.extend({
            "instrument": "load", "method": method, "status": "not_executed",
            "reason": reason,
        } for method, reason in (
            ("configure_trace", ("R15-B1/F8: repeated or inherited NEXT configuration conflict; "
             "production fix is a separate batch")),
            ("fetch_trace", "R15-B1/F2: no validated capture in the no-energy fixture"),
        ))
        self.require_off()
        for method in ("read_input_state", "read_questionable_status",
                       "measure_voltage", "measure_current", "measure_power"):
            self.case_call("load", method)
        self.configure_dcv()
        self.case_call("dmm_voltage", "set_nplc", 1.0, readbacks=(("VOLT:DC:NPLC?", 1.0),))
        self.case_call("dmm_voltage", "autorange_off", readbacks=(("VOLT:DC:RANG:AUTO?", False),))
        for method in ("get_nplc", "query_sample_timer_capability",
                       "abort_measurement"):
            self.case_call("dmm_voltage", method)
        self.case = "dmm_voltage.open_input_read"
        for iteration in range(self.args.repeat):
            self.iteration = iteration
            self.diagnostic_read("dmm_voltage", "voltage")
        minimum = finite(self.invoke("dmm_voltage", "query_sample_timer_capability"))
        interval = max(0.41, minimum * 1.1)
        self.case_call("dmm_voltage", "configure_timed_acquisition", 2, interval, readbacks=(
            ("SAMP:COUN?", 2), ("SAMP:SOUR?", "TIM"), ("SAMP:TIM?", interval)))
        self.case_call("dmm_voltage", "get_sample_timer_s")
        self.case = "dmm_voltage.timed_acquisition"
        for iteration in range(self.args.repeat):
            self.iteration = iteration
            self.invoke("dmm_voltage", "arm_timed_acquisition")
            raw = self.invoke("dmm_voltage", "fetch_timed_acquisition")
            samples = raw.split(",")  # Floating input: record values without acceptance limits.
            self.measurements.append({
                "instrument": "dmm_voltage", "case": self.case, "iteration": iteration,
                "utc": utc(), "status": "observed_unassessed", "samples_raw": samples,
            })
            if len(samples) != 2:
                raise InventoryError("Unexpected DMM sample count")
        self.case_call("dmm_voltage", "reset_to_single_sample", readbacks=(
            ("SAMP:COUN?", 1), ("SAMP:SOUR?", "IMM")))
        self.require_off()
        self.ntc_case("dmm_temperature.ntc_before_pt100")
        # Set before sending configuration: an uncertain result may still excite the NTC.
        self.pt100_attempted = True
        self.dmm_state["dmm_temperature"] = {"mode": "PT100_with_NTC_fixture", "nplc": 10.0}
        self.case_call("dmm_temperature", "configure_temp_4wire_pt100", 10.0, readbacks=(
            ("SENS:TEMP:NPLC?", 10.0), ("SENS:TEMP:TRAN:FRTD:RES?", 100.0),
            ("UNIT:TEMP?", "C")))
        self.expect_configuration("TEMP")
        self.case = "dmm_temperature.pt100_with_ntc_fixture"
        for iteration in range(self.args.repeat):
            self.iteration = iteration
            self.diagnostic_read("dmm_temperature", "pt100")
        self.require_off()
        self.ntc_case("dmm_temperature.ntc_read")

    def ntc_case(self, label: str) -> None:
        self.case_call("dmm_temperature", "configure_temp_2wire_ntc_thermistor",
                       *NTC_PARAMETERS, readbacks=(("RES:NPLC?", 10.0),
                                                  ("RES:RANG:AUTO?", True)))
        self.expect_configuration("RES")
        self.dmm_state["dmm_temperature"] = {"mode": "NTC_2W_BETA", "nplc": 10.0,
                                             "r25_ohm": 10000.0, "beta_K": 3950.0}
        self.case = label
        for iteration in range(self.args.repeat):
            self.iteration = iteration
            self.diagnostic_read("dmm_temperature", "ntc")

    def expect_configuration(self, function: str) -> str:
        # Truevolt manual 34460-90901 Ed.7, p.244: quoted short function + settings.
        raw = self.query("dmm_temperature", "CONF?")
        tokens = raw.strip().strip('"').upper().split()
        if not tokens or tokens[0] != function:
            self.halted = True
            raise InventoryError(f"Thermal CONF?: {raw!r}, expected {function}")
        return raw

    def diagnostic_read(self, name: str, mode: str) -> None:
        first = len(self.records)
        value: float | None = None
        invalid: str | None = None
        try:
            value = self.invoke(name, "read_voltage" if mode == "voltage" else "read_temperature")
        except device_dmm.InstrumentInvalidReading as exc:
            if self.halted:
                raise  # I/O/SCPI errors must never be classified as fixture invalidity.
            invalid = f"{type(exc).__name__}: {exc}"
        reads = [row for row in self.records[first:]
                 if row["instrument"] == name and row["kind"] == "query"
                 and row["role"] == "driver" and row["message"] == "READ?"]
        if len(reads) != 1 or reads[0]["exception"] is not None:
            raise InventoryError("Diagnostic reading requires exactly one successful READ?")
        raw = reads[0]["response_raw"]
        row: dict[str, Any] = {
            "instrument": name, "case": self.case, "iteration": self.iteration,
            "utc": utc(), "raw_record_id": reads[0]["id"], "reading_raw": raw,
            "driver_value": value, "driver_exception": invalid,
        }
        self.measurements.append(row)
        if mode == "pt100":
            row["status"] = "expected_invalid_with_ntc_fixture"
            row["temperature_c"] = None
        elif mode == "voltage":
            row["status"] = "observed_unassessed"
        else:
            row["metrologically_assessable"] = not self.pt100_attempted
            row["metrology_limitation"] = NTC_AFTER_PT100 if self.pt100_attempted else None
            row.update(temperature_c=value, raw_resistance_ohm=None,
                       plausible=None, plausible_range_c=[self.args.ntc_min_c, self.args.ntc_max_c])
            try:
                resistance = finite(raw)
                if resistance <= 0:
                    raise InventoryError("Non-positive NTC resistance")
                row["raw_resistance_ohm"] = resistance
            except (ValueError, InventoryError):
                invalid = invalid or "Invalid NTC resistance"
            plausible = value is not None and self.args.ntc_min_c <= value <= self.args.ntc_max_c
            row["plausible"] = plausible if value is not None else None
            row["status"] = ("invalid_reading" if invalid else
                             "plausible" if plausible else "outside_plausibility_range")
            if invalid or not plausible:
                warning = f"NTC diagnostic warning: {row['status']}; C={value!r}; ohm={raw!r}"
                self.warnings.append(warning)
                logger.warning(warning)

    def restore_psu(self) -> None:
        # R15-B1: mode restoration needs a successful OFF and exact False readback.
        if "psu" not in self.verified:
            return
        if not self.psu_off_verified:
            self.psu_restore = {"status": "skipped_off_unverified", "utc": utc()}
            self.cleanup_failures.append("PSU mode restore skipped: OFF unverified")
            return
        self.case = "final_restore_psu"
        previous = self.cleaning
        self.cleaning = True
        self.psu_restore = {"status": "attempting", "utc": utc()}
        first = len(self.records)
        try:
            self.invoke("psu", "set_mode_independent")
            mode = self.invoke("psu", "query_combination_mode")
            if mode != "OFF":
                raise InventoryError(f"PSU restore mode mismatch: {mode!r}")
            if any(r["exception"] or r["scpi_error"] for r in self.records[first:]):
                raise InventoryError("PSU restore I/O/SCPI error")
            self.psu_restore.update(status="verified", mode=mode)
        except BaseException as exc:
            logger.exception("PSU independent-mode restore failed")
            self.psu_restore.update(status="failed", exception=f"{type(exc).__name__}: {exc}")
            self.cleanup_failures.append(f"PSU mode restore: {type(exc).__name__}: {exc}")
        finally:
            self.cleaning = previous

    def restore_ntc(self) -> None:
        # One attempt, after BOTH safe-off attempts; never retry an uncertain command.
        if not self.thermal_restore_allowed:
            return
        self.case = "final_restore_ntc"
        self.cleaning = True
        self.thermal_restore = {"status": "attempting", "utc": utc()}
        try:
            self.invoke("dmm_temperature", "configure_temp_2wire_ntc_thermistor", *NTC_PARAMETERS)
            conf = self.expect_configuration("RES")
            nplc = self.expect("dmm_temperature", "RES:NPLC?", 10.0)
            autorange = self.expect("dmm_temperature", "RES:RANG:AUTO?", True)
            self.dmm_state["dmm_temperature"] = {"mode": "NTC_2W_BETA", "nplc": 10.0,
                                                 "r25_ohm": 10000.0, "beta_K": 3950.0}
            self.thermal_restore.update(status="verified", conf_raw=conf, nplc_raw=nplc,
                                        autorange_raw=autorange)
        except BaseException as exc:
            logger.exception("Thermal NTC restore failed")
            self.thermal_restore.update(status="failed", exception=f"{type(exc).__name__}: {exc}")
            self.cleanup_failures.append(f"Thermal NTC restore: {type(exc).__name__}: {exc}")
        finally:
            self.cleaning = False

    def prepare_energy(self) -> None:
        self.require_off()
        self.ask("WIRING_" + self.args.psu_mode.upper(),
                 "Verify isolated fixture and the selected PSU channel wiring; no battery.")
        self.case_call("psu", "set_mode_" + self.args.psu_mode)
        expected = {"independent": "OFF", "series": "SER", "parallel": "PAR"}[self.args.psu_mode]
        if self.invoke("psu", "query_combination_mode") != expected:
            raise InventoryError("PSU mode readback mismatch")
        self.case_call("psu", "set_output_voltage", self.args.voltage_v)
        self.case_call("psu", "set_output_current", self.args.current_a)
        # Production setters select CH1 only. Never assume safe inherited CH2 values.
        for channel in ("CH1", "CH2"):
            for quantity, maximum in (("VOLT", self.args.voltage_v),
                                      ("CURR", self.args.current_a)):
                raw = self.query("psu", f"INST:SEL {channel};:SOUR:{quantity}?")
                value = finite(raw)
                if channel == "CH2" and quantity == "VOLT":
                    self.ch2_voltage_v = value
                if (channel == "CH1" or self.args.psu_mode != "independent") and not (
                        0 <= value <= maximum * (1 + 1e-5)):
                    raise InventoryError(f"Unsafe inherited {channel} {quantity}: {raw!r}")
        self.invoke("psu", "_select_ch1")
        self.case_call("load", "set_mode_cc")
        self.case_call("load", "set_current", self.args.current_a,
                       readbacks=(("SOUR:CURR?", self.args.current_a),
                                  ("CURR:RANG?", self.args.current_a)))
        self.configure_dcv()

    def energized(self) -> None:
        self.prepare_energy()
        for name, on in (("psu", "output_on"), ("load", "input_on")):
            self.require_off()
            self.ask("CONNECT_" + name.upper(),
                     "Isolate the other source. Load test needs an external current-limited source;"
                     " the PSU tested here must remain OFF.")
            self.require_off()
            if name == "load":
                voltage = finite(self.invoke("load", "measure_voltage"))
                if not 0 <= voltage <= self.args.voltage_v:
                    raise InventoryError("External source voltage exceeds declared fixture voltage")
            self.case = f"{name}.{on}"
            try:
                started = time.perf_counter()
                first = len(self.records)
                self.invoke(name, on)
                started = self.command_start(name, first, started, on=True)
                self.wait_state(name, True, started)
            finally:
                self.require_off()
        if self.args.measure_settle:
            self.require_off()
            self.ask("DMM_PSU_DCV", "Connect DMM to isolated PSU output. Load disconnected.")
            self.invoke("psu", "set_output_voltage", self.args.voltage_v / 2)
            self.case = "psu.active_voltage_setpoint"
            try:
                on_started = time.perf_counter()
                first = len(self.records)
                self.invoke("psu", "output_on")
                on_started = self.command_start("psu", first, on_started, on=True)
                self.wait_state("psu", True, on_started)
                first = len(self.records)
                self.invoke("psu", "set_output_voltage", self.args.voltage_v)
                started = next(float(r["start_perf_s"]) for r in self.records[first:]
                               if r["kind"] == "query" and r["role"] == "driver")
                target = self.args.voltage_v  # R15-B1: commanded combined voltage.
                deadline = started + self.args.poll_max_s
                while True:
                    first = len(self.records)
                    value = finite(self.invoke("dmm_voltage", "read_voltage"))
                    observed = next(r["start_perf_s"] + r["elapsed_s"]
                                    for r in self.records[first:]
                                    if r["kind"] == "query" and r["role"] == "driver")
                    now = time.perf_counter()
                    if now > deadline:
                        raise InventoryError("DMM settling deadline exceeded")
                    if abs(value - target) <= self.args.tolerance_v:
                        self.transitions.append({
                            "instrument": "dmm_voltage", "case": self.case, "utc": utc(),
                            "elapsed_s": observed - started, "measured_v": value,
                            "target_v": target, "tolerance_v": self.args.tolerance_v,
                            "limitation": LIMITATION,
                        })
                        break
                    time.sleep(min(self.args.poll_interval_s, deadline - now))
            finally:
                self.require_off()

    def run(self) -> dict[str, Any]:
        try:
            validate(self.args)
            resolve_resources(self.args)
            if self.args.phase == "energized":
                self.ask(TOKEN["energized"],
                         "Confirm DUT/battery disconnected and isolated lab arrangement.")
                self.confirmed = True
            self.open()
            if self.args.phase == "no-energy":
                self.no_energy()
            else:
                self.energized()
        except BaseException as exc:
            logger.exception("HIL stopped; safe-off follows")
            self.halted = True
            chain: list[str] = []
            current: BaseException | None = exc
            while current is not None:
                chain.append(f"{type(current).__name__}: {current}")
                current = current.__cause__
            self.failure = " <- ".join(chain)
        finally:
            self.case = "final_safe_off"
            self.cleanup_failures.extend(self.off())
            self.restore_psu()
            self.restore_ntc()
            self.cleaning = True
            for name in CLASSES:
                if name in self.sessions:
                    try:
                        if name in self.verified:
                            self.invoke(name, "disconnect")
                        else:
                            self.sessions[name].close()
                    except BaseException as exc:
                        logger.exception("Disconnect failed: %s", name)
                        self.cleanup_failures.append(f"{name} disconnect: {exc}")
            self.cleaning = False
        return self.report()

    def report(self) -> dict[str, Any]:
        try:
            visa_version = version("pyvisa")
        except PackageNotFoundError:
            visa_version = "not_installed"
        return {
            "schema": 2, "utc": utc(), "status": (
                "candidate" if self.failure is None and not self.cleanup_failures else "failed"),
            "phase": self.args.phase, "arguments": vars(self.args),
            "identities": self.identities, "failure": self.failure,
            "fixture_state": self.args.fixture_state,
            "measurements": self.measurements, "warnings": self.warnings,
            "thermal_restore": self.thermal_restore, "psu_restore": self.psu_restore,
            "safe_off": self.safe_off_status,
            "readback_policy": "documented mandatory queries only; all SCPI errors are fatal",
            "ntc_after_pt100_limitation": NTC_AFTER_PT100 if self.pt100_attempted else None,
            "ntc_parameters": {"r25_ohm": 10000.0, "beta_K": 3950.0, "nplc": 10.0},
            "cleanup_failures": self.cleanup_failures, "limitation": LIMITATION,
            "environment": {"python": platform.python_version(), "pyvisa": visa_version,
                            "interfaces": {n: interface(getattr(self.args, n)) for n in CLASSES},
                            "timeout_ms": self.args.timeout_ms},
            "source_inventory": self.inventory, "driver_calls": self.calls,
            "not_executed": self.not_executed,
            "load_range_readback_limitation": (
                "Programmed-value configuration check; 0.01 A observed on 2380 fw 1.00-1.00. "
                "Physical LOW/HIGH range and other values/firmware remain unverified."),
            "records": self.records, "statistics": summary(self.records),
            "transitions": self.transitions,
            "coverage_limit": "source_inventory is exhaustive AST metadata, not observed coverage."
                              " A/B, physical mode/fixtures and firmware fallback need separate evidence.",
        }


def interface(address: str | None) -> str:
    prefix = (address or "").split("::", 1)[0].upper()
    return next((label for start, label in
                 (("USB", "USB"), ("TCPIP", "LAN"), ("GPIB", "GPIB"), ("ASRL", "SERIAL"))
                 if prefix.startswith(start)), "UNKNOWN")


def resolve_resources(args: argparse.Namespace) -> None:
    missing = [name for name in CLASSES if not getattr(args, name)]
    if missing:
        import yaml  # type: ignore[import-untyped]

        try:
            config = yaml.safe_load(LOCAL_CONFIG.read_text(encoding="utf-8"))
            instruments = config["instruments"]
            for name in missing:
                address = instruments[name]["resource"]
                if not isinstance(address, str) or not address.strip():
                    raise ValueError(f"Empty resource for {name}")
                setattr(args, name, address)
        except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise InventoryError(f"Cannot resolve resources from {LOCAL_CONFIG}: {exc}") from exc


def validate(args: argparse.Namespace) -> None:
    # Authorization checks precede ResourceManager construction and all session I/O.
    if args.phase == "no-energy" and args.fixture_state != FIXTURE_STATE:
        raise InventoryError(f"--fixture-state must equal {FIXTURE_STATE!r}")
    if args.phase == "energized":
        if not sys.stdin.isatty():
            raise InventoryError("Energized phase requires interactive stdin (isatty)")
        if not args.i_confirm_energize:
            raise InventoryError("--i-confirm-energize is required")
    if (not math.isfinite(args.ntc_min_c) or not math.isfinite(args.ntc_max_c)
            or args.ntc_min_c >= args.ntc_max_c):
        raise InventoryError("Invalid NTC plausibility range")
    if args.phase not in TOKEN or args.psu_mode not in ("independent", "series", "parallel"):
        raise InventoryError("Invalid phase/mode")
    if not 1 <= args.repeat <= 100 or not 1 <= args.timeout_ms <= 60000:
        raise InventoryError("Invalid repeat/timeout")
    for value, low, high in ((args.voltage_v, 0.01, 2.0), (args.current_a, 0.001, 0.1),
                             (args.poll_max_s, 0.05, 10.0),
                             (args.poll_interval_s, 0.001, 0.1),
                             (args.tolerance_v, 0.0001, 0.1)):
        if not math.isfinite(value) or not low <= value <= high:
            raise InventoryError("Argument outside isolated-fixture HIL bounds")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--phase", required=True, choices=tuple(TOKEN))
    for name in CLASSES:
        result.add_argument("--" + name.replace("_", "-"),
                            help="VISA resource; default: instruments.<role>.resource in local_config.yaml")
    result.add_argument("--fixture-state", help=f"A requires the exact fixed-fixture token: {FIXTURE_STATE}")
    result.add_argument("--ntc-min-c", type=float, default=5.0)
    result.add_argument("--ntc-max-c", type=float, default=45.0)
    result.add_argument("--output-dir", required=True)
    result.add_argument("--i-confirm-energize", action="store_true")
    result.add_argument("--psu-mode", choices=("independent", "series", "parallel"),
                        default="independent")
    result.add_argument("--repeat", type=int, default=5)
    result.add_argument("--timeout-ms", type=int, default=2000)
    result.add_argument("--voltage-v", type=float, default=1.0)
    result.add_argument("--current-a", type=float, default=0.01)
    result.add_argument("--poll-max-s", type=float, default=3.0)
    result.add_argument("--poll-interval-s", type=float, default=0.01)
    result.add_argument("--measure-settle", action="store_true")
    result.add_argument("--tolerance-v", type=float, default=0.02)
    return result


def export(report: dict[str, Any], output_dir: str) -> None:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(timezone.utc).strftime("scpi_%Y%m%dT%H%M%S_%fZ")
    with (path / (stem + ".json")).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    for suffix, rows in (("commands", report["records"]),
                         ("statistics", report["statistics"]),
                         ("transitions", report["transitions"]),
                         ("measurements", report["measurements"])):
        with (path / (stem + "_" + suffix + ".csv")).open(
                "x", encoding="utf-8", newline="") as stream:
            if rows:
                fields = sorted({key for row in rows for key in row})
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: json.dumps(value, ensure_ascii=False)
                                     if isinstance(value, (dict, list, tuple)) else value
                                     for key, value in row.items()})


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    validate(args)
    resolve_resources(args)
    # No ResourceManager/session is created before phase/fixture/TTY/flag validation.
    import pyvisa

    rm = pyvisa.ResourceManager()
    try:
        report = Runner(args, rm).run()
    finally:
        try:
            rm.close()
        except BaseException as exc:
            if "report" in locals():
                report["cleanup_failures"].append(f"ResourceManager close: {exc}")
                report["status"] = "failed"
            else:
                raise
    export(report, args.output_dir)
    print(json.dumps({"status": report["status"], "failure": report["failure"],
                      "cleanup_failures": report["cleanup_failures"]}))
    return 0 if report["status"] == "candidate" else 1


if __name__ == "__main__":
    raise SystemExit(main())


