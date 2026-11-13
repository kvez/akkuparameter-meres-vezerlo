"""
Musszer-only HIL-ellenorzes: DMM NPLC / SAMPle:TIMer osszeferhetoseg.

CSAK a feszultseg-DMM-et (dmm_voltage, local_config.yaml) erinti. PSU-t es
Load-ot NEM epit fel, NEM kapcsol be -- akku/terheles bevonasa nelkuli,
CLAUDE.md 6. pont safety-jovahagyasat NEM igenylo lepes (nincs OUTPUT ON/
INPUT ON parancs a teljes futas alatt).

Cel (a felhasznaloi megbeszelt sorrend szerint -- elobb a muszer-parancsok
HIL-ellenorzese, csak utana johet barmelyik elo terhelesu pulzus-meres):

  (A) STANDARD 1s/10s/30s pulzus utvonal: NPLC=10 (session-alapertelmezes,
      main_window.py:202) + SAMP:TIM=0.2s -- a MAX-effort review agent es a
      fuggetlen verifier egyarant HIL_PENDING-kent tartja nyilvan
      (CLAIM_LEDGER.yaml INSTR-DMM-SAMPTIM-001), mert ez pontosan az
      50 Hz-es 0,2s-es apertura HATARAN van.
  (B) FAST_TRANSIENT utvonal: NPLC=0.02 (dinamikusan valtott) + SAMP:TIM=
      0.005s (5ms) -- meg soha nem probalt HIL-en, a fuggetlen verifier
      2026-09-12-i VERIFIED_WITH_FINDINGS jelentese explicit HIL-meresre
      utalt (a mock nem modellezi az autozero hatasat a tenyleges MIN-re).

Minden lepes utan SYST:ERR? -es hibasor-ellenorzes (rules/40-instruments.md,
feedback_check_scpi_errors_after_hil_commands.md). A szkript vegen a DMM
garantaltan visszaall NPLC=10-re es egyetlen-mintas/azonnali modra
(reset_to_single_sample()), fuggetlenul attol, hogy kozben mi tortent.

Futtatas:
    python Prog/tools/hil_dmm_nplc_samptim_instrument_only_check.py
"""
from __future__ import annotations
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402

from Prog.drivers.device_dmm import OVERLOAD_THRESHOLD, Keysight34465ADMM  # noqa: E402


def load_config() -> dict:
    local_path = _ROOT / "Prog" / "config" / "local_config.yaml"
    default_path = _ROOT / "Prog" / "config" / "default_config.yaml"
    path = local_path if local_path.exists() else default_path
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _errs(dmm: Keysight34465ADMM) -> list[str]:
    return dmm.check_error()


def _report_errs(label: str, errs: list[str]) -> None:
    if errs:
        print(f"    [SYST:ERR?] {label}: {len(errs)} hiba -> {errs}")
    else:
        print(f"    [SYST:ERR?] {label}: OK (0 hiba)")


def probe_case(
    dmm: Keysight34465ADMM,
    label: str,
    nplc: float,
    requested_interval_s: float,
    sample_count: int,
    run_real_acquisition: bool,
) -> dict:
    print(f"\n--- {label}: NPLC={nplc}, keert SAMP:TIM={requested_interval_s}s ---")
    result: dict = {"label": label, "nplc": nplc, "requested_interval_s": requested_interval_s}

    dmm.set_nplc(nplc)
    errs = _errs(dmm)
    _report_errs("set_nplc", errs)
    result["set_nplc_errors"] = errs

    nplc_readback = dmm.get_nplc()
    print(f"    NPLC readback (VOLT:DC:NPLC?): {nplc_readback}")
    result["nplc_readback"] = nplc_readback

    min_raw = dmm.query_sample_timer_capability()
    try:
        min_s = float(min_raw.strip())
    except ValueError:
        min_s = float("nan")
    print(f"    SAMP:TIM? MIN (a beallitott NPLC mellett): raw={min_raw!r} -> {min_s:.6g}s")
    result["samp_tim_min_raw"] = min_raw
    result["samp_tim_min_s"] = min_s
    result["min_exceeds_requested"] = (
        min_s > requested_interval_s if min_s == min_s else None  # NaN check
    )
    if result["min_exceeds_requested"]:
        print(
            f"    -> INFEASIBLE: MIN {min_s:.6g}s > kert {requested_interval_s:.6g}s "
            f"(a projekt sajat fail-safe kapuja EZEN a ponton utasitana el a merest)"
        )
    else:
        print(f"    -> MIN {min_s:.6g}s <= kert {requested_interval_s:.6g}s, elvben teljesitheto")

    dmm.configure_timed_acquisition(sample_count=sample_count, interval_s=requested_interval_s)
    errs = _errs(dmm)
    _report_errs("configure_timed_acquisition (SAMP:COUN/SOUR/TIM)", errs)
    result["configure_timed_acquisition_errors"] = errs

    # Kör 21 F16 (HW21-05(a)): korábban a privát `dmm._query("SAMP:TIM?")`-ot
    # hívta -- a driver saját, publikus `get_sample_timer_s()` accessora
    # (l. device_dmm.py) EPP EZT teszi (readback + `_parse_float()`), a
    # SCPI I/O-t a driver felelősségi körében tartva (rules/40-instruments.md).
    samp_tim_actual_s = dmm.get_sample_timer_s()
    print(f"    SAMP:TIM? (tenyleges, a muszer altal esetleg modositva): {samp_tim_actual_s:.6g}s")
    result["samp_tim_actual_s"] = samp_tim_actual_s

    if run_real_acquisition:
        import time

        dmm.arm_timed_acquisition()
        t0 = time.monotonic()
        timeout_s = max(5.0, sample_count * requested_interval_s * 3.0 + 5.0)
        raw_fetch = None
        while time.monotonic() - t0 < timeout_s:
            try:
                raw_fetch = dmm.fetch_timed_acquisition()
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(0.2)
        if raw_fetch is None:
            print(f"    [ACQUISITION] FETC? nem adott eredmenyt {timeout_s:.1f}s alatt: {last_exc}")
            result["acquisition_error"] = str(last_exc)
        else:
            # Kör 21 F16 (HW21-05(b)): korábban `[float(x) for x in ...]`
            # védtelen volt -- egy hibás/nem-numerikus token uncaught
            # ValueError-t dobott volna (elveszítve a MÁR megszerzett
            # mérési eredményt), és egy "9.9E37" overload-token CSENDBEN
            # bekerült volna a mintasorba valódi mérésként (rules/10-
            # safety.md 4. pont, ill. CLAUDE.md §6: "Aktív energiaátadásnál
            # kritikus readback hibát... tilos 0-val/setpointtal csendben
            # helyettesíteni" -- ez egy HIL-only diagnosztikai szkript,
            # nincs aktív energiaátadás, de a mintázat azonos: a hibás/
            # overload adat NE keveredjen a valós mérésekkel). A
            # `OVERLOAD_THRESHOLD`-ot a driverből importálva EZEN a
            # hívóhelyen NEM duplikáljuk (CLAUDE.md §8) -- a konstans maga
            # a repóban NEM egyetlen forrásból él (device_dmm.py/
            # device_load.py/device_psu.py/pulse_measurement_service.py
            # mind saját `1e30`-at deklarálnak), ez a hívóhely csak ezt a
            # MEGLÉVŐ szórtságot nem növeli egy ÖTÖDIK másolattal. Kör 21
            # F17 (ARC21-6 sibling): a device_dmm.py-beli konstans ekkor
            # kapta meg a vezető-aláhúzás-nélküli, publikus nevet -- EZ a
            # hívóhely maga volt az oka, hogy a konstans egy réteghatáron
            # át exportált, SZÁNDÉKOSAN publikus szimbólummá vált.
            samples: list[float] = []
            invalid_tokens: list[str] = []
            overload_samples: list[float] = []
            for token in raw_fetch.strip().split(","):
                token = token.strip()
                if not token:
                    continue
                try:
                    value = float(token)
                except ValueError:
                    invalid_tokens.append(token)
                    continue
                if math.isnan(value) or abs(value) > OVERLOAD_THRESHOLD:  # NaN vagy overload
                    overload_samples.append(value)
                    continue
                samples.append(value)
            print(f"    [ACQUISITION] {len(samples)} minta: {samples}")
            result["acquisition_samples"] = samples
            if invalid_tokens:
                print(f"    [ACQUISITION] {len(invalid_tokens)} nem-numerikus token: {invalid_tokens}")
                result["acquisition_invalid_tokens"] = invalid_tokens
            if overload_samples:
                print(f"    [ACQUISITION] {len(overload_samples)} NaN/overload minta: {overload_samples}")
                result["acquisition_overload_samples"] = overload_samples
        errs = _errs(dmm)
        _report_errs("post-acquisition", errs)
        result["post_acquisition_errors"] = errs
        dmm.reset_to_single_sample()
        errs = _errs(dmm)
        _report_errs("reset_to_single_sample", errs)

    return result


def main() -> int:
    cfg = load_config()
    resource = cfg["instruments"]["dmm_voltage"]["resource"]
    print(f"=== DMM NPLC / SAMP:TIM musszer-only HIL-ellenorzes ===")
    print(f"DMM (dmm_voltage) resource: {resource}")
    print("PSU/Load NEM erintve, NEM kapcsol be. Csak a DMM-en futnak SCPI parancsok.\n")

    dmm = Keysight34465ADMM()
    results: list[dict] = []
    try:
        dmm.connect(resource)
        idn = dmm.idn()
        print(f"IDN: {idn.strip()}")
        dmm.clear_status()

        # Session-alapertelmezes szerinti indulo konfiguracio (main_window.py:202)
        dmm.configure_dcv(range_V=100, nplc=10.0)
        errs = _errs(dmm)
        _report_errs("configure_dcv(range=100V, nplc=10)", errs)

        # (A) STANDARD utvonal: NPLC=10, SAMP:TIM=0.2s, 5 minta, VALODI FETC?-cel
        results.append(
            probe_case(
                dmm,
                label="STANDARD (1s/10s/30s pulzus, NPLC=10)",
                nplc=10.0,
                requested_interval_s=0.2,
                sample_count=5,
                run_real_acquisition=True,
            )
        )

        # (B) FAST_TRANSIENT utvonal: NPLC=0.02, SAMP:TIM=5ms, 20 minta, VALODI FETC?-cel
        results.append(
            probe_case(
                dmm,
                label="FAST_TRANSIENT (NPLC=0.02)",
                nplc=0.02,
                requested_interval_s=0.005,
                sample_count=20,
                run_real_acquisition=True,
            )
        )

    finally:
        # Garantalt visszaallitas -- fuggetlenul attol, mi tortent fent.
        try:
            dmm.set_nplc(10.0)
            dmm.reset_to_single_sample()
            final_errs = _errs(dmm)
            _report_errs("VEGSO visszaallitas (NPLC=10, single-sample)", final_errs)
            final_nplc = dmm.get_nplc()
            print(f"\nVegso NPLC readback: {final_nplc}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[FIGYELEM] Vegso visszaallitas hibazott: {exc}")
        dmm.disconnect()

    print("\n=== OSSZEFOGLALO ===")
    for r in results:
        verdict = "INFEASIBLE (-221 varhato)" if r.get("min_exceeds_requested") else "FEASIBLE"
        acq = "acquisition OK" if "acquisition_samples" in r else "acquisition HIBA/HIANYZIK"
        print(
            f"  {r['label']}: SAMP:TIM?MIN={r['samp_tim_min_s']:.6g}s vs kert "
            f"{r['requested_interval_s']}s -> {verdict} | {acq}"
        )
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import io as _io
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
