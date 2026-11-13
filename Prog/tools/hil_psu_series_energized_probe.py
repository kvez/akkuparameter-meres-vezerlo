"""R13 HIL B (energized) -- PSU SERIES-mod meresi szemantika, 2026-11-05.

Felhasznaloi engedelyek (chat, 2026-11-05): "Teszteljuk le az energizalo parancsokat is",
"te inditod majd a parancsokat", "Kesz a series bekotes, a DMM a kimeneten";
elolap Output Recall = Off ellenorizve (R13/AGREED_FINDINGS.md).

Fixture: 2220-30-1 SERIES-athidalassal (2220S-900-01 Rev. C 19-20. o.), a kimeneten csak
a DMM_V (34465A) DCV-bemenete; terheles es akku NINCS; a Load bemenete ures es vegig OFF.
Setpoint: 1 V / 0.01 A. A produkcios drivereken keresztul fut; a PSU connect PON-lepese
a produkciobol eltavolitva (R13-B4; fw 1.15-1.05 nem tamogatja, HIL-igazolt).

Kapuk ON elott (pre-run safety review B-1/B-2): a muszer mod-valasza == "SER"; CH1/CH2
SOUR:VOLT?/SOUR:CURR? kivetel nelkul es <= setpoint; DMM |V| < 0.1 V. ON-hataridő MAX_ON_S
(B-3), rovid VISA-timeout, max_retries=1. Cleanup minden kilepesi uton, BaseException-t is
fogva (B-4): Load OFF -> PSU ALL OFF -> CH2 OUTP:ENAB OFF -> readback; INDEPENDENT mod csak
igazolt OFF utan. A mert idok a VISA/USB/LAN atvitelt is tartalmazzak; nem fizikai
kapcsolasi idok.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyvisa
import yaml  # type: ignore[import-untyped]

from Prog.drivers.device_dmm import Keysight34465ADMM
from Prog.drivers.device_load import Keithley2380Load
from Prog.drivers.device_psu import Keithley2220PSU

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
CONFIRM_TOKEN = "ENERGIZE_SERIES_DMM_ONLY_FIXTURE"
# Mod -> (energize token, setter, a muszer vart valasza). PARALLEL: 2220S-900-01 Rev. C
# 24-25. o. parhuzamos bekotes; a soros athidalas ELTAVOLITVA (felhasznaloi megerosites).
MODES = {
    "series": ("ENERGIZE_SERIES_DMM_ONLY_FIXTURE", "set_mode_series", "SER"),
    "parallel": ("ENERGIZE_PARALLEL_NO_SERIES_JUMPER_FIXTURE", "set_mode_parallel", "PAR"),
}
SETPOINT_V = 1.0
SETPOINT_A = 0.01
TOL = 1e-3
MAX_ON_S = 12.0
POLL_MAX_S = 3.0
IO_TIMEOUT_MS = 2000
OFF_DMM_LIMIT_V = 0.1
ON_PLAUSIBLE_MIN_V = 0.3


class ProbeAbort(RuntimeError):
    pass


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--energize-token", required=True)
    ap.add_argument("--mode", choices=tuple(MODES), default="series")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--current-a", type=float, default=SETPOINT_A,
                    help="PSU aramkorlat (0.001..0.05 A; 47 ohm mellett 0.05 A -> CV ~21 mA)")
    ap.add_argument("--skip-production-measure", action="store_true",
                    help="A mar HIL-igazoltan akado produkcios SERIES-meres kihagyasa (rovidebb ON)")
    ap.add_argument("--fixture", default="SERIES jumper; DMM_V on series output; no load; "
                    "no battery; Load input open")
    args = ap.parse_args()
    token, setter, expected_mode = MODES[args.mode]
    if args.energize_token != token:
        raise SystemExit(f"--energize-token must equal {token} for --mode {args.mode}")
    if not (math.isfinite(args.current_a) and 0.001 <= args.current_a <= 0.05):
        raise SystemExit("--current-a must be within 0.001..0.05 A")
    current_a = args.current_a
    cfg = yaml.safe_load((ROOT / "Prog/config/local_config.yaml").read_text(encoding="utf-8"))
    res = {k: cfg["instruments"][k]["resource"] for k in ("psu", "load", "dmm_voltage")}
    report: dict[str, Any] = {
        "utc": utc(), "status": "running", "steps": [], "cleanup": {}, "mode": args.mode,
        "fixture": args.fixture,
        "setpoint": [SETPOINT_V, current_a], "max_on_s": MAX_ON_S, "io_timeout_ms": IO_TIMEOUT_MS,
        "pon_gate": "removed in production (R13-B4, R13-UD-PSU-PON-REMOVE)",
        "limitation": "durations include VISA transport; not physical switching times",
    }
    rm = pyvisa.ResourceManager()
    psu = Keithley2220PSU(timeout_ms=IO_TIMEOUT_MS, max_retries=1)
    load = Keithley2380Load(timeout_ms=IO_TIMEOUT_MS, max_retries=1)
    dmm = Keysight34465ADMM(timeout_ms=IO_TIMEOUT_MS, max_retries=1)
    t_on: float | None = None

    def rec(name: str, fn, *a, required: bool = False) -> dict[str, Any]:
        if t_on is not None and time.perf_counter() - t_on > MAX_ON_S:
            raise ProbeAbort(f"MAX_ON_S exceeded before step {name}")
        t0 = time.perf_counter()
        entry: dict[str, Any] = {"step": name, "utc": utc()}
        try:
            entry["value"] = fn(*a)
        except Exception as exc:  # noqa: BLE001 -- rogzitjuk; required eseten megallunk
            entry["exception"] = f"{type(exc).__name__}: {exc}"
        entry["elapsed_s"] = round(time.perf_counter() - t0, 4)
        report["steps"].append(entry)
        if required and "exception" in entry:
            raise ProbeAbort(f"required step failed: {name}: {entry['exception']}")
        return entry

    def raw(msg: str) -> str:
        return psu._query(msg).strip()

    def raw_err(name: str, msg: str, required: bool = False) -> dict[str, Any]:
        entry = rec(name + " " + msg, raw, msg, required=required)
        if "exception" in entry:
            rec(name + " device_clear", psu._resource.clear)
        err = rec(name + " SYST:ERR?", raw, "SYST:ERR?")
        if required and not str(err.get("value", "")).startswith("0,"):
            raise ProbeAbort(f"SCPI error after {msg}: {err}")
        return entry

    def finite(entry: dict[str, Any]) -> float:
        value = float(entry["value"])
        if not math.isfinite(value):
            raise ProbeAbort(f"non-finite {entry}")
        return value

    try:
        # R15-B1: a failed energy connection must not skip its peer.
        connect_errors: list[BaseException] = []
        for name, dev in (("load", load), ("psu", psu)):
            try:
                dev.connect(res[name], rm=rm)
                report[f"{name}_idn"] = dev.idn().strip()
            except BaseException as exc:
                logger.exception("Energy connection failed: %s", name)
                report.setdefault("connect_failures", {})[name] = repr(exc)
                connect_errors.append(exc)
        if connect_errors:
            raise connect_errors[0]
        dmm.connect(res["dmm_voltage"], rm=rm)
        report["dmm_voltage_idn"] = dmm.idn().strip()
        if "2380-120-60" not in report["load_idn"] or "2220-30-1" not in report["psu_idn"] \
                or "34465A" not in report["dmm_voltage_idn"]:
            raise ProbeAbort("identity mismatch")
        load.input_off()
        if load.read_input_state() is not False:
            raise ProbeAbort("Load INP not OFF")
        psu.all_outputs_off()
        if psu.read_output_state() is not False:
            raise ProbeAbort("PSU not OFF")
        dmm.configure_dcv(10.0, 1.0)
        getattr(psu, setter)()
        mode = rec("mode_readback", psu.query_combination_mode, required=True)
        if mode["value"] != expected_mode:               # B-1: a muszer valasza
            raise ProbeAbort(f"{args.mode} not confirmed by instrument: {mode['value']!r}")
        psu.set_output_voltage(SETPOINT_V)
        psu.set_output_current(current_a)
        for ch in ("CH1", "CH2"):                        # B-2: pre-ON setpoint kapu
            v = finite(raw_err("pre_on", f"INST:SEL {ch};:SOUR:VOLT?", required=True))
            i = finite(raw_err("pre_on", f"INST:SEL {ch};:SOUR:CURR?", required=True))
            if not (0.0 <= v <= SETPOINT_V * (1 + TOL)) or not (0.0 <= i <= current_a * (1 + TOL)):
                raise ProbeAbort(f"{ch} inherited setpoint unsafe: V={v} I={i}")
        raw_err("pre_on", "INST:SEL CH1;*OPC?", required=True)
        for k in range(2):
            v = finite(rec(f"dmm_off_{k}", dmm.read_voltage, required=True))
            if abs(v) >= OFF_DMM_LIMIT_V:
                raise ProbeAbort(f"DMM not ~0 V before ON: {v}")
        if load.read_input_state() is not False:
            raise ProbeAbort("Load INP not OFF before PSU ON")
        # --- ENERGIZE (produkcios ut, egy kiserlet) ---
        t_on = time.perf_counter()
        report["on_command_utc"] = utc()
        on = rec("psu.output_on", psu.output_on)
        if "exception" in on:
            raise ProbeAbort(f"output_on uncertain: {on['exception']}")
        while True:
            if psu.read_output_state():
                report["on_readback_s"] = round(time.perf_counter() - t_on, 4)
                break
            if time.perf_counter() - t_on > POLL_MAX_S:
                raise ProbeAbort("PSU ON readback not reached")
        time.sleep(0.5)
        # Plauzibilitas ON utan (pre-run review (d)): bennmaradt athidalas -> ~0 V.
        first = rec("dmm_on_0", dmm.read_voltage, required=True)
        if finite(first) < ON_PLAUSIBLE_MIN_V:
            raise ProbeAbort(f"DMM below {ON_PLAUSIBLE_MIN_V} V after ON (wiring?): {first['value']}")
        for k in range(1, 3):
            rec(f"dmm_on_{k}", dmm.read_voltage)
        for msg in ("MEAS:VOLT? CH1", "MEAS:VOLT? ALL", "MEAS:VOLT?",
                    "INST:SEL CH2;:MEAS:VOLT?;:INST:SEL CH1;*OPC?", "INST:SEL?",
                    "MEAS:CURR? CH1", "MEAS:CURR? ALL", "STAT:OPER:INST:ISUM1:COND?",
                    "STAT:OPER:INST:ISUM2:COND?"):
            raw_err("on", msg)
        rec("on dmm_after_queries", dmm.read_voltage)
        # produkcios SERIES-feszultsegmeres (parameteres CH2) -- utolso ON-lepes
        if args.skip_production_measure:
            report["production_measure"] = "skipped (HIL 2026-11-05: ON-ban is 13.6 s timeout)"
        else:
            prod = rec("on production measure_output_voltage", psu.measure_output_voltage)
            if "exception" in prod:
                rec("on device_clear", psu._resource.clear)
            raw_err("on_after_prod", "INST:SEL?")
        # --- OFF ---
        t_off = time.perf_counter()
        rec("psu.all_outputs_off", psu.all_outputs_off, required=True)
        while psu.read_output_state():
            if time.perf_counter() - t_off > POLL_MAX_S:
                raise ProbeAbort("PSU OFF readback not reached")
        report["off_readback_s"] = round(time.perf_counter() - t_off, 4)
        report["on_total_s"] = round(time.perf_counter() - t_on, 4)
        t_on = None
        time.sleep(0.5)
        for k in range(2):
            rec(f"dmm_after_off_{k}", dmm.read_voltage)
        report["status"] = "done"
    except BaseException as exc:  # noqa: BLE001 -- minden ut a cleanupra fut
        report["status"] = "failed"
        report["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        cl = report["cleanup"]
        steps = (("load_off", load.input_off), ("psu_off", psu.all_outputs_off),
                 ("psu_ch2_enab_off", lambda: psu._query("INST:SEL CH2;:OUTP:ENAB OFF;:INST:SEL CH1;*OPC?")))
        for name, fn in steps:                            # B-4: BaseException is
            try:
                fn()
                cl[name] = "ok"
            except BaseException as exc:  # noqa: BLE001
                cl[name] = repr(exc)
        for name, reader in (("load_inp", load.read_input_state), ("psu_outp", psu.read_output_state)):
            try:
                cl[name] = reader()
            except BaseException as exc:
                logger.exception("Cleanup readback failed: %s", name)
                cl[name] = repr(exc)
        if (cl.get("psu_off") == "ok" and cl.get("psu_ch2_enab_off") == "ok"
                and cl.get("psu_outp") is False):
            try:
                psu.set_mode_independent()
                cl["psu_mode"] = psu.query_combination_mode()
            except BaseException as exc:
                logger.exception("PSU mode restore failed")
                cl["psu_mode"] = repr(exc)
        else:
            cl["psu_mode"] = "skipped_off_unverified"
        for name, instrument in (("dmm", dmm), ("load", load), ("psu", psu)):
            try:
                instrument.disconnect()
            except BaseException as exc:  # noqa: BLE001
                cl[f"{name}_disconnect"] = repr(exc)
        try:
            rm.close()
        except BaseException as exc:  # noqa: BLE001
            cl["rm_close"] = repr(exc)
        # R15-B1: required cleanup is part of the final result.
        failures = [name for name in ("load_off", "psu_off", "psu_ch2_enab_off") if cl.get(name) != "ok"]
        failures.extend(name for name in ("load_inp", "psu_outp") if cl.get(name) is not False)
        if cl.get("psu_mode") != "OFF":
            failures.append("psu_mode")
        report["cleanup_failures"] = failures
        if failures:
            report["status"] = "failed"
            report["cleanup_fault"] = "SAFE_OFF_OR_RESTORE_UNCONFIRMED"
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / datetime.now(timezone.utc).strftime(f"psu_{args.mode}_energized_%Y%m%dT%H%M%SZ.json")
        path.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        print(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    return 0 if report["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
