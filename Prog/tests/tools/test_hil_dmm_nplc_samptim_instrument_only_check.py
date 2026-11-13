"""Kör 21 Batch F16 (HW21-05, két fél) regressziós tesztjei.

`hil_dmm_nplc_samptim_instrument_only_check.py::probe_case()`:
(a) korábban a privát `dmm._query("SAMP:TIM?")`-ot hívta a tényleges
    SAMP:TIM readback-hez, holott a driver saját, publikus
    `get_sample_timer_s()` accessora EPP EZT teszi -- javítva, most azt
    hívja.
(b) az élő mérés (`run_real_acquisition=True`) ágon a mintasor
    feldolgozása (`[float(x) for x in ...]`) védtelen volt -- egy hibás
    token uncaught ValueError-t dobott volna, egy "9.9E37" overload-
    token pedig csendben bekerült volna a mintasorba valódi mérésként.
    Javítva: per-token try/except + overload/NaN-detekció.

A `MockDMM` egy determinisztikus, szimulált DMM (Prog/tests/mock_drivers/
mock_dmm.py) -- SOSEM nyúl valós soros porthoz/hardverhez.
"""
from __future__ import annotations

from Prog.tests.mock_drivers.mock_dmm import MockDMM
from Prog.tools.hil_dmm_nplc_samptim_instrument_only_check import probe_case


def _probe(
    dmm: MockDMM,
    label: str = "TEST",
    nplc: float = 10.0,
    requested_interval_s: float = 0.2,
    sample_count: int = 3,
    run_real_acquisition: bool = False,
) -> dict:
    return probe_case(
        dmm,  # type: ignore[arg-type]
        label=label,
        nplc=nplc,
        requested_interval_s=requested_interval_s,
        sample_count=sample_count,
        run_real_acquisition=run_real_acquisition,
    )


class TestSampTimActualUsesPublicAccessor:
    """[Kör 21 F16, HW21-05(a)] a tényleges SAMP:TIM readback a driver
    SAJÁT, publikus `get_sample_timer_s()`-én megy, nem egy privát
    `_query()`-n -- ez a teszt a `probe_case()` eredmény-dict-jét
    ellenőrzi, ami csak a publikus accessoron át érhető el helyesen."""

    def test_samp_tim_actual_s_reflects_configured_interval(self):
        dmm = MockDMM()
        result = _probe(dmm, requested_interval_s=0.15)

        assert result["samp_tim_actual_s"] == 0.15
        assert "get_sample_timer_s()" in dmm.calls

    def test_samp_tim_actual_s_reflects_instrument_override(self):
        """A driver 'jól viselkedő' feltételezését (a kért érték
        alkalmazódott) a mock felülírhatja -- a `probe_case()`-nek EZT az
        értéket kell jelentenie, nem a kértet."""
        dmm = MockDMM(actual_sample_timer_override_s=1.0)
        result = _probe(dmm, requested_interval_s=0.41)

        assert result["samp_tim_actual_s"] == 1.0

    def test_result_dict_no_longer_carries_raw_string_key(self):
        """Regresszió: a korábbi `samp_tim_actual_readback` (nyers
        string) kulcs megszűnt -- a mezőnév/típus is jelzi, hogy immár
        parse-olt float, nem nyers SCPI-válasz."""
        dmm = MockDMM()
        result = _probe(dmm)

        assert "samp_tim_actual_readback" not in result
        assert isinstance(result["samp_tim_actual_s"], float)


class TestAcquisitionSampleParsingIsGuarded:
    """[Kör 21 F16, HW21-05(b)] a mintasor-feldolgozás nem dobhat
    uncaught ValueError-t, és nem keverhet overload/NaN-sentinelt a
    valós mérésekkel."""

    def test_normal_samples_parsed_as_before(self):
        dmm = MockDMM(voltage_V=12.5)
        result = _probe(dmm, sample_count=3, run_real_acquisition=True)

        assert result["acquisition_samples"] == [12.5, 12.5, 12.5]
        assert "acquisition_invalid_tokens" not in result
        assert "acquisition_overload_samples" not in result

    def test_malformed_token_does_not_raise_and_is_reported_separately(self):
        dmm = MockDMM(fetch_timed_acquisition_raw_override="1.234,GARBAGE,5.678")
        result = _probe(dmm, run_real_acquisition=True)

        assert result["acquisition_samples"] == [1.234, 5.678]
        assert result["acquisition_invalid_tokens"] == ["GARBAGE"]
        assert "acquisition_overload_samples" not in result

    def test_overload_token_excluded_from_real_samples(self):
        """A '9.9E37' overload-sentinel NEM kerülhet be a valós
        mintasorba -- külön, nevesített listában jelenik meg."""
        dmm = MockDMM(fetch_timed_acquisition_raw_override="1.234,9.9E37,5.678")
        result = _probe(dmm, run_real_acquisition=True)

        assert result["acquisition_samples"] == [1.234, 5.678]
        assert result["acquisition_overload_samples"] == [9.9e37]
        assert "acquisition_invalid_tokens" not in result

    def test_only_malformed_tokens_yields_empty_sample_list_not_crash(self):
        dmm = MockDMM(fetch_timed_acquisition_raw_override="GARBAGE,ALSO_BAD")
        result = _probe(dmm, run_real_acquisition=True)

        assert result["acquisition_samples"] == []
        assert result["acquisition_invalid_tokens"] == ["GARBAGE", "ALSO_BAD"]
