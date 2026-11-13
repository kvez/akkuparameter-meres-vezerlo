"""R13 HIL B (energized) -- Keithley 2380 Load INPUT ON/OFF ures bemenettel, 2026-11-05.

Felhasznaloi engedelyek (chat, 2026-11-05): "Teszteljuk le az energizalo parancsokat is",
"te inditod majd a parancsokat", "igen, utana csinaljuk a load energizalo tesztet".
Fixture: a Load bemenete URES (nincs forras, nincs akku); a PSU vegig OFF (a probe csak
OFF-ot ad ki a PSU-ra es readbacket ker). Aram nem folyik: a teszt a parancs-elfogadast,
readbacket, hibasort es idoziteset igazolja, NEM a valodi aramfelvetelt.

A produkcios Load-driveren keresztul fut (connect: *CLS, INP OFF, TRAN 0, INP:SHOR/TIM OFF,
VOLT:ON 0, FIX mod). CC 0.01 A, input_on() egy kiserlettel, ON-readback korlatos pollinggal,
merese, azonnali input_off(), OFF-readback. A Load tranziens (TRAN 1 / FORC:TRIG) ebben a
probeban NEM fut (INSTR-LOAD-RANGE-002 nyitott eloffeltetel; Codex rev3 indoklas).
Kapuk ON elott: Load OFF-readback; PSU OFF-readback; Load MEAS:VOLT? |V| < 0.5 V (ures
bemenet); SYST:ERR? ures. Cleanup minden uton (BaseException is): Load OFF -> PSU ALL OFF ->
readback. A mert idok a VISA/USB atvitelt is tartalmazzak; nem fizikai kapcsolasi idok.
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

from Prog.drivers.device_load import Keithley2380Load
from Prog.drivers.device_psu import Keithley2220PSU

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
CONFIRM_TOKEN = "ENERGIZE_LOAD_EMPTY_INPUT_FIXTURE"
CURRENT_A = 0.01
MAX_ON_S = 10.0
POLL_MAX_S = 3.0
IO_TIMEOUT_MS = 2000
EMPTY_INPUT_MAX_V = 0.5
ON_MAX_ABS_A = 0.002   # ures bemenettel aram nem folyhat (review javaslat 2)


class ProbeAbort(RuntimeError):
    pass


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--energize-token", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    if args.energize_token != CONFIRM_TOKEN:
        raise SystemExit(f"--energize-token must equal {CONFIRM_TOKEN}")
    if not 1 <= args.repeat <= 5:
        raise SystemExit("--repeat must be 1..5")
    cfg = yaml.safe_load((ROOT / "Prog/config/local_config.yaml").read_text(encoding="utf-8"))
    res = {k: cfg["instruments"][k]["resource"] for k in ("psu", "load")}
    report: dict[str, Any] = {
        "utc": utc(), "status": "running", "steps": [], "cycles": [], "cleanup": {},
        "fixture": "Load input open (no source, no battery); PSU OFF throughout",
        "current_a": CURRENT_A, "max_on_s": MAX_ON_S, "io_timeout_ms": IO_TIMEOUT_MS,
        "pon_gate": "removed in production (R13-B4); PSU only OFF",
        "limitation": "no current flows (empty input); durations include VISA transport",
    }
    rm = pyvisa.ResourceManager()
    load = Keithley2380Load(timeout_ms=IO_TIMEOUT_MS, max_retries=1)
    psu = Keithley2220PSU(timeout_ms=IO_TIMEOUT_MS, max_retries=1)
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

    def load_err(name: str) -> None:
        err = rec(name + " SYST:ERR?", lambda: load._query("SYST:ERR?").strip(), required=True)
        if not str(err["value"]).replace(" ", "").startswith("0,"):
            raise ProbeAbort(f"Load SCPI error at {name}: {err['value']}")

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
        if "2380-120-60" not in report["load_idn"] or "2220-30-1" not in report["psu_idn"]:
            raise ProbeAbort("identity mismatch")
        load.input_off()
        if load.read_input_state() is not False:
            raise ProbeAbort("Load INP not OFF")
        psu.all_outputs_off()
        if psu.read_output_state() is not False:
            raise ProbeAbort("PSU not OFF")
        load.set_mode_cc()
        load.set_current(CURRENT_A)
        load_err("pre_on setup")
        # Remote sense OFF kell, kulonben a MEAS:VOLT? a sense-kapcsokat meri (review javaslat 1).
        sense = rec("pre_on SYST:SENS?", lambda: load._query("SYST:SENS?").strip(), required=True)
        if str(sense["value"]).upper() not in {"0", "OFF"}:
            raise ProbeAbort(f"Load remote sense not OFF: {sense['value']!r}")
        load_err("pre_on SYST:SENS?")
        v_in = rec("pre_on load MEAS:VOLT", load.measure_voltage, required=True)
        if not math.isfinite(float(v_in["value"])) or abs(float(v_in["value"])) >= EMPTY_INPUT_MAX_V:
            raise ProbeAbort(f"Load input not empty/open (|V| >= {EMPTY_INPUT_MAX_V}): {v_in['value']}")
        for cycle in range(args.repeat):
            if psu.read_output_state() is not False:
                raise ProbeAbort("PSU not OFF before Load ON")
            c: dict[str, Any] = {"cycle": cycle}
            t_on = time.perf_counter()
            # R16-B10F1: a saját 3 s-os poll előtt ne fusson driver-nyugta.
            on = rec(f"c{cycle} load.input_on", lambda: load.input_on(wait_for_ack=False))
            if "exception" in on:
                raise ProbeAbort(f"input_on uncertain: {on['exception']}")
            while True:
                if load.read_input_state():
                    c["on_readback_s"] = round(time.perf_counter() - t_on, 4)
                    break
                if time.perf_counter() - t_on > POLL_MAX_S:
                    raise ProbeAbort("Load ON readback not reached")
            for mname, fn in (("MEAS:VOLT", load.measure_voltage), ("MEAS:CURR", load.measure_current),
                              ("MEAS:POW", load.measure_power), ("QUES", load.read_questionable_status)):
                c[mname] = rec(f"c{cycle} on {mname}", fn).get("value")
            v_on, i_on = c.get("MEAS:VOLT"), c.get("MEAS:CURR")
            if not isinstance(v_on, float) or not isinstance(i_on, float)                     or abs(v_on) >= EMPTY_INPUT_MAX_V or abs(i_on) > ON_MAX_ABS_A:
                raise ProbeAbort(f"Load ON plausibility failed (source connected?): V={v_on} I={i_on}")
            load_err(f"c{cycle} on")
            t_off = time.perf_counter()
            rec(f"c{cycle} load.input_off", load.input_off, required=True)
            while load.read_input_state():
                if time.perf_counter() - t_off > POLL_MAX_S:
                    raise ProbeAbort("Load OFF readback not reached")
            c["off_readback_s"] = round(time.perf_counter() - t_off, 4)
            c["on_total_s"] = round(time.perf_counter() - t_on, 4)
            t_on = None
            load_err(f"c{cycle} off")
            report["cycles"].append(c)
        report["status"] = "done"
    except BaseException as exc:  # noqa: BLE001 -- minden ut a cleanupra fut
        report["status"] = "failed"
        report["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        cl = report["cleanup"]
        for name, off_fn in (("load_off", load.input_off), ("psu_off", psu.all_outputs_off)):
            try:
                off_fn()
                cl[name] = "ok"
            except BaseException as exc:  # noqa: BLE001
                cl[name] = repr(exc)
        for name, reader in (("load_inp", load.read_input_state), ("psu_outp", psu.read_output_state)):
            try:
                cl[name] = reader()
            except BaseException as exc:  # noqa: BLE001
                cl[name] = repr(exc)
        for name, dev in (("load", load), ("psu", psu)):
            try:
                dev.disconnect()
            except BaseException as exc:  # noqa: BLE001
                cl[f"{name}_disconnect"] = repr(exc)
        try:
            rm.close()
        except BaseException as exc:  # noqa: BLE001
            cl["rm_close"] = repr(exc)
        # R15-B1: required cleanup is part of the final result.
        failures = [name for name in ("load_off", "psu_off") if cl.get(name) != "ok"]
        failures.extend(name for name in ("load_inp", "psu_outp") if cl.get(name) is not False)
        report["cleanup_failures"] = failures
        if failures:
            report["status"] = "failed"
            report["cleanup_fault"] = "SAFE_OFF_OR_RESTORE_UNCONFIRMED"
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / datetime.now(timezone.utc).strftime("load_energized_%Y%m%dT%H%M%SZ.json")
        path.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        print(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    return 0 if report["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
