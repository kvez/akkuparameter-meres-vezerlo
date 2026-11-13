"""R13: in-memory fake VISA only; never opens a port or creates files."""
from __future__ import annotations

import ast
import inspect
import json
import unittest
from contextlib import ExitStack
from itertools import count
from pathlib import Path
from typing import Any
from unittest.mock import patch

from Prog.tools import hil_load_energized_probe as load_probe
from Prog.tools import hil_psu_series_energized_probe as psu_probe
from Prog.tools import hil_scpi_command_inventory as hil

# Independent, frozen acceptance oracle (DFAM-006): no production guard imports.
# Full A stream, including connect, readbacks, restore and disconnect, is checked.
FORBIDDEN_A_UNITS = frozenset({
    "OUTP ON", "OUTP 1", "OUTP:STAT ON", "OUTP:STAT 1",
    "OUTP:STATE ON", "OUTP:STATE 1", "OUTP:ENAB ON", "OUTP:ENAB 1",
    "OUTPUT ON", "OUTPUT 1", "OUTPUT:STATE ON", "OUTPUT:STATE 1",
    "OUTPUT:ENABLE ON", "OUTPUT:ENABLE 1",
    "INP ON", "INP 1", "INPUT ON", "INPUT 1",
    "INP:STAT ON", "INPUT:STATE ON",
    "TRAN ON", "TRAN 1", "TRANSIENT ON", "TRANSIENT 1",
    "FORC:TRIG", "FORCE:TRIGGER", "*TRG", "*RST", "SYST:PRES", "SYSTEM:PRESET",
})
FORBIDDEN_A_METHODS = {
    "output_on", "input_on", "configure_transient_pulse", "fire_transient_pulse",
    "begin_pulse", "fire_pulse", "reset",
}


def forbidden_a(message: str) -> bool:
    units = [" ".join(unit.strip().lstrip(":").upper().split())
             for unit in message.split(";")]
    # Explicit source forms; never derived from production driver strings.
    banned = FORBIDDEN_A_UNITS | {
        prefix + unit for prefix in ("SOUR:", "SOURCE:")
        for unit in FORBIDDEN_A_UNITS if not unit.startswith("*")
    }
    return any(unit in banned for unit in units)


class FakeResource:
    def __init__(self, name: str, manager: FakeRM) -> None:
        self.name, self.manager = name, manager
        self.timeout = 2000
        self.selected = "CH1"
        self.output = False
        self.input_on = False
        self.closed = False
        self.identity = {
            "psu": "KEITHLEY INSTRUMENTS,2220-30-1,TEST,1.15-1.05",
            "load": "Keithley,2380-120-60,TEST,1.00-1.00",
            "dmm_voltage": "Keysight Technologies,34465A,VOLT_SERIAL,A.02.17",
            "dmm_temperature": "Keysight Technologies,34465A,TEMP_SERIAL,A.02.17",
        }[name]
        self.values: dict[str, str] = {
            "SOUR:OUTP:SER": "0", "SOUR:OUTP:PAR": "0",
            "OUTP:TIM:STAT": "0", "INP:SHOR": "0", "INP:TIM": "0",
            "VOLT:ON": "0", "SOUR:FUNC:MODE": "FIX",
            "VOLT:DC:NPLC": "1", "VOLT:DC:RANG": "10",
            "VOLT:DC:RANG:AUTO": "0", "SAMP:COUN": "1",
            "SAMP:SOUR": "IMM", "TRIG:SOUR": "IMM", "TRIG:COUN": "1",
            "SAMP:TIM": "0.41", "TRAN": "0",
        }
        if name == "load":  # R15-B1: HIL 2026-11-06, fw 1.00-1.00, 0.01 A.
            self.values.update({"CURR:RANG": "0.01", "TRAC:FEED:CONT": "NEXT"})
        self.channel_values = {
            "CH1": {"SOUR:VOLT": "1", "SOUR:CURR": "0.01", "OUTP:ENAB": "0"},
            "CH2": {"SOUR:VOLT": "1", "SOUR:CURR": "0.01", "OUTP:ENAB": "0"},
        }
        self.ntc_resistance = "10000"
        self.pt100_reading = "9.9E37"
        self.voltage_reading = "1"
        self.mode = "DCV"
        self.error = "0,No error"

    def clear(self) -> None:
        self.manager.actions.append((self.name, "clear", ""))

    def close(self) -> None:
        self.closed = True
        self.manager.actions.append((self.name, "close", ""))

    def execute(self, kind: str, message: str) -> str:
        self.manager.actions.append((self.name, kind, message))
        result = ""
        for unit in message.split(";"):
            unit = unit.lstrip(":").strip()
            if unit == "*IDN?":
                result = self.identity
            elif unit == "*OPC?":
                result = "1"
            elif unit == "SYST:ERR?":
                result, self.error = self.error, "0,No error"
            elif unit == "OUTP?":
                result = str(int(self.output))
            elif unit == "INP?":
                result = str(int(self.input_on))
            elif unit == "READ?":
                result = (self.ntc_resistance if self.mode == "NTC" else
                          self.pt100_reading if self.mode == "RTD" else self.voltage_reading)
            elif unit == "CONF?":
                result = {"NTC": '"RES +1.00000000E+04,+1.00000000E-02"',
                          "RTD": '"TEMP FRTD,85,1,+1.00000000E-03"',
                          "DCV": '"VOLT +1.00000000E+01,+3.00000000E-06"'}[self.mode]
            elif unit == "FETC?":
                result = "1,1"
            elif unit == "TRAC:DATA?":
                self.error = '-230,"Data Corrupt or Stale"'
                raise TimeoutError("HIL: no validated trace capture")
            elif unit == "SAMP:TIM? MIN":
                result = "0.1"
            elif (unit in ("STAT:QUES:COND?", "STAT:OPER:INST:ISUM1:COND?")
                  or unit.startswith("MEAS:")):
                result = "0"
            elif unit == "*RST":
                raise AssertionError("Reset forbidden in fake HIL session")
            elif unit in ("*CLS", "ABOR", "INIT", "FORC:TRIG", "TRAC:CLE"):
                pass
            elif unit.startswith("CONF:"):
                self.mode = "RTD" if "TEMP" in unit else "NTC" if unit == "CONF:RES" else "DCV"
            elif unit == "OUTP ON":
                self.output = True
            elif unit == "OUTP OFF":
                self.output = False
            elif unit == "INP ON":
                self.input_on = True
            elif unit == "INP OFF":
                self.input_on = False
            elif unit.startswith("INST:SEL "):
                self.selected = unit.split()[-1]
            elif unit.endswith("?"):
                if self.name == "psu" and unit in ("CURR:RANG?", "SOUR:CURR:RANG?"):
                    raise AssertionError("2220 has no current-range query")
                key = unit[:-1]
                if key in self.channel_values[self.selected]:
                    result = self.channel_values[self.selected][key]
                elif key in self.values:
                    result = self.values[key]
                else:
                    raise AssertionError(f"Unmodelled query: {self.name} {unit}")
            elif " " in unit:
                key, value = unit.split(" ", 1)
                value = {"ON": "1", "OFF": "0"}.get(value, value)
                if self.name == "psu" and key == "SOUR:OUTP:SER" and value == "1":
                    self.values["SOUR:OUTP:PAR"] = "0"
                    for channel in self.channel_values.values():
                        channel["OUTP:ENAB"] = "1"
                if self.name == "psu" and key == "OUTP:ENAB" and value == "0" and self.values["SOUR:OUTP:SER"] == "1":
                    # R16 HIL: a SERIES megszűnik, de az enable változatlan.
                    self.values["SOUR:OUTP:SER"] = "0"
                elif key in self.channel_values[self.selected]:
                    self.channel_values[self.selected][key] = value
                else:
                    if key == "TRAC:POIN" and self.values.get("TRAC:FEED:CONT") == "NEXT":
                        self.error = '-221,"Settings conflict"'
                    else:
                        self.values[key] = {"FIX": "FIXED", "CURR": "CURRENT",
                                            "VOLT": "VOLTAGE", "NEV": "NEVER"}.get(value, value)
            else:
                raise AssertionError(f"Unmodelled command: {self.name} {unit}")
        psu = self.manager.resources.get("psu")
        load = self.manager.resources.get("load")
        if psu is not None and load is not None and psu.output and load.input_on:
            raise AssertionError("PSU and Load simultaneously active")
        if self.manager.error_on == (self.name, message):
            self.manager.error_on = None
            self.error = '-100,"Command error"'
        if self.manager.fail_on == (self.name, message):
            self.manager.fail_on = None
            raise self.manager.exception("uncertain I/O after command applied")
        return result

    def write(self, message: str) -> int:
        self.execute("write", message)
        return len(message)

    def query(self, message: str) -> str:
        return self.execute("query", message)


class FakeRM:
    def __init__(self) -> None:
        self.actions: list[tuple[str, str, str]] = []
        self.resources: dict[str, FakeResource] = {}
        self.fail_on: tuple[str, str] | None = None
        self.error_on: tuple[str, str] | None = None
        self.exception: type[BaseException] = TimeoutError
        self.opened: list[str] = []
        for name in ("psu", "load", "dmm_voltage", "dmm_temperature"):
            self.resources[name] = FakeResource(name, self)

    def open_resource(self, name: str) -> FakeResource:
        self.opened.append(name)
        if name not in self.resources:
            raise AssertionError("A real resource was requested")
        return self.resources[name]


def args(phase: str = "no-energy", *extra: str) -> Any:
    return hil.parser().parse_args([
        "--phase", phase, "--load", "load", "--psu", "psu",
        "--dmm-voltage", "dmm_voltage", "--dmm-temperature", "dmm_temperature",
        "--fixture-state", hil.FIXTURE_STATE,
        "--output-dir", "NEVER_WRITTEN", "--repeat", "2", *extra,
    ])


def confirm(prompt: str) -> str:
    # Simulates typed input at each runtime gate, never hardware_wiring_confirmed=True.
    return prompt.rsplit("Type ", 1)[1].split(":", 1)[0]


class TestHilScpiCommandInventory(unittest.TestCase):
    def setUp(self) -> None:
        tty = patch.object(hil.sys.stdin, "isatty", return_value=True)
        tty.start()
        self.addCleanup(tty.stop)
        guard = patch("pyvisa.ResourceManager", side_effect=AssertionError("Real VISA forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def run_case(self, phase: str = "no-energy", *extra: str,
                 rm: FakeRM | None = None) -> tuple[dict[str, Any], FakeRM]:
        manager = rm if rm is not None else FakeRM()
        report = hil.Runner(args(phase, *extra), manager, confirm).run()
        return report, manager

    def assert_off(self, manager: FakeRM) -> None:
        self.assertFalse(manager.resources["load"].input_on)
        self.assertFalse(manager.resources["psu"].output)

    def test_a_has_no_on_and_serializable_complete_records(self) -> None:
        report, manager = self.run_case()
        self.assertEqual(report["status"], "candidate", report["failure"])
        self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))
        self.assertFalse(any(c["method"] in FORBIDDEN_A_METHODS for c in report["driver_calls"]))
        self.assertFalse(any(n == "psu" and "CURR:RANG?" in m for n, _, m in manager.actions))
        self.assert_off(manager)
        self.assertTrue(all(r.closed for r in manager.resources.values()))
        json.dumps(report, allow_nan=False)
        for row in report["records"]:
            self.assertGreaterEqual(row["elapsed_s"], 0)
            self.assertIn("+00:00", row["utc"])
            if row["kind"] == "query":
                self.assertEqual(row["response_length"], len(row["response_raw"]))
            if row["kind"] in ("write", "query") and row["message"] != "SYST:ERR?":
                self.assertIsNotNone(row["error_record_id"])
        self.assertTrue(any(s["n"] >= 2 for s in report["statistics"]))

    def test_b_flag_and_typed_confirmation_both_required(self) -> None:
        manager = FakeRM()
        report = hil.Runner(args("energized"), manager, confirm).run()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(manager.actions, [])
        for answer in ("", "yes", "DUT_DISCONNECTED"):
            manager = FakeRM()
            def reply(_: str, response: str = answer) -> str:
                return response
            report = hil.Runner(args("energized", "--i-confirm-energize"),
                                manager, reply).run()
            self.assertEqual(report["status"], "failed")
            self.assertEqual(manager.actions, [])

    def test_b_on_once_then_off_and_separate_settle_measurement(self) -> None:
        report, manager = self.run_case(
            "energized", "--i-confirm-energize", "--measure-settle")
        self.assertEqual(report["status"], "candidate", report["failure"])
        self.assert_off(manager)
        load_ons = [m for n, _, m in manager.actions if n == "load" and m == "INP ON"]
        self.assertEqual(load_ons, ["INP ON"])
        psu_ons = [m for n, _, m in manager.actions if n == "psu" and m == "OUTP ON;*OPC?"]
        self.assertEqual(psu_ons, ["OUTP ON;*OPC?", "OUTP ON;*OPC?"])
        self.assertTrue(any(t["case"] == "psu.active_voltage_setpoint"
                            for t in report["transitions"]))

    def test_error_and_keyboardinterrupt_always_attempt_both_offs(self) -> None:
        for exception in (TimeoutError, KeyboardInterrupt):
            with self.subTest(exception=exception):
                manager = FakeRM()
                manager.fail_on = ("psu", "OUTP ON;*OPC?")
                manager.exception = exception
                report, manager = self.run_case(
                    "energized", "--i-confirm-energize", rm=manager)
                self.assertEqual(report["status"], "failed")
                self.assert_off(manager)
                messages = [m for _, _, m in manager.actions]
                self.assertEqual(messages.count("OUTP ON;*OPC?"), 1)
                index = messages.index("OUTP ON;*OPC?")
                tail = messages[index + 1:]
                self.assertIn("INP OFF;*OPC?", tail)
                self.assertIn("OUTP OFF;*OPC?", tail)
                self.assertFalse(any(forbidden_a(m) for m in tail))

    def test_off_failure_does_not_block_other_off(self) -> None:
        manager = FakeRM()
        manager.fail_on = ("load", "INP OFF;*OPC?")
        report, manager = self.run_case(rm=manager)
        self.assertEqual(report["status"], "failed")
        self.assertIn(("psu", "query", "OUTP OFF;*OPC?"), manager.actions)
        self.assert_off(manager)

    def test_scpi_error_stops_after_exact_message(self) -> None:
        manager = FakeRM()
        message = "CURR:RANG 0.01;:SOUR:CURR 0.01;*OPC?"
        manager.error_on = ("load", message)
        report, manager = self.run_case(rm=manager)
        self.assertEqual(report["status"], "failed")
        self.assertIn("SCPI error", report["failure"])
        self.assert_off(manager)
        self.assertEqual([m for _, _, m in manager.actions].count(message), 1)

    def test_identity_gate_checks_exact_model_including_dmm(self) -> None:
        for name, model in (("load", "2380-500-15"), ("psu", "2220-30-1-EXTRA"),
                            ("dmm_voltage", "34461A"), ("dmm_temperature", "34461A")):
            with self.subTest(name=name):
                manager = FakeRM()
                fields = manager.resources[name].identity.split(",")
                fields[1] = model
                manager.resources[name].identity = ",".join(fields)
                report, manager = self.run_case(rm=manager)
                self.assertEqual(report["status"], "failed")
                self.assertIn("identity mismatch", report["failure"])
                own = [m for n, kind, m in manager.actions
                       if n == name and kind in ("write", "query")]
                self.assertEqual(own, ["*IDN?", "SYST:ERR?"])
                self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))

    def test_b_rejects_unsafe_inherited_ch2_and_wiring_refusal(self) -> None:
        manager = FakeRM()
        manager.resources["psu"].channel_values["CH2"]["SOUR:VOLT"] = "30"
        report, manager = self.run_case(
            "energized", "--i-confirm-energize", "--psu-mode", "series", rm=manager)
        self.assertEqual(report["status"], "failed")
        self.assertIn("Unsafe inherited CH2", report["failure"])
        self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))
        manager = FakeRM()
        def refuse_wiring(prompt: str) -> str:
            return "no" if "WIRING_" in prompt else confirm(prompt)
        report = hil.Runner(args("energized", "--i-confirm-energize"),
                            manager, refuse_wiring).run()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))

    def test_b_all_modes_emit_exact_on_sequence(self) -> None:
        for mode in ("independent", "series", "parallel"):
            with self.subTest(mode=mode):
                report, manager = self.run_case(
                    "energized", "--i-confirm-energize", "--psu-mode", mode)
                self.assertEqual(report["status"], "candidate", report["failure"])
                observed = [(r["kind"], r["message"]) for r in report["records"]
                            if r["instrument"] == "psu" and ".output_on" in r["case"]
                            and r["role"] == "driver" and r["kind"] in ("write", "query")]
                mode_read = [("query", "SOUR:OUTP:SER?"), ("query", "SOUR:OUTP:PAR?")]
                expected = list(mode_read)
                if mode == "independent":
                    expected += [("query", "INST:SEL CH2;:OUTP:ENAB OFF;:INST:SEL CH1;*OPC?")]
                    expected += mode_read
                expected += [("query", "INST:SEL CH1;:OUTP:ENAB ON;*OPC?")]
                if mode != "independent":
                    expected += mode_read
                    expected += [("query", "INST:SEL CH2;:OUTP:ENAB ON;:INST:SEL CH1;*OPC?")]
                expected += mode_read
                expected += [("query", "OUTP ON;*OPC?"), ("query", "OUTP?")]
                expected += mode_read
                # Finally safe-off is logged in the same case.
                self.assertEqual(observed[:len(expected)], expected)
                self.assert_off(manager)

    def test_internal_fallback_branch_with_fake_resource_only(self) -> None:
        manager = FakeRM()
        raw = manager.resources["psu"]
        original = raw.query
        def unsupported_probe(message: str) -> str:
            if message == "*CLS;:OUTP OFF;:SYST:ERR?":
                raw.execute("query", message)
                return '-113,"Undefined header"'
            return original(message)
        raw.query = unsupported_probe  # type: ignore[method-assign]
        driver = hil.device_psu.Keithley2220PSU(max_retries=1)
        driver._resource = raw
        driver._detect_output_command()
        self.assertEqual(driver._output_cmd, "OUTP:ENAB")
        # Detection must not issue channel-scoped ENAB OFF before mode readback.
        self.assertEqual(
            [(kind, message) for _, kind, message in manager.actions
             if kind in ("write", "query")],
            [("query", "*CLS;:OUTP OFF;:SYST:ERR?")],
        )
        driver.all_outputs_off()
        self.assertFalse(driver.read_output_state())
        observed = [(kind, message) for _, kind, message in manager.actions
                    if kind in ("write", "query")]
        self.assertEqual(observed, [
            ("query", "*CLS;:OUTP OFF;:SYST:ERR?"),
            ("query", "OUTP OFF;*OPC?"),
            ("query", "SOUR:OUTP:SER?"), ("query", "SOUR:OUTP:PAR?"),
            ("query", "INST:SEL CH1;:OUTP:ENAB OFF;*OPC?"),
            ("query", "INST:SEL CH2;:OUTP:ENAB OFF;*OPC?"),
            ("query", "SOUR:OUTP:SER?"), ("query", "SOUR:OUTP:PAR?"),
            ("query", "INST:SEL CH1;:OUTP:ENAB?"),
            ("query", "INST:SEL CH2;:OUTP:ENAB?"), ("write", "INST:SEL CH1"),
        ])

    def test_poll_deadline_has_no_on_retry(self) -> None:
        manager = FakeRM()
        raw = manager.resources["load"]
        original = raw.query
        def never_reports_on(message: str) -> str:
            value = original(message)
            return "0" if message == "INP?" else value
        raw.query = never_reports_on  # type: ignore[method-assign]
        clock = count(0.0, 0.001)
        with patch.object(hil.time, "perf_counter", side_effect=lambda: next(clock)), \
                patch.object(hil.time, "sleep", return_value=None):
            report, manager = self.run_case(
                "energized", "--i-confirm-energize", rm=manager)
        self.assertEqual(report["status"], "failed")
        self.assertIn("state polling deadline exceeded", report["failure"])
        self.assertEqual([m for _, _, m in manager.actions].count("INP ON"), 1)
        self.assert_off(manager)

    def test_load_on_uses_inventory_poll_without_driver_ack(self) -> None:
        # A produkciós driver nyugtapollja nem előzheti meg a saját állapotpollt.
        with patch.object(hil.device_load.Keithley2380Load, "_wait_input_activation",
                          side_effect=AssertionError("Váratlan driver-nyugtapoll")) as driver_poll:
            report, manager = self.run_case("energized", "--i-confirm-energize")
        self.assertEqual(report["status"], "candidate", report["failure"])
        driver_poll.assert_not_called()
        self.assertTrue(any(t["instrument"] == "load" and t["target"] is True
                            for t in report["transitions"]))
        self.assertEqual([m for _, _, m in manager.actions].count("INP ON"), 1)
        self.assert_off(manager)

    def test_no_io_helpers_and_disconnected_state(self) -> None:
        manager = FakeRM()
        for name, cls in hil.CLASSES.items():
            driver = cls(max_retries=1)
            self.assertFalse(driver.is_connected())
            driver._resource = manager.resources[name]
            driver._connected = True
            self.assertTrue(driver.is_connected())
        dmm = hil.device_dmm.Keysight34465ADMM()
        dmm.set_io_timeout_ms(123)
        dmm._resource = manager.resources["dmm_voltage"]
        dmm.set_io_timeout_ms(321)
        self.assertEqual(manager.resources["dmm_voltage"].timeout, 321)
        self.assertEqual(manager.actions, [])

    def assert_ntc_restored(self, report: dict[str, Any], manager: FakeRM) -> None:
        self.assertEqual(report["thermal_restore"]["status"], "verified", report["cleanup_failures"])
        thermal = manager.resources["dmm_temperature"]
        self.assertEqual(thermal.mode, "NTC")
        self.assertEqual(thermal.values["RES:NPLC"], "10")
        final = [r for r in report["records"] if r["case"] == "final_safe_off"]
        load_off = next(r["id"] for r in final if r["instrument"] == "load"
                        and r["message"] == "INP OFF;*OPC?")
        psu_off = next(r["id"] for r in final if r["instrument"] == "psu"
                       and r["message"] == "OUTP OFF;*OPC?")
        restore = [r for r in report["records"] if r["case"] == "final_restore_ntc"]
        config = next(r["id"] for r in restore if "CONF:RES" in r["message"])
        self.assertLess(load_off, psu_off)
        self.assertLess(psu_off, config)
        self.assertTrue(any(r["message"] == "CONF?" for r in restore))
        self.assertTrue(any(r["message"] == "RES:NPLC?" for r in restore))
        self.assertTrue(all(r.closed for r in manager.resources.values()))

    def test_two_meters_route_commands_and_never_ask_in_a(self) -> None:
        manager = FakeRM()
        def forbidden_prompt(_: str) -> str:
            self.fail("A must not prompt or request fixture replacement")
        report = hil.Runner(args(), manager, forbidden_prompt).run()
        self.assertEqual(report["status"], "candidate", report["failure"])
        self.assertEqual(report["fixture_state"], hil.FIXTURE_STATE)
        self.assertNotEqual(report["identities"]["dmm_voltage"],
                            report["identities"]["dmm_temperature"])
        for name, _, message in manager.actions:
            if (name.startswith("dmm_") and
                    any(token in message for token in ("CONF:VOLT", "SAMP:", "TRIG:", "INIT", "FETC?"))):
                self.assertEqual(name, "dmm_voltage")
            if "CONF:TEMP" in message or "CONF:RES" in message:
                self.assertEqual(name, "dmm_temperature")
        self.assert_ntc_restored(report, manager)

    def test_duplicate_idn_and_address_gates_precede_configuration(self) -> None:
        manager = FakeRM()
        manager.resources["dmm_temperature"].identity = manager.resources["dmm_voltage"].identity
        report, manager = self.run_case(rm=manager)
        self.assertEqual(report["status"], "failed")
        self.assertIn("Duplicate IDN", report["failure"])
        self.assertEqual(report["thermal_restore"]["status"], "not_attempted_identity_gate")
        self.assertFalse(any("CONF:" in message for _, _, message in manager.actions))
        self.assert_off(manager)
        manager = FakeRM()
        report, manager = self.run_case("no-energy", "--dmm-temperature", "dmm_voltage", rm=manager)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(manager.opened, [])

    def test_ntc_raw_resistance_is_the_drivers_same_sample_and_plausible(self) -> None:
        report, manager = self.run_case()
        samples = [r for r in report["measurements"] if r.get("raw_resistance_ohm") is not None]
        self.assertEqual(len(samples), 4)
        for sample in samples:
            self.assertEqual(sample["raw_resistance_ohm"], 10000.0)
            self.assertAlmostEqual(sample["temperature_c"], 25.0, places=9)
            self.assertEqual(sample["status"], "plausible")
            self.assertTrue(sample["plausible"])
            raw = report["records"][sample["raw_record_id"]]
            self.assertEqual(raw["message"], "READ?")
            self.assertEqual(raw["instrument"], "dmm_temperature")
            self.assertEqual(raw["response_raw"], "10000")
        self.assertEqual(sum(1 for r in report["records"]
                             if r["case"] == "dmm_temperature.ntc_read"
                             and r["message"] == "READ?"), 2)
        self.assertEqual(report["warnings"], [])
        self.assert_ntc_restored(report, manager)

    def test_ntc_outside_plausibility_warns_and_range_is_configurable(self) -> None:
        # Independent fixture value: 10k/B3950 is about 55 C at 2.98 kohm.
        for extra, status in (((), "outside_plausibility_range"),
                              (("--ntc-min-c", "0", "--ntc-max-c", "60"), "plausible")):
            manager = FakeRM()
            manager.resources["dmm_temperature"].ntc_resistance = "2980"
            report, manager = self.run_case("no-energy", *extra, rm=manager)
            self.assertEqual(report["status"], "candidate", report["failure"])
            readings = [r for r in report["measurements"] if "raw_resistance_ohm" in r]
            self.assertTrue(all(r["status"] == status for r in readings))
            self.assertEqual(bool(report["warnings"]), status != "plausible")
            self.assert_ntc_restored(report, manager)

    def test_pt100_numeric_and_invalid_are_both_expected_with_ntc_fixture(self) -> None:
        for value in ("0", "9.9E37", "NaN"):
            manager = FakeRM()
            manager.resources["dmm_temperature"].pt100_reading = value
            report, manager = self.run_case(rm=manager)
            self.assertEqual(report["status"], "candidate", report["failure"])
            readings = [r for r in report["measurements"]
                        if r["case"] == "dmm_temperature.pt100_with_ntc_fixture"]
            self.assertEqual(len(readings), 2)
            self.assertTrue(all(r["status"] == "expected_invalid_with_ntc_fixture" for r in readings))
            self.assertTrue(all(r["reading_raw"] == value for r in readings))
            self.assertTrue(all(r["temperature_c"] is None for r in readings))
            if value != "0":
                self.assertTrue(all("InstrumentInvalidReading" in r["driver_exception"]
                                    for r in readings))
            self.assert_ntc_restored(report, manager)

    def test_floating_voltage_invalid_is_recorded_without_numeric_acceptance(self) -> None:
        manager = FakeRM()
        manager.resources["dmm_voltage"].voltage_reading = "9.9E37"
        report, manager = self.run_case(rm=manager)
        self.assertEqual(report["status"], "candidate", report["failure"])
        readings = [r for r in report["measurements"] if r.get("reading_raw") == "9.9E37"
                    and r["instrument"] == "dmm_voltage"]
        self.assertEqual(len(readings), 2)
        self.assertTrue(all(r["status"] == "observed_unassessed" for r in readings))
        self.assert_ntc_restored(report, manager)

    def test_final_ntc_restore_after_exception_interrupt_and_initial_off_failure(self) -> None:
        for exception in (TimeoutError, KeyboardInterrupt):
            for target in (("dmm_temperature", "READ?"), ("load", "INP OFF;*OPC?"),
                           ("dmm_voltage", "*CLS;*OPC?")):
                with self.subTest(exception=exception, target=target):
                    manager = FakeRM()
                    manager.fail_on, manager.exception = target, exception
                    report, manager = self.run_case(rm=manager)
                    self.assertEqual(report["status"], "failed")
                    self.assert_ntc_restored(report, manager)
                    self.assert_off(manager)
                    self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))

    def test_final_off_failure_still_restores_ntc(self) -> None:
        for exception in (TimeoutError, KeyboardInterrupt):
            manager = FakeRM()
            def stop_at_body(runner: Any, manager: FakeRM = manager,
                             exception: type[BaseException] = exception) -> None:
                manager.fail_on = ("load", "INP OFF;*OPC?")
                manager.exception = exception
                raise RuntimeError("body failure")
            with patch.object(hil.Runner, "no_energy", stop_at_body):
                report, manager = self.run_case(rm=manager)
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["cleanup_failures"])
            self.assert_ntc_restored(report, manager)
            self.assert_off(manager)

    def test_restore_failure_is_reported_and_all_sessions_close(self) -> None:
        for exception in (TimeoutError, KeyboardInterrupt):
            manager = FakeRM()
            message = "ABOR;:CONF:RES;:RES:RANG:AUTO ON;:RES:NPLC 10;*OPC?"
            def stop_at_body(runner: Any, manager: FakeRM = manager,
                             message: str = message,
                             exception: type[BaseException] = exception) -> None:
                manager.fail_on, manager.exception = ("dmm_temperature", message), exception
                raise RuntimeError("body failure")
            with patch.object(hil.Runner, "no_energy", stop_at_body):
                report, manager = self.run_case(rm=manager)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["thermal_restore"]["status"], "failed")
            self.assertTrue(report["cleanup_failures"])
            self.assertTrue(all(r.closed for r in manager.resources.values()))
            self.assertEqual(sum(m == message for _, _, m in manager.actions), 1)
            self.assert_off(manager)

    def test_a_fixture_missing_or_wrong_refuses_before_any_open(self) -> None:
        for token in (None, "", "DUT_DISCONNECTED", hil.FIXTURE_STATE.lower(),
                      hil.FIXTURE_STATE + " "):
            options = args()
            options.fixture_state = token
            manager = FakeRM()
            report = hil.Runner(options, manager, confirm).run()
            self.assertEqual(report["status"], "failed")
            self.assertIn("--fixture-state", report["failure"])
            self.assertEqual(manager.opened, [])
            self.assertEqual(manager.actions, [])

    def test_b_noninteractive_refuses_even_with_flag_and_correct_answer(self) -> None:
        options = args("energized", "--i-confirm-energize")
        manager = FakeRM()
        with patch.object(hil.sys.stdin, "isatty", return_value=False):
            report = hil.Runner(options, manager, confirm).run()
            with self.assertRaisesRegex(hil.InventoryError, "interactive stdin"):
                hil.main(["--phase", "energized", "--i-confirm-energize",
                          "--output-dir", "NEVER_WRITTEN"])
        self.assertEqual(report["status"], "failed")
        self.assertIn("interactive stdin", report["failure"])
        self.assertEqual(manager.opened, [])
        self.assertEqual(manager.actions, [])

    def test_unresolved_resources_still_produce_failed_report_without_io(self) -> None:
        options = hil.parser().parse_args(["--phase", "no-energy", "--output-dir", "NEVER_WRITTEN",
                                          "--fixture-state", hil.FIXTURE_STATE])
        manager = FakeRM()
        with patch.object(hil, "LOCAL_CONFIG") as local:
            local.read_text.side_effect = FileNotFoundError("fake missing config")
            report = hil.Runner(options, manager, confirm).run()
        self.assertEqual(report["status"], "failed")
        self.assertIn("Cannot resolve resources", report["failure"])
        self.assertEqual(manager.opened, [])
        self.assertEqual(manager.actions, [])

    def test_resource_defaults_and_explicit_overrides(self) -> None:
        import yaml  # type: ignore[import-untyped]

        config = {"instruments": {name: {"resource": name} for name in hil.CLASSES}}
        options = hil.parser().parse_args(["--phase", "no-energy", "--output-dir", "NEVER_WRITTEN",
                                          "--fixture-state", hil.FIXTURE_STATE,
                                          "--dmm-voltage", "explicit_voltage"])
        self.assertIsNone(options.dmm_temperature)
        with patch.object(hil, "LOCAL_CONFIG") as local:
            local.read_text.return_value = yaml.safe_dump(config)
            hil.resolve_resources(options)
        self.assertEqual(options.dmm_voltage, "explicit_voltage")
        self.assertEqual(options.dmm_temperature, "dmm_temperature")
        self.assertEqual(options.load, "load")
        self.assertEqual(options.psu, "psu")

    def test_restore_readback_mismatch_is_not_a_candidate(self) -> None:
        manager = FakeRM()
        raw = manager.resources["dmm_temperature"]
        original = raw.query
        def wrong_configuration(message: str) -> str:
            result = original(message)
            return '"FRES +1E4,+0.01"' if message == "CONF?" else result
        raw.query = wrong_configuration  # type: ignore[method-assign]
        report, manager = self.run_case(rm=manager)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["thermal_restore"]["status"], "failed")
        self.assertTrue(all(r.closed for r in manager.resources.values()))
        self.assert_off(manager)

    def test_all_inventory_methods_present_and_exact_messages_observed(self) -> None:
        report, _ = self.run_case()
        actual = {(r["instrument"], r["method"]) for r in report["source_inventory"]}
        expected: set[tuple[str, str]] = set()
        for name, cls in hil.CLASSES.items():
            with open(inspect.getfile(cls), encoding="utf-8") as stream:
                tree = ast.parse(stream.read())
            for node in tree.body:
                nodes = node.body if isinstance(node, ast.ClassDef) else [node]
                expected.update((name, n.name) for n in nodes if isinstance(n, ast.FunctionDef))
        self.assertEqual(actual, expected)
        # Frozen from source + fake characterization; every message is exact.
        expected_messages = {
            "dmm_voltage": {
                "query": [
                    "*CLS;*OPC?", "*IDN?", "SYST:ERR?", "FETC?", "READ?",
                    "SAMP:TIM?", "SAMP:TIM? MIN", "VOLT:DC:NPLC 1;*OPC?", "VOLT:DC:NPLC?",
                    ("ABOR;:CONF:VOLT:DC 10;:VOLT:DC:NPLC 1;:VOLT:DC:RANG 10;"
                     ":VOLT:DC:RANG:AUTO OFF;:TRIG:SOUR IMM;:TRIG:COUN 1;:SAMP:COUN 1;"
                     ":SAMP:SOUR IMM;*OPC?"),
                    "ABOR;:SAMP:COUN 1;:SAMP:SOUR IMM;*OPC?",
                    "ABOR;:SAMP:COUN 2;:SAMP:SOUR TIM;:SAMP:TIM 0.41;*OPC?",
                ],
                "write": ["*CLS", "ABOR", "INIT", "VOLT:DC:RANG:AUTO OFF"],
            },
            "dmm_temperature": {
                "query": [
                    "*CLS;*OPC?", "*IDN?", "SYST:ERR?", "READ?",
                    "ABOR;:CONF:RES;:RES:RANG:AUTO ON;:RES:NPLC 10;*OPC?",
                    ("ABOR;:CONF:TEMP FRTD,85;:SENS:TEMP:TRAN:FRTD:RES 100;"
                     ":SENS:TEMP:NPLC 10;:UNIT:TEMP C;*OPC?"),
                ],
                "write": ["*CLS", "ABOR"],
            },
            "load": {
                "query": [
                    "*CLS;*OPC?", "*IDN?", "SYST:ERR?", "INP OFF;*OPC?", "INP?",
                    "INP:SHOR OFF;:INP:TIM OFF;*OPC?", "INP:SHOR?", "INP:TIM?",
                    "CURR:RANG 0.01;:SOUR:CURR 0.01;*OPC?",
                    "MEAS:CURR?", "MEAS:POW?", "MEAS:VOLT?", "STAT:QUES:COND?",
                ],
                "write": [
                    "*CLS",
                    "SOUR:FUNC CURR", "SOUR:FUNC:MODE FIX",
                    "TRAN 0", "VOLT:ON 0",
                ],
            },
            "psu": {
                "query": [
                    "*CLS;:OUTP OFF;:SYST:ERR?", "*IDN?", "SYST:ERR?",
                    "INST:SEL CH1;:OUTP:TIM:STAT OFF;*OPC?",
                    "INST:SEL CH2;:OUTP:TIM:STAT OFF;*OPC?",
                    "INST:SEL CH1;:SOUR:CURR 0.01;*OPC?",
                    "INST:SEL CH1;:SOUR:VOLT 1;*OPC?",
                    # R13-B2: SERIES-ben sincs parameteres CH2 MEAS (HIL 2026-11-05).
                    "MEAS:CURR? CH1", "MEAS:VOLT? CH1",
                    "OUTP OFF;*OPC?", "OUTP?",  # R13-B4: nincs OUTP:PON?
                     "SOUR:OUTP:PAR?", "SOUR:OUTP:SER?",
                    "SOUR:OUTP:PAR OFF;:SOUR:OUTP:SER ON;*OPC?",
                    "SOUR:OUTP:SER OFF;:SOUR:OUTP:PAR OFF;*OPC?",
                    "SOUR:OUTP:SER OFF;:SOUR:OUTP:PAR ON;*OPC?",
                    "STAT:OPER:INST:ISUM1:COND?",
                ],
                "write": ["*CLS", "INST:SEL CH1"],
            },
        }
        required = {(name, kind, message)
                    for name, kinds in expected_messages.items()
                    for kind, commands in kinds.items() for message in commands}
        observed = {(r["instrument"], r["kind"], r["message"]) for r in report["records"]
                    if r["role"] == "driver" and r["kind"] in ("write", "query")}
        self.assertEqual(observed, required)
        self.assertTrue(all(r["evidence"] == "source_only"
                            for r in report["source_inventory"]))
        self.assertIn("not observed coverage", report["coverage_limit"])


    def test_independent_oracle_detects_original_a_violations(self) -> None:
        for message in ("TRAN 1", "FORC:TRIG", "*RST", "OUTP:STAT ON",
                        "OUTPUT ON", "INPUT ON", "INP 1", "*TRG"):
            with self.subTest(message=message):
                self.assertTrue(forbidden_a(message))
        # This full-stream oracle remains effective if the production guard regresses.
        with patch.object(hil, "energizes", return_value=False):
            report, manager = self.run_case()
        self.assertEqual(report["status"], "candidate", report["failure"])
        self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))

    def test_command_guard_stops_before_resource_io(self) -> None:
        messages = sorted(FORBIDDEN_A_UNITS) + [
            "SOUR:OUTP ON", "SOURCE:OUTPUT:STATE ON", "OUTP +1",
            "OUTP 1.0", "INP 1E0", "TRAN +1", "  :tran	ON ",
            "*CLS;:FORC:TRIG;*OPC?", "OUTP:STAT OFF;STAT ON",
            "SOUR:OUTP:STAT OFF;STAT ON", "*RCL 0",
        ]
        for message in messages:
            for kind in ("write", "query"):
                with self.subTest(message=message, kind=kind):
                    manager = FakeRM()
                    runner = hil.Runner(args(), manager, confirm)
                    session = hil.Session(runner, "load", manager.resources["load"])
                    with self.assertRaises(hil.InventoryError):
                        session.io(kind, message)
                    self.assertTrue(runner.halted)
                    self.assertEqual(manager.actions, [])
                    with self.assertRaises(hil.InventoryError):
                        session.io("query", "INP?")
        for message in ("OUTP OFF", "OUTP 0", "INP?", "TRAN 0",
                        "SOUR:OUTP:SER ON;*OPC?", "CONF:RES", "SAMP:SOUR TIM"):
            self.assertFalse(hil.energizes(message), message)

    def test_method_guard_cannot_be_bypassed_by_a_zero_io_driver(self) -> None:
        for method in sorted(FORBIDDEN_A_METHODS | {"begin_current_pulse", "fire_load_pulse"}):
            with self.subTest(method=method):
                manager = FakeRM()
                runner = hil.Runner(args(), manager, confirm)
                with patch.object(runner.devices["load"], method, create=True) as called:
                    with self.assertRaises(hil.InventoryError):
                        runner.invoke("load", method)
                    called.assert_not_called()
                self.assertTrue(runner.halted)
                self.assertEqual(manager.actions, [])

    def test_reset_and_pulse_are_explicitly_source_only_in_a(self) -> None:
        report, _ = self.run_case()
        resets = [r for r in report["source_inventory"] if r["method"] == "reset"]
        self.assertEqual(len(resets), 4)
        self.assertTrue(all("not executed: may reset active protection settings" in
                            r["execution_note"] for r in resets))
        pulses = [r for r in report["source_inventory"] if r["method"] in
                  ("configure_transient_pulse", "fire_transient_pulse")]
        self.assertEqual(len(pulses), 2)
        self.assertTrue(all("source-only in A" in r["execution_note"] for r in pulses))

    def test_fake_does_not_invent_psu_current_range(self) -> None:
        manager = FakeRM()
        self.assertNotIn("CURR:RANG", manager.resources["psu"].values)
        with self.assertRaisesRegex(AssertionError, "2220 has no current-range"):
            manager.resources["psu"].query("CURR:RANG?")
        self.assertEqual(manager.resources["load"].query("CURR:RANG?"), "0.01")

    def test_ntc_precedes_pt100_and_post_reading_has_self_heating_limitation(self) -> None:
        report, _ = self.run_case()
        readings = [r for r in report["measurements"] if r["instrument"] == "dmm_temperature"]
        self.assertEqual([r["case"] for r in readings], [
            "dmm_temperature.ntc_before_pt100", "dmm_temperature.ntc_before_pt100",
            "dmm_temperature.pt100_with_ntc_fixture", "dmm_temperature.pt100_with_ntc_fixture",
            "dmm_temperature.ntc_read", "dmm_temperature.ntc_read",
        ])
        ntc = [r for r in readings if "raw_resistance_ohm" in r]
        self.assertEqual([r["metrologically_assessable"] for r in ntc], [True, True, False, False])
        self.assertTrue(all(r["metrology_limitation"] is None for r in ntc[:2]))
        self.assertTrue(all("self-heating" in r["metrology_limitation"] for r in ntc[2:]))
        self.assertIn("not metrologically assessable", report["ntc_after_pt100_limitation"])

    def test_identity_rejection_reports_safe_off_not_attempted(self) -> None:
        for name in ("load", "psu"):
            manager = FakeRM()
            manager.resources[name].identity = "UNKNOWN,WRONG,TEST,0"
            report, manager = self.run_case(rm=manager)
            self.assertEqual(report["status"], "failed")
            for energy in ("load", "psu"):
                expected = "safe_off_not_attempted" if energy == name else "attempted"
                self.assertEqual(report["safe_off"][energy]["status"], expected)
            if name == "psu":
                self.assertEqual(report["psu_restore"]["status"], "not_attempted_identity_gate")

    def test_psu_mode_restore_after_interrupted_series_or_parallel(self) -> None:
        for mode, message in (
                ("SER", "SOUR:OUTP:PAR OFF;:SOUR:OUTP:SER ON;*OPC?"),
                ("PAR", "SOUR:OUTP:SER OFF;:SOUR:OUTP:PAR ON;*OPC?")):
            for exception in (TimeoutError, KeyboardInterrupt):
                with self.subTest(mode=mode, exception=exception):
                    manager = FakeRM()
                    manager.fail_on, manager.exception = ("psu", message), exception
                    report, manager = self.run_case(rm=manager)
                    self.assertEqual(report["status"], "failed")
                    self.assertEqual(report["psu_restore"]["status"], "verified")
                    self.assertEqual(manager.resources["psu"].values["SOUR:OUTP:SER"], "0")
                    self.assertEqual(manager.resources["psu"].values["SOUR:OUTP:PAR"], "0")
                    self.assert_ntc_restored(report, manager)
                    rows = report["records"]
                    offs = [r["id"] for r in rows if r["case"] == "final_safe_off"
                            and r["message"] in ("INP OFF;*OPC?", "OUTP OFF;*OPC?")]
                    restores = [r["id"] for r in rows if r["case"] == "final_restore_psu"]
                    self.assertLess(max(offs), min(restores))
                    self.assert_off(manager)

    def test_psu_restore_failure_does_not_skip_ntc_or_close(self) -> None:
        for exception in (TimeoutError, KeyboardInterrupt):
            manager = FakeRM()
            def fail_restore(runner: Any, manager: FakeRM = manager,
                             exception: type[BaseException] = exception) -> None:
                manager.fail_on = ("psu", "SOUR:OUTP:SER OFF;:SOUR:OUTP:PAR OFF;*OPC?")
                manager.exception = exception
                raise RuntimeError("stop before final cleanup")
            with patch.object(hil.Runner, "no_energy", fail_restore):
                report, manager = self.run_case(rm=manager)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["psu_restore"]["status"], "failed")
            self.assert_ntc_restored(report, manager)
            self.assert_off(manager)

    def test_every_a_io_failure_or_interrupt_cleans_up(self) -> None:
        baseline, _ = self.run_case("no-energy", "--repeat", "1")
        targets = sorted({(r["instrument"], r["message"]) for r in baseline["records"]
                          if r["kind"] in ("query", "write") and
                          not r["case"].startswith("final_")})
        for target in targets:
            for exception in (TimeoutError, KeyboardInterrupt):
                with self.subTest(target=target, exception=exception):
                    manager = FakeRM()
                    manager.fail_on, manager.exception = target, exception
                    report, manager = self.run_case("no-energy", "--repeat", "1", rm=manager)
                    self.assertEqual(report["status"], "failed")
                    self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))
                    self.assertTrue(all(manager.resources[n].closed for n in manager.opened))
                    self.assert_off(manager)
                    for name in ("load", "psu"):
                        if report["safe_off"][name]["status"] == "attempted":
                            final = [r for r in report["records"] if
                                     r["case"] == "final_safe_off" and r["instrument"] == name]
                            self.assertTrue(any("OFF" in r["message"] for r in final))


    def test_guard_violation_runs_final_cleanup_and_stops_a(self) -> None:
        for method in (None, "configure_transient_pulse"):
            manager = FakeRM()
            def prohibited(runner: Any, method: str | None = method) -> None:
                if method is None:
                    runner.sessions["load"].write("FORC:TRIG")
                else:
                    runner.invoke("load", method, 0.01, 0.0, 0.01)
                self.fail("A continued past the safety guard")
            with patch.object(hil.Runner, "no_energy", prohibited):
                report, manager = self.run_case(rm=manager)
            self.assertEqual(report["status"], "failed")
            self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))
            self.assert_ntc_restored(report, manager)
            self.assertEqual(report["psu_restore"]["status"], "verified")
            self.assert_off(manager)

    def test_psu_restore_readback_mismatch_is_failed_and_ntc_still_restored(self) -> None:
        manager = FakeRM()
        runner = hil.Runner(args(), manager, confirm)
        original = manager.resources["psu"].query
        def mismatch(message: str) -> str:
            value = original(message)
            if runner.case == "final_restore_psu" and message == "SOUR:OUTP:SER?":
                return "1"
            return value
        manager.resources["psu"].query = mismatch  # type: ignore[method-assign]
        report = runner.run()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["psu_restore"]["status"], "failed")
        self.assertIn("mode mismatch", report["psu_restore"]["exception"])
        self.assert_ntc_restored(report, manager)
        self.assert_off(manager)

if __name__ == "__main__":
    unittest.main()




class TestHilReadbackScpiLongForm(unittest.TestCase):
    """HIL 2026-11-05: a 2380 SOUR:FUNC:MODE? valasza "FIXED" (hosszu forma)."""

    def _runner_with_reply(self, reply):
        runner = hil.Runner.__new__(hil.Runner)
        runner.halted = False
        runner.query = lambda name, message: reply
        return runner

    def test_short_and_long_form_exact_match_accepted(self):
        for reply in ("FIX\n", "FIXED\n", '"FIXED"\n'):
            runner = self._runner_with_reply(reply)
            self.assertEqual(runner.expect("load", "SOUR:FUNC:MODE?", "FIX"), reply)

    def test_prefix_or_other_word_rejected(self):
        for reply in ("FIXE\n", "FIXEDX\n", "LIST\n", "FI\n"):
            runner = self._runner_with_reply(reply)
            with self.assertRaises(hil.InventoryError):
                runner.expect("load", "SOUR:FUNC:MODE?", "FIX")
            self.assertTrue(runner.halted)

    def test_expectation_without_alias_stays_exact(self):
        runner = self._runner_with_reply("CEL\n")
        with self.assertRaises(hil.InventoryError):
            runner.expect("dmm_temperature", "UNIT:TEMP?", "C")


class TestR15B1InventoryRegression(unittest.TestCase):
    """R15-B1: frozen HIL replies; other current values are software controls."""

    def test_programmed_range_in_both_phases_and_custom_values(self) -> None:
        for phase in ("no-energy", "energized"):
            for current in ("0.01", "0.025"):
                with self.subTest(phase=phase, current=current):
                    manager = FakeRM()
                    extra = ("--i-confirm-energize",) if phase == "energized" else ()
                    with patch.object(hil.sys.stdin, "isatty", return_value=True):
                        report = hil.Runner(args(phase, "--repeat", "1", "--current-a", current,
                                                 *extra), manager, confirm).run()
                    self.assertEqual(report["status"], "candidate", report["failure"])
                    replies = [r["response_raw"] for r in report["records"]
                               if r["message"] == "CURR:RANG?"]
                    self.assertEqual(replies, [current])
                    self.assertIn("LOW/HIGH", report["load_range_readback_limitation"])

    def test_no_energy_trace_is_explicitly_unexecuted_and_never_prepared(self) -> None:
        manager = FakeRM()
        report = hil.Runner(args(), manager, confirm).run()
        self.assertEqual(report["status"], "candidate", report["failure"])
        skipped = report["not_executed"]
        self.assertEqual({r["method"] for r in skipped}, {"configure_trace", "fetch_trace"})
        self.assertTrue(all(r["status"] == "not_executed" and r["reason"] for r in skipped))
        self.assertTrue(all("value" not in r for r in skipped))
        self.assertFalse(any(c["method"] in {"configure_trace", "fetch_trace"}
                             for c in report["driver_calls"]))
        rows = report["records"]
        prep = [r for r in rows if r["message"] == "TRAC:FEED:CONT NEV;*OPC?"]
        self.assertEqual(len(prep), 1)
        self.assertEqual(prep[0]["role"], "preparation")
        self.assertEqual(prep[0]["response_raw"], "1")
        readback = next(r for r in rows if r["message"] == "TRAC:FEED:CONT?")
        self.assertEqual(readback["response_raw"], "NEVER")
        self.assertLess(prep[0]["id"], readback["id"])
        for name, off in (("load", "INP OFF;*OPC?"), ("psu", "OUTP OFF;*OPC?")):
            self.assertTrue(any(r["instrument"] == name and r["message"] == "*IDN?"
                                and r["id"] < prep[0]["id"] for r in rows))
            self.assertTrue(any(r["instrument"] == name and r["message"] == off
                                and r["exception"] is None and r["id"] < prep[0]["id"]
                                for r in rows))
        self.assertFalse(any(r["message"] == "TRAC:DATA?" for r in rows))

    def test_frozen_trace_fake_preserves_timeout_error_and_successful_off(self) -> None:
        raw = FakeRM().resources["load"]
        raw.write("TRAC:POIN 2")
        self.assertEqual(raw.query("SYST:ERR?"), '-221,"Settings conflict"')
        raw.clear()
        self.assertEqual(raw.query("TRAC:FEED:CONT?"), "NEXT")
        raw.write("TRAC:FEED:CONT NEV")
        self.assertEqual(raw.query("TRAC:FEED:CONT?"), "NEVER")
        raw.write("TRAC:POIN 2")
        self.assertEqual(raw.query("SYST:ERR?"), "0,No error")
        raw.write("TRAC:FEED VOLT")
        self.assertEqual(raw.query("TRAC:FEED?"), "VOLTAGE")
        with self.assertRaises(TimeoutError):
            raw.query("TRAC:DATA?")
        raw.clear()
        self.assertEqual(raw.query("INP OFF;*OPC?"), "1")
        self.assertEqual(raw.query("SYST:ERR?"), '-230,"Data Corrupt or Stale"')
        self.assertEqual(raw.query("INP?"), "0")

    def test_voltage_and_never_aliases_are_exact(self) -> None:
        for expected, good, bad in (
                ("VOLT", ("VOLT", "VOLTAGE"), ("VOL", "VOLTAG", "VOLTAGES")),
                ("NEV", ("NEV", "NEVER"), ("NE", "NEVE", "NEVERX"))):
            for reply in (*good, *bad):
                with self.subTest(expected=expected, reply=reply):
                    runner = hil.Runner(args(), FakeRM(), confirm)
                    with patch.object(runner, "query", return_value='"' + reply + '"\n'):
                        if reply in good:
                            runner.expect("load", "unused", expected)
                        else:
                            with self.assertRaises(hil.InventoryError):
                                runner.expect("load", "unused", expected)
                            self.assertTrue(runner.halted)

    def test_combined_settle_target_accepts_one_volt_rejects_two(self) -> None:
        for mode in ("independent", "series", "parallel"):
            for voltage, expected in (("1", "candidate"), ("2", "failed")):
                with self.subTest(mode=mode, voltage=voltage):
                    manager = FakeRM()
                    manager.resources["dmm_voltage"].voltage_reading = voltage
                    runner = hil.Runner(args(
                        "energized", "--i-confirm-energize", "--measure-settle",
                        "--psu-mode", mode, "--repeat", "1", "--poll-max-s", "0.05",
                        "--poll-interval-s", "0.001"), manager, confirm)
                    original_expect = runner.expect
                    def expect(name: str, message: str, expected: Any,
                               original_expect: Any = original_expect) -> str:
                        # Isolate F4 from the separate F1 range oracle on HEAD.
                        return original_expect(name, message, 0.01 if message == "CURR:RANG?"
                                               else expected)
                    with patch.object(hil.sys.stdin, "isatty", return_value=True), \
                            patch.object(runner, "expect", side_effect=expect):
                        report = runner.run()
                    self.assertEqual(report["status"], expected, report["failure"])
                    settled = [t for t in report["transitions"] if "target_v" in t]
                    if expected == "candidate":
                        self.assertEqual([t["target_v"] for t in settled], [1.0])
                    else:
                        self.assertIn("settling deadline", report["failure"])
                        self.assertEqual(settled, [])
                    self.assertFalse(manager.resources["psu"].output)
                    self.assertFalse(manager.resources["load"].input_on)

    def test_restore_requires_clean_off_and_exact_false(self) -> None:
        for fault in ("off", "on", "unknown", "readback", "interrupt"):
            with self.subTest(fault=fault):
                manager = FakeRM()
                runner = hil.Runner(args(
                    "no-energy", "--poll-max-s", "0.05", "--poll-interval-s", "0.001"),
                    manager, confirm)
                original = manager.resources["psu"].query
                def query(message: str, runner: Any = runner, fault: str = fault,
                          original: Any = original) -> str:
                    if runner.case == "final_safe_off":
                        if message == "OUTP OFF;*OPC?" and fault in ("off", "interrupt"):
                            raise (KeyboardInterrupt if fault == "interrupt" else TimeoutError)("final OFF")
                        if message == "OUTP?" and fault in ("on", "unknown", "readback"):
                            if fault == "readback":
                                raise TimeoutError("final readback")
                            return "1" if fault == "on" else "UNKNOWN"
                    return original(message)
                with patch.object(manager.resources["psu"], "query", side_effect=query), \
                        patch.object(runner, "no_energy", side_effect=RuntimeError("primary probe failure")):
                    report = runner.run()
                self.assertIn("primary probe failure", report["failure"])
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["psu_restore"]["status"], "skipped_off_unverified")
                self.assertTrue(report["cleanup_failures"])
                self.assertFalse(any(r["case"] == "final_restore_psu" for r in report["records"]))
                self.assertEqual(report["thermal_restore"]["status"], "verified")
                self.assertTrue(all(r.closed for r in manager.resources.values()))

    def test_peer_connection_and_off_survive_open_connect_and_off_faults(self) -> None:
        for name in ("load", "psu"):
            for stage in ("open", "connect", "off"):
                for exception in (TimeoutError, KeyboardInterrupt):
                    with self.subTest(name=name, stage=stage, exception=exception):
                        manager = FakeRM()
                        manager.resources["psu"].output = name == "load"
                        manager.resources["load"].input_on = name == "psu"
                        runner = hil.Runner(args(), manager, confirm)
                        original_open = manager.open_resource
                        def open_resource(address: str, name: str = name, stage: str = stage,
                                          manager: FakeRM = manager, original_open: Any = original_open,
                                          exception: type[BaseException] = exception) -> FakeResource:
                            if address == name and stage == "open":
                                manager.opened.append(address)
                                raise exception("first connection")
                            return original_open(address)
                        with ExitStack() as stack:
                            stack.enter_context(patch.object(manager, "open_resource", side_effect=open_resource))
                            if stage == "connect":
                                stack.enter_context(patch.object(runner.devices[name], "connect",
                                                                 side_effect=exception("first connection")))
                            if stage == "off":
                                manager.fail_on = (name, "INP OFF;*OPC?" if name == "load"
                                                   else "OUTP OFF;*OPC?")
                                manager.exception = exception
                            report = runner.run()
                        peer = "psu" if name == "load" else "load"
                        self.assertEqual(report["status"], "failed")
                        self.assertIsNotNone(report["failure"])
                        self.assertIn(peer, manager.opened)
                        off = "OUTP OFF;*OPC?" if peer == "psu" else "INP OFF;*OPC?"
                        self.assertIn((peer, "query", off), manager.actions)
                        if stage == "connect":
                            self.assertTrue(any(r["case"] == peer + ".connect"
                                                for r in report["records"]))
                        self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))
                        self.assertTrue(all(manager.resources[n].closed for n in manager.opened
                                            if stage != "open" or n != name))


    def test_peer_connection_and_off_survive_recorded_io_faults(self) -> None:
        for stage, case, message in (
                ("idn_io", "startup", "*IDN?"),
                ("connect_io", "load.connect", "INP OFF;*OPC?")):
            for exception in (TimeoutError, KeyboardInterrupt):
                with self.subTest(stage=stage, exception=exception):
                    manager = FakeRM()
                    manager.resources["psu"].output = True
                    runner = hil.Runner(args(), manager, confirm)
                    original_query = manager.resources["load"].query
                    injected = False
                    def query(msg: str, runner: Any = runner, case: str = case,
                              message: str = message, manager: FakeRM = manager,
                              exception: type[BaseException] = exception,
                              original_query: Any = original_query) -> str:
                        nonlocal injected
                        if not injected and runner.case == case and msg == message:
                            injected = True
                            manager.fail_on = ("load", msg)
                            manager.exception = exception
                        return original_query(msg)
                    with patch.object(manager.resources["load"], "query", side_effect=query):
                        report = runner.run()
                    failure = f"{exception.__name__}: uncertain I/O after command applied"
                    self.assertTrue(injected)
                    self.assertTrue(runner.halted)
                    self.assertEqual(report["status"], "failed")
                    self.assertIn(failure, report["failure"])
                    self.assertTrue(any(r["instrument"] == "load" and r["case"] == case
                                        and r["message"] == message and r["exception"] == failure
                                        for r in report["records"]))
                    self.assertIn("psu", runner.verified)
                    self.assertIn(("psu", "query", "OUTP OFF;*OPC?"), manager.actions)
                    self.assertIs(manager.resources["psu"].output, False)
                    if stage == "idn_io":
                        self.assertNotIn("load", runner.verified)
                        allowed = {("query", "*IDN?"), ("query", "SYST:ERR?"),
                                   ("clear", ""), ("close", "")}
                        self.assertTrue(all((kind, msg) in allowed
                                            for name, kind, msg in manager.actions if name == "load"))
                    else:
                        self.assertTrue(any(r["instrument"] == "psu" and r["case"] == "psu.connect"
                                            and r["kind"] == "query" and r["exception"] is None
                                            for r in report["records"]))
                    self.assertFalse(any(forbidden_a(msg) for _, _, msg in manager.actions))
                    self.assertTrue(all(r.closed for r in manager.resources.values()))


class TestR15B1ProbeRegression(unittest.TestCase):
    """Full main() paths, production drivers with fake VISA and memory-only reports."""

    def run_probe(self, module: Any, fault: str = "", name: str = "psu",
                  exception: type[BaseException] = TimeoutError) -> tuple[int, dict[str, Any], FakeRM]:
        manager = FakeRM()
        manager.resources["load"].values["SYST:SENS"] = "0"
        if fault in ("connect", "startup_off"):
            manager.resources["psu"].output = name == "load"
            manager.resources["load"].input_on = name == "psu"
        load = module.Keithley2380Load(max_retries=1)
        psu = module.Keithley2220PSU(max_retries=1)
        active = manager.resources["psu" if module is psu_probe else "load"]
        completed = False
        final_started = False
        startup_failed = False
        devices = {"load": load, "psu": psu}
        real_off = {"load": load.input_off, "psu": psu.all_outputs_off}
        real_read = {"load": load.read_input_state, "psu": psu.read_output_state}
        def off(which: str) -> None:
            nonlocal completed, final_started, startup_failed
            if completed:
                final_started = True
            manager.actions.append((which, "attempt", "OFF"))
            if fault == "startup_off" and which == name and not startup_failed:
                startup_failed = True
                raise exception("first connection OFF")
            if final_started and fault == "off" and which == name:
                raise exception("final OFF")
            was_active = active.output or active.input_on
            real_off[which]()
            if was_active and not (active.output or active.input_on):
                completed = True
        def read(which: str) -> Any:
            if final_started and which == name:
                if fault == "readback":
                    raise exception("final readback")
                if fault == "on":
                    return True
                if fault == "unknown":
                    return None
            return real_read[which]()
        config = {"instruments": {n: {"resource": n} for n in manager.resources}}
        argv = ["probe", "--energize-token", module.CONFIRM_TOKEN, "--output-dir", "NEVER_WRITTEN"]
        argv += ["--skip-production-measure"] if module is psu_probe else ["--repeat", "1"]
        captured: list[str] = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(module.pyvisa, "ResourceManager", return_value=manager))
            stack.enter_context(patch.object(manager, "close", create=True))
            stack.enter_context(patch.object(module, "Keithley2380Load", return_value=load))
            stack.enter_context(patch.object(module, "Keithley2220PSU", return_value=psu))
            stack.enter_context(patch.object(Path, "read_text", return_value=json.dumps(config)))
            stack.enter_context(patch.object(Path, "mkdir"))
            stack.enter_context(patch.object(Path, "write_text",
                                             side_effect=lambda text, **kw: captured.append(text)))
            stack.enter_context(patch("sys.argv", argv))
            stack.enter_context(patch("builtins.print"))
            stack.enter_context(patch.object(module.time, "sleep", return_value=None))
            for which, dev in devices.items():
                stack.enter_context(patch.object(dev, "input_off" if which == "load" else "all_outputs_off",
                                                 side_effect=lambda which=which: off(which)))
                stack.enter_context(patch.object(dev, "read_input_state" if which == "load"
                                                 else "read_output_state",
                                                 side_effect=lambda which=which: read(which)))
            if fault == "connect":
                stack.enter_context(patch.object(devices[name], "connect",
                                                 side_effect=exception("first connection")))
            if module is psu_probe:
                dmm = module.Keysight34465ADMM(max_retries=1)
                stack.enter_context(patch.object(module, "Keysight34465ADMM", return_value=dmm))
                raw_dmm = manager.resources["dmm_voltage"]
                original_query = raw_dmm.query
                def dmm_query(message: str) -> str:
                    raw_dmm.voltage_reading = "1" if active.output else "0"
                    return original_query(message)
                stack.enter_context(patch.object(raw_dmm, "query", side_effect=dmm_query))
                if fault == "ch2":
                    original_psu_query = psu._query
                    def psu_query(message: str, **kwargs: Any) -> str:
                        if final_started and "OUTP:ENAB OFF" in message:
                            raise exception("final CH2 OFF")
                        return original_psu_query(message, **kwargs)
                    stack.enter_context(patch.object(psu, "_query", side_effect=psu_query))
                if fault == "restore":
                    stack.enter_context(patch.object(psu, "set_mode_independent",
                                                     side_effect=exception("restore failed")))
                if fault == "mode":
                    original_mode = psu.query_combination_mode
                    stack.enter_context(patch.object(psu, "query_combination_mode",
                                                     side_effect=lambda: "SER" if final_started
                                                     else original_mode()))
            rc = module.main()
        self.assertEqual(len(captured), 1)
        return rc, json.loads(captured[0]), manager

    def test_load_probe_uses_own_poll_without_driver_ack(self) -> None:
        # Valódi driver, fake VISA: a probe saját pollja maradjon az ON-nyugta.
        with patch.object(load_probe.Keithley2380Load, "_wait_input_activation",
                          side_effect=AssertionError("Váratlan driver-nyugtapoll")) as driver_poll:
            rc, report, manager = self.run_probe(load_probe)
        self.assertEqual(rc, 0, report.get("failure"))
        self.assertEqual(report["status"], "done")
        driver_poll.assert_not_called()
        self.assertIn("on_readback_s", report["cycles"][0])
        self.assertEqual([m for _, _, m in manager.actions].count("INP ON"), 1)
        self.assertFalse(manager.resources["load"].input_on)
        self.assertFalse(manager.resources["psu"].output)

    def test_startup_off_order_is_load_then_psu(self) -> None:
        for module in (load_probe, psu_probe):
            with self.subTest(module=module.__name__):
                rc, report, manager = self.run_probe(module)
                self.assertEqual(rc, 0)
                self.assertEqual(report["status"], "done")
                attempts = [name for name, kind, msg in manager.actions
                            if kind == "attempt" and msg == "OFF"]
                # First pair: driver connect; second pair: explicit startup OFF.
                self.assertEqual(attempts[:4], ["load", "psu", "load", "psu"])

    def test_clean_controls_and_cleanup_matrix(self) -> None:
        for module in (load_probe, psu_probe):
            for fault in ("", "off", "on", "unknown", "readback"):
                for name in ("load", "psu"):
                    with self.subTest(module=module.__name__, fault=fault, name=name):
                        rc, report, manager = self.run_probe(module, fault, name)
                        self.assertEqual(report["status"], "failed" if fault else "done")
                        self.assertEqual(rc, 1 if fault else 0)
                        if fault:
                            self.assertEqual(report["cleanup_fault"], "SAFE_OFF_OR_RESTORE_UNCONFIRMED")
                            self.assertTrue(report["cleanup_failures"])
                            if module is psu_probe and name == "psu":
                                self.assertEqual(report["cleanup"]["psu_mode"], "skipped_off_unverified")
                        tail = [(n, m) for n, _, m in manager.actions]
                        last_load = max(i for i, (n, m) in enumerate(tail)
                                        if n == "load" and m == "OFF")
                        self.assertTrue(any(n == "psu" and m == "OFF" for n, m in tail[last_load:]))
                        self.assertFalse(any(forbidden_a(m) for _, m in tail[last_load:]))
                        self.assertTrue(manager.resources["psu"].closed)
                        self.assertTrue(manager.resources["load"].closed)

    def test_connect_failure_attempts_peer_and_preserves_original_failure(self) -> None:
        for module in (load_probe, psu_probe):
            for name in ("load", "psu"):
                for exception in (TimeoutError, KeyboardInterrupt):
                    for fault in ("connect", "startup_off"):
                        with self.subTest(module=module.__name__, name=name,
                                          exception=exception, fault=fault):
                            rc, report, manager = self.run_probe(module, fault, name, exception)
                            self.assertEqual(rc, 1)
                            self.assertEqual(report["status"], "failed")
                            self.assertIn("first connection", report["failure"])
                            peer = "psu" if name == "load" else "load"
                            self.assertIn(peer, manager.opened)
                            off = "OUTP OFF;*OPC?" if peer == "psu" else "INP OFF;*OPC?"
                            self.assertIn((peer, "query", off), manager.actions)
                            self.assertFalse(manager.resources[peer].output)
                            self.assertFalse(manager.resources[peer].input_on)
                            self.assertFalse(any(forbidden_a(m) for _, _, m in manager.actions))

    def test_psu_restore_failure_or_mismatch_fails_final_result(self) -> None:
        for fault in ("restore", "mode"):
            rc, report, _ = self.run_probe(psu_probe, fault)
            self.assertEqual(rc, 1)
            self.assertEqual(report["status"], "failed")
            self.assertIn("psu_mode", report["cleanup_failures"])

    def test_cleanup_interrupts_and_ch2_failure_are_consumed(self) -> None:
        for module in (load_probe, psu_probe):
            for fault in ("off", "readback"):
                for name in ("load", "psu"):
                    rc, report, _ = self.run_probe(module, fault, name, KeyboardInterrupt)
                    self.assertEqual(rc, 1)
                    self.assertEqual(report["status"], "failed")
                    self.assertTrue(report["cleanup_failures"])
        rc, report, _ = self.run_probe(psu_probe, "ch2")
        self.assertEqual(rc, 1)
        self.assertIn("psu_ch2_enab_off", report["cleanup_failures"])
        self.assertEqual(report["cleanup"]["psu_mode"], "skipped_off_unverified")
