"""
Parancs-teszt — minden program által kiadható SCPI parancs ellenőrzése.
Akkumulátor nélkül futtatható.
  PSU : 0 V / 0.01 A szint, OUTPUT ON majd OFF
  Load: CC mód, 0 A setpoint, INPUT ON majd OFF

Futtatás (Kör 20 Batch E7, HW20-04b óta explicit megerősítés kell, l.
_confirm_energizing_allowed()):
    python Prog/tools/command_test.py --i-confirm-no-battery-connected
vagy interaktívan (flag nélkül, a program megkérdezi):
    python Prog/tools/command_test.py
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from typing import Callable, Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402


# ------------------------------------------------------------------ #
# Eredmény nyilvántartás                                              #
# ------------------------------------------------------------------ #

_RESULTS: list[tuple[str, str, str, str]] = []   # (status, section, cmd, detail)

# Kör 20 Batch E7 (HW20-06, DFAM-001): ugyanaz az elvárt-modell-substring
# lista, mint az éles úton már bevált instrument_manager.py::identify_all(
# expected_models=...) / main_window.py:340-341 hívóhelye — itt kézzel
# ismételve, mert ez a szkript nem InstrumentManager-en, hanem nyers
# driver-objektumokon keresztül dolgozik.
_PSU_EXPECTED_IDN_SUBSTR = "2220"
_LOAD_EXPECTED_IDN_SUBSTR = "2380-120-60"  # R3-B2: pontos modell


def _ok(section: str, cmd: str, detail: str = "") -> None:
    _RESULTS.append(("✅", section, cmd, detail))
    detail_str = f"  → {detail}" if detail else ""
    print(f"    ✅ {cmd}{detail_str}")


def _fail(section: str, cmd: str, exc: Exception) -> None:
    _RESULTS.append(("❌", section, cmd, str(exc)))
    print(f"    ❌ {cmd}  [{type(exc).__name__}: {exc}]")


def _run(
    section: str,
    label: str,
    fn: Callable[[], Any],
    fmt: Callable[[Any], str] | None = None,
) -> bool:
    try:
        result = fn()
        detail = fmt(result) if fmt and result is not None else (str(result) if result is not None else "")
        _ok(section, label, detail)
        return True
    except Exception as exc:
        _fail(section, label, exc)
        return False


# ------------------------------------------------------------------ #
# PSU — Keithley 2220-30-1                                           #
# ------------------------------------------------------------------ #

def test_psu(cfg: dict) -> None:
    from Prog.drivers.device_psu import Keithley2220PSU
    resource = cfg["instruments"]["psu"]["resource"]
    sec = "PSU"
    print(f"\n=== {sec}  ({resource}) ===")

    psu = Keithley2220PSU()
    try:
        if not _run(sec, "connect()", lambda: psu.connect(resource)):
            print("    ⏭  connect sikertelen — PSU tesztek kihagyva")
            return

        idn = ""
        try:
            idn = psu.idn()
            _ok(sec, "idn()", idn.strip())
        except Exception as exc:
            _fail(sec, "idn()", exc)

        # HW20-06 (Kör 20 Batch E7, DFAM-001): felcserélt VISA resource-
        # string esetén a driver a MÁSIK fizikai eszközre küldené az
        # OUTPUT ON-t — ugyanaz az ellenőrzés, mint az éles úton már bevált
        # instrument_manager.py::identify_all(expected_models=...), ide
        # kiterjesztve, MIELŐTT bármilyen setpoint-írás vagy energizálás
        # történne.
        if _PSU_EXPECTED_IDN_SUBSTR not in idn:
            print(f"    ⛔ PSU azonosítás sikertelen: várt {_PSU_EXPECTED_IDN_SUBSTR!r} "
                  f"substring, kapott IDN={idn!r} — energizálás KIHAGYVA "
                  f"(felcserélt VISA resource gyanús).")
            return

        _run(sec, "clear_status()", psu.clear_status)
        _run(sec, "check_error()",  psu.check_error,  lambda r: "OK" if not r else f"hibák: {r}")
        # query_combination_mode() NEM programparancs — a program combination_mode attribútumot
        # használja. INST:COMB? fw 1.15-1.05-ön nem támogatott (170 Invalid command).

        # Setpoint — 0 V / 0.01 A (biztonságos, nincs akkumulátor)
        voltage_setpoint_ok = _run(sec, "set_output_voltage(0.0)",  lambda: psu.set_output_voltage(0.0))
        current_setpoint_ok = _run(sec, "set_output_current(0.01)", lambda: psu.set_output_current(0.01))

        # Readback — OUTPUT OFF
        _run(sec, "measure_output_voltage() [OFF]", psu.measure_output_voltage, lambda r: f"{r:.4f} V")
        _run(sec, "measure_output_current() [OFF]", psu.measure_output_current, lambda r: f"{r:.4f} A")

        # HW20-04(a) (Kör 20 Batch E7, DFAM-018): output_on() korábban
        # FELTÉTEL NÉLKÜL futott, akkor is, ha a fenti két setpoint-írás
        # valamelyike hibázott — ezen az úton output_on() az ELŐZŐ
        # session/előlap setpointjával energizált volna, nem a most kért
        # 0 V / 0.01 A-val.
        if voltage_setpoint_ok and current_setpoint_ok:
            _run(sec, "output_on()", psu.output_on)
            _run(sec, "measure_output_voltage() [ON]",  psu.measure_output_voltage, lambda r: f"{r:.4f} V")
            _run(sec, "measure_output_current() [ON]",  psu.measure_output_current, lambda r: f"{r:.4f} A")
            _run(sec, "output_off()", psu.output_off)
        else:
            print("    ⏭  setpoint írása sikertelen — output_on() KIHAGYVA (HW20-04a)")

        # all_outputs_off (instrument_manager safe_all_off hívja)
        _run(sec, "all_outputs_off()", psu.all_outputs_off)

        # Kombinációs mód visszaállítás INDEPENDENT-re (safety_manager hívja mód-ellenőrzéskor)
        _run(sec, "set_mode_independent()", psu.set_mode_independent)

        _run(sec, "check_error() post", psu.check_error, lambda r: "OK" if not r else f"hibák: {r}")

    finally:
        psu.safe_off()
        psu.disconnect()


# ------------------------------------------------------------------ #
# Load — Keithley 2380-120-60                                        #
# ------------------------------------------------------------------ #

def test_load(cfg: dict) -> None:
    from Prog.drivers.device_load import Keithley2380Load
    resource = cfg["instruments"]["load"]["resource"]
    sec = "Load"
    print(f"\n=== {sec}  ({resource}) ===")

    load = Keithley2380Load()
    try:
        if not _run(sec, "connect()", lambda: load.connect(resource)):
            print("    ⏭  connect sikertelen — Load tesztek kihagyva")
            return

        idn = ""
        try:
            idn = load.idn()
            _ok(sec, "idn()", idn.strip())
        except Exception as exc:
            _fail(sec, "idn()", exc)

        # HW20-06 (Kör 20 Batch E7, DFAM-001): l. test_psu() azonos
        # kommentje — itt a Load oldalon, MIELŐTT bármilyen setpoint-írás
        # vagy energizálás (input_on()) történne.
        if _LOAD_EXPECTED_IDN_SUBSTR not in idn:
            print(f"    ⛔ Load azonosítás sikertelen: várt {_LOAD_EXPECTED_IDN_SUBSTR!r} "
                  f"substring, kapott IDN={idn!r} — energizálás KIHAGYVA "
                  f"(felcserélt VISA resource vagy nem támogatott Load-modell gyanús).")
            return

        _run(sec, "clear_status()", load.clear_status)
        _run(sec, "check_error()",  load.check_error,  lambda r: "OK" if not r else f"hibák: {r}")

        # CC mód + 0 A setpoint
        mode_cc_ok = _run(sec, "set_mode_cc()",    load.set_mode_cc)
        current_setpoint_ok = _run(sec, "set_current(0.0)", lambda: load.set_current(0.0))

        # Readback — INPUT OFF
        _run(sec, "measure_voltage() [OFF]", load.measure_voltage, lambda r: f"{r:.4f} V")
        _run(sec, "measure_current() [OFF]", load.measure_current, lambda r: f"{r:.4f} A")
        _run(sec, "measure_power()   [OFF]", load.measure_power,   lambda r: f"{r:.4f} W")

        # HW20-04(a) (Kör 20 Batch E7, DFAM-018): l. test_psu() azonos
        # kommentje — input_on() korábban feltétel nélkül futott, akkor is,
        # ha a mód/setpoint-írás hibázott.
        if mode_cc_ok and current_setpoint_ok:
            _run(sec, "input_on()", load.input_on)
            _run(sec, "measure_voltage() [ON]",  load.measure_voltage, lambda r: f"{r:.4f} V")
            _run(sec, "measure_current() [ON]",  load.measure_current, lambda r: f"{r:.4f} A")
            _run(sec, "measure_power()   [ON]",  load.measure_power,   lambda r: f"{r:.4f} W")
            _run(sec, "input_off()", load.input_off)
        else:
            print("    ⏭  mód/setpoint írása sikertelen — input_on() KIHAGYVA (HW20-04a)")

        _run(sec, "check_error() post", load.check_error, lambda r: "OK" if not r else f"hibák: {r}")

    finally:
        load.safe_off()
        load.disconnect()


# ------------------------------------------------------------------ #
# DMM_V — Keysight 34465A (DCV)                                      #
# ------------------------------------------------------------------ #

def test_dmm_voltage(cfg: dict) -> None:
    from Prog.drivers.device_dmm import Keysight34465ADMM
    resource = cfg["instruments"]["dmm_voltage"]["resource"]
    sec = "DMM_V"
    print(f"\n=== {sec}  ({resource}) ===")

    dmm = Keysight34465ADMM()
    try:
        if not _run(sec, "connect()", lambda: dmm.connect(resource)):
            print("    ⏭  connect sikertelen — DMM_V tesztek kihagyva")
            return

        _run(sec, "idn()",          dmm.idn,          lambda r: r.strip())
        _run(sec, "clear_status()", dmm.clear_status)
        _run(sec, "check_error()",  dmm.check_error,  lambda r: "OK" if not r else f"hibák: {r}")

        # DCV konfiguráció (program szerinti paraméterek: 100 V range, 10 NPLC)
        _run(sec, "configure_dcv(100, 10.0)", lambda: dmm.configure_dcv(range_V=100, nplc=10.0))
        _run(sec, "read_voltage() [1. minta]", dmm.read_voltage, lambda r: f"{r:.6f} V")
        _run(sec, "read_voltage() [2. minta — jump detector]", dmm.read_voltage, lambda r: f"{r:.6f} V")

        # NPLC váltás (charge_controller gyors/lassú mód)
        _run(sec, "set_nplc(1.0)",  lambda: dmm.set_nplc(1.0))
        _run(sec, "autorange_off()", dmm.autorange_off)
        _run(sec, "read_voltage() [NPLC=1]", dmm.read_voltage, lambda r: f"{r:.6f} V")

        _run(sec, "check_error() post", dmm.check_error, lambda r: "OK" if not r else f"hibák: {r}")

    finally:
        dmm.disconnect()


# ------------------------------------------------------------------ #
# DMM_T — Keysight 34465A (4-wire PT100)                             #
# ------------------------------------------------------------------ #

def test_dmm_temperature(cfg: dict) -> None:
    from Prog.drivers.device_dmm import Keysight34465ADMM
    resource = cfg["instruments"]["dmm_temperature"]["resource"]
    sec = "DMM_T"
    print(f"\n=== {sec}  ({resource}) ===")

    dmm = Keysight34465ADMM()
    try:
        if not _run(sec, "connect()", lambda: dmm.connect(resource)):
            print("    ⏭  connect sikertelen — DMM_T tesztek kihagyva")
            return

        _run(sec, "idn()",          dmm.idn,          lambda r: r.strip())
        _run(sec, "clear_status()", dmm.clear_status)
        _run(sec, "check_error()",  dmm.check_error,  lambda r: "OK" if not r else f"hibák: {r}")

        # FRTD konfiguráció + két mérés
        _run(sec, "configure_temp_4wire_pt100(10.0)", lambda: dmm.configure_temp_4wire_pt100(nplc=10.0))
        _run(sec, "read_temperature() [1. minta]", dmm.read_temperature, lambda r: f"{r:.2f} °C")
        _run(sec, "read_temperature() [2. minta]", dmm.read_temperature, lambda r: f"{r:.2f} °C")

        _run(sec, "check_error() post", dmm.check_error, lambda r: "OK" if not r else f"hibák: {r}")

    finally:
        dmm.disconnect()


# ------------------------------------------------------------------ #
# Összefoglaló + main                                                 #
# ------------------------------------------------------------------ #

def print_summary() -> int:
    print("\n" + "=" * 60)
    print("=== Összefoglaló ===")
    sections: dict[str, dict] = {}
    for status, section, cmd, detail in _RESULTS:
        s = sections.setdefault(section, {"ok": 0, "fail": 0, "fails": []})
        if status == "✅":
            s["ok"] += 1
        else:
            s["fail"] += 1
            s["fails"].append(cmd)

    total_ok = total_fail = 0
    for sec, counts in sections.items():
        ok, fail = counts["ok"], counts["fail"]
        total_ok += ok
        total_fail += fail
        icon = "✅" if fail == 0 else "❌"
        fail_str = f"  FAIL: {counts['fails']}" if counts["fails"] else ""
        print(f"  {icon} {sec:<8s}: {ok} OK, {fail} FAIL{fail_str}")

    print(f"\n  Összesen: {total_ok} OK, {total_fail} FAIL")
    return 1 if total_fail > 0 else 0


def load_config() -> dict:
    local = _ROOT / "Prog" / "config" / "local_config.yaml"
    default = _ROOT / "Prog" / "config" / "default_config.yaml"
    path = local if local.exists() else default
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _confirm_energizing_allowed(auto_confirmed: bool) -> bool:
    """HW20-04(b) (Kör 20 Batch E7, DFAM-044 `energizing_tool_script_
    without_operator_approval_gate`): korábban a szkript FELTÉTEL NÉLKÜL
    energizált (OUTPUT ON / INPUT ON) a `--i-confirm-no-battery-connected`
    flag vagy bármilyen interaktív megerősítés nélkül (CLAUDE.md §6,
    rules/10-safety.md #8: explicit felhasználói HIL-jóváhagyás). Ha
    `auto_confirmed` True (a flag meg lett adva), nincs kérdés. Egyébként
    interaktív megerősítést kér — ha a bemenet nem terminál (`EOFError`,
    pl. automatizált/nem-interaktív futtatás), FAIL-CLOSED: nincs
    energizálás."""
    if auto_confirmed:
        return True
    print("Ez a szkript VALÓDI OUTPUT ON / INPUT ON parancsot küld a PSU-nak és a")
    print("Load-nak (CLAUDE.md §6, rules/10-safety.md #8: explicit HIL-jóváhagyás).")
    try:
        answer = input("Biztosan NINCS akkumulátor csatlakoztatva a kimenetekre? (igen/nem): ")
    except EOFError:
        print("\nNincs interaktív bemenet és a --i-confirm-no-battery-connected flag "
              "sincs megadva — megszakítva.")
        return False
    return answer.strip().lower() in ("igen", "i", "yes", "y")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Minden program által kiadható SCPI parancs ellenőrzése — "
                     "VALÓDI OUTPUT ON / INPUT ON parancsot küld a PSU-nak és a Load-nak.",
    )
    parser.add_argument(
        "--i-confirm-no-battery-connected",
        action="store_true",
        help="Explicit megerősítés: nincs akkumulátor a PSU/Load kimenetére csatlakoztatva "
             "(kihagyja az interaktív megerősítő kérdést).",
    )
    args = parser.parse_args(argv)

    print("=== Akkuteszter — Command Test ===")
    print("Minden program által kiadható SCPI parancs ellenőrzése.")
    print("PSU: 0 V / 0.01 A. Load: 0 A CC mód. Akkumulátor NEM szükséges.\n")

    if not _confirm_energizing_allowed(args.i_confirm_no_battery_connected):
        print("Megszakítva — nincs megerősítés az energizáláshoz.")
        return 1

    try:
        cfg = load_config()
    except Exception as exc:
        print(f"❌ Config betöltési hiba: {exc}")
        return 1

    test_psu(cfg)
    test_load(cfg)
    test_dmm_voltage(cfg)
    test_dmm_temperature(cfg)
    return print_summary()


if __name__ == "__main__":
    if sys.platform == "win32":
        import io as _io
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
