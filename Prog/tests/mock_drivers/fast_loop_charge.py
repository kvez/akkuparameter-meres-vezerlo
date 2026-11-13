"""
R1 B8a, F-20 (DFAM-035) tesztsegéd: a composite controllerekbe (IEC, ChemID,
OCV_SOC, GoldenGG) ágyazott, VALÓDI ChargeController fast-loop-jának
determinisztikus futtatása.

A korábbi C-06 tesztek a `_last_fast_loop_extra_s`-t kézzel injektálták a
gyermek BELSŐ integrálása nélkül, és a várt értéket ugyanabból a feltevésből
(egyetlen áram × (dt + extra)) számolták -- tautologikus fixture (DFAM-006).
Ez a segéd ehelyett:

* a `Prog.src.charge_controller` modul `time` hivatkozását (és CSAK azt)
  egy kézi órára cseréli: `sleep()` léptet, `perf_counter()` olvas -- így a
  fast-loop résztickjeinek száma és saját dt-je pontosan ismert;
* a PSU-áramot és a DMM-feszültséget hívásonként VÁLTOZÓ, előre megadott
  sorozatból adja, és résztickenként rögzíti a ténylegesen kiadott
  (áram, feszültség) párokat -- a várt Ah/Wh ebből, a kódtól függetlenül
  számolható (Σ i_k·dt_k, Σ i_k·v_k·dt_k).

Unit/mock bizonyíték, nem HIL.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from Prog.src.charge_controller import ChargeConfig, ChargeState

# Determinisztikus fast-loop ütemezés (nulla résztick-I/O-idő mellett):
# budget = min(DT_S, max_substeps*period) = 2.0, deadline = budget - period =
# 1.75 -> a kezdő _advance_single(DT_S) után PONTOSAN 7 további résztick,
# mindegyik period_s=0.25 s saját dt-vel (binárisan pontos értékek, nincs
# lebegőpontos határeset). Egy teljes tick: 8 résztick, extra = 1.75 s.
DT_S = 2.0
PERIOD_S = 0.25
EXTRA_S = 1.75
SUBSTEPS_PER_TICK = 8


def fast_loop_charge_config(**kwargs) -> ChargeConfig:
    return ChargeConfig(
        fast_loop_enabled=True,
        fast_loop_period_s=PERIOD_S,
        fast_loop_max_substeps=20,
        **kwargs,
    )


def cv_currents(n: int = 64) -> list[float]:
    """CV-szerű, csökkenő áram (a taper-küszöb fölött marad)."""
    return [1.0 - 0.01 * k for k in range(n)]


def taper_currents(n: int = 64) -> list[float]:
    """Taper-küszöb (≈0,21 A) alatti, csökkenő áram."""
    return [0.20 - 0.002 * k for k in range(n)]


def varying_voltages(target_V: float, n: int = 64) -> list[float]:
    """A CV-deadbanden (±10 mV) belül váltakozó feszültség -- így a
    _regulate_cv() nem ír a PSU-ra, de a résztickenkénti Wh eltér a
    végmintás közelítéstől."""
    return [target_V + (0.008 if k % 2 else -0.008) * (1.0 - k / (2.0 * n)) for k in range(n)]


class FastLoopClock:
    """Kézi monoton óra a ChargeController fast-loop-jához."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def install_fast_loop_clock(monkeypatch) -> FastLoopClock:
    """Csak a charge_controller modul `time` nevét cseréli (a globális
    `time` modult NEM), így más modulok (pl. a GG BQ-poll perf_counter-e)
    érintetlenek."""
    import Prog.src.charge_controller as cc_module

    clock = FastLoopClock()
    monkeypatch.setattr(
        cc_module, "time",
        SimpleNamespace(perf_counter=clock.perf_counter, sleep=clock.sleep),
    )
    return clock


class ScriptedChargeReadings:
    """Hívásonként változó PSU-áram és DMM-feszültség; résztickenként
    rögzíti a kiadott (áram, feszültség) párt.

    `fail_current_at`: ennyiedik (0-alapú) PSU-áramolvasás kivételt dob
    (-> a ChargeController PSU_COMM_LOST FAULT-ba lép, l. _read_psu_current).
    """

    def __init__(
        self,
        psu,
        dmm_v,
        currents: list[float],
        voltages: list[float],
        fail_current_at: int | None = None,
    ) -> None:
        self._currents = list(currents)
        self._voltages = list(voltages)
        self._fail_current_at = fail_current_at
        self._i_idx = 0
        self._v_idx = 0
        self._last_v: float | None = None
        self.pairs: list[tuple[float, float]] = []
        psu.measure_output_current = self._current  # type: ignore[method-assign]
        dmm_v.read_voltage = self._voltage  # type: ignore[method-assign]

    def _voltage(self) -> float:
        v = self._voltages[self._v_idx]
        self._v_idx += 1
        self._last_v = v
        return v

    def _current(self) -> float:
        idx = self._i_idx
        self._i_idx += 1
        if self._fail_current_at is not None and idx == self._fail_current_at:
            raise RuntimeError("scripted PSU current readback failure")
        i = self._currents[idx]
        assert self._last_v is not None
        self.pairs.append((i, self._last_v))
        return i

    @property
    def voltage_reads(self) -> int:
        return self._v_idx


def force_child_state(charge_ctrl, psu, state: ChargeState, psu_headroom_V: float = 0.2) -> float:
    """A beágyazott ChargeController-t közvetlenül CV/TAPER_HOLD-ba állítja
    (a régi C-06 tesztek CHARGE_CC-kényszerítésének mintájára), a PSU-t a
    célfeszültség + `psu_headroom_V` értékre. Visszaadja a célfeszültséget."""
    charge_ctrl._run_psu_preset()  # Mock PSU aramparancs a kozvetlen allapotugras elott.
    assert charge_ctrl.state is ChargeState.CHARGE_CC
    target_V = charge_ctrl._effective_charge_target_V()
    charge_ctrl._state = state
    charge_ctrl._u_psu_set = target_V + psu_headroom_V
    psu.voltage_V = target_V + psu_headroom_V
    return target_V


def expected_delta(
    pairs: list[tuple[float, float]], dt_s: float, period_s: float
) -> tuple[float, float]:
    """Független elvárás: az első résztick az outer dt_s-t, a további
    fast-loop résztickek a period_s-t kapják (nulla I/O-idő mellett)."""
    ah = 0.0
    wh = 0.0
    for k, (i, v) in enumerate(pairs):
        dt = dt_s if k == 0 else period_s
        ah += i * dt / 3600.0
        wh += i * v * dt / 3600.0
    return ah, wh


def last_sample_model(pairs: list[tuple[float, float]], total_dt_s: float) -> tuple[float, float]:
    """A JAVÍTÁS ELŐTTI (C-06) modell: a tick UTOLSÓ résztickjének árama és
    feszültsége a teljes dt_s + extra időre. Csak annak igazolására, hogy a
    forgatókönyv diszkriminál (az elvárás ettől mérhetően eltér)."""
    i, v = pairs[-1]
    return i * total_dt_s / 3600.0, i * v * total_dt_s / 3600.0


@dataclass
class TickRecord:
    parent_dAh: float
    parent_dWh: float
    child_dAh: float
    child_dWh: float
    pairs: list[tuple[float, float]]


def run_tick(ctrl, child, readings: ScriptedChargeReadings, dt_s: float = DT_S) -> TickRecord:
    """Egy outer advance(dt_s); a szülő és a (előre elmentett referenciájú)
    gyermek Ah/Wh-deltája, valamint a tick résztickjeinek (i, v) párjai."""
    p_ah, p_wh = ctrl.accumulated_charge_Ah, ctrl.accumulated_charge_Wh
    c_ah, c_wh = child.accumulated_charge_Ah, child.accumulated_charge_Wh
    n0 = len(readings.pairs)
    ctrl.advance(dt_s)
    return TickRecord(
        parent_dAh=ctrl.accumulated_charge_Ah - p_ah,
        parent_dWh=ctrl.accumulated_charge_Wh - p_wh,
        child_dAh=child.accumulated_charge_Ah - c_ah,
        child_dWh=child.accumulated_charge_Wh - c_wh,
        pairs=list(readings.pairs[n0:]),
    )


def assert_parent_delta_equals_independent_expectation(
    rec: TickRecord, n_pairs: int, total_dt_s: float, dt_s: float = DT_S,
    check_last_sample_model: bool = True,
) -> None:
    """A szülő Ah/Wh-deltája == a résztickenkénti független elvárás ==
    a gyermek saját deltája; a forgatókönyv diszkriminál a régi modelltől
    (`check_last_sample_model=False` a FAULT-ticknél, ahol a régi kód a
    sikertelen utolsó olvasás miatt 0,0 áramot integrált)."""
    assert len(rec.pairs) == n_pairs
    exp_ah, exp_wh = expected_delta(rec.pairs, dt_s, PERIOD_S)
    assert exp_ah > 0.0 and exp_wh > 0.0
    assert rec.parent_dAh == pytest.approx(exp_ah, rel=1e-12, abs=1e-15)
    assert rec.parent_dWh == pytest.approx(exp_wh, rel=1e-12, abs=1e-15)
    assert rec.parent_dAh == pytest.approx(rec.child_dAh, rel=1e-12, abs=1e-15)
    assert rec.parent_dWh == pytest.approx(rec.child_dWh, rel=1e-12, abs=1e-15)
    if check_last_sample_model:
        old_ah, old_wh = last_sample_model(rec.pairs, total_dt_s)
        assert abs(old_ah - exp_ah) > 1e-3 * exp_ah
        assert abs(old_wh - exp_wh) > 1e-3 * exp_wh
