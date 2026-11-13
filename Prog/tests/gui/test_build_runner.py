"""
MainWindow._build_runner() unit tesztek — mock driverekkel, valós hardver nélkül.
Fixek (dmm_feedback_lost_hibaelemzes_20260628.txt) ellenőrzése:
  - #2: max_voltage_jump_V mód-függő (SERIES → 4.0V, egyébként 2.0V)
  - #3: is_bq_mode kiterjesztve OCV_SOC_CHARACTERIZATION-re
  - #4: Logger.close() hívása, ha a runner-építés kivétellel áll le
"""
from __future__ import annotations

import contextlib
import gc
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

import Prog.gui.main_window as mw
import Prog.src.session_recovery as sr
from Prog import app_paths
from Prog.drivers.device_dmm import Keysight34465ADMM
from Prog.drivers.device_psu import Keithley2220PSU
from Prog.gui.main_window import MainWindow
from Prog.gui.panels.config_panel import SessionConfig
from Prog.main import _install_emergency_excepthook
from Prog.src import report_generator
from Prog.src.instrument_manager import InstrumentManager
from Prog.src.logger import Logger
from Prog.src.test_runner import TestResult as _R2TestResult
from Prog.src.test_runner import TestRunner


def _patch_hardware(monkeypatch) -> None:
    """Minden fizikai I/O-t no-op-ra cserél — csak az objektumgráf-építést teszteljük."""
    monkeypatch.setattr(InstrumentManager, "connect_all", lambda self, cfg: None)
    # WP-AK-02: identify_all() valós *IDN?-t hívna minden driveren — a mockolt
    # objektumgráf-tesztekben ez fizikai I/O nélkül fut, ezért mock identitást ad.
    # Governance-v2 review-kör 1 (CF-38): identify_all() kapott egy opcionális
    # expected_models kwargot (modell-substring validáció) — a mock aláírása
    # ezt elfogadja (és figyelmen kívül hagyja, ahogy eddig is fizikai I/O
    # nélkül futott), különben minden _build_runner()-hívás TypeError-ral
    # bukna a hívóhelyen átadott expected_models miatt.
    monkeypatch.setattr(
        InstrumentManager, "identify_all",
        lambda self, expected_models=None: {
            "PSU": "MOCK_PSU", "Load": "MOCK_LOAD", "DMM_V": "MOCK_DMM_V", "DMM_T": "MOCK_DMM_T",
        },
    )
    monkeypatch.setattr(
        Keysight34465ADMM, "configure_dcv",
        lambda self, range_V, nplc: setattr(self, "_test_nplc", nplc),
    )
    monkeypatch.setattr(
        Keysight34465ADMM, "get_nplc", lambda self: self._test_nplc,
    )
    monkeypatch.setattr(
        Keysight34465ADMM, "enable_autozero",
        lambda self: setattr(self, "_test_autozero", True),
    )
    # A konfigurációs hibasor is kizárólag mockolt műszerből érkezik.
    monkeypatch.setattr(Keysight34465ADMM, "check_error", lambda self: [])
    monkeypatch.setattr(
        Keysight34465ADMM, "configure_temp_4wire_pt100", lambda self, nplc=10.0: None
    )
    monkeypatch.setattr(
        Keysight34465ADMM, "configure_temp_2wire_ntc_thermistor",
        lambda self, r25_ohm=10000.0, beta_K=3950.0, nplc=10.0: None
    )
    # P1-javítás (2026-08-26): a set_mode_*() hívások UTÁN _build_runner()
    # mostantól query_combination_mode()-dal visszaigazolja a beállítást
    # (HW-003) — a no-op set_mode_*() mockok mellett ezt is szimulálni kell,
    # különben minden teszt ConfigurationError-ral bukna (a hardver nélküli
    # query_combination_mode() valós SCPI-t próbálna küldeni).
    monkeypatch.setattr(
        Keithley2220PSU, "set_mode_independent",
        lambda self: setattr(self, "_test_combination_mode", "OFF"),
    )
    monkeypatch.setattr(
        Keithley2220PSU, "set_mode_parallel",
        lambda self: setattr(self, "_test_combination_mode", "PAR"),
    )
    monkeypatch.setattr(
        Keithley2220PSU, "set_mode_series",
        lambda self: setattr(self, "_test_combination_mode", "SER"),
    )
    monkeypatch.setattr(
        Keithley2220PSU, "query_combination_mode",
        lambda self: getattr(self, "_test_combination_mode", "OFF"),
    )
    # R16-B15aF1: szándékosan túl nagy MIN-t injektálunk az elutasítási tesztekhez.
    # Ez a történeti 10 NPLC-s HIL-adat, nem az 1 NPLC/AZ ON minimum állítása.
    # A T9 külön, konfigurációfüggő mockkal ellenőrzi az új impulzusbeállítást.
    monkeypatch.setattr(
        Keysight34465ADMM, "query_sample_timer_capability", lambda self: "+4.04496000E-01"
    )


def _use_ocv_soc_pulse_interval(monkeypatch, interval_s: float) -> None:
    """R1 B5 (F-19): CSAK teszt-setup -- a main_window._ocv_soc_config_for()
    altal adott OCV/SOC config impulzus-mintaveteli kozet irja felul. NEM
    alapertek-modositas: a gyari OcvSocConfig.pulse_sample_interval_s (0,2 s)
    valtozatlan; annak megvaltoztatasa a Q-6 felhasznaloi dontes."""
    import dataclasses

    import Prog.gui.main_window as main_window_module

    original = main_window_module._ocv_soc_config_for
    monkeypatch.setattr(
        main_window_module, "_ocv_soc_config_for",
        lambda cfg: dataclasses.replace(original(cfg), pulse_sample_interval_s=interval_s),
    )


def _use_hil_verified_ocv_soc_pulse_interval(monkeypatch) -> None:
    """R1 B5 (F-19): 0,41 s -- a valos 34465A-n hiba es elteres nelkul
    visszaigazolt ertek (CLAIM_LEDGER INSTR-DMM-SAMPTIM-001). Azoknak a
    MEGLEVO teszteknek a setupja, amelyek nem a preflightot vizsgaljak, de
    OCV_SOC bring-upot futtatnak; az eredeti allitasaik valtozatlanok."""
    _use_ocv_soc_pulse_interval(monkeypatch, 0.41)


def _make_cfg(**overrides) -> SessionConfig:
    cfg = SessionConfig(
        battery_profile_name="FIAMM_12V",
        battery_model="TEST7AH",
        nominal_capacity_ah=7.2,
        psu_resource="MOCK_PSU",
        load_resource="MOCK_LOAD",
        dmm_voltage_resource="MOCK_DMM_V",
        dmm_temperature_resource="MOCK_DMM_T",
        psu_mode="INDEPENDENT",
        test_type="CHARACTERIZATION",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    if cfg.test_type in {"TRADITIONAL_LEARNING", "GOLDEN_GG"}:
        if "battery_model" not in overrides:
            cfg.battery_model = "FG20721"  # Q-9/Q-23 exact forrásolt BQ tesztprofil.
        if "nominal_capacity_ah" not in overrides:
            cfg.nominal_capacity_ah = 6.7  # A GUI/SessionConfig bemenet C10.
    return cfg


@contextlib.contextmanager
def _gc_only_at_boundary() -> Iterator[None]:
    """Tesztinfra (R1 TINFRA-BR-GC, P2): a blokk idejere tiltja az automatikus
    ciklikus GC-t; kilepeskor (kivetel eseten is, `finally`) eloszor EGY
    explicit gc.collect() fut -- meg tiltott automatikus GC mellett --, utana
    visszaall a belepeskori GC-allapot (gc.enable() csak akkor, ha belepeskor
    engedelyezve volt). A blokkbol kilepo kivetelt nem nyeli el.

    Bizonyitott (kodolvasas): a `_build_runner()` referenciahaloja ciklust
    kepez -- a TestRunner altal tarolt controller-factoryk egy resze a
    `self`-et (MainWindow) fogja, a factoryk elerik a
    `_forward_controller_event` closure-t, az pedig a `runner`-t --, ezert egy
    tesztben letrehozott MainWindow es Qt/pyqtgraph objektumgrafja nem
    refcounttal, hanem csak a ciklikus GC-vel szabadul fel.

    Megfigyelt reprodukcio (nem altalanos bizonyitek): izolalt scratch-fakon
    (HEAD 833988e es 6230efe, .pyc-vel es anelkul; PySide6 6.11.2, pyqtgraph
    0.14.0) a teljes modul egy processzben futtatva tobbszor nativ hibaval
    (0xC0000005 / 0xC0000374) allt le, amikor a tesztfuggvenyek belsejeben
    automatikus GC futhatott; tesztenkenti tiltassal es hatarponti explicit
    gyujtessel ugyanezek a futasok tisztak voltak. HIPOTEZIS (nem
    bizonyitott): az automatikus GC-menetek mas veglegesitesi sorrendet
    okoznak (generacios listak), ami double delete / use-after-free-hez
    vezet; a pontos nativ keret nem azonositott.

    Dokumentalt tesztinfra-korlat: ebben a modulban a teszttorzsbeli
    automatikus GC lefedettsege szandekosan megszunik; a zold eredmenybol
    produkcios eletciklus-helyesseg NEM kovetkezik. A hatarponti gyujtes nem
    garantalja, hogy a teszt minden szemete ekkor felszabadul: traceback vagy
    a gyujtes pillanataban meg vissza nem allitott monkeypatch tarthat
    referenciat; a fixture- es a monkeypatch-teardown sorrendje nem
    garantalt."""
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        gc.collect()
        if was_enabled:
            gc.enable()


@pytest.fixture(autouse=True)
def _cyclic_gc_only_at_test_boundary() -> Iterator[None]:
    """Autouse (function scope): ennek a modulnak minden tesztje a
    `_gc_only_at_boundary()` blokkban fut -- a reszletek es a korlatok ott."""
    with _gc_only_at_boundary():
        yield


def test_cyclic_gc_is_disabled_inside_build_runner_tests():
    """Regresszios or: ha a fenti fixture nem fut (eltavolitottak, vagy
    autouse=False lett), a teszttorzsben az automatikus GC engedelyezett
    marad, es ez a teszt bukik."""
    assert gc.isenabled() is False


def _spy_on_helper_gc_collect(monkeypatch) -> list[bool]:
    """A `_gc_only_at_boundary()` modulja altal elert `gc.collect`-et
    figyeli: minden hivasnal rogziti, engedelyezett volt-e akkor az
    automatikus GC, es tovabbhiv az eredetire."""
    helper_gc = sys.modules[_gc_only_at_boundary.__module__].gc
    real_collect = helper_gc.collect
    calls: list[bool] = []

    def _spy(*args, **kwargs):
        calls.append(helper_gc.isenabled())
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(helper_gc, "collect", _spy)
    return calls


class TestGcOnlyAtBoundaryLifecycle:
    """R1 TINFRA-BR-GC (C4): a `_gc_only_at_boundary()` helper eletciklusa.
    A tesztek az autouse fixture-on BELUL futnak (belepeskor tiltott GC);
    ahol engedelyezett induloallapot kell, a teszt maga allitja be, es
    `finally`-ban a sajat induloallapotat allitja vissza."""

    def test_a_initially_enabled_gc_is_disabled_inside_and_reenabled_after(self):
        was_enabled = gc.isenabled()
        gc.enable()
        try:
            with _gc_only_at_boundary():
                assert gc.isenabled() is False
            assert gc.isenabled() is True
        finally:
            if not was_enabled:
                gc.disable()

    def test_b_initially_disabled_gc_stays_disabled_after(self):
        assert gc.isenabled() is False  # autouse fixture
        with _gc_only_at_boundary():
            assert gc.isenabled() is False
        assert gc.isenabled() is False

    def test_c_exactly_one_collect_on_exit_while_gc_still_disabled(self, monkeypatch):
        calls = _spy_on_helper_gc_collect(monkeypatch)
        was_enabled = gc.isenabled()
        gc.enable()
        try:
            with _gc_only_at_boundary():
                assert calls == []
            # pontosan egy gyujtes, es az meg az allapot-visszaallitas ELOTT
            assert calls == [False]
            assert gc.isenabled() is True
        finally:
            if not was_enabled:
                gc.disable()

    def test_d_exception_restores_state_collects_and_propagates(self, monkeypatch):
        calls = _spy_on_helper_gc_collect(monkeypatch)

        class _Boom(Exception):
            pass

        was_enabled = gc.isenabled()
        gc.enable()
        try:
            with pytest.raises(_Boom), _gc_only_at_boundary():
                raise _Boom()
            assert calls == [False]
            assert gc.isenabled() is True
        finally:
            if not was_enabled:
                gc.disable()


class TestDmmJumpLimitByMode:
    def test_series_mode_uses_wider_jump_limit(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(psu_mode="SERIES", hardware_wiring_confirmed=True)

        _runner, _plan, _ = window._build_runner(cfg)

        assert window._instruments.dmm_voltage.max_voltage_jump_V == pytest.approx(4.0)

    def test_independent_mode_keeps_default_jump_limit(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(psu_mode="INDEPENDENT")

        _runner, _plan, _ = window._build_runner(cfg)

        assert window._instruments.dmm_voltage.max_voltage_jump_V == pytest.approx(2.0)

    def test_bq_mode_with_independent_psu_uses_wider_jump_limit(self, qapp, monkeypatch, tmp_path):
        """dmm_feedback_lost_hibaelemzes_20260823: 12V-os BQ-módú tesztek (pl.
        BQ_LEARNING_PHYSICAL, TF1 panel a töltőágban) tipikusan INDEPENDENT psu
        móddal futnak, nem SERIES-szel — a szűk 2.0V jump-limit a panel normál
        FET-kapcsolási tranziensét (~3V) hamis DMM_FEEDBACK_LOST-ként jelezte."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(psu_mode="INDEPENDENT", test_type="BQ_LEARNING_PHYSICAL")

        _runner, _plan, _ = window._build_runner(cfg)

        assert window._instruments.dmm_voltage.max_voltage_jump_V == pytest.approx(4.0)

    def test_construct_and_connect_instruments_iec_mode_uses_wider_jump_limit(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, 2026-09-15, C-05/DFAM-017+DFAM-010] _construct_and_
        connect_instruments() (a hardveres bring-up, dmm_v jump-limithez)
        korábban CSAK 5 test_type-ot ismert BQ-módúnak, _build_runner()
        (SafetyManager.is_bq_mode-hoz) 6-ot -- IEC_DC_INTERNAL_RESISTANCE-
        szel, holott egy komment mindkét helyen "ugyanaz"-ként hivatkozott
        rá. IEC_DC_INTERNAL_RESISTANCE + INDEPENDENT PSU-módban ez a
        hívóhely hamisan 2.0-t adott a helyes 4.0 helyett -- egy legitim
        soros-esés (a PRECHARGE fázis ugyanazon TF1/soros-dióda útvonalon
        tölt, mint OCV_SOC/CHEMID) hamis DMM_FEEDBACK_LOST-ot válthatott
        volna ki. Most mindkét hívóhely a közös _IS_BQ_MODE_TEST_TYPES-ból
        olvas."""
        from Prog.gui.main_window import _construct_and_connect_instruments
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        cfg = _make_cfg(psu_mode="INDEPENDENT", test_type="IEC_DC_INTERNAL_RESISTANCE")

        result = _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        assert result.dmm_v.max_voltage_jump_V == pytest.approx(4.0)

    def test_is_bq_mode_test_types_identical_across_both_call_sites(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, 2026-09-15, C-05] Fail-closed mutáció-védelem: a
        _construct_and_connect_instruments() (dmm_v jump-limit) és a
        _build_runner() (SafetyManager.is_bq_mode) UGYANAZT a halmazt
        használja minden test_type-ra -- ha a jövőben valaki az egyik
        hívóhelyet módosítja a másik nélkül, ez a teszt buktatja."""
        import serial  # type: ignore[import-untyped]

        from Prog.gui.main_window import _IS_BQ_MODE_TEST_TYPES, _construct_and_connect_instruments
        from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial
        _patch_hardware(monkeypatch)
        _use_hil_verified_ocv_soc_pulse_interval(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        fake = FakePicoSerial()
        monkeypatch.setattr(FakePicoSerial, "close", lambda self: None, raising=False)
        monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)

        all_test_types = [
            "CHARGE_ONLY", "DISCHARGE_ONLY", "BQ_LEARNING_PHYSICAL",
            "OCV_SOC_CHARACTERIZATION", "CHEMID_CHARACTERIZATION_PBA",
            "TRADITIONAL_LEARNING", "GOLDEN_GG", "IEC_DC_INTERNAL_RESISTANCE",
        ]
        for test_type in all_test_types:
            window = MainWindow()
            cfg = _make_cfg(psu_mode="INDEPENDENT", test_type=test_type, bq_port="COM8")

            result = _construct_and_connect_instruments(cfg, needs_bq_transport=False)
            runner, _plan, _ = window._build_runner(cfg)

            expected_is_bq_mode = test_type in _IS_BQ_MODE_TEST_TYPES
            construct_side_jump_limit_indicates_bq = (
                result.dmm_v.max_voltage_jump_V == pytest.approx(4.0)
            )
            build_runner_side_is_bq_mode = runner._safety.is_bq_mode
            assert construct_side_jump_limit_indicates_bq == expected_is_bq_mode, test_type
            assert build_runner_side_is_bq_mode == expected_is_bq_mode, test_type


class TestC14NeedsBqTransportSingleSource:
    """[regresszió, 2026-09-16, Kör 18 Batch C14/SW-P3-3] a
    `needs_bq_transport = cfg.test_type in ("TRADITIONAL_LEARNING",
    "GOLDEN_GG")` szó szerinti tuple-literál két hívóhelyen (_start_test(),
    _build_runner()) volt duplikálva -- egyetlen forrásra
    (_NEEDS_BQ_TRANSPORT_TEST_TYPES) egyesítve, mint _IS_BQ_MODE_TEST_TYPES."""

    def test_needs_bq_transport_is_a_single_source_and_matches_bq_port_policy(self):
        from Prog.gui.main_window import _NEEDS_BQ_TRANSPORT_TEST_TYPES
        from Prog.gui.ui_policy import is_group_required
        from Prog.src.test_runner import TestType

        for test_type in TestType:
            expected = is_group_required(test_type.value, "bq_port")
            assert (test_type.value in _NEEDS_BQ_TRANSPORT_TEST_TYPES) == expected, (
                test_type.value
            )

    def test_no_hardcoded_needs_bq_transport_literal_remains(self):
        main_window_path = (
            Path(__file__).resolve().parent.parent.parent / "gui" / "main_window.py"
        )
        source = main_window_path.read_text(encoding="utf-8")
        assert 'in ("TRADITIONAL_LEARNING", "GOLDEN_GG")' not in source, (
            "a needs_bq_transport hardcoded tuple-literálja visszakerült "
            "_NEEDS_BQ_TRANSPORT_TEST_TYPES helyett"
        )


class TestIsBqModeForOcvSoc:
    def test_ocv_soc_characterization_sets_bq_mode(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        _use_hil_verified_ocv_soc_pulse_interval(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")

        runner, _plan, _ = window._build_runner(cfg)

        assert runner._safety.is_bq_mode is True

    def test_bq_learning_physical_still_sets_bq_mode(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="BQ_LEARNING_PHYSICAL")

        runner, _plan, _ = window._build_runner(cfg)

        assert runner._safety.is_bq_mode is True

    def test_characterization_does_not_set_bq_mode(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHARACTERIZATION")

        runner, _plan, _ = window._build_runner(cfg)

        assert runner._safety.is_bq_mode is False


class TestTemperatureSensorModeWiring:
    """Sérült 4-wire PT100 RTD pótlása 2-wire 10kΩ/B3950 NTC termisztorral —
    l. dmm_feedback_lost_hibaelemzes-hez hasonló hibaelemzés (2026-08-23,
    thermistor_gap_20260823)."""

    def test_default_mode_configures_pt100(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        calls: list[str] = []
        monkeypatch.setattr(
            Keysight34465ADMM, "configure_temp_4wire_pt100",
            lambda self, nplc=10.0: calls.append("pt100")
        )
        window = MainWindow()
        cfg = _make_cfg()  # default temperature_sensor_mode == RTD_4W_PT100

        window._build_runner(cfg)

        assert calls == ["pt100"]

    def test_thermistor_mode_configures_ntc(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        calls: list[tuple] = []
        monkeypatch.setattr(
            Keysight34465ADMM, "configure_temp_2wire_ntc_thermistor",
            lambda self, r25_ohm=10000.0, beta_K=3950.0, nplc=10.0:
                calls.append((r25_ohm, beta_K, nplc))
        )
        window = MainWindow()
        cfg = _make_cfg(temperature_sensor_mode="THERMISTOR_2W_NTC_10K_B3950")

        window._build_runner(cfg)

        assert calls == [(10000.0, 3950.0, 10)]

    def test_unknown_sensor_mode_raises_instead_of_silent_fallback(
        self, qapp, monkeypatch, tmp_path
    ):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(temperature_sensor_mode="TYPO_NOT_A_REAL_SENSOR_MODE")

        with pytest.raises(ValueError, match="TYPO_NOT_A_REAL_SENSOR_MODE"):
            window._build_runner(cfg)


class TestWpAk05ChemIdWiring:
    """WP-AK-05: CHEMID_CHARACTERIZATION_PBA ne essen a BQ_LEARNING_PHYSICAL
    fallbackba, kapjon saját ctrl-factoryt, és a GPCCHEM export automatikusan
    a session könyvtárába íródjon."""

    def test_chemid_test_type_maps_to_chemid_plan_not_bq_fallback(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestType
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHEMID_CHARACTERIZATION_PBA")

        _runner, plan, _ = window._build_runner(cfg)

        assert plan.test_type == TestType.CHEMID_CHARACTERIZATION_PBA

    def test_chemid_sets_bq_mode(self, qapp, monkeypatch, tmp_path):
        """A ChemID ugyanazon a hardveren fut teljes töltés/relax/kisütés
        ciklust, mint BQ_LEARNING_PHYSICAL/OCV_SOC_CHARACTERIZATION — ugyanaz
        a magasabb soros-esés limit vonatkozik rá."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHEMID_CHARACTERIZATION_PBA")

        runner, _plan, _ = window._build_runner(cfg)

        assert runner._safety.is_bq_mode is True

    def test_unknown_test_type_raises_instead_of_silent_bq_fallback(self, qapp, monkeypatch, tmp_path):
        """Review-szempont: a korábbi kód minden fel nem ismert test_type-ot
        csendben bq_learning_physical()-re cserélt — most explicit hiba."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TYPO_NOT_A_REAL_TEST_TYPE")

        with pytest.raises(ValueError, match="TYPO_NOT_A_REAL_TEST_TYPE"):
            window._build_runner(cfg)

    def test_chemid_ctrl_factory_builds_controller_with_export_wiring(self, qapp, monkeypatch, tmp_path):
        from Prog.src.chemid_controller import ChemIdCharacterizationController
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHEMID_CHARACTERIZATION_PBA", sample_id="S42")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._chemid_ctrl_factory()

        assert isinstance(ctrl, ChemIdCharacterizationController)
        assert ctrl._export_config is not None
        assert ctrl._export_config.num_cell_series == runner._profile.cell_count
        assert ctrl._export_config.battery_sample_id == "S42"
        assert ctrl._export_output_dir == window._session_dir / "gpcchem_export"

    def test_chemid_export_sample_interval_synced_with_controller_config(self, qapp, monkeypatch, tmp_path):
        """Review-lelet: a GpcChemConfig.sample_interval_s ne egy egymástól
        független, csak véletlenül egyező 30.0 alapérték legyen — a
        ChemIdConfig SAJÁT sample_interval_s-éből származzon."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHEMID_CHARACTERIZATION_PBA")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._chemid_ctrl_factory()

        assert ctrl._export_config.sample_interval_s == pytest.approx(
            ctrl._config.sample_interval_s
        )

    def test_chemid_export_missing_identifiers_flagged(self, qapp, monkeypatch, tmp_path):
        """Review-lelet: mivel a projektben nincs app_version/git_commit/
        operator forrás, a wiring ezt explicit limitációként adja át, nem
        hallgatja el."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHEMID_CHARACTERIZATION_PBA")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._chemid_ctrl_factory()

        assert set(ctrl._missing_identifier_fields) == {"AppVersion", "GitCommit", "Operator"}


class TestWpAk16BqTransportWiring:
    """WP-AK-16 (BQ-TRANSPORT-SESSION-GAP-01): a TraditionalLearningController
    (WP-AK-10) és GoldenGgController (WP-AK-11) session-szintű PicoBQTransport-ot
    kapjanak, kölcsönös kizárásban a BQ Diagnosztika/Kalibráció panelekkel."""

    @staticmethod
    def _patch_bq_serial(monkeypatch, **fake_kwargs):
        import serial  # type: ignore[import-untyped]

        from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial

        fake = FakePicoSerial(**fake_kwargs)
        monkeypatch.setattr(FakePicoSerial, "close", lambda self: None, raising=False)
        monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)
        return fake

    def test_traditional_learning_maps_to_plan(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestType
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        _runner, plan, _ = window._build_runner(cfg)

        assert plan.test_type == TestType.TRADITIONAL_LEARNING

    def test_golden_gg_maps_to_plan(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestType
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.5,
        )

        _runner, plan, _ = window._build_runner(cfg)

        assert plan.test_type == TestType.GOLDEN_GG

    def test_traditional_learning_sets_bq_mode(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        runner, _plan, _ = window._build_runner(cfg)

        assert runner._safety.is_bq_mode is True

    def test_non_bq_test_type_does_not_open_bq_transport(self, qapp, monkeypatch, tmp_path):
        """CHARACTERIZATION-nál sem serial.Serial nem hívódik, sem a
        _session_owns_bq_transport flag nem áll be — a portot nem is kellene
        megadni ehhez a típushoz."""
        _patch_hardware(monkeypatch)
        serial_calls: list[tuple] = []
        import serial  # type: ignore[import-untyped]
        monkeypatch.setattr(
            serial, "Serial", lambda *a, **k: serial_calls.append((a, k)) or None
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHARACTERIZATION")

        window._build_runner(cfg)

        assert serial_calls == []
        assert window._session_owns_bq_transport is False

    def test_traditional_learning_opens_transport_and_sets_owns_flag(
        self, qapp, monkeypatch, tmp_path
    ):
        _patch_hardware(monkeypatch)
        fake = self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        window._build_runner(cfg)

        assert window._session_owns_bq_transport is True
        assert window._session_bq_serial is fake

    def test_traditional_learning_syncs_bq_panel_poll_interval_to_30s(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, teljes kódbázis review 3. kör, 2026-08-29, P2] a
        BqDiagnosticsPanel staleness-küszöbe (alapértelmezés: 2s pollingra
        hardcode-olva) korábban nem lett szinkronban a session-vezérelt
        (30s, LearningConfig/GoldenGgPhysicalConfig.sample_interval_s)
        gauge-mintavétellel — hamis "ELAVULT ADAT" jelzést okozva a session
        idejének nagy részében."""
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        window._build_runner(cfg)

        assert window._bq_diagnostics_panel._poll_interval_ms == 30000

    def test_close_session_bq_transport_resets_bq_panel_poll_interval(
        self, qapp, monkeypatch, tmp_path
    ):
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")
        window._build_runner(cfg)
        assert window._bq_diagnostics_panel._poll_interval_ms == 30000

        window._close_session_bq_transport()

        assert window._bq_diagnostics_panel._poll_interval_ms == 2000

    def test_traditional_learning_ctrl_factory_builds_controller(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.src.traditional_learning_controller import (
            TraditionalLearningController,
        )
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._traditional_learning_ctrl_factory()

        assert isinstance(ctrl, TraditionalLearningController)

    def test_golden_gg_ctrl_factory_builds_controller_with_export_dir(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.src.golden_gg_controller import GoldenGgController
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.72,
        )

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._golden_gg_ctrl_factory()

        assert isinstance(ctrl, GoldenGgController)
        assert ctrl._config.system_rate_current_A == pytest.approx(0.72)
        assert ctrl._export_output_dir == window._session_dir / "golden_gg_export"

    def test_golden_gg_ctrl_factory_wires_export_config(self, qapp, monkeypatch, tmp_path):
        """[regresszió, release-audit 2026-08-23 2.1] export_config-nak
        ténylegesen bekötve kell lennie — enélkül a TI submission csomag
        sosem készül el az EXPORT_PACKAGE lépésben (CLAUDE.md #11.6-adjacent
        adatvesztés)."""
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.72,
            sample_id="SN001",
        )

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._golden_gg_ctrl_factory()

        assert ctrl._export_config is not None
        assert ctrl._export_config.num_cell_series == window._cell_count
        assert ctrl._export_config.battery_model == cfg.battery_model
        assert ctrl._export_config.battery_sample_id == "SN001"
        assert ctrl._export_config.system_rate_current_A == pytest.approx(0.72)

    def test_golden_gg_ctrl_factory_flags_missing_thermal_metadata_fields(
        self, qapp, monkeypatch, tmp_path
    ):
        """Governance-v2 review-kör 1 (CF-31, DFAM-003): a golden_gg_export.
        py::GoldenGgConfig.to_text() SUP03-GG-003/004 szerint a
        ThermalEnvironmentDescription/ThermistorPlacementDescription
        mezőket KÖTELEZŐNEK jelöli ("a rendszer NEM tudja automatikusan
        eldönteni, hogy a leírt felállás megfelelő-e"), de a `_make_golden_
        gg_ctrl()` sosem állítja be ezeket ÉS korábban sosem is jelölte
        hiányzóként az AppVersion/GitCommit/Operator melletti missing_
        metadata_fields listában — a config.txt csendben, jelöletlenül
        üresen ment ki a TI submissionba."""
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.72,
        )

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._golden_gg_ctrl_factory()

        assert "ThermalEnvironmentDescription" in ctrl._missing_metadata_fields
        assert "ThermistorPlacementDescription" in ctrl._missing_metadata_fields

    def test_golden_gg_ctrl_factory_wires_on_event_to_logger(self, qapp, monkeypatch, tmp_path):
        """[regresszió, release-audit 2026-08-23 2.2 + MAX verifier CF-07
        pontosítás, batch 4.2] on_event nélkül a GOLDEN_GG_PACKAGE_READY/
        GOLDEN_GG_FAULT stb. esemény sosem jut el az eseménynaplóba. A CF-07
        lelet szerint az eredeti fix csak a LOGGERBE írt — a `runner.
        on_event` láncot (worker._handle_event -> event_ready ->
        LivePanel.append_event) sosem érte el, tehát az operátor élőben nem
        látta az eseményt (csak a session zárása után, a logfájlban). Ez a
        teszt MOST már azt is igazolja, hogy a `runner.on_event`-en
        keresztül is eljut."""
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.72,
        )

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._golden_gg_ctrl_factory()
        assert ctrl.on_event is not None

        # A worker.py __init__-jét szimuláljuk: runner.on_event a valós
        # rendszerben a live-panel felé vezető láncra van kötve.
        received = []
        runner.on_event = received.append

        # A logger.log_event-et közvetlenül nem mockoljuk (fájlba írna) —
        # elég igazolni, hogy on_event meghívása nem dob kivételt és a
        # naplózó réteg felé irányul (a Logger saját tesztjei fedik a
        # tényleges fájlírást).
        event = {"event_code": "GOLDEN_GG_TEST_EVENT", "event_message": "x", "severity": "CRITICAL"}
        ctrl.on_event(event)

        assert received == [event]

    def test_golden_gg_ctrl_factory_sets_last_golden_gg_ctrl(self, qapp, monkeypatch, tmp_path):
        """[regresszió, release-audit 2026-08-23 2.2] _write_report() a
        session_meta bq_learning_result/golden_gg_* mezőit ebből a
        referenciából olvassa ki a session végén."""
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.72,
        )

        assert window._last_golden_gg_ctrl is None
        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._golden_gg_ctrl_factory()

        assert window._last_golden_gg_ctrl is ctrl

    def test_traditional_learning_ctrl_factory_wires_on_event_and_sets_last_ctrl(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, release-audit 2026-08-23 2.2]"""
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        assert window._last_traditional_learning_ctrl is None
        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._traditional_learning_ctrl_factory()

        assert ctrl.on_event is not None
        assert window._last_traditional_learning_ctrl is ctrl

    def test_build_runner_resets_last_ctrl_refs_for_non_bq_session(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, release-audit 2026-08-23 2.2] egy korábbi GOLDEN_GG
        session controller-referenciája ne szivárogjon át egy ezt követő,
        más teszttípusú session riportjába."""
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        gg_cfg = _make_cfg(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.72,
        )
        runner, _plan, _ = window._build_runner(gg_cfg)
        runner._golden_gg_ctrl_factory()
        assert window._last_golden_gg_ctrl is not None

        char_cfg = _make_cfg(test_type="CHARACTERIZATION")
        window._build_runner(char_cfg)

        assert window._last_golden_gg_ctrl is None
        assert window._last_traditional_learning_ctrl is None

    def test_build_runner_raises_when_bq_diagnostics_active(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._bq_worker = object()  # sentinel: BQ Diagnosztika "aktív"
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        with pytest.raises(ValueError, match="BQ Diagnosztika"):
            window._build_runner(cfg)

    def test_build_runner_closes_bq_transport_on_later_exception(
        self, qapp, monkeypatch, tmp_path
    ):
        """A TestRunner-konstrukció (vagy bármi más a try-blokkban) UTÁN
        elszálló hiba ne hagyja 'foglaltként' ragadva a kölcsönös kizárás
        flag-jét — l. TestLoggerClosedOnBuildFailure minta."""
        _patch_hardware(monkeypatch)
        fake = self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)

        close_calls: list[bool] = []
        monkeypatch.setattr(fake, "close", lambda: close_calls.append(True), raising=False)

        def _raise_init(self, *args, **kwargs):
            raise RuntimeError("simulated TestRunner construction failure")

        monkeypatch.setattr(TestRunner, "__init__", _raise_init)

        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        with pytest.raises(RuntimeError, match="simulated TestRunner construction failure"):
            window._build_runner(cfg)

        assert window._session_owns_bq_transport is False
        assert window._session_bq_serial is None
        assert close_calls == [True]

    def test_bq_connect_requested_blocked_when_session_owns_transport(
        self, qapp, monkeypatch, tmp_path
    ):
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        window = MainWindow()
        window._session_owns_bq_transport = True

        window._on_bq_connect_requested("COM8")

        assert window._bq_worker is None
        assert len(warnings) == 1

    def test_bq_calibration_requested_blocked_when_session_owns_transport(
        self, qapp, monkeypatch, tmp_path
    ):
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        window = MainWindow()
        window._session_owns_bq_transport = True

        window._on_bq_calibration_requested("COM8")

        assert len(warnings) == 1

    def test_bq_calibration_requested_blocked_during_active_physical_test(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31), GUI/UX/
        Állapot P0 lelet] a BqCalibrationDialog.exec() alkalmazás-modális —
        egy AKTÍV, nem-BQ-transportot használó fizikai teszt (self._worker
        not None) alatt korábban akadálytalanul megnyitható volt, teljesen
        elérhetetlenné téve a MainWindow-t (a Vészleállítás gombot is
        beleértve) a dialógus nyitva-tartása alatt."""
        from types import SimpleNamespace

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        window = MainWindow()
        window._worker = SimpleNamespace()

        window._on_bq_calibration_requested("COM8")

        assert len(warnings) == 1

    def test_bq_connect_requested_blocked_during_bq_needing_session_bringup(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 11. kör (2026-08-31), GUI/UX/
        Állapot P2 lelet] self._session_owns_bq_transport csak a MÁR
        MEGKÖTÖTT session-transportot védi — egy folyamatban lévő
        TRADITIONAL_LEARNING/GOLDEN_GG hardveres bring-up (_connect_worker)
        alatt ez még False, mégis ugyanahhoz a fizikai Pico soros porthoz
        próbálna kapcsolódni egy MÁSODIK, önálló klienssel."""
        from types import SimpleNamespace

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        window = MainWindow()
        window._connect_worker = SimpleNamespace(needs_bq_transport=True)

        window._on_bq_connect_requested("COM8")

        assert window._bq_worker is None
        assert window._bq_connect_worker is None
        assert len(warnings) == 1

    def test_bq_connect_requested_allowed_during_non_bq_session_bringup(
        self, qapp, monkeypatch, tmp_path
    ):
        """Kontroll-eset: egy folyamatban lévő bring-up, ami NEM igényli a
        BQ-transportot (pl. CHARGE_ONLY), nem szabad, hogy elutasítsa a BQ
        Diagnosztika kapcsolódását."""
        from types import SimpleNamespace

        from PySide6.QtWidgets import QMessageBox
        self._patch_bq_serial_via_fake(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        window = MainWindow()
        window._connect_worker = SimpleNamespace(needs_bq_transport=False)

        window._on_bq_connect_requested("COM8")

        assert warnings == []
        assert window._bq_connect_worker is not None

        # A teszt VALÓDI QThread-et indít (_BqConnectWorker) — enélkül a
        # takarítás nélkül egy élő szál maradna a folyamatban a teszt
        # visszatérése után, ami a KÖVETKEZŐ teszt Qt-eseményhurkát
        # megzavarhatja (l. TestReview2ndRoundBqConnectWorker::
        # test_connect_does_not_block_gui_thread_synchronously ugyanezen
        # mintája).
        deadline = time.monotonic() + 5.0
        while window._bq_worker is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert window._bq_worker is not None
        window._cleanup_bq_thread()

    @staticmethod
    def _patch_bq_serial_via_fake(monkeypatch):
        import serial  # type: ignore[import-untyped]

        from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial

        fake = FakePicoSerial()
        monkeypatch.setattr(FakePicoSerial, "close", lambda self: None, raising=False)
        monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)
        return fake

    def test_bq_calibration_requested_blocked_during_bq_connect_worker_bringup(
        self, qapp, monkeypatch, tmp_path
    ):
        """l. test_bq_connect_requested_blocked_during_bq_needing_session_
        bringup docstringje — ugyanaz a hibaosztály, itt a MÁSIK versengő
        folyamatra (BQ Diagnosztika "Kapcsolódás" folyamatban)."""
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        window = MainWindow()
        window._bq_connect_worker = object()  # sentinel: "kapcsolódás folyamatban"

        window._on_bq_calibration_requested("COM8")

        assert len(warnings) == 1

    def test_bq_calibration_requested_blocked_during_bq_needing_session_bringup(
        self, qapp, monkeypatch, tmp_path
    ):
        from types import SimpleNamespace

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        window = MainWindow()
        window._connect_worker = SimpleNamespace(needs_bq_transport=True)

        window._on_bq_calibration_requested("COM8")

        assert len(warnings) == 1

    def test_bq_calibration_requested_blocked_during_non_bq_session_bringup(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 15. kör (2026-09-04), GUI/UX/
        Állapot P1 lelet] a fenti (needs_bq_transport=True) kizárás mellett
        korábban egy NEM BQ-transportot igénylő teszttípus (CHARGE_ONLY/
        DISCHARGE_ONLY/CHARACTERIZATION/OCV_SOC/CHEMID) hardveres bring-upja
        alatt (self._connect_worker fut, self._worker MÉG None) a kalibrációs
        dialógus akadálytalanul megnyitható volt — a dlg.exec() alkalmazás-
        modális eseményhurka pumpálja a Qt-eseménysort, tehát a még függőben
        lévő connect_ready szignál a dialógus nyitva-tartása KÖZBEN
        kézbesítődhetett volna, elindítva a fizikai tesztet a Vészleállítás
        elérhetetlensége mellett."""
        from types import SimpleNamespace

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        window = MainWindow()
        window._connect_worker = SimpleNamespace(needs_bq_transport=False)

        window._on_bq_calibration_requested("COM8")

        assert len(warnings) == 1


class TestReview2ndRoundBqConnectWorker:
    """Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P0 lelet (F1): a
    BQ Diagnosztika "Kapcsolódás" hardveres bring-up-ja (soros port + negotiate)
    _BqConnectWorker-en, külön valós QThread-en fusson — a GUI-szál (és vele
    az Emergency Stop) a kapcsolódás alatt is válaszképes maradjon."""

    @staticmethod
    def _patch_bq_serial(monkeypatch, **fake_kwargs):
        import serial  # type: ignore[import-untyped]

        from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial

        fake = FakePicoSerial(**fake_kwargs)
        monkeypatch.setattr(FakePicoSerial, "close", lambda self: None, raising=False)
        monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)
        return fake

    def test_connect_does_not_block_gui_thread_synchronously(self, qapp, monkeypatch, tmp_path):
        """A _on_bq_connect_requested() visszatérése UTÁN a kapcsolódás
        eredménye (bq_worker) MÉG nem lehet kész — csak a queued connect_ready
        jel feldolgozása UTÁN, ami bizonyítja, hogy a soros port nyitás +
        negotiate() nem a hívó (GUI) szálon, szinkron futott le."""
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        self._patch_bq_serial(monkeypatch)
        window = MainWindow()

        window._on_bq_connect_requested("COM8")

        assert window._bq_connect_thread is not None
        assert window._bq_worker is None

        deadline = time.monotonic() + 5.0
        while window._bq_worker is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert window._bq_worker is not None
        assert window._bq_connect_thread is None  # cleanup megtörtént
        assert window._bq_connect_worker is None
        # A tényleges BqDiagnosticsWorker QThread-jét is le kell állítani a
        # teszt végén — enélkül egy élő, még pollozó szál marad a folyamatban
        # a teszt visszatérése után, ami a Python-interpreter leállásakor
        # összeomláshoz vezethet (l. TestBqDiagnosticsWorkerThreadSafety minta).
        window._cleanup_bq_thread()

    def test_connect_failure_shows_critical_and_clears_state(self, qapp, monkeypatch, tmp_path):
        import serial  # type: ignore[import-untyped]
        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)

        def _raise_serial(*a, **k):
            raise RuntimeError("simulated port open failure")

        monkeypatch.setattr(serial, "Serial", _raise_serial)
        criticals: list = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a))
        window = MainWindow()

        window._on_bq_connect_requested("COM8")

        deadline = time.monotonic() + 5.0
        while not criticals and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert len(criticals) == 1
        assert window._bq_worker is None
        assert window._bq_connect_thread is None
        assert window._bq_connect_worker is None

    def test_negotiate_timeout_shows_critical_and_clears_state(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31), Hardver/
        Műszervezérlés P1 lelet] negotiate() a testvér PicoBQTransportError/
        InstrumentTimeoutError kivételek BÁRMELYIKÉT dobhatja (soros I/O
        timeout esetén az utóbbit) — a korábbi szűk `except
        PicoBQTransportError` ezt @Slot()-on belül elkapatlanul hagyta
        volna, sem connect_ready, sem connect_failed nem tüzelt volna."""
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        self._patch_bq_serial(monkeypatch, simulate_no_response=True)
        criticals: list = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a))
        window = MainWindow()

        window._on_bq_connect_requested("COM8")

        deadline = time.monotonic() + 5.0
        while not criticals and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert len(criticals) == 1
        assert window._bq_worker is None
        assert window._bq_connect_thread is None
        assert window._bq_connect_worker is None

    def test_double_connect_request_does_not_spawn_second_worker(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        self._patch_bq_serial(monkeypatch)
        window = MainWindow()

        window._on_bq_connect_requested("COM8")
        first_worker = window._bq_connect_worker
        window._on_bq_connect_requested("COM8")  # a kapcsolódás még folyamatban van

        assert window._bq_connect_worker is first_worker

        deadline = time.monotonic() + 5.0
        while window._bq_worker is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert window._bq_worker is not None
        window._cleanup_bq_thread()


class TestBatteryProfileLoadedFromYaml:
    """BAT-001/SRC-003/CFG-001: Prog/config/battery_profiles/*.yaml legyen a
    gyári/mérnöki paraméterek hiteles forrása, ne csak session-specifikus
    mezők (kapacitás, modell) érkezzenek a GUI-tól."""

    def test_profile_source_populated_from_yaml(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(battery_profile_name="FIAMM_12V")

        runner, _plan, _ = window._build_runner(cfg)

        assert runner._profile.profile_source == "FIAMM Engineering Manual"

    def test_manufacturer_populated_from_yaml(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(battery_profile_name="FIAMM_24V")

        runner, _plan, _ = window._build_runner(cfg)

        assert runner._profile.manufacturer == "FIAMM"
        assert runner._profile.cell_count == 12
        assert runner._profile.nominal_voltage_V == pytest.approx(24.0)

    def test_missing_profile_yaml_falls_back_safely(self, qapp, monkeypatch, tmp_path):
        """Ha a profil YAML nem található, a fallback dict + dataclass default tartsa fenn a működést."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path / "nonexistent_bundle")
        window = MainWindow()
        cfg = _make_cfg(battery_profile_name="FIAMM_12V")

        runner, _plan, _ = window._build_runner(cfg)

        assert runner._profile.cell_count == 6
        assert runner._profile.charge_voltage_per_cell_V == pytest.approx(2.40)

    def test_missing_profile_yaml_fallback_reads_canonical_derived_parameters_source(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Governance-v2 review-kör 1 (CF-15/DFAM-017 3.
        előfordulás, batch 4.5)] a fenti fallback (cell_count/nominal_
        voltage_V/manufacturer, ha a profil YAML hiányzik) korábban egy
        main_window.py-specifikus _PROFILE_META_FALLBACK dict-ből jött —
        mostantól a kanonikus derived_parameters.PROFILE_META-ból. A
        monkeypatch bizonyítja, hogy a forrás TÉNYLEGESEN ez, nem egy
        másolat (egy másolat nem reagálna a monkeypatch-re)."""
        from Prog.gui import derived_parameters
        monkeypatch.setitem(
            derived_parameters.PROFILE_META, "FIAMM_12V",
            {"cell_count": 20, "nominal_voltage_V": 999.0, "manufacturer": "MOCK_MFG"},
        )
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path / "nonexistent_bundle")
        window = MainWindow()
        cfg = _make_cfg(battery_profile_name="FIAMM_12V")

        runner, _plan, _ = window._build_runner(cfg)

        assert runner._profile.cell_count == 20
        assert runner._profile.nominal_voltage_V == pytest.approx(999.0)
        assert runner._profile.manufacturer == "MOCK_MFG"


class TestLoggerClosedOnBuildFailure:
    def test_logger_closed_when_runner_construction_raises(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)

        close_calls: list[bool] = []
        original_close = Logger.close

        def _spy_close(self) -> None:
            close_calls.append(True)
            original_close(self)

        monkeypatch.setattr(Logger, "close", _spy_close)

        def _raise_init(self, *args, **kwargs):
            raise RuntimeError("simulated TestRunner construction failure")

        monkeypatch.setattr(TestRunner, "__init__", _raise_init)

        window = MainWindow()
        cfg = _make_cfg()

        with pytest.raises(RuntimeError, match="simulated TestRunner construction failure"):
            window._build_runner(cfg)

        assert close_calls == [True]

    def test_no_exception_does_not_close_logger_prematurely(self, qapp, monkeypatch, tmp_path):
        """Regresszió: sikeres build esetén a logger nem záródik (a runnernek kellene)."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)

        close_calls: list[bool] = []
        monkeypatch.setattr(
            Logger, "close", lambda self: close_calls.append(True)
        )

        window = MainWindow()
        cfg = _make_cfg()

        window._build_runner(cfg)

        assert close_calls == []


class TestReportWrittenForAllTerminalStates:
    """DATA-003 (GAP-OLD-001): report.json minden terminális állapotra
    elkészüljön — korábban kizárólag DONE esetén generálódott."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)  # beállítja window._session_dir/_psu_mode_str/_cell_count
        return window

    def _report_path(self, window):
        return window._session_dir / "report.json"

    def test_done_writes_report(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        # A DONE ág egy blokkoló QMessageBox.information()-t is megnyit —
        # teszt alatt nincs kezelő, aki bezárná, ezért no-op-ra cserélve.
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
        ))
        assert self._report_path(window).exists()

    def test_done_with_communication_faults_is_degraded_not_ok(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Teljes kódbázis review 9. kör (2026-08-30),
        Metrológia/Adatintegritás P2 lelet] capacity_result_quality korábban
        CSAK is_resumed/status alapján döntött — egy DONE, nem resumed
        session "OK"-ot kapott akkor is, ha közben nem-fatális SCPI
        kommunikációs hiba történt, holott a communication_faults_count
        mező ugyanabban a session_meta-ban ellentmondva jelezte."""
        import json as _json

        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        # A DONE ág egy blokkoló QMessageBox.information()-t is megnyit —
        # teszt alatt nincs kezelő, aki bezárná, ezért no-op-ra cserélve
        # (l. test_done_writes_report ugyanezen mintája).
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            communication_faults_count=3,
        ))
        report = _json.loads(self._report_path(window).read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"

    def test_done_with_extreme_dt_s_is_degraded_not_ok(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Teljes kódbázis review 9. kör (2026-08-30),
        Metrológia/Adatintegritás P2 lelet] ugyanaz a hibaosztály, mint a
        communication_faults_count esetén — az extreme_dt_s_count mező
        (blokkoló VISA I/O miatti dt_s-tüske jelzője) korábban a
        report.json-ban egyáltalán nem is jelent meg, session-szintű
        minősítésbe pedig nem jutott be."""
        import json as _json

        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            extreme_dt_s_count=2, max_observed_dt_s=47.3,
        ))
        report = _json.loads(self._report_path(window).read_text(encoding="utf-8"))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["capacity_result_quality"] == "DEGRADED"
        assert limitations["extreme_dt_s_count"] == 2
        assert limitations["max_observed_dt_s"] == pytest.approx(47.3)

    def test_done_degraded_quality_surfaces_in_popup_as_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F1 (P1)]
        A session MÁR kiszámolt capacity_result_quality-ja korábban SOHA nem
        jutott el a "Kész" popupig -- egy DEGRADED minősítésű, akár 20-30
        órás fizikai ciklus is minősítés nélküli "Kész"-ként (QMessageBox.
        information) jelent meg. Mostantól a popup tartalmazza a
        minősítést, és QMessageBox.warning-ot használ information helyett."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            communication_faults_count=3,
        ))
        assert info_calls == []
        assert len(warning_calls) == 1
        assert "Minőség: DEGRADED" in warning_calls[0]

    def test_done_popup_warns_on_uncertain_safe_off(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Kör 17 MAX-effort review, R17-03] a bizonytalan
        safe-off eredmenye korabban SEMMILYEN popup-csatornan nem jelent
        meg -- az operator egy feltetel nelkuli "Kesz"-et latott, akkor
        is, ha a PSU/Load OFF parancs NEM igazoltan sikeres."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        warning_calls: list = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            safe_off_attempted=True,
            safe_off_result={"PSU": False, "Load": True, "DMM_V": True, "DMM_T": True},
        ))
        assert len(warning_calls) == 1
        assert "SAFE" in warning_calls[0] or "biztonságos lekapcsolás" in warning_calls[0]
        assert "PSU" in warning_calls[0]

    def test_done_popup_silent_when_safe_off_confirmed(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True, "DMM_V": True, "DMM_T": True},
        ))
        assert len(info_calls) == 1
        assert warning_calls == []
        assert "biztonságos lekapcsolás" not in info_calls[0]

    def test_done_popup_warns_on_uncertain_safe_off_even_when_report_write_fails(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kör 18 MAX-effort review, GUI/UX + session/workflow
        reviewer KONVERGENS lelet] a safe-off bizonytalansági figyelmeztetés
        korábban a `if session_meta is not None:` ágon BELÜL állt, holott a
        forrása (`result.safe_off_attempted`/`safe_off_result`) a
        `TestResult`-ból jön -- ha `_write_report()` kivétellel bukott
        (session_meta is None), a figyelmeztetés ÉPP a legrosszabb esetben
        (nincs report.json SEM) tűnt el. Most feltétel nélkül megjelenik."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        warning_calls: list = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(
            window, "_write_report", lambda result: None,
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            safe_off_attempted=True,
            safe_off_result={"PSU": False, "Load": True},
        ))
        assert len(warning_calls) == 1
        assert "PSU" in warning_calls[0]
        assert "report.json" in warning_calls[0]

    def test_done_popup_warns_when_safe_off_never_attempted(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Kör 18 MAX-effort review, GUI/UX reviewer P3-2]
        safe_off_attempted=False (safe-off SOHA nem lett megkísérelve)
        korábban néma volt a befejezés-popupban -- most külön, meg-
        különböztetett sort kap (DFAM-018: nem összeolvasztva az
        "uncertain" esettel)."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            safe_off_attempted=False,
        ))
        assert info_calls == []
        assert len(warning_calls) == 1
        assert "SOHA nem lett megkísérelve" in warning_calls[0]

    def test_done_popup_warns_on_invalid_iec_quality(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Kör 17 MAX-effort review, R17-12, DFAM-021+
        DFAM-018] egy érvénytelen IEC DC belső ellenállás mérés
        (Ri_ohm/Isc_A=None, quality=INVALID_RESISTANCE) korábban "Kész"-
        ként (QMessageBox.information) jelent meg minősítés nélkül."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_iec_dc_resistance_ctrl = _FakeIecDcResistanceCtrl(
            result={"Ri_ohm": None, "Isc_A": None, "quality": "INVALID_RESISTANCE"},
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=0.0, total_discharge_ah=0.0,
            active_step_label="load_2",
        ))
        assert info_calls == []
        assert len(warning_calls) == 1
        assert "INVALID_RESISTANCE" in warning_calls[0]

    def test_done_popup_silent_on_iec_timing_degraded_alone(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Kör 17 MAX-effort review, R17-12, a döntés
        zárótesztje] timing_degraded=True STRUKTURÁLISAN garantált minden
        alapértelmezett-configú IEC sessionön (l. main_window.py
        Ri_DC_IEC_quality-hoz fűzött kommentje a forrás-ellenőrzött
        levezetésért) -- ezt a popup TUDATOSAN NEM veszi figyelembe, csak
        a quality-t. Ez a teszt zárja le a döntést: egy jövőbeli kör NE
        "javítsa" ezt a timing_degraded bekötésével, ami minden IEC
        sessiont hamisan DEGRADED-nek jelezne."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_iec_dc_resistance_ctrl = _FakeIecDcResistanceCtrl(
            result={
                "Ri_ohm": 0.0504, "Isc_A": 254.4, "quality": "OK",
                "timing_degraded": True,
            },
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=0.0, total_discharge_ah=0.0,
            active_step_label="load_2",
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert len(info_calls) == 1
        assert warning_calls == []

    def test_done_popup_warns_on_degraded_ocv_soc_point_quality(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kör 18, Batch C12, METRO-P2-1, DFAM-021+DFAM-018]
        egy tobb pontot is degradaltkent minositett OCV/SOC session
        korabban "Kesz"-kent (QMessageBox.information) jelent meg
        minosites nelkul -- a per-pont measurement_quality sosem jutott
        el a session-szintu osszesitesig."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_ocv_soc_ctrl = _FakeOcvSocCtrl(
            point_quality_counts={"OK": 8, "CUTOFF_DURING_PULSE": 2},
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=0.0, total_discharge_ah=0.0,
            active_step_label="log_soc_point",
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert info_calls == []
        assert len(warning_calls) == 1
        assert "CUTOFF_DURING_PULSE" in warning_calls[0]
        assert "×2" in warning_calls[0]

    def test_done_popup_silent_when_all_ocv_soc_points_ok(self, qapp, monkeypatch, tmp_path):
        """Kontroll-eset: ha MINDEN pont OK, a popup NEM valthat warning-ra."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_ocv_soc_ctrl = _FakeOcvSocCtrl(point_quality_counts={"OK": 10})
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=0.0, total_discharge_ah=0.0,
            active_step_label="log_soc_point",
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert len(info_calls) == 1
        assert warning_calls == []

    def test_done_popup_silent_on_ocv_soc_timing_tolerance_alone(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kör 18, Batch C12, fuggetlen MAX verifier P1
        lelete, a dontes zarotesztje] DEGRADED_TIMING_TOLERANCE
        STRUKTURALISAN garantalt (kb. 1-2s-mal kesik minden alapertelmezett
        runner_tick_s=2.0s configu OCV/SOC session minden pontja) -- 1:1
        ugyanaz az alakzat, mint az IEC timing_degraded, amit a popup
        TUDATOSAN NEM vesz figyelembe. Ez a teszt zarja le a dontest: egy
        jovobeli kor NE "javitsa" ezt a mezo popup-kapuba kapcsolasaval,
        ami minden alapertelmezett-configu OCV/SOC sessiont hamisan
        DEGRADED-nek jelezne (alarm-fatigue)."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_ocv_soc_ctrl = _FakeOcvSocCtrl(
            point_quality_counts={"DEGRADED_TIMING_TOLERANCE": 21},
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=0.0, total_discharge_ah=0.0,
            active_step_label="log_soc_point",
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert len(info_calls) == 1
        assert warning_calls == []

    def test_done_popup_warns_when_timing_tolerance_mixed_with_real_anomaly(
        self, qapp, monkeypatch, tmp_path
    ):
        """A kizaras SZUK -- ha egy VALODI anomalia (pl. CUTOFF_DURING_
        PULSE) EGYUTT jelenik meg a strukturalisan garantalt timing-
        tolerance-szal, a popup MEGIS warning-ra valt, csak a valodi
        anomaliat nevezve meg (a timing-tolerance NEM szerepel a
        felsorolasban)."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_ocv_soc_ctrl = _FakeOcvSocCtrl(
            point_quality_counts={
                "OK": 15, "DEGRADED_TIMING_TOLERANCE": 20, "CUTOFF_DURING_PULSE": 1,
            },
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=0.0, total_discharge_ah=0.0,
            active_step_label="log_soc_point",
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert info_calls == []
        assert len(warning_calls) == 1
        assert "CUTOFF_DURING_PULSE" in warning_calls[0]
        assert "DEGRADED_TIMING_TOLERANCE" not in warning_calls[0]

    def test_done_ok_quality_uses_information_not_warning(self, qapp, monkeypatch, tmp_path):
        """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F1 (P1),
        kontroll-eset] egy valódi OK minősítésű DONE session-nél a popup
        NEM válthat warning-ra -- csak a MÁR meglévő information marad."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert warning_calls == []
        assert len(info_calls) == 1
        assert "Minőség:" not in info_calls[0]

    def test_popup_header_names_fault_and_stopped_not_kesz(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Kör 18 MAX-effort review, GUI/UX reviewer P3-1] a
        fejsor korábban feltétel nélkül "Teszt kész — {status}"-t írt --
        egy FAULT/STOPPED session dialógusa "Test done" szöveggel nyílt,
        pontosan azt a hamis-befejezés szöveget ismételve, amit a
        státuszsor-testvér (6. kör, 2026-08-29) már kiküszöbölt."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="FAULT", total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert len(warning_calls) == 1
        assert "Teszt kész" not in warning_calls[0]
        assert "HIBA" in warning_calls[0] or "FAULT" in warning_calls[0]

        warning_calls.clear()
        window._on_finished(TestResult(
            status="STOPPED", total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert len(warning_calls) == 1
        assert "Teszt kész" not in warning_calls[0]
        assert "Leállítva" in warning_calls[0]

    def test_failed_reseal_flag_appears_in_session_end_popup(self, qapp, monkeypatch, tmp_path):
        """R2-11a (2026-09-15, 14. writer-kör, DFAM-026 4. előfordulása):
        a GoldenGgController.quality_flags korábban SOHA nem jutott el a
        kezelőig -- egy sikertelen, visszaellenőrzött reseal (BQ_SEAL_
        RESTORE_FAILED_AT_EXPORT_PACKAGE) egy egyébként sikeres sessionön
        csak a tranziens live event-feedben jelent meg, a chip fizikailag
        UNSEALED maradhatott jelzés nélkül."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_golden_gg_ctrl = _FakeGoldenGgCtrl(
            True, str(tmp_path / "gg_export"), str(tmp_path / "gg_export" / "pkg.zip"),
            quality_flags=("BQ_SEAL_RESTORE_FAILED_AT_EXPORT_PACKAGE:seal accepted but SS bit clear",),
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
        ))
        assert info_calls == []
        assert len(warning_calls) == 1
        assert "UNSEALED" in warning_calls[0]
        assert "BQ_SEAL_RESTORE_FAILED_AT_EXPORT_PACKAGE" in warning_calls[0]

    def test_non_seal_golden_gg_flag_does_not_trigger_seal_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """Fail-closed a prefix-szűrésre: egy tisztán metrológiai/egyéb GG-
        flag NE váltson ki seal-figyelmeztetést -- a szűrés szándékosan
        szűk (csak BQ_SEAL_*/BQ_TRANSPORT_* prefixekre)."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_golden_gg_ctrl = _FakeGoldenGgCtrl(
            True, str(tmp_path / "gg_export"), str(tmp_path / "gg_export" / "pkg.zip"),
            quality_flags=("SOME_OTHER_METROLOGY_FLAG",),
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert warning_calls == []
        assert len(info_calls) == 1
        assert "UNSEALED" not in info_calls[0]

    def test_bl_threshold_restore_failed_flag_warns_about_physical_chip(
        self, qapp, monkeypatch, tmp_path
    ):
        """R2-10 (2026-09-15, 14. writer-kör, DFAM-026 5. érintése): a
        generikus 'BQ tanulás eredménye: {bq_result}' sor NEM mondja meg,
        hogy a FIZIKAI chipen ideiglenes Cell BL Set Volt Threshold
        maradhatott és a chip UNSEALED lehet -- a pending_bq_df_restore
        checkpoint-csatorna csak a report.json NÉLKÜLI (crash) sessiont
        fedi, a normálisan lezárt, de sikertelen restore-ú sessiont senki
        nem jelezte a kezelőnek."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
            _FakeLearningResultSummary(
                "BQ_LEARNING_AWAITING",
                ("BL_THRESHOLD_RESTORE_FAILED(stopped_at=cell_bl_set_volt_mV)",),
            )
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
        ))
        assert info_calls == []
        assert len(warning_calls) == 1
        assert "UNSEALED" in warning_calls[0]
        assert "Cell BL" in warning_calls[0]
        assert "BL_THRESHOLD_RESTORE_FAILED(stopped_at=cell_bl_set_volt_mV)" in warning_calls[0]

    def test_seal_restore_failed_at_cell_bl_restore_warns(self, qapp, monkeypatch, tmp_path):
        """R2-10: a BQ_SEAL_RESTORE_FAILED_AT_CELL_BL_RESTORE flag (a
        Cell BL visszaírás UTÁNI reseal-kísérlet hibája) ugyanabba a
        prefix-szűrésbe esik, mint a BL_THRESHOLD_RESTORE_FAILED."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
            _FakeLearningResultSummary(
                "BQ_LEARNING_AWAITING",
                ("BQ_SEAL_RESTORE_FAILED_AT_CELL_BL_RESTORE:timeout",),
            )
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
        ))
        assert info_calls == []
        assert len(warning_calls) == 1
        assert "UNSEALED" in warning_calls[0]
        assert "BQ_SEAL_RESTORE_FAILED_AT_CELL_BL_RESTORE:timeout" in warning_calls[0]

    def test_non_chip_bq_learning_flag_does_not_trigger_chip_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """Fail-closed a prefix-szűrésre: egy nem chip-állapotú learning
        quality flag (pl. UPDATE_STATUS ellenőrzés) NE váltsa ki a Cell
        BL/UNSEALED figyelmeztetést -- a generikus bq_result sor
        megjelenik, de a 'Cell BL' szöveg nem."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
            _FakeLearningResultSummary(
                "BQ_LEARNING_AWAITING",
                ("UPDATE_STATUS_NOT_0X06_AT_FINAL_CHECK(got=5)",),
            )
        )
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
        ))
        assert len(warning_calls) == 1
        assert "BQ tanulás eredménye: BQ_LEARNING_AWAITING" in warning_calls[0]
        assert "Cell BL" not in warning_calls[0]
        assert "UNSEALED" not in warning_calls[0]

    def test_done_shows_gpcchem_package_path_in_popup(self, qapp, monkeypatch, tmp_path):
        """[regresszió, D9/F-06, 2026-09-13, Riportok/ui_policy_audit_
        20260913.md] a sikeres GPCCHEM-export útvonala korábban sehol nem
        jelent meg az operátor számára a záró popupban -- informatív sor,
        NEM degraded-jelző (marad .information, nem válik .warning-ra)."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        info_calls: list = []
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(qapp, monkeypatch, tmp_path)
        zip_path = str(tmp_path / "chemid_export" / "gpcchem_submission.zip")
        window._last_chemid_ctrl = _FakeChemIdCtrl(export_zip_path=zip_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert warning_calls == []
        assert len(info_calls) == 1
        assert f"GPCCHEM csomag: {zip_path}" in info_calls[0]

    def test_fault_writes_report(self, qapp, monkeypatch, tmp_path):
        """A korábbi kódban ez az eset NEM írt report.json-t — ez a fő GAP-OLD-001 hiba."""
        from Prog.src.test_runner import TestResult
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="FAULT", reason="DMM_FEEDBACK_LOST",
            total_charge_ah=0.3, total_discharge_ah=0.0,
            active_step_label="charge", safe_off_attempted=True,
            communication_faults_count=1,
        ))
        report_path = self._report_path(window)
        assert report_path.exists()
        import json
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["status"] == "FAULT"
        assert report["reason"] == "DMM_FEEDBACK_LOST"
        assert report["safe_off_attempted"] is True
        assert report["active_phase"] == "charge"
        assert report["MEASUREMENT_LIMITATIONS"]["communication_faults_count"] == 1

    def test_report_write_failure_shows_visible_warning_not_only_status_bar(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/
        Állapot P2 lelet #1] Ha _write_report() kivételt dob, a hibaüzenetet
        korábban KIZÁRÓLAG a státuszsorba írta — amit a KÖZVETLENÜL utána,
        ugyanabban a szinkron hívásláncban lefutó, feltétel nélküli "Kész |
        Töltve.../Kisütve..." blokk azonnal felülírt, DONE esetén egy "Teszt
        befejezve" popup kíséretében, ami szintén nem említette a hibát. Az
        operátor SEMMILYEN látható jelzést nem kapott a report.json-hiányról.
        Most egy modális QMessageBox.warning is megjelenik, ami — ellentétben
        a bármikor felülírható státuszsorral — garantáltan látható marad.

        [FRISSÍTVE, review-kör 4/5 GUI F4 (P2, DFAM-026 sibling), 2026-09-10]:
        a KÖZVETLENÜL ez után megjelenő "Teszt befejezve" popup korábban
        VÁLTOZATLANUL feltétel nélküli, minősítés nélküli .information()-t
        hívott (session_meta=None esetén egyik degradációs ág sem futott le)
        -- az operátor egy explicit hibaüzenet UTÁN egy látszólag "minden
        rendben" összegzést látott, ami a report.json tényleges hiányát
        nem említette. Mostantól ez a MÁSODIK popup is .warning()-ot hív és
        explicit megemlíti a report.json hiányát -- ÖSSZESEN 2 warning."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.report_generator import ReportGenerator
        from Prog.src.test_runner import TestResult

        warnings: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))

        def _raise_generate(self, session_meta):
            raise RuntimeError("simulated disk full")
        monkeypatch.setattr(ReportGenerator, "generate", _raise_generate)

        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            active_step_label="relax_after_discharge",
        ))

        assert not self._report_path(window).exists()
        assert len(warnings) == 2
        assert "simulated disk full" in warnings[0][2]
        assert warnings[1][1] == "Teszt befejezve"
        assert "report.json fájlja NEM készült el" in warnings[1][2]
        # A modális figyelmeztetés UTÁN a szokásos "Kész" üzenet is
        # beállítódik a státuszsorban — a fix nem távolítja el ezt, csak
        # garantálja, hogy a hiba NE vesszen el láthatatlanul előtte.
        assert "kész" in window._status_bar.currentMessage().lower()

    def test_stopped_writes_report(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestResult
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="STOPPED", reason="USER_STOP_REQUESTED",
            total_charge_ah=0.2, total_discharge_ah=0.0,
            safe_off_attempted=True,
        ))
        assert self._report_path(window).exists()

    def test_terminal_checkpoint_writes_report(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestResult
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=False,
        ))
        assert self._report_path(window).exists()

    def test_fault_via_real_worker_signal_path_writes_report(self, qapp, monkeypatch, tmp_path):
        """Review-talált Critical hiba regressziós tesztje: korábban a
        worker.py FAULT ágán CSAK a `fault` szignál tüzelt, a `finished`
        (amire _write_report kötve van) soha — ezért a VALÓS szignál-
        útvonalon keresztül (nem window._on_finished() közvetlen hívásával)
        egyetlen FAULT session sem kapott report.json-t. Ez a teszt a teljes
        MainWindow._start_test() szignál-huzalozást használja."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.gui.worker import TestRunnerWorker
        from Prog.src.test_runner import TestResult

        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
        window = self._make_window(qapp, monkeypatch, tmp_path)

        class _FaultingRunner:
            def __init__(self):
                self.on_sample = None
                self.on_event = None
                self.on_step_changed = None
                self.stop_requested = False
                self.emergency_stop_requested = False
                self.emergency_stop_reason = ""

            def run(self, plan, start_step_index: int = 0):
                return TestResult(
                    status="FAULT", reason="DMM_FEEDBACK_LOST",
                    active_step_label="charge", safe_off_attempted=True,
                )

            def request_stop(self):
                pass

            def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP"):
                pass

        window._worker = TestRunnerWorker(_FaultingRunner(), None)
        window._worker.finished.connect(window._on_finished)
        window._worker.fault.connect(window._on_fault)
        window._worker.run()  # szinkron hívás — nincs valós QThread a tesztben

        report_path = self._report_path(window)
        assert report_path.exists()
        import json
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["status"] == "FAULT"
        assert report["reason"] == "DMM_FEEDBACK_LOST"

    def test_resumable_checkpoint_does_not_write_final_report(self, qapp, monkeypatch, tmp_path):
        """Folytatható checkpointnál NEM végleges report kell — a szabály szerint
        legfeljebb interim/segment report lenne indokolt, ami nincs ebben a
        körben implementálva (dokumentált, nem hallgatólagos hiány)."""
        from Prog.src.test_runner import TestResult
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True,
        ))
        assert not self._report_path(window).exists()

    def test_report_reflects_configured_temperature_compensation_mode(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 20. kör (2026-09-06),
        Metrológia/Adatintegritás P1 lelet] a GUI-n kiválasztott
        temperature_compensation_mode korábban csak a SafetyManager
        konstruktorába jutott el — a _write_report() session_meta dict-je
        sosem kapott ilyen kulcsot, ezért a report.json MINDIG "MONITOR_
        ONLY"-t mutatott, függetlenül a ténylegesen kiválasztott (OFF/
        MONITOR_ONLY/ENABLED) módtól."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg(temperature_compensation_mode="ENABLED")
        window._build_runner(cfg)

        window._on_finished(TestResult(status="DONE"))

        import json
        report = json.loads(self._report_path(window).read_text(encoding="utf-8"))
        assert report["temperature_compensation_mode"] == "ENABLED"

    def test_report_reflects_psu_mode_max_voltage_and_current(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 22. kör (2026-09-07),
        Metrológia/Adatintegritás P3 lelet] a report_generator.py régóta
        exponálja a psu_mode_max_voltage_V/current_A mezőket, de
        session_meta-ba egyik production hívóhely sem tette be a hozzájuk
        tartozó értéket — a riportban mindig null volt, függetlenül a
        ténylegesen konfigurált PSU-módtól."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg(psu_mode="SERIES", hardware_wiring_confirmed=True)
        window._build_runner(cfg)

        window._on_finished(TestResult(status="DONE"))

        import json
        report = json.loads(self._report_path(window).read_text(encoding="utf-8"))
        assert report["psu_mode_max_voltage_V"] == 60.0
        assert report["psu_mode_max_current_A"] == 1.5

    def test_report_psu_mode_max_reads_canonical_derived_parameters_source(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Governance-v2 review-kör 1 (CF-15/DFAM-017 3.
        előfordulás, batch 4.5)] a fenti psu_mode_max_voltage_V/current_A
        korábban egy main_window.py-specifikus, function-scoped dict-ből
        jött — mostantól a kanonikus derived_parameters.PSU_HW_MAX_
        VOLTAGE_V/CURRENT_A-ból. A monkeypatch bizonyítja, hogy a forrás
        TÉNYLEGESEN ez, nem egy másolat."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.gui import derived_parameters
        from Prog.src.test_runner import TestResult

        monkeypatch.setitem(derived_parameters.PSU_HW_MAX_VOLTAGE_V, "SERIES", 999.0)
        monkeypatch.setitem(derived_parameters.PSU_HW_MAX_CURRENT_A, "SERIES", 888.0)

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg(psu_mode="SERIES", hardware_wiring_confirmed=True)
        window._build_runner(cfg)

        window._on_finished(TestResult(status="DONE"))

        import json
        report = json.loads(self._report_path(window).read_text(encoding="utf-8"))
        assert report["psu_mode_max_voltage_V"] == 999.0
        assert report["psu_mode_max_current_A"] == 888.0


class TestCf34RechargeRequiredWarningConsumer:
    """[regresszió, governance-v2 review-kör 1, CF-34, DFAM-026
    declared_policy_without_consumer] ui_policy.py "recharge_required_
    warning" csoportját korábban SENKI nem kérdezte le -- a DISCHARGE_ONLY/
    CHARACTERIZATION/OCV_SOC_CHARACTERIZATION/CHEMID_CHARACTERIZATION_PBA
    módok mind lemerített AGM/VRLA akkumulátorral zárultak, de a "Kész"
    popup erről semmit nem jelzett."""

    def _make_window(self, monkeypatch, tmp_path, test_type: str):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type=test_type)
        window._build_runner(cfg)
        return window

    def test_characterization_done_shows_recharge_warning(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "CHARACTERIZATION")
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert len(messages) == 1
        assert "vissza kell tölteni" in messages[0]

    def test_charge_only_done_does_not_show_recharge_warning(self, qapp, monkeypatch, tmp_path):
        """A CHARGE_ONLY mód TÖLTÉSSEL zárul -- az akku NEM marad kisütve,
        a figyelmeztetés itt hamis riasztás lenne."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "CHARGE_ONLY")
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.0,
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert len(messages) == 1
        assert "vissza kell tölteni" not in messages[0]

    def test_characterization_stopped_shows_recharge_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, review-kör 5 (2026-09-10), GUI/UX G5-04 (P3,
        DFAM-026 sibling)] Egy kézi Stop a kisütés közepén UGYANÚGY
        mélykisütött állapotban hagyhatja az akkut, mint egy DONE -- a fix
        előtt a "Kész" gate csak status in (DONE, CHECKPOINT_STOPPED)-re
        nézett, STOPPED-nél NÉMÁN kimaradt a figyelmeztetés. capacity_
        result_quality itt DEGRADED (status != DONE), tehát QMessageBox.
        warning-ot hív, nem .information-t."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "CHARACTERIZATION")
        window._on_finished(TestResult(status="STOPPED", total_charge_ah=1.0, total_discharge_ah=0.3))
        assert len(messages) == 1
        assert "vissza kell tölteni" in messages[0]

    def test_characterization_fault_shows_recharge_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, review-kör 5 (2026-09-10), GUI/UX G5-04 (P3,
        DFAM-026 sibling)] Ugyanaz, mint a STOPPED testvér fent, de FAULT-ra
        -- egy safety-fault a kisütés közben szintén mélykisütött állapotban
        hagyhatja az akkut, a fix előtt itt sem jelent meg a figyelmeztetés
        (a `_on_fault()` külön kritikus dialógusa csak a hiba okát közli,
        nem a fizikai akku-állapotot)."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "CHARACTERIZATION")
        window._on_finished(TestResult(
            status="FAULT", reason="X", total_charge_ah=1.0, total_discharge_ah=0.3,
        ))
        assert len(messages) == 1
        assert "vissza kell tölteni" in messages[0]
    # A policy-deklaráció (mely test_type-ok látják a "recharge_required_
    # warning" csoportot) nem PySide6-függő -- azt Prog/tests/test_ui_
    # policy.py::TestCf34RechargeRequiredWarningPolicy fedi le, GUI nélkül.


class TestReviewRound2PhysicalCycleOnlyWarningConsumer:
    """[regresszió, Review-kör 2 (2026-09-08), GUI/UX F1/F2 lelet,
    DFAM-026 declared_policy_without_consumer -- a CF-34/recharge_
    required_warning PONTOS testvére] ui_policy.py "physical_cycle_only_
    warning" csoportját (BQ_LEARNING_PHYSICAL-ra deklarálva) korábban
    SENKI nem kérdezte le -- a kezelő a "Kész" popupból nem tudta meg,
    hogy ez a mód CSAK a fizikai ciklust futtatta le, BQ-oldali
    tanulás-ellenőrzés nélkül (CLAUDE.md §7)."""

    def _make_window(self, monkeypatch, tmp_path, test_type: str):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type=test_type)
        window._build_runner(cfg)
        return window

    def test_bq_learning_physical_terminal_checkpoint_shows_physical_cycle_only_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F2 (P2,
        DFAM-028)] Ez a teszt korábban egy kézzel gyártott status="DONE"
        TestResult-tal hívta az _on_finished()-t -- ez a status ÉLESEN
        SOHA nem fordulhat elő BQ_LEARNING_PHYSICAL-nál, mert a
        TestPlan.bq_learning_physical() utolsó lépése MANUAL_CHECKPOINT
        (l. TestStructuralGuardBqLearningPhysicalEndsWithCheckpoint lent),
        ami mindig CHECKPOINT_STOPPED-del zár. A teszt ezért egy
        ELÉRHETETLEN útvonalat fedett le, miközben az éles ág (a fix előtt)
        HOLT volt -- a "Kész" gate csak status=="DONE"-ra nézett. Mostantól
        a valós, terminális (resume_possible=False) CHECKPOINT_STOPPED
        státuszt szimuláljuk. A capacity_result_quality régóta
        "status != DONE" -> DEGRADED-et ad, tehát a friss GUI/UX F1 fix
        óta ez QMessageBox.warning-ot hív (NEM .information-t) -- l.
        Prog/tests/gui/conftest.py::_no_blocking_message_boxes."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "BQ_LEARNING_PHYSICAL")
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", resume_possible=False,
            total_charge_ah=1.0, total_discharge_ah=0.5,
        ))
        assert len(messages) == 1
        assert "NEM jelenti a BQ34Z110 tanulás sikerét" in messages[0]

    def test_bq_learning_physical_resumable_checkpoint_shows_no_popup(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F2 kiegészítő
        eset] egy FOLYTATHATÓ (resume_possible=True) checkpoint NEM a
        session vége -- itt a "Kész" popupnak NEM szabad megjelennie (a
        checkpoint fülön már megjelent a saját üzenet)."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "BQ_LEARNING_PHYSICAL")
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", resume_possible=True,
            total_charge_ah=1.0, total_discharge_ah=0.5,
        ))
        assert messages == []

    def test_bq_learning_physical_terminal_checkpoint_shows_recharge_required_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, review-kör 4 (2026-09-09), friss GUI/UX F1 (P2,
        DFAM-001)] BQ_LEARNING_PHYSICAL utolsó energiaátadó lépése egy
        DISCHARGE (l. test_runner.py::TestPlan.bq_learning_physical()) --
        a session lemerített akkuval zár, pontosan úgy, mint DISCHARGE_
        ONLY/CHARACTERIZATION/stb. Az ui_policy.py "recharge_required_
        warning" csoportja korábban nem volt deklarálva erre a módra --
        a CF-34/GUI-F3 sibling-keresése egy fix módlistára szűkült, nem a
        tényleges végállapotra. Ugyanaz a terminális (resume_possible=
        False) CHECKPOINT_STOPPED útvonal, mint a physical_cycle_only_
        warning tesztje fent."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "BQ_LEARNING_PHYSICAL")
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", resume_possible=False,
            total_charge_ah=1.0, total_discharge_ah=0.5,
        ))
        assert len(messages) == 1
        assert "szulfatáció elkerülése érdekében azonnal vissza kell tölteni" in messages[0]

    def test_empty_active_test_type_suppresses_both_warnings(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, D9/F-08, 2026-09-13, Riportok/ui_policy_audit_
        20260913.md] `ui_policy.is_group_visible()` ismeretlen/üres
        test_type-ra fail-open módon MINDEN csoportra True-t adna --
        `_active_test_type` __init__-beli "" kezdőértéke ma nem érhető el
        élesen (`_build_runner()` a cfg.test_type-ot egy ValueError-ral
        védett dispatch-ágon már leellenőrizte, mielőtt `_active_test_
        type` beállítódna), de egy jövőbeli, a dispatch-ágból kimaradó
        test_type esetén a kezelő MINDKÉT, egymástól független
        figyelmeztetést (recharge_required_warning ÉS physical_cycle_
        only_warning) megkapná, akkor is, ha azok irrelevánsak. Ez a
        teszt a védőőr viselkedését direktben igazolja, az
        `_active_test_type`-ot a _build_runner() utáni, sikeres
        állapotból explicit ""-ra állítva -- diszkrimináló: a guard
        nélkül mindkét üzenet megjelenne egy OCV_SOC_CHARACTERIZATION
        módra is, amire se a recharge, se a physical-cycle-only
        figyelmeztetés nem releváns policy-szerint."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        _use_hil_verified_ocv_soc_pulse_interval(monkeypatch)
        window = self._make_window(monkeypatch, tmp_path, "OCV_SOC_CHARACTERIZATION")
        window._active_test_type = ""
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
        ))
        combined = " ".join(messages)
        assert "szulfatáció" not in combined
        assert "fizikai töltés/kisütés" not in combined

    def test_characterization_done_does_not_show_physical_cycle_only_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """A CHARACTERIZATION mód nem BQ_LEARNING_PHYSICAL -- a figyelmeztetés
        itt nem releváns (és a recharge_required_warning-gal sem szabad
        összekeveredniük -- ez a teszt azt is igazolja, hogy a két
        figyelmeztetés egymástól függetlenül, a saját policy-csoportja
        szerint jelenik meg)."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "CHARACTERIZATION")
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True,
            safe_off_result={"PSU": True, "Load": True},
        ))
        assert len(messages) == 1
        assert "NEM jelenti a BQ34Z110 tanulás sikerét" not in messages[0]

    def test_bq_learning_physical_stopped_shows_physical_cycle_only_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, review-kör 5 (2026-09-10), GUI/UX G5-04 (P3,
        DFAM-026 sibling)] BQ_LEARNING_PHYSICAL-nál (ellentétben a DONE
        esettel fent, ami szerkezetileg elérhetetlen) egy kézi Stop A
        FIZIKAI CIKLUS KÖZBEN, a záró MANUAL_CHECKPOINT előtt, VALÓSÁGOSAN
        bekövetkezhet -- a fix előtt a gate ezt az útvonalat is kihagyta."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "BQ_LEARNING_PHYSICAL")
        window._on_finished(TestResult(
            status="STOPPED", total_charge_ah=1.0, total_discharge_ah=0.2,
        ))
        assert len(messages) == 1
        assert "NEM jelenti a BQ34Z110 tanulás sikerét" in messages[0]

    def test_bq_learning_physical_fault_shows_physical_cycle_only_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, review-kör 5 (2026-09-10), GUI/UX G5-04 (P3,
        DFAM-026 sibling)] Ugyanaz a testvér-eset, mint fent, de FAULT-ra."""
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        messages: list = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: messages.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = self._make_window(monkeypatch, tmp_path, "BQ_LEARNING_PHYSICAL")
        window._on_finished(TestResult(
            status="FAULT", reason="X", total_charge_ah=1.0, total_discharge_ah=0.2,
        ))
        assert len(messages) == 1
        assert "NEM jelenti a BQ34Z110 tanulás sikerét" in messages[0]


class TestStructuralGuardBqLearningPhysicalEndsWithCheckpoint:
    """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F2 (P2,
    DFAM-028)] Szerkezeti őrteszt: a fenti physical_cycle_only_warning
    tesztek azért CHECKPOINT_STOPPED (resume_possible=False) státusszal
    szimulálják a BQ_LEARNING_PHYSICAL session végét, mert a
    TestPlan.bq_learning_physical() terve UTOLSÓ lépése MANUAL_CHECKPOINT
    -- ha ez a jövőben megváltozna (pl. egy záró lépés kerülne utána), a
    fenti tesztek csendben ELÉRHETETLEN útvonalat kezdenének fedni,
    ugyanúgy, ahogy a korábbi, DONE-alapú teszt is észrevétlenül azzá
    vált. Ez a teszt buktatja azt a jövőbeli driftet."""

    def test_last_step_is_manual_checkpoint(self):
        from Prog.src.test_runner import StepKind, TestPlan
        plan = TestPlan.bq_learning_physical()
        assert plan.steps[-1].kind is StepKind.MANUAL_CHECKPOINT


class TestP2BatteryTemperatureSourceReflectsConfiguredMode:
    """Teljes kódbázis review 1. kör (2026-08-26), Szoftver/Architektúra P2
    lelet: report_generator.py korábban feltétel nélkül "DMM2_surface_PT100"-ot
    jelentett, függetlenül a ténylegesen konfigurált temperature_sensor_mode-tól
    (CLAUDE.md §12)."""

    def test_rtd_mode_reports_pt100_source(self, qapp, monkeypatch, tmp_path):
        import json

        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult

        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(temperature_sensor_mode="RTD_4W_PT100")
        window._build_runner(cfg)
        window._on_finished(TestResult(status="DONE", total_charge_ah=1.0))
        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["battery_temperature_source"] == "DMM2_surface_PT100"

    def test_ntc_mode_reports_ntc_source_not_pt100(self, qapp, monkeypatch, tmp_path):
        import json

        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult

        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(temperature_sensor_mode="THERMISTOR_2W_NTC_10K_B3950")
        window._build_runner(cfg)
        window._on_finished(TestResult(status="DONE", total_charge_ah=1.0))
        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["battery_temperature_source"] == "DMM2_surface_NTC_10K_B3950"
        assert "PT100" not in report["battery_temperature_source"]


class TestCf28EmergencyStopWithNoActiveSessionIsNotASilentNoop:
    """Governance-v2 review-kör 1 (GUI reviewer, CF-28/GUI-R1-09,
    DFAM-018): a CheckpointPanel "Safe Off" gombja mindig aktív, és ha a
    session már véget ért (worker None) VAGY egy checkpointnál parkolva
    a worker eltakarítva (`_cleanup_thread()` már lefutott, l.
    `_on_finished()`), `_emergency_stop()` `self._worker is None` ÉS
    `self._connect_worker is None` esetén SEMMIT nem csinál — nincs
    `else` ág. A kezelő megnyom egy vészleállító gombot, és NEM kap
    visszajelzést arról, hogy az nem hajtott végre semmit."""

    def test_emergency_stop_with_no_worker_shows_a_status_message(
        self, qapp, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        _patch_hardware(monkeypatch)
        window = MainWindow()
        assert window._worker is None
        assert window._connect_worker is None
        window._status_bar.showMessage("SENTINEL_UNCHANGED_IF_NOOP")

        window._emergency_stop()

        message = window._status_bar.currentMessage()
        assert message != "SENTINEL_UNCHANGED_IF_NOOP"


class TestP2EmergencyStopDuringConnect:
    """Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet: a
    hardveres bring-up (VISA connect/identify, BQ negotiate) alatt korábban
    az Emergency Stop gomb tiltva maradt, a kezelőnek nem volt módja
    jelezni egy elakadt kapcsolódási kísérlet megszakítási szándékát."""

    def test_emergency_stop_button_enabled_when_connect_starts(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cp = window._config_panel
        cp._model_edit.setText("TEST7AH")
        cp._capacity_spin.setValue(1.0)
        cp._psu_res_edit.setText("MOCK_PSU")
        cp._load_res_edit.setText("MOCK_LOAD")
        cp._dmm_v_res_edit.setText("MOCK_DMM_V")
        cp._dmm_t_res_edit.setText("MOCK_DMM_T")
        cp._psu_mode_combo.setCurrentText("INDEPENDENT")
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        window._build_runner = lambda cfg, pre_connected=None: (_ for _ in ()).throw(
            RuntimeError("stop before real TestRunnerWorker/QThread in test")
        )
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        assert window._live_panel._emstop_btn.isEnabled() is False
        assert window._config_panel._search_btn.isEnabled() is True
        window._start_test()
        assert window._connect_worker is not None
        assert window._live_panel._emstop_btn.isEnabled() is True
        # Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet:
        # a device search gomb is tiltva kell legyen kapcsolódás alatt.
        assert window._config_panel._search_btn.isEnabled() is False

        deadline = time.monotonic() + 5.0
        while window._connect_worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

    def test_calibration_button_disabled_during_connect_bringup(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 23. kör (2026-09-07), GUI/UX/
        Állapot P3 lelet] a "Gauge kalibráció…" gomb korábban akadálytalanul
        elérhető maradt egy MÉG folyamatban lévő hardveres bring-up
        (_connect_worker, akár egy NEM BQ-transportot igénylő fizikai teszt
        indításakor is) alatt — csak a kattintás-idejű QMessageBox.warning()
        védte, a widget-állapot nem tükrözte."""
        from PySide6.QtWidgets import QMessageBox

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cp = window._config_panel
        cp._model_edit.setText("TEST7AH")
        cp._capacity_spin.setValue(1.0)
        cp._psu_res_edit.setText("MOCK_PSU")
        cp._load_res_edit.setText("MOCK_LOAD")
        cp._dmm_v_res_edit.setText("MOCK_DMM_V")
        cp._dmm_t_res_edit.setText("MOCK_DMM_T")
        cp._psu_mode_combo.setCurrentText("INDEPENDENT")
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        window._build_runner = lambda cfg, pre_connected=None: (_ for _ in ()).throw(
            RuntimeError("stop before real TestRunnerWorker/QThread in test")
        )
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        assert window._bq_diagnostics_panel._calibration_btn.isEnabled() is True
        window._start_test()
        assert window._connect_worker is not None
        assert window._bq_diagnostics_panel._calibration_btn.isEnabled() is False

        deadline = time.monotonic() + 5.0
        while window._connect_worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert window._bq_diagnostics_panel._calibration_btn.isEnabled() is True

    def test_emergency_stop_during_connect_sets_abort_flag(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._connect_worker = object()  # sentinel: "kapcsolódás folyamatban"

        assert window._connect_abort_requested is False
        window._emergency_stop()
        assert window._connect_abort_requested is True

    def test_connect_ready_after_abort_disconnects_and_does_not_start_session(
        self, qapp, monkeypatch, tmp_path
    ):
        """[a lelet lényege] Ha vészleállítás történt kapcsolódás közben, a
        MÁR sikeresen megnyitott kapcsolatok azonnal lezáródnak, ÉS a
        session NEM indul el (self._worker marad None)."""
        from unittest.mock import MagicMock

        from Prog.gui.main_window import _PreConnectedInstruments

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._connect_abort_requested = True
        window._pending_start_cfg = _make_cfg()

        mock_instruments = MagicMock()
        pre_connected = _PreConnectedInstruments(
            instruments=mock_instruments,
            psu=MagicMock(), load=MagicMock(), dmm_v=MagicMock(), dmm_t=MagicMock(),
            bq_transport=None, session_bq_serial=None, instrument_identities={},
        )

        window._on_connect_ready(pre_connected)

        mock_instruments.disconnect_all.assert_called_once()
        # Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-04 lelet:
        # a fizikai PSU/Load kimenetet safe_all_off()-fal KI KELL kapcsolni,
        # MIELŐTT a VISA-kapcsolat lezáródik (disconnect_all()) -- egy
        # kapcsolódás-közbeni vészleállítás sem hagyhatja aktívan a kimenetet.
        mock_instruments.safe_all_off.assert_called_once()
        assert [c[0] for c in mock_instruments.mock_calls] == [
            "safe_all_off", "disconnect_all",
        ]
        assert window._worker is None
        assert window._connect_abort_requested is False
        assert window._live_panel._start_btn.isEnabled() is True
        assert window._config_panel._search_btn.isEnabled() is True

    def test_connect_ready_after_abort_also_closes_bq_serial(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 4. kör (2026-08-29), GUI/UX P2
        lelet] A fenti abort-ág korábban csak instruments.disconnect_all()-t
        hívott — a pre_connected.session_bq_serial (ha a test_type BQ-
        transportot is igényelt) sosem záródott le, ellentétben a
        _construct_and_connect_instruments() except-ágával, ami ugyanezt a
        kapcsolatot kivétel esetén helyesen lezárja. Nyitva maradt
        pyserial.Serial a következő BQ-kapcsolódási kísérletet "port
        busy"-hibába futtathatta volna."""
        from unittest.mock import MagicMock

        from Prog.gui.main_window import _PreConnectedInstruments

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._connect_abort_requested = True
        window._pending_start_cfg = _make_cfg()

        mock_instruments = MagicMock()
        mock_bq_serial = MagicMock()
        pre_connected = _PreConnectedInstruments(
            instruments=mock_instruments,
            psu=MagicMock(), load=MagicMock(), dmm_v=MagicMock(), dmm_t=MagicMock(),
            bq_transport=MagicMock(), session_bq_serial=mock_bq_serial,
            instrument_identities={},
        )

        window._on_connect_ready(pre_connected)

        mock_instruments.disconnect_all.assert_called_once()
        mock_bq_serial.close.assert_called_once()

    def test_connect_ready_success_clears_device_error_panel(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 16. kör (2026-09-04), GUI/UX/
        Állapot P3 lelet] a DeviceErrorPanel korábban semmilyen Start-
        eseménynél nem törlődött — egy korábbi session SCPI-hibái láthatók
        maradtak egy KÉSŐBBI, hibamentes session alatt is. A LivePanel.
        reset_plots() mintájára a sikeres _on_connect_ready() mostantól
        DeviceErrorPanel.clear()-t is hívja — ez a QThread-konstrukció ELŐTT
        történik, ezért a QThread.start()-ot no-op-ra cserélve (nincs valós
        háttérszál, l. feedback_qthread_test_leak_and_stash_chaining.md)
        biztonságosan, cleanup-igény nélkül tesztelhető."""
        from unittest.mock import MagicMock

        from PySide6.QtCore import QThread

        from Prog.gui.main_window import _PreConnectedInstruments

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QThread, "start", lambda self: None)
        window = MainWindow()
        window._connect_abort_requested = False
        window._pending_start_cfg = _make_cfg()
        window._device_error_panel.append_device_error(
            {"device": "PSU", "error": "korábbi session hibája"}
        )
        assert window._device_error_panel._list.count() == 1

        window._build_runner = lambda cfg, pre_connected=None: (MagicMock(), None, 0)

        mock_instruments = MagicMock()
        pre_connected = _PreConnectedInstruments(
            instruments=mock_instruments,
            psu=MagicMock(), load=MagicMock(), dmm_v=MagicMock(), dmm_t=MagicMock(),
            bq_transport=None, session_bq_serial=None, instrument_identities={},
        )

        window._on_connect_ready(pre_connected)

        assert window._device_error_panel._list.count() == 0


class TestP2DmmConfigurationLogged:
    """Teljes kódbázis review 1. kör (2026-08-26), Hardver/Műszervezérlés P2
    lelet: a ténylegesen alkalmazott DMM range/NPLC/hőmérséklet-mód
    konfiguráció korábban sehol nem került session-szintű naplózásra
    (CLAUDE.md §12)."""

    def test_dmm_configuration_event_logged_with_actual_mode(self, qapp, monkeypatch, tmp_path):
        import csv

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(temperature_sensor_mode="THERMISTOR_2W_NTC_10K_B3950")
        window._build_runner(cfg)

        rows = list(csv.DictReader((window._session_dir / "events.csv").open()))
        config_rows = [r for r in rows if r["event_code"] == "DMM_CONFIGURATION"]
        assert len(config_rows) == 1
        assert "THERMISTOR_2W_NTC_10K_B3950" in config_rows[0]["event_message"]
        # R1 B8b, F-26: a nplc=10 bring-up érték, nem a lépésenkénti NPLC.
        assert "bring-up" in config_rows[0]["event_message"]
        assert "NPLC_SET" in config_rows[0]["event_message"]


class TestP1PsuCombinationModeConfirmation:
    """Teljes kódbázis review 1. kör (2026-08-26), Hardver/Műszervezérlés
    P1 lelet (HW-003): _build_runner() a set_mode_*() hívás UTÁN korábban
    sosem ellenőrizte vissza (query_combination_mode()), hogy a PSU
    ténylegesen elfogadta-e a kért kombinációs módot — egy csendben
    elutasított SOUR:OUTP:SER/PAR parancs (pl. output még ON, remote lock)
    észrevétlen maradt volna, 24V/12-cellás pack esetén rossz
    feszültségosztás-feltételezéssel."""

    def test_series_mode_confirmed_matches_does_not_raise(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(psu_mode="SERIES")
        window._build_runner(cfg)  # nem dob kivételt

    def test_series_mode_rejected_by_psu_raises_configuration_error(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.src.exceptions import ConfigurationError

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        # A set_mode_series() a mock szerint "SER"-t állítana be, de a PSU
        # (szimulált csendes elutasítás) valójában "OFF"-ot jelent vissza.
        monkeypatch.setattr(Keithley2220PSU, "query_combination_mode", lambda self: "OFF")
        window = MainWindow()
        cfg = _make_cfg(psu_mode="SERIES")
        with pytest.raises(ConfigurationError, match="PSU kombinációs mód"):
            window._build_runner(cfg)


class TestP0ContinueFromCheckpointRunsOnWorkerThread:
    """Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P0 lelet:
    MainWindow._on_continue_requested() korábban KÖZVETLEN Python-hívással
    (self._worker.request_continue_from_checkpoint()) indította a folytatást,
    NEM a _continue_from_checkpoint_requested signalon keresztül — emiatt a
    folytatott ciklus a GUI-szálon futott volna, nem a worker saját
    QThread-jén. Ez a teszt VALÓS QThread-del igazolja a javítást: a
    folytatás ténylegesen a worker szálán fut, nem a hívó (GUI/teszt)
    szálán."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)
        return window

    def test_continue_from_checkpoint_executes_on_worker_thread_not_caller_thread(
        self, qapp, monkeypatch, tmp_path
    ):
        import threading

        from PySide6.QtCore import QThread

        from Prog.gui.worker import TestRunnerWorker
        from Prog.src.test_runner import TestPlan, TestResult

        window = self._make_window(qapp, monkeypatch, tmp_path)

        recorded: dict = {}

        class _ContinuationRunner:
            def __init__(self):
                self.on_sample = None
                self.on_event = None
                self.on_step_changed = None
                # Teljes kódbázis review 19. kör (2026-09-06), GUI/UX/Állapot
                # P1 lelet: request_continue_from_checkpoint() mostantól
                # közvetlenül olvassa ezt az attribútumot (getattr helyett)
                # — enélkül minden ezt a stub-ot használó teszt AttributeError-
                # ral bukna.
                self.emergency_stop_requested = False
                self.emergency_stop_reason = ""

            def run(self, plan, start_step_index: int = 0):
                recorded["thread_ident"] = threading.get_ident()
                recorded["start_step_index"] = start_step_index
                return TestResult(status="DONE")

            def request_stop(self):
                pass

            def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP"):
                pass

            def reset_control_flags(self):
                pass

        window._thread = QThread()
        window._worker = TestRunnerWorker(_ContinuationRunner(), TestPlan.characterization())
        window._worker.moveToThread(window._thread)
        # ugyanaz a huzalozás, mint _start_test()-ben:
        window._continue_from_checkpoint_requested.connect(
            window._worker.request_continue_from_checkpoint
        )
        # szimulált, korábban elért checkpoint (a valós útvonalon a worker
        # _handle_event()-je állítaná be MANUAL_BQ_CHECKPOINT_REACHED-nél)
        window._worker._checkpoint_next_step_index = 7
        window._thread.start()

        caller_thread_ident = threading.get_ident()
        window._on_continue_requested()

        deadline = time.monotonic() + 5.0
        while "thread_ident" not in recorded and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert "thread_ident" in recorded, "a folytatás sosem futott le a várt időn belül"
        assert recorded["thread_ident"] != caller_thread_ident, (
            "a folytatás a hívó (GUI/teszt) szálon futott közvetlen "
            "metódushívással, nem a worker saját QThread-jén queued "
            "signal-kapcsolaton keresztül"
        )
        assert recorded["start_step_index"] == 7

        window._thread.quit()
        window._thread.wait(5000)


class TestReview3rdRoundContinueSignalDisconnectedOnCleanup:
    """Teljes kódbázis review 3. kör (2026-08-29), GUI/UX P2 lelet: a
    _continue_from_checkpoint_requested (MainWindow-szintű, az app teljes
    élettartama alatt élő Signal) minden sikeres Start új kapcsolatot ad
    hozzá az adott session workerére, de korábban sosem lett disconnect-elve
    _cleanup_thread()-ben — minden lezárt session worker-gráfja a folyamat
    végéig memóriában maradt volna (memory leak, CLAUDE.md §15)."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)
        return window

    def test_cleanup_thread_disconnects_continue_signal(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtCore import QThread

        from Prog.gui.worker import TestRunnerWorker
        from Prog.src.test_runner import TestPlan, TestResult

        window = self._make_window(qapp, monkeypatch, tmp_path)

        recorded: dict = {"calls": 0}

        class _ContinuationRunner:
            def __init__(self):
                self.on_sample = None
                self.on_event = None
                self.on_step_changed = None
                # Teljes kódbázis review 19. kör (2026-09-06), GUI/UX/Állapot
                # P1 lelet: request_continue_from_checkpoint() mostantól
                # közvetlenül olvassa ezt az attribútumot (getattr helyett)
                # — enélkül minden ezt a stub-ot használó teszt AttributeError-
                # ral bukna.
                self.emergency_stop_requested = False
                self.emergency_stop_reason = ""

            def run(self, plan, start_step_index: int = 0):
                recorded["calls"] += 1
                return TestResult(status="DONE")

            def request_stop(self):
                pass

            def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP"):
                pass

            def reset_control_flags(self):
                pass

        window._thread = QThread()
        window._worker = TestRunnerWorker(_ContinuationRunner(), TestPlan.characterization())
        window._worker.moveToThread(window._thread)
        window._continue_from_checkpoint_requested.connect(
            window._worker.request_continue_from_checkpoint
        )
        window._thread.start()

        window._cleanup_thread()
        assert window._worker is None
        assert window._thread is None

        # Ha a signal NEM lett disconnect-elve, ez az emit() még mindig a
        # (most már deleteLater()-re várakozó) régi workert hívná meg egy
        # queued eseményen keresztül — a régi (javítás előtti) kód ezt
        # sosem akadályozta volna meg.
        window._continue_from_checkpoint_requested.emit()
        qapp.processEvents()

        assert recorded["calls"] == 0

    def test_cleanup_thread_disconnect_is_idempotent_when_never_connected(
        self, qapp, monkeypatch, tmp_path
    ):
        """_cleanup_thread() akkor is hívható, ha a signal sosem lett
        bekötve (pl. a session sosem jutott el _start_test()-ig) — a
        try/except (TypeError, RuntimeError) csendben átlép."""
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._cleanup_thread()  # nem dob kivételt


class TestWpAk02SessionRecoveryStartupReview:
    """WP-AK-02: app-indításkori "operator review" (REC-001..004 Restart
    workflow "incomplete session detect" lépése)."""

    def test_no_incomplete_sessions_no_dialog(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox
        calls = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        MainWindow()
        assert calls == []

    def test_incomplete_session_shows_informational_dialog(self, qapp, monkeypatch, tmp_path):
        import json

        from PySide6.QtWidgets import QMessageBox

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_STALE_20260101_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "safe_boundary": True, "step": "relax_after_charge",
                # Kör 19 D8e (GUI-R19-04): explicit, TELJESEN igazolt safe-off
                # -- e nélkül a hiányzó last_safe_off_result kulcs ÖNMAGÁBAN
                # is ⚠-jelölt "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" sort váltana ki,
                # a dialógust .warning()-útra terelve (ez a teszt kifejezetten
                # az "informational" -- nem-figyelmeztető -- útvonalat teszteli).
                "last_safe_off_result": {"PSU": True, "Load": True, "DMM_V": True, "DMM_T": True},
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        calls = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: calls.append(a))

        MainWindow()

        assert len(calls) == 1
        message_text = calls[0][2]
        assert "session_STALE_20260101_000000" in message_text
        assert "biztonságos határon" in message_text

    def test_incomplete_session_reviewed_gets_marker_file(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Teljes kódbázis review 9. kör (2026-08-30), Session/
        Workflow P2 lelet] korábban a megszakadt session mappája SEMMILYEN
        nyomot nem kapott arról, hogy az operátor ténylegesen megtekintette
        a startup review-n — örökre "sorsa dokumentálatlan" maradt, ha nem
        folytatták (CLAUDE.md §13: auditálható log és riport)."""
        import json

        from PySide6.QtWidgets import QMessageBox

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_STALE_20260101_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({"safe_boundary": True, "step": "relax_after_charge"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        # Kör 19 D8e (GUI-R19-04): ez a checkpoint nem hordoz last_safe_off_
        # result-ot, tehát a dialógus MOST .warning()-on megy -- ha ez
        # patch-elés nélkül maradna, egy VALÓDI, blokkoló Qt modális
        # dialógust nyitna meg a teszt alatt.
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)

        MainWindow()

        marker_path = session_dir / "startup_review_note.json"
        assert marker_path.exists()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert "reviewed_at_iso" in marker
        assert marker["note"]

    def test_incomplete_session_with_report_json_not_flagged(self, qapp, monkeypatch, tmp_path):
        """DATA-003 óta: ha van report.json, a session rendesen lefutott, NEM
        megszakadt — nem szabad az induláskori review-listában megjelennie."""
        import json

        from PySide6.QtWidgets import QMessageBox

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_DONE_20260101_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(json.dumps({}), encoding="utf-8")
        (session_dir / "report.json").write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        calls = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: calls.append(a))

        MainWindow()

        assert calls == []


class TestWpAk02ResumeAtStartTest:
    """WP-AK-02: `_build_runner()` a session_recovery döntési réteget hívja —
    ha talál egyező, biztonságos-határú megszakadt sessiont, és az operátor
    megerősíti, cross-process resume-ot ajánl (start_step_index a checkpoint
    next_safe_step_index-éből, parent_session_id rögzítve).

    Kör 19 D8e (GUI-R19-04) NORMAL verifier P3-megjegyzése: az itteni
    `_write_stale_checkpoint()` fixture NEM állít be `last_safe_off_result`-ot
    -- a MainWindow() konstrukciók ezen az osztályon emiatt MOST a
    `_review_incomplete_sessions_on_startup()` .warning()-ágán mennek át
    (l. main_window.py, GUI-R19-04). Ez itt SZÁNDÉKOSAN NINCS lokálisan
    patch-elve: a conftest.py `_no_blocking_message_boxes` autouse fixture-je
    MÁR minden GUI-teszthez no-op-ra cseréli a QMessageBox.information/
    warning/critical hívásokat (eredetileg a `_on_finished()`/`is_degraded`
    ugyanezen kockázati alakzatára vezetve be) -- ez az osztály erre a
    globális védőhálóra TÁMASZKODIK, nem egy helyi patch-re. Ha ez a
    conftest.py fixture valaha eltávolításra/szűkítésre kerülne, ez az
    osztály (és minden hasonló, last_safe_off_result nélküli checkpoint-
    fixture-t használó teszt) valódi, blokkoló Qt modális dialógust nyitna
    meg futás közben."""

    def _first_run_to_capture_hashes(self, qapp, monkeypatch, tmp_path):
        """Első, 'sikeres' build — csak azért, hogy megkapjuk a ténylegesen
        használt config_hash/profile_hash/instrument_identities értékeket,
        ne duplikáljuk a hash-számítás logikáját a tesztben."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        runner, _plan, _ = window._build_runner(cfg)
        return runner._config.config_hash, runner._config.profile_hash, runner._config.instrument_identities

    def _write_stale_checkpoint(
        self, tmp_path, config_hash, profile_hash, identities, *,
        safe_boundary=True, sample_id="", next_safe_step_index=3,
        charge_ah=2.5, discharge_ah=1.1,
        # CF-17/MET-R1-08 (batch 4.3): Wh-testvérpár, ugyanaz a minta, mint
        # a charge_ah/discharge_ah-nál.
        charge_wh=32.5, discharge_wh=14.3,
        # CF-18 + V-4 (batch 4.3, SPR-04 hátralévő fele): idő-origó + 5
        # diagnosztikai számláló, ugyanaz a minta.
        session_start_time_iso="2026-01-01T00:00:00+00:00",
        communication_faults_count=3, extreme_dt_s_count=2,
        max_observed_dt_s=7.5, relax_dmm_fault_count=1,
        temperature_out_of_range_count=4,
        # MAX verifier (review-kör 3, adbbda9f6120dfd33) P3, DFAM-001 5.
        # előfordulása: ugyanaz a resume-seed minta, mint
        # temperature_out_of_range_count-nál.
        temp_dmm_fault_ticks=5,
        # Review-kör 4 (2026-09-09), S4-05/M4-05 (P2/P3, DFAM-022):
        # ugyanaz a resume-seed minta, mint temp_dmm_fault_ticks-nél.
        warning_code_counts=None,
    ):
        if warning_code_counts is None:
            warning_code_counts = {"TEMPERATURE_OUT_OF_RANGE": 7}
        import json
        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_STALE_20260101_000000"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "safe_boundary": safe_boundary,
                "config_hash": config_hash,
                "profile_hash": profile_hash,
                "instrument_identities": identities,
                "sample_id": sample_id,
                "next_safe_step_index": next_safe_step_index,
                "charge_ah": charge_ah,
                "discharge_ah": discharge_ah,
                "charge_wh": charge_wh,
                "discharge_wh": discharge_wh,
                "session_start_time_iso": session_start_time_iso,
                "communication_faults_count": communication_faults_count,
                "extreme_dt_s_count": extreme_dt_s_count,
                "max_observed_dt_s": max_observed_dt_s,
                "relax_dmm_fault_count": relax_dmm_fault_count,
                "temperature_out_of_range_count": temperature_out_of_range_count,
                "temp_dmm_fault_ticks": temp_dmm_fault_ticks,
                "warning_code_counts": warning_code_counts,
            }),
            encoding="utf-8",
        )
        return session_dir

    def test_matching_stale_session_offers_resume_and_operator_accepts(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, next_safe_step_index=3
        )

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg()
        runner, _plan, resume_start_step_index = window._build_runner(cfg)

        assert resume_start_step_index == 3
        assert runner._config.parent_session_id == "session_STALE_20260101_000000"
        # WP-AK-02 review-lelet (Important/P1): a megszakadt session Ah-jai
        # átveendők, nem veszhetnek el.
        assert runner._config.initial_charge_ah == pytest.approx(2.5)
        assert runner._config.initial_discharge_ah == pytest.approx(1.1)
        # CF-17/MET-R1-08 (batch 4.3): Wh-testvérpár, ugyanaz az elvárás.
        assert runner._config.initial_charge_wh == pytest.approx(32.5)
        assert runner._config.initial_discharge_wh == pytest.approx(14.3)
        # CF-18 + V-4 (batch 4.3, SPR-04 hátralévő fele): idő-origó + 5
        # diagnosztikai számláló, ugyanaz az elvárás.
        assert runner._config.initial_session_start_time_iso == "2026-01-01T00:00:00+00:00"
        assert runner._config.initial_communication_faults_count == 3
        assert runner._config.initial_extreme_dt_s_count == 2
        assert runner._config.initial_max_observed_dt_s == pytest.approx(7.5)
        assert runner._config.initial_relax_dmm_fault_count == 1
        assert runner._config.initial_temperature_out_of_range_count == 4
        # MAX verifier (review-kör 3, adbbda9f6120dfd33) P3, DFAM-001 5.
        # előfordulása: a temp_dmm_fault_ticks korábban csak ÍRÓDOTT a
        # checkpoint.json-ba, sosem OLVASÓDOTT vissza resume-nál.
        assert runner._config.initial_temp_dmm_fault_ticks == 5
        # Review-kör 4 (2026-09-09), S4-05/M4-05 (P2/P3, DFAM-022): a
        # warning_code_counts volt az EGYETLEN session-szintű TestResult-
        # összeg, ami korábban nem élte túl a cross-process resume-ot.
        assert runner._config.initial_warning_code_counts == {"TEMPERATURE_OUT_OF_RANGE": 7}
        # WP-AK-02 review-lelet (Important/P1, REC-003): a végleges riportnak
        # jeleznie kell, hogy ez egy resume-olt session.
        import json as _json

        from Prog.src.test_runner import TestResult
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=2.53, total_discharge_ah=1.13,
        ))
        report = _json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["interrupted_session_recovered"] is True
        assert report["parent_session_id"] == "session_STALE_20260101_000000"
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"

    def test_matching_stale_session_operator_declines_starts_fresh(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, next_safe_step_index=3
        )

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.No,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg()
        runner, _plan, resume_start_step_index = window._build_runner(cfg)

        assert resume_start_step_index == 0
        assert runner._config.parent_session_id == ""

    def test_accepted_resume_marks_parent_session_so_it_is_not_offered_again(
        self, qapp, monkeypatch, tmp_path
    ):
        """Review-kör 2 (2026-09-08), Session/Workflow/Állapotgép S-2 lelet:
        korábban a sikeresen folytatott SZÜLŐ session könyvtárában semmi nem
        jelezte, hogy már folytatva lett -- discover_incomplete_sessions()
        (session_recovery.py) kizárólag a report.json HIÁNYÁT nézi, ami a
        szülő könyvtárban VÁLTOZATLANUL fennállt (a report.json az ÚJ,
        folytatott sessionbe íródik). A szülő ezért MINDEN jövőbeli
        induláskor újra felkínálta magát folytatásra. A fix a szülő
        könyvtárba ír egy minimális report.json-t elfogadáskor, kihasználva
        a MEGLÉVŐ 'van report.json -> nem incomplete' szabályt."""
        import json as _json

        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        parent_session_dir = self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, next_safe_step_index=3
        )
        assert not (parent_session_dir / "report.json").exists()

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg()
        _runner, plan, _resume_start_step_index = window._build_runner(cfg)

        assert (parent_session_dir / "report.json").exists()
        parent_report = _json.loads(
            (parent_session_dir / "report.json").read_text(encoding="utf-8")
        )
        assert parent_report["status"] == "RESUMED_INTO_NEW_SESSION"

        # A find_resumable_session()-nek mostantól NEM szabad ismét
        # felkínálnia ugyanezt a (most már report.json-nal rendelkező)
        # szülő session-t -- ez a konkrét, ismétlődő felkínálás, amit a
        # lelet leírt.
        # [Kör 21 F17, DFAM-010 sibling, javítás] `total_step_count` kötelező
        # 6. pozicionális paraméter (main_window.py:3729 óta, l. Governance-v2
        # O-3/F5) -- ez a hívóhely a bevezetésekor nem lett szinkronban
        # frissítve, a `evaluate_resume()`-on belüli `next_safe_step_index >=
        # total_step_count` kapu miatt korábban `TypeError`-ral bukott. A
        # produkciós hívás mintáját követve a FRISS terv lépésszámát adjuk át
        # (a checkpoint next_safe_step_index=3-nál lényegesen nagyobb, hogy a
        # teszt valóban a report.json-alapú kizárást ellenőrizze, ne a
        # lépésszám-kaput).
        from Prog.src.session_recovery import find_resumable_session
        match = find_resumable_session(
            tmp_path / "Mérések" / "Sessions", config_hash, profile_hash,
            identities, "", total_step_count=len(plan.steps),
        )
        assert match is None

    def test_accepted_resume_with_failed_construction_leaves_parent_resumable(
        self, qapp, monkeypatch, tmp_path
    ):
        """Normal-effort independent verifier follow-up (2026-09-09, P2
        lelet): ha az operátor elfogadja a folytatást, DE a session
        tényleges felépítése (itt: Logger konstruálása) UTÁNA hibázik, a
        szülő session könyvtárát NEM szabad megjelölni. Ha a jelölés a
        "Igen" válasz PILLANATÁBAN történne (a korábbi hely), a szülő
        "resumed"-nek tűnne, miközben SOSEM jött létre ténylegesen
        folytatott, checkpointolható session -- az operátor haladása
        látszólag eltűnne minden GUI-oldali helyreállítási folyamat elől.

        FONTOS: a `test_type`-nak VÁLTOZATLANNAK (a stale checkpoint-tal
        megegyezőnek) kell maradnia -- `test_type` a `CONFIG_HASH_FIELDS`
        része (session_recovery.py), egy eltérő érték már a resume-
        egyezést magát megakadályozná, és a teszt hamisan "átmenne"
        anélkül, hogy a resume-ágat egyáltalán érintené."""
        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        parent_session_dir = self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, next_safe_step_index=3
        )

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

        def _raise_logger_construction_failed(*a, **k):
            raise RuntimeError("simulated Logger construction failure")
        monkeypatch.setattr(
            "Prog.src.logger.Logger", _raise_logger_construction_failed
        )

        window = MainWindow()
        cfg = _make_cfg()  # ugyanaz a test_type, mint _first_run_to_capture_hashes()-ben

        with pytest.raises(RuntimeError, match="simulated Logger construction failure"):
            window._build_runner(cfg)

        assert not (parent_session_dir / "report.json").exists()

    def test_resume_marker_write_failure_refuses_session_not_silently(
        self, qapp, monkeypatch, tmp_path
    ):
        """Kör 21 Batch F5 (SES21-02, DFAM-032+DFAM-004): a szülő session
        "RESUMED_INTO_NEW_SESSION" jelölése korábban `except OSError: pass`-
        szal védett volt -- egy sikertelen lemez-írás után a szülő
        TOVÁBBRA IS resumable-nek látszott volna, és egy KÉSŐBBI indítás
        ismét felkínálhatta volna, két FÜGGETLEN gyerek-sessiont indítva
        UGYANAZON a fizikai akkun, UGYANAZZAL a felhalmozott Ah/Wh-
        seeddel. JAVÍTVA: a hiba MOST a `_build_runner()`-t körülvevő,
        MÁR meglévő `except Exception:` ágra fut -- ez a teszt a
        `_build_runner()` SAJÁT propagálását ellenőrzi, 1:1 a fenti
        `test_accepted_resume_with_failed_construction_leaves_parent_
        resumable` mintáját követve; a hívó (`_on_connect_ready()`)
        `QMessageBox.critical()`-ját (ami a worker/thread felépítése
        ELŐTT `return`-öl) ÉS a teljes cleanup-láncot a MÁR MEGLÉVŐ,
        generikus `_build_runner()`-kivétel tesztek (l.
        `test_start_accepting_summary_calls_build_runner` és testvérei)
        fedik, nem duplikálandó itt. A jelölés MOST egy `.json.tmp`
        fájlra ír először (l. MAX verifier lelete lent), ezért ez a
        teszt a `.tmp` írási lépésen szimulálja a hibát.

        Kör 21 F15 (SES21-04) UTÓLAGOS KORREKCIÓJA: a jelölés írása
        `Path.write_text()`-ről egy explicit `Path.open("w")` +
        `f.write()` + `f.flush()` + `os.fsync()` láncra váltott (fsync
        a rename ELŐTT, l. logger.py::write_checkpoint() azonos
        mintája) -- a `Path.write_text()` monkeypatch emiatt TÖBBÉ NEM
        éri el ezt a kódutat; a hibát most a `Path.open()` hívás szintjén
        szimuláljuk."""
        from pathlib import Path

        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        parent_session_dir = self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, next_safe_step_index=3
        )
        target = parent_session_dir / "report.json"
        target_tmp = parent_session_dir / "report.json.tmp"

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

        original_open = Path.open

        def _raise_only_for_parent_marker(self, *a, **k):
            if self == target_tmp:
                raise OSError("simulated disk write failure")
            return original_open(self, *a, **k)

        monkeypatch.setattr(Path, "open", _raise_only_for_parent_marker)

        window = MainWindow()
        cfg = _make_cfg()  # ugyanaz a test_type, mint _first_run_to_capture_hashes()-ben

        with pytest.raises(OSError, match="simulated disk write failure"):
            window._build_runner(cfg)

        assert not target.exists()

    def test_resume_marker_rename_failure_also_refuses_session(
        self, qapp, monkeypatch, tmp_path
    ):
        """MAX verifier lelete (Kör 21 F5, 2026-09-19): a `.write_text()`
        önmagában NEM atomikus -- egy `open()` UTÁNI, de a teljes
        tartalom kiírása ELŐTTI hiba (pl. ENOSPC) egy CSONKA
        `report.json`-t hagyott volna a szülő könyvtárban, ami a
        `discover_incomplete_sessions()`-t (létezés-alapú, nem parse-
        alapú) örökre kizárta volna a resume-kínálatból. JAVÍTVA: a
        marker most `.json.tmp`-re íródik, majd egy KÜLÖN, egy-lépéses
        `Path.replace()`-szel kerül a végleges névre -- ez a teszt a
        RENAME lépés hibáját szimulálja, és ellenőrzi, hogy a VÉGLEGES
        `report.json` ilyenkor is hiányzik (nincs csonka/félig-írt
        állapot a végleges néven)."""
        from pathlib import Path

        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        parent_session_dir = self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, next_safe_step_index=3
        )
        target = parent_session_dir / "report.json"
        target_tmp = parent_session_dir / "report.json.tmp"

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

        original_replace = Path.replace

        def _raise_only_for_parent_marker_rename(self, target_path):
            if self == target_tmp:
                raise OSError("simulated rename failure")
            return original_replace(self, target_path)

        monkeypatch.setattr(Path, "replace", _raise_only_for_parent_marker_rename)

        window = MainWindow()
        cfg = _make_cfg()  # ugyanaz a test_type, mint _first_run_to_capture_hashes()-ben

        with pytest.raises(OSError, match="simulated rename failure"):
            window._build_runner(cfg)

        assert not target.exists()

    def test_resume_marker_fsyncs_before_atomic_rename(
        self, qapp, monkeypatch, tmp_path
    ):
        """Kör 21 F15 (SES21-04) UTÓLAGOS KORREKCIÓJA (MAX verifier lelete):
        ez a hely a `logger.py::write_checkpoint()` tmp+rename mintáját
        vette át 1:1 (l. fenti kommentek), de az eredeti Kör 21 F15
        archívum-sweep ("grep os.replace") NEM találta meg, mert
        `Path.replace()`-t hív, nem szó szerinti `os.replace()`-t --
        holott a kettő azonos (CPython: `Path.replace()` maga hívja az
        `os.replace()`-t). Ez a teszt igazolja, hogy a MOST pótolt
        fsync a rename ELŐTT fut ezen a helyen is, 1:1 a logger.py
        oldali `test_checkpoint_fsyncs_before_atomic_rename` mintáját
        követve -- kizárólag a kód-oldali hívási sorrendet igazolja,
        NEM a fizikai (power-loss) tartósságot (az HIL_PENDING marad)."""
        from PySide6.QtWidgets import QMessageBox

        import Prog.gui.main_window as main_window_module

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, next_safe_step_index=3
        )

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

        call_order = []
        real_fsync = main_window_module.os.fsync
        real_replace = Path.replace

        def spy_fsync(fd):
            call_order.append("fsync")
            return real_fsync(fd)

        def spy_replace(self, target_path):
            call_order.append("replace")
            return real_replace(self, target_path)

        monkeypatch.setattr(main_window_module.os, "fsync", spy_fsync)
        monkeypatch.setattr(Path, "replace", spy_replace)

        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)

        assert call_order == ["fsync", "replace"], (
            "az fsync-nek a replace ELŐTT kell megtörténnie, különben a "
            "rename lathatosaga megelozheti a tartalom fizikai kiirasat"
        )

    def test_declined_resume_does_not_mark_parent_session(self, qapp, monkeypatch, tmp_path):
        """Kontroll: ha az operátor NEM fogadja el a folytatást, a szülő
        session könyvtárát nem szabad megjelölni -- lehet, hogy KÉSŐBB,
        egy másik alkalommal mégis folytatni akarja."""
        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        parent_session_dir = self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, next_safe_step_index=3
        )

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.No,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)

        assert not (parent_session_dir / "report.json").exists()

    def test_unsafe_boundary_stale_session_never_offered(self, qapp, monkeypatch, tmp_path):
        """Ha a megszakadt session NEM biztonságos határon állt meg, ne is
        jelenjen meg megerősítő kérdés — evaluate_resume() BLOCKED_UNSAFE_
        BOUNDARY-t ad, find_resumable_session() None-t, nincs mit kérdezni."""
        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, safe_boundary=False,
        )

        question_calls = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: question_calls.append(a) or QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg()
        _runner, _plan, resume_start_step_index = window._build_runner(cfg)

        assert resume_start_step_index == 0
        assert question_calls == []

    def test_empty_sample_id_shows_extra_warning_in_dialog(self, qapp, monkeypatch, tmp_path):
        """WP-AK-02 review-lelet (Important/P2): üres sample_id mellett egyező
        config/profil/műszer-felállás elméletben más fizikai akkumulátorra is
        felkínálhatja a folytatást — ezt a döntés előtt explicit jeleznünk kell,
        nem szabad elhallgatni."""
        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, sample_id="",
        )

        question_texts = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: question_texts.append(a[2]) or QMessageBox.StandardButton.No,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg()  # SessionConfig.sample_id alapból "" (üres)
        window._build_runner(cfg)

        assert len(question_texts) == 1
        assert "FIGYELEM" in question_texts[0]
        assert "sample_id" in question_texts[0] or "minta-azonosító" in question_texts[0]

    def test_mismatched_sample_id_never_offered(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox

        config_hash, profile_hash, identities = self._first_run_to_capture_hashes(
            qapp, monkeypatch, tmp_path
        )
        self._write_stale_checkpoint(
            tmp_path, config_hash, profile_hash, identities, sample_id="DIFFERENT_SAMPLE",
        )

        question_calls = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: question_calls.append(a) or QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        cfg = _make_cfg()
        _runner, _plan, resume_start_step_index = window._build_runner(cfg)

        assert resume_start_step_index == 0
        assert question_calls == []


class TestWpAk03StartGateAndSummary:
    """WP-AK-03 (SUP02-ACCEPT-001/11/12): _start_test() revision-gate +
    effective summary megerősítés a tényleges futtatás (worker/thread) előtt."""

    def _make_window_with_valid_config(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cp = window._config_panel
        cp._model_edit.setText("TEST7AH")
        cp._capacity_spin.setValue(1.0)  # 0.25*1=0.25A << 1.5A INDEPENDENT limit
        cp._psu_res_edit.setText("MOCK_PSU")
        cp._load_res_edit.setText("MOCK_LOAD")
        cp._dmm_v_res_edit.setText("MOCK_DMM_V")
        cp._dmm_t_res_edit.setText("MOCK_DMM_T")
        cp._psu_mode_combo.setCurrentText("INDEPENDENT")
        return window

    def test_start_blocked_when_charge_current_config_incompatible(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        cp = window._config_panel
        cp._capacity_spin.setValue(10.0)  # 0.25*10=2.5A > 1.5A INDEPENDENT limit

        warning_calls = []
        monkeypatch.setattr(
            QMessageBox, "warning", lambda *a, **k: warning_calls.append(a) or None
        )
        question_calls = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: question_calls.append(a) or QMessageBox.StandardButton.Yes,
        )

        window._start_test()

        assert window._worker is None
        assert len(warning_calls) == 1
        # A summary-dialógusig el sem jut, mert a revision-gate előbb blokkol.
        assert question_calls == []

    def test_start_blocked_message_includes_specific_blocking_error_text(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F7 (DFAM-026
        declared_policy_without_consumer)] a DynamicSessionModel.build_
        summary()["blocking_errors"] korábban egy konkrét, human-readable
        okot épített fel (pl. a kért töltőáram > PSU-mód limitje), de a
        _start_test() blokkoló ága eddig csak egy generikus üzenetet
        mutatott -- a kezelő nem tudta meg a konkrét okot a mezők manuális
        átvizsgálása nélkül."""
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        cp = window._config_panel
        cp._capacity_spin.setValue(10.0)  # 0.25*10=2.5A > 1.5A INDEPENDENT limit

        warning_texts = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_texts.append(a[2] if len(a) > 2 else k.get("text", "")),
        )

        window._start_test()

        assert len(warning_texts) == 1
        # "CONFIG_INCOMPATIBLE" önmagában NEM diszkriminál -- az a generikus
        # üzenetben is szerepel példaként. A "kért töltőáram" kifejezés
        # KIZÁRÓLAG a blocking_errors konkrét szövegében jelenik meg.
        assert "kért töltőáram" in warning_texts[0]

    def test_start_shows_summary_and_declining_does_not_start(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)

        question_texts = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: question_texts.append(a[2]) or QMessageBox.StandardButton.No,
        )

        window._start_test()

        assert window._worker is None
        assert len(question_texts) == 1
        assert "Indítod a mérést?" in question_texts[0]

    def test_start_blocked_when_bq_connect_worker_bringup_in_progress(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, Teljes kódbázis review 11. kör (2026-08-31), GUI/UX/
        Állapot P2 lelet] a meglévő `needs_bq_transport and self._bq_worker
        is not None` guard csak a MÁR MEGKÖTÖTT BQ Diagnosztika kapcsolatot
        védi — egy folyamatban lévő "Kapcsolódás" (_bq_connect_worker) alatt
        ez még None, mégis ugyanahhoz a fizikai Pico soros porthoz próbálna
        kapcsolódni egy MÁSODIK, önálló klienssel."""
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        cp = window._config_panel
        cp._test_type_combo.setCurrentText("TRADITIONAL_LEARNING")
        cp._bq_port_edit.setText("COM8")
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        criticals: list = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a))
        window._bq_connect_worker = object()  # sentinel: "kapcsolódás folyamatban"

        window._start_test()

        assert window._connect_worker is None
        assert len(criticals) == 1

    def test_start_accepting_summary_calls_build_runner(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        build_calls = []

        def _spy_build_runner(cfg, pre_connected=None):
            build_calls.append((cfg, pre_connected))
            raise RuntimeError("stop before real TestRunnerWorker/QThread in test")

        window._build_runner = _spy_build_runner
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        window._start_test()

        # Release-audit 2026-08-23 (2.6): a hardveres bring-up (itt mockolt
        # connect_all/identify_all, l. _patch_hardware) mostantól egy valódi,
        # külön QThread-en fut. A connect_ready jel keresztszálas (queued)
        # kézbesítéséhez a GUI-szál (itt: a teszt szála) eseményhurkát kell
        # pörgetni — SOSEM `connect_thread.wait()`-tel várni rá: az blokkolná
        # a hívó szálat, ami épp azt a szálat blokkolná, aminek fel kellene
        # dolgoznia a queued jelet a quit()-hoz (holtpont — l.
        # _on_connect_ready()/_cleanup_connect_thread() docstringjét).
        assert window._connect_thread is not None
        deadline = time.monotonic() + 5.0
        while not build_calls and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert len(build_calls) == 1  # a gate+summary átengedte, _build_runner ténylegesen hívva
        assert build_calls[0][1] is not None  # pre_connected átadva (mockolt connect eredménye)

    def test_start_resets_state_label_before_connect_begins(self, qapp, monkeypatch, tmp_path):
        """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F8 (P3,
        DFAM-001)] a reset_plots() korábban csak _on_connect_ready()-ben (a
        hardveres bring-up SIKERES befejezése UTÁN) futott le -- egy előző
        FAULT session "Állapot" címkéje a teljes bring-up idejére (akár
        tíz másodpercekig) az ELŐZŐ session állapotát mutatta volna. Most
        a reset a connect INDÍTÁSA előtt, szinkron módon fut le."""
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        window._live_panel.set_status("FAULT")
        assert window._live_panel._state_lbl.text() == "FAULT"

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        build_calls = []

        def _spy_build_runner(cfg, pre_connected=None):
            build_calls.append((cfg, pre_connected))
            raise RuntimeError("stop before real TestRunnerWorker/QThread in test")

        window._build_runner = _spy_build_runner
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        window._start_test()

        # A reset _start_test()-en BELÜL, a connect_thread.start() ELŐTT,
        # szinkron fut -- tehát itt, közvetlenül a hívás után is már látszania
        # kell, nem kell megvárni a (mockolt) connect befejezését.
        assert window._live_panel._state_lbl.text() == "IDLE"

        # Takarítás: a valódi QThread-et a bevált mintát követve hagyjuk
        # lefutni (l. test_start_accepting_summary_calls_build_runner
        # docstringje -- SOSEM connect_thread.wait()-tel, holtpont-veszély).
        deadline = time.monotonic() + 5.0
        while not build_calls and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(build_calls) == 1


class TestReleaseAudit20260823ConnectWorker:
    """Release-audit 2026-08-23 (2.6): a Start-folyamat blokkoló hardveres
    bring-up-ja (_InstrumentConnectWorker) nem fagyaszthatja a GUI-szálat,
    a Start gomb tiltva legyen kapcsolódás közben, dupla-Start ne indítson
    két worker-t, és hiba esetén a felhasználó egyértelmű hibaüzenetet
    kapjon, a Start gomb pedig visszakapja az engedélyezett állapotát."""

    def _make_window_with_valid_config(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cp = window._config_panel
        cp._model_edit.setText("TEST7AH")
        cp._capacity_spin.setValue(1.0)
        cp._psu_res_edit.setText("MOCK_PSU")
        cp._load_res_edit.setText("MOCK_LOAD")
        cp._dmm_v_res_edit.setText("MOCK_DMM_V")
        cp._dmm_t_res_edit.setText("MOCK_DMM_T")
        cp._psu_mode_combo.setCurrentText("INDEPENDENT")
        return window

    def _accept_all_dialogs(self, monkeypatch) -> None:
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )

    def test_start_disables_start_button_during_connect(self, qapp, monkeypatch, tmp_path):
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        self._accept_all_dialogs(monkeypatch)
        window._build_runner = lambda cfg, pre_connected=None: (_ for _ in ()).throw(
            RuntimeError("stop before real TestRunnerWorker/QThread in test")
        )
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        assert window._live_panel._start_btn.isEnabled() is True
        window._start_test()
        # A connect worker elindult, de a queued connect_ready jelet még nem
        # dolgoztuk fel — a Start gombnak MOST tiltva kell lennie.
        assert window._live_panel._start_btn.isEnabled() is False

        deadline = time.monotonic() + 5.0
        while window._connect_worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        # A hiba (spy RuntimeError) miatt a Start gombnak vissza kell kapnia
        # az engedélyezett állapotát.
        assert window._live_panel._start_btn.isEnabled() is True

    def test_double_start_does_not_spawn_second_connect_worker(self, qapp, monkeypatch, tmp_path):
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        self._accept_all_dialogs(monkeypatch)
        window._build_runner = lambda cfg, pre_connected=None: (_ for _ in ()).throw(
            RuntimeError("stop before real TestRunnerWorker/QThread in test")
        )
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
        question_calls = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: question_calls.append(a) or QMessageBox.StandardButton.Yes,
        )

        window._start_test()
        first_worker = window._connect_worker
        assert first_worker is not None
        window._start_test()  # dupla kattintás, amíg a connect még fut

        # A második hívásnak korán vissza kellett térnie (self._connect_worker
        # is not None guard) — nem futott le még egyszer a teljes gate+summary
        # lánc, tehát csak EGY "Összefoglaló" dialógus jelent meg.
        assert window._connect_worker is first_worker
        assert len(question_calls) == 1

        deadline = time.monotonic() + 5.0
        while window._connect_worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

    def test_close_event_ignored_while_connecting(self, qapp, monkeypatch, tmp_path):
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        self._accept_all_dialogs(monkeypatch)
        # A connect worker sosem kap esélyt lefutni ebben a tesztben (nincs
        # qapp.processEvents() hívás) — pont az az ablak vizsgált, amikor a
        # kapcsolódás MÉG folyamatban van.
        window._start_test()
        assert window._connect_worker is not None

        from PySide6.QtWidgets import QMessageBox
        info_calls = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a, **k: info_calls.append(a) or None,
        )

        class _FakeEvent:
            def __init__(self):
                self.ignored = False
                self.accepted = False

            def ignore(self):
                self.ignored = True

            def accept(self):
                self.accepted = True

        event = _FakeEvent()
        window.closeEvent(event)

        assert event.ignored is True
        assert event.accepted is False
        assert len(info_calls) == 1

        # takarítás: hagyjuk lefutni a connect workert, hogy ne maradjon
        # függő QThread a teszt után.
        deadline = time.monotonic() + 5.0
        while window._connect_worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

    def test_close_event_ignored_when_worker_thread_does_not_stop_in_time(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, review-kör 4 (2026-09-09), Session/Workflow S4-01
        (P1, MAX verifier kötelező)] a closeEvent() Yes-ága korábban
        eldobta a QThread.wait(5000) visszatérési értékét és event.accept()
        -et hívott akkor is, ha a worker-szál MÉG FUTOTT (a safe-off/BQ
        Cell BL restore közepén) -- az ablak bezárása után a PSU fizikailag
        ON maradhatott, vagy a BQ chip véglegesen az ideiglenes learning-
        küszöbön ragadhatott. Most a wait()==False esetén az ablak NEM
        zárható be."""
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)

        class _FakeWorker:
            def request_emergency_stop(self, reason):
                pass

            def request_continue_from_checkpoint(self):
                pass

            def deleteLater(self):
                pass

        class _FakeThreadStillRunning:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return False  # a szál MÉG FUT az 5s timeout után is

            def deleteLater(self):
                raise AssertionError(
                    "deleteLater() nem hívható egy még futó QThread-en"
                )

        window._worker = _FakeWorker()
        window._thread = _FakeThreadStillRunning()

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        warning_calls = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **k: warning_calls.append(a) or None,
        )

        class _FakeEvent:
            def __init__(self):
                self.ignored = False
                self.accepted = False

            def ignore(self):
                self.ignored = True

            def accept(self):
                self.accepted = True

        event = _FakeEvent()
        window.closeEvent(event)

        assert event.ignored is True
        assert event.accepted is False
        assert len(warning_calls) == 1
        # A szál még "fut" -- a referenciát NEM szabad eldobni, különben a
        # következő closeEvent()/_start_test() egy már törölt QThread-re
        # hivatkozna.
        assert window._worker is not None
        assert window._thread is not None

    def _install_still_running_worker(self, window) -> None:
        """S5-04 (P2, S4-01 pontos sibling-mintája fent) -- ugyanaz a
        _FakeWorker/_FakeThreadStillRunning pár, ami a wait(5000)
        timeoutot szimulálja."""
        class _FakeWorker:
            def request_emergency_stop(self, reason):
                pass

            def request_continue_from_checkpoint(self):
                pass

            def deleteLater(self):
                pass

        class _FakeThreadStillRunning:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return False  # a szál MÉG FUT az 5s timeout után is

            def deleteLater(self):
                raise AssertionError(
                    "deleteLater() nem hívható egy még futó QThread-en"
                )

        window._worker = _FakeWorker()
        window._thread = _FakeThreadStillRunning()

    def test_on_finished_disables_start_when_worker_thread_does_not_stop_in_time(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, review-kör 5, Session/Workflow S5-04 (P2, S4-01
        sibling)] _on_finished() korábban eldobta a _cleanup_thread()
        visszatérési értékét -- a Start gombot ténylegesen a KORÁBBAN
        (a `finished` előtt) tüzelő `status_changed` jel nyomán a
        LivePanel.set_status() engedélyezte vissza, FÜGGETLENÜL attól,
        hogy a szál ténylegesen leállt-e. Egy new Start ekkor egy MÁSODIK,
        egyidejű InstrumentManager/TestRunner-t hozhatna létre a MÉG ÉLŐ
        régi worker-szál mellett."""
        from Prog.src.test_runner import TestResult

        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        self._install_still_running_worker(window)
        window._live_panel.set_start_enabled(True)  # szimulálja a set_status() korábbi hatását
        events: list = []
        monkeypatch.setattr(window._live_panel, "append_event", events.append)

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

        window._on_finished(TestResult(status="DONE", total_charge_ah=1.0))

        assert window._live_panel._start_btn.isEnabled() is False
        assert any(e.get("event_code") == "WORKER_THREAD_CLEANUP_TIMEOUT" for e in events)
        # A szál még "fut" -- a referenciát NEM szabad eldobni.
        assert window._worker is not None
        assert window._thread is not None

    def test_on_finished_enables_start_when_worker_thread_stops_in_time(
        self, qapp, monkeypatch, tmp_path,
    ):
        """Ellenpróba: ha a wait() sikeres, a Start gomb rendesen
        engedélyezett marad -- a fix kizárólag a timeout-ágat érinti."""
        from Prog.src.test_runner import TestResult

        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)

        class _FakeWorker:
            def request_emergency_stop(self, reason):
                pass

            def request_continue_from_checkpoint(self):
                pass

            def deleteLater(self):
                pass

        class _FakeThreadStops:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return True

            def deleteLater(self):
                pass

        window._worker = _FakeWorker()
        window._thread = _FakeThreadStops()

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

        window._on_finished(TestResult(status="DONE", total_charge_ah=1.0))

        assert window._live_panel._start_btn.isEnabled() is True

    def test_on_fault_does_not_directly_change_start_button_state(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, review-kör 5, Session/Workflow S5-04 MAX verifier
        B1 lelet (2026-09-10, PARTIAL -> javítva)] Az EREDETI S5-04 fix
        _on_fault()-ban is explicit set_start_enabled(False)-t hívott
        cleanup_ok=False esetén, azzal az (empirikusan MEGCÁFOLT)
        feltevéssel, hogy _on_finished() ELŐBB fut le. A VALÓS sorrend
        fordított (l. a következő teszt) -- _on_fault() mostantól NEM
        állítja be a Start gomb állapotát, KIZÁRÓLAG _on_finished() teszi
        ezt, függetlenül attól, mikor fut le a `finished` jel a `fault`-hoz
        képest."""
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        self._install_still_running_worker(window)
        window._live_panel.set_start_enabled(True)

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        window._on_fault("SIMULATED_FAULT")

        # _on_fault() önmagában NEM nyúl a Start gombhoz -- a régi állapot
        # (True) változatlan marad, mert a valós útvonalon MINDIG lefutó
        # _on_finished() dönt majd, nem ez a metódus.
        assert window._live_panel._start_btn.isEnabled() is True

    def test_on_fault_modal_dialog_does_not_override_on_finished_final_state(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, review-kör 5, Session/Workflow S5-04 MAX verifier
        B1 lelet (2026-09-10, PARTIAL -> javítva)] A VALÓS Qt-viselkedést
        reprodukálja: `QMessageBox.critical()` egy application-modal
        dialógus, ami egy BEÁGYAZOTT event loopot nyit -- a worker.py::
        _do_run() a `fault` jel UTÁN, UGYANABBAN a hívásban emittálja a
        `finished`-et is, ami emiatt PONTOSAN a modal alatt kézbesítődhet
        (ezt szimuláljuk: a `critical()` monkeypatch maga hívja meg
        window._on_finished()-t). A FAKE szál első (`_on_fault()`-beli)
        wait()-je timeoutol, a MÁSODIK (`_on_finished()`-beli, a modal
        ALATT lefutó) már sikeres -- a fix ELŐTT ez a forgatókönyv a
        Start gombot VÉGLEGESEN letiltva hagyta volna, holott a szál
        ténylegesen leállt."""
        from Prog.src.test_runner import TestResult

        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)

        class _FakeWorker:
            def request_emergency_stop(self, reason):
                pass

            def request_continue_from_checkpoint(self):
                pass

            def deleteLater(self):
                pass

        class _FakeThreadEventuallyStops:
            def __init__(self):
                self.wait_calls = 0

            def quit(self):
                pass

            def wait(self, timeout_ms):
                self.wait_calls += 1
                # 1. hívás (_on_fault()) timeoutol, a 2. (_on_finished(),
                # a modal "alatt" lefutva) már sikeres.
                return self.wait_calls >= 2

            def deleteLater(self):
                pass

        window._worker = _FakeWorker()
        window._thread = _FakeThreadEventuallyStops()
        window._live_panel.set_start_enabled(True)

        from PySide6.QtWidgets import QMessageBox

        def _critical_delivers_queued_finished_signal(*a, **k):
            window._on_finished(TestResult(status="FAULT", reason="SIMULATED_FAULT"))

        monkeypatch.setattr(QMessageBox, "critical", _critical_delivers_queued_finished_signal)

        window._on_fault("SIMULATED_FAULT")

        assert window._live_panel._start_btn.isEnabled() is True
        assert window._worker is None
        assert window._thread is None

    def test_on_checkpoint_close_disables_start_when_worker_thread_does_not_stop_in_time(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, review-kör 5, Session/Workflow S5-04 (P2, S4-01
        sibling)] _on_checkpoint_close() korábban FELTÉTEL NÉLKÜL
        engedélyezte a Start gombot a _cleanup_thread() eredményétől
        függetlenül -- ez a legegyértelműbb eset, mert itt nincs egy
        korábbi status_changed jel, ami "véletlenül helyesen" hagyná a
        gombot letiltva."""
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)
        self._install_still_running_worker(window)
        window._pending_checkpoint_result = None
        events: list = []
        monkeypatch.setattr(window._live_panel, "append_event", events.append)

        window._on_checkpoint_close()

        assert window._live_panel._start_btn.isEnabled() is False
        assert any(e.get("event_code") == "WORKER_THREAD_CLEANUP_TIMEOUT" for e in events)
        assert window._worker is not None
        assert window._thread is not None

    def test_on_checkpoint_close_enables_start_when_worker_thread_stops_in_time(
        self, qapp, monkeypatch, tmp_path,
    ):
        """Ellenpróba: sikeres cleanup esetén a Start gomb visszaáll --
        ez már a fix ELŐTT is így működött, csak most explicit a
        cleanup_ok-tól függ, nem feltétel nélküli."""
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)

        class _FakeWorker:
            def request_emergency_stop(self, reason):
                pass

            def request_continue_from_checkpoint(self):
                pass

            def deleteLater(self):
                pass

        class _FakeThreadStops:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return True

            def deleteLater(self):
                pass

        window._worker = _FakeWorker()
        window._thread = _FakeThreadStops()
        window._pending_checkpoint_result = None

        window._on_checkpoint_close()

        assert window._live_panel._start_btn.isEnabled() is True

    def test_close_event_accepted_when_worker_thread_stops_in_time(
        self, qapp, monkeypatch, tmp_path,
    ):
        """Ellenpróba: ha a wait() sikeres (a szál időben leáll), a
        bezárás továbbra is működik -- a fix kizárólag a timeout-ágat
        érinti."""
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)

        class _FakeWorker:
            def request_emergency_stop(self, reason):
                pass

            def request_continue_from_checkpoint(self):
                pass

            def deleteLater(self):
                pass

        class _FakeThreadStopsInTime:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return True

            def deleteLater(self):
                pass

        window._worker = _FakeWorker()
        window._thread = _FakeThreadStopsInTime()

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )

        class _FakeEvent:
            def __init__(self):
                self.ignored = False
                self.accepted = False

            def ignore(self):
                self.ignored = True

            def accept(self):
                self.accepted = True

        event = _FakeEvent()
        window.closeEvent(event)

        assert event.accepted is True
        assert event.ignored is False


class TestReview3rdRoundDmmPsuConfigMovedToConnectWorker:
    """Teljes kódbázis review 3. kör (2026-08-29), GUI/UX P0 lelet: a DMM
    mérési mód + PSU kombinációs mód beállítás/visszaigazolás korábban
    _build_runner()-ben, a GUI-szálon, SZINKRON futott — közvetlenül azután,
    hogy _on_connect_ready() letiltotta az Emergency Stop gombot. Ezek
    blokkoló SCPI write/query hívások, most _construct_and_connect_
    instruments()-be lettek kiszervezve (ami már a connect-worker szálon fut)."""

    def test_construct_and_connect_instruments_configures_dmm_and_psu(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.gui.main_window import _construct_and_connect_instruments
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        cfg = _make_cfg(psu_mode="INDEPENDENT")

        result = _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        # A _patch_hardware() set_mode_independent() mockja _test_combination_
        # mode="OFF"-ot állít be — ha ez megtörtént, a DMM/PSU konfiguráció
        # ténylegesen lefutott ebben a függvényben (nem csak connect/identify).
        assert result.psu._test_combination_mode == "OFF"

    def test_construct_and_connect_instruments_rolls_back_on_combination_mode_mismatch(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió] a PSU kombinációs mód visszaigazolás sikertelensége
        (HW-003, 1. kör P1 lelet) most _construct_and_connect_instruments()
        SAJÁT try/except-jében kap rollbackot (disconnect_all()) — korábban
        ez csak a _build_runner()-t hívó _on_connect_ready() rétegben történt
        meg, mert a konfiguráció maga is csak ott futott."""
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import ConfigurationError
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        # A set_mode_independent() mock rendben beállítja "OFF"-ot, de a
        # query_combination_mode()-ot felülírjuk, hogy eltérést szimuláljon.
        monkeypatch.setattr(Keithley2220PSU, "query_combination_mode", lambda self: "SER")
        call_order: list = []
        monkeypatch.setattr(
            InstrumentManager, "disconnect_all",
            lambda self: call_order.append("disconnect_all"),
        )
        # Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-04 lelet:
        # disconnect_all() önmagában nem ad ki OUTPUT/INPUT OFF parancsot --
        # config-hiba esetén is safe_all_off()-nak KELL futnia, ÉS a
        # disconnect_all() ELŐTT (CLAUDE.md §6: connect/config-hiba -> Load
        # OFF + PSU ALL OFF).
        monkeypatch.setattr(
            InstrumentManager, "safe_all_off",
            lambda self: call_order.append("safe_all_off"),
        )
        cfg = _make_cfg(psu_mode="INDEPENDENT")

        with pytest.raises(ConfigurationError, match="HW-003"):
            _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        assert call_order == ["safe_all_off", "disconnect_all"]

    def test_negotiate_timeout_closes_bq_serial_before_propagating(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 13. kör (2026-08-31), Hardver/
        Műszervezérlés P1 lelet] negotiate() a testvér PicoBQTransportError/
        InstrumentTimeoutError kivételek BÁRMELYIKÉT dobhatja (soros I/O
        timeout esetén az utóbbit) — a korábbi szűk `except
        PicoBQTransportError` ezt itt NEM kapta el, emiatt a lenti
        bq_serial.close() kimaradt (a soros port fájlleíró szivárgott), és
        session_bq_serial sem lett beállítva, mire a külső except Exception
        ágig felfutott a kivétel."""
        import serial  # type: ignore[import-untyped]

        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import InstrumentTimeoutError
        from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        fake = FakePicoSerial(simulate_no_response=True)
        close_calls: list = []
        monkeypatch.setattr(
            FakePicoSerial, "close", lambda self: close_calls.append(True), raising=False
        )
        monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)
        cfg = _make_cfg(psu_mode="INDEPENDENT", test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        with pytest.raises(InstrumentTimeoutError):
            _construct_and_connect_instruments(cfg, needs_bq_transport=True)

        assert close_calls == [True]

    def test_construct_and_connect_instruments_uses_transport_connect_not_bypass(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, 2026-09-15, C-02/DFAM-040+DFAM-001] Korábban
        `PicoBQTransport(serial=bq_serial)` (a dataclass mezőt közvetlenül
        átadva) MEGKERÜLTE a transport SAJÁT `connect()`-jét -- emiatt
        `self._port` SOHA nem állt be, és a HIL-D5 reconnect-fix
        (`_reopen_serial()`, CLAIM_LEDGER.yaml PICO-RECONNECT-001) a
        leghosszabb (session-owned TRADITIONAL_LEARNING/GOLDEN_GG)
        produkciós úton NÉMA NO-OP volt: egy soros I/O-hiba a fizikai
        ciklus közben SOHA nem állt volna helyre. Ez a teszt azt igazolja,
        hogy a visszaadott bq_transport._port a ténylegesen kért portra
        van állítva -- a régi kódon ez None maradt volna."""
        import serial  # type: ignore[import-untyped]

        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        fake = FakePicoSerial()
        monkeypatch.setattr(FakePicoSerial, "close", lambda self: None, raising=False)
        monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)
        cfg = _make_cfg(psu_mode="INDEPENDENT", test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        result = _construct_and_connect_instruments(cfg, needs_bq_transport=True)

        assert result.bq_transport._port == "COM8"
        assert result.bq_transport.serial is fake
        assert result.bq_transport.is_connected() is True

    def test_construct_and_connect_instruments_rejects_firmware_missing_write_methods(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kör 21 F1/ARC21-3(b)] TRADITIONAL_LEARNING/GOLDEN_GG
        a session során unseal()/full_access()/seal()/it_enable()/
        write_data_flash_block()/restore_df_snapshot()-ot használ -- ha a
        csatlakoztatott firmware ezeket NEM exponálja (pl. régi, WP-AK-06-
        kori vagy hiányos build, de UGYANAZZAL a protocol_version-nel), a
        bring-upnak MÁR itt kell buknia, MIELŐTT a session elindulna --
        NEM az első valódi write-nál, energizált/bekötött akku mellett."""
        import serial  # type: ignore[import-untyped]

        from Prog.drivers.pico_bq_transport import PicoBQTransportError
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        fake = FakePicoSerial(
            results={
                "capabilities": {
                    "protocol_version": "1.0",
                    "methods": ["capabilities", "voltage_mV"],
                    "read_only": True,
                    "write_methods": [],
                }
            }
        )
        close_calls: list = []
        monkeypatch.setattr(
            FakePicoSerial, "close", lambda self: close_calls.append(True), raising=False
        )
        monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)
        cfg = _make_cfg(psu_mode="INDEPENDENT", test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        with pytest.raises(PicoBQTransportError, match="unseal"):
            _construct_and_connect_instruments(cfg, needs_bq_transport=True)

        # A hivatkozott safe-close ág (l. a testvér timeout-teszt fent)
        # ugyanúgy lefut ezen az úton is -- a soros port fájlleíró nem
        # szivárog egy negotiate()-szintű elutasításnál sem.
        assert close_calls == [True]

    def test_unknown_temperature_sensor_mode_rolls_back(self, qapp, monkeypatch, tmp_path):
        from Prog.gui.main_window import _construct_and_connect_instruments
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        disconnect_calls: list = []
        monkeypatch.setattr(
            InstrumentManager, "disconnect_all",
            lambda self: disconnect_calls.append(True),
        )
        cfg = _make_cfg(psu_mode="INDEPENDENT")
        cfg.temperature_sensor_mode = "UNKNOWN_MODE"

        with pytest.raises(ValueError, match="Ismeretlen temperature_sensor_mode"):
            _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        assert disconnect_calls == [True]

    def test_connect_ready_does_not_block_gui_thread_with_config(self, qapp, monkeypatch, tmp_path):
        """A DMM/PSU konfiguráció (itt: query_combination_mode() lelassítva)
        a connect-worker szálon fut — a Start gombnak a teljes konfiguráció
        alatt is tiltva kell maradnia, majd a connect_ready jel feldolgozása
        UTÁN kell visszakapnia az engedélyezett állapotát, BÁRMILYEN
        _build_runner()-beli hiba nélkül is (a konfiguráció már megtörtént)."""
        import time as _time

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)

        def _slow_query_combination_mode(self):
            _time.sleep(0.2)
            return getattr(self, "_test_combination_mode", "OFF")

        monkeypatch.setattr(
            Keithley2220PSU, "query_combination_mode", _slow_query_combination_mode
        )
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        window = MainWindow()
        cp = window._config_panel
        cp._model_edit.setText("TEST7AH")
        cp._capacity_spin.setValue(1.0)
        cp._psu_res_edit.setText("MOCK_PSU")
        cp._load_res_edit.setText("MOCK_LOAD")
        cp._dmm_v_res_edit.setText("MOCK_DMM_V")
        cp._dmm_t_res_edit.setText("MOCK_DMM_T")
        cp._psu_mode_combo.setCurrentText("INDEPENDENT")

        build_calls: list = []
        window._build_runner = lambda cfg, pre_connected=None: build_calls.append(
            pre_connected
        ) or (_ for _ in ()).throw(
            RuntimeError("stop before real TestRunnerWorker/QThread in test")
        )

        window._start_test()
        assert window._live_panel._start_btn.isEnabled() is False
        assert build_calls == []  # a lassú query_combination_mode() miatt még nem futott le

        deadline = _time.monotonic() + 5.0
        while window._connect_worker is not None and _time.monotonic() < deadline:
            qapp.processEvents()
            _time.sleep(0.01)

        # A connect_ready UTÁN _build_runner() egyszer, a MÁR konfigurált
        # pre_connected objektummal hívódott.
        assert len(build_calls) == 1
        assert build_calls[0] is not None


class TestR3B2LoadModelIdentityGate:
    """[regresszio, R3-B2, R3-HW-01, DFAM-003 (rokon DFAM-041)]
    A bring-up identify_all() Load-mintaja korabban a "2380" csaladnev volt,
    igy egy 2380-500-15 (200 W, 3/15 A; 2380-120-60-900-01 Rev. A Sec.4) is
    atjutott, mikozben a safety.py 250 W-ja es a device_load.py 6/60 A-es
    tartomanyai a 2380-120-60-ra vonatkoznak. A valodi identify_all() fut a
    produkcios expected_models mintaval; minden ON elott kell bukni, a
    meglevo safe_all_off() -> disconnect_all() uton. A J-valtozat
    (2380J-120-60) SOURCE_REQUIRED, ezert elutasitva marad."""

    _LAB_PSU_IDN = "Keithley instruments, 2220-30-1, 9204604, 1.15-1.05"
    _LAB_LOAD_IDN = "Keithley, 2380-120-60, 802436052777870003, 1.00-1.00"

    def _arrange(self, monkeypatch, tmp_path, load_idn):
        from Prog.drivers.device_load import Keithley2380Load
        real_identify_all = InstrumentManager.identify_all
        _patch_hardware(monkeypatch)
        # A _patch_hardware() mockolja az identify_all()-t -- itt a VALODI
        # kell, kulonben a produkcios minta nem ervenyesul.
        monkeypatch.setattr(InstrumentManager, "identify_all", real_identify_all)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        psu_idn = self._LAB_PSU_IDN
        monkeypatch.setattr(Keithley2220PSU, "idn", lambda _self: psu_idn)
        monkeypatch.setattr(Keithley2380Load, "idn", lambda self: load_idn)
        # Ket kulonbozo DMM-IDN, kulonben a HW5-05 duplikatum-ellenorzes dob.
        serials: dict[int, int] = {}
        monkeypatch.setattr(
            Keysight34465ADMM, "idn",
            lambda self: "Keysight Technologies,34465A,"
            f"MY{serials.setdefault(id(self), len(serials) + 1):08d},A.03.03",
        )
        calls: list[str] = []
        monkeypatch.setattr(
            Keithley2220PSU, "output_on", lambda self: calls.append("psu.output_on")
        )
        monkeypatch.setattr(
            Keithley2380Load, "input_on",
            lambda self, clear_first=True: calls.append("load.input_on"),
        )
        monkeypatch.setattr(
            InstrumentManager, "safe_all_off", lambda self: calls.append("safe_all_off")
        )
        monkeypatch.setattr(
            InstrumentManager, "disconnect_all", lambda self: calls.append("disconnect_all")
        )
        return calls

    @pytest.mark.parametrize("load_idn", [
        pytest.param("Keithley, 2380-500-15, 802436052777870003, 1.00-1.00", id="500-15"),
        pytest.param("Keithley, 2380J-120-60, 802436052777870003, 1.00-1.00", id="J-120-60"),
    ])
    def test_wrong_2380_variant_rejected_before_any_on(
        self, qapp, monkeypatch, tmp_path, load_idn
    ):
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import ConfigurationError
        calls = self._arrange(monkeypatch, tmp_path, load_idn)

        with pytest.raises(ConfigurationError, match="Load") as ei:
            _construct_and_connect_instruments(
                _make_cfg(psu_mode="INDEPENDENT"), needs_bq_transport=False
            )

        assert "'2380-120-60'" in str(ei.value)
        # Nincs ON-parancs; a meglevo cleanup sorrendje valtozatlan.
        assert calls == ["safe_all_off", "disconnect_all"]
        assert ei.value.safe_off_attempted is True
        assert ei.value.safe_off_identity_unconfirmed is True

    def test_lab_2380_120_60_accepted(self, qapp, monkeypatch, tmp_path):
        # Pozitiv kontroll: a labor valos IDN-je (nyers log,
        # hil_iec_dc_resistance_method_only_20260912_raw_output.txt) atjut.
        from Prog.gui.main_window import _construct_and_connect_instruments
        calls = self._arrange(monkeypatch, tmp_path, self._LAB_LOAD_IDN)

        result = _construct_and_connect_instruments(
            _make_cfg(psu_mode="INDEPENDENT"), needs_bq_transport=False
        )

        assert result.instrument_identities["Load"] == self._LAB_LOAD_IDN
        assert calls == []


class TestR18HwP21IdentityUnconfirmedSafeOffSignal:
    """[regresszió, Kor 18, Batch C11, HW-P2-1, DFAM-018+DFAM-016]
    connect_all() allapot-mutato SCPI-t kuld MINDEN muszernek MIELOTT
    identify_all() igazolna a szerep->fizikai eszkoz lekepezest -- ha
    ez utobbi hibat jelez (pl. felcserelt VISA resource-stringek), a
    korabbi except-ag safe_all_off() eredmenyet csendben elhagyta,
    semmit nem allitva a kimeneti allapotrol. Most a kivetel hordozza
    a strukturalt flageket, es _InstrumentConnectWorker.run() ezekbol
    allitja ossze az operatori figyelmeztetest."""

    def test_identity_failure_marks_safe_off_identity_unconfirmed(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import ConfigurationError
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(
            InstrumentManager, "identify_all",
            lambda self, expected_models=None: (_ for _ in ()).throw(
                ConfigurationError(
                    "Műszer-azonosítás sikertelen: felcserélt VISA "
                    "resource-string gyanús."
                )
            ),
        )
        cfg = _make_cfg(psu_mode="INDEPENDENT")

        with pytest.raises(ConfigurationError) as excinfo:
            _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        assert getattr(excinfo.value, "safe_off_attempted", False) is True
        assert getattr(excinfo.value, "safe_off_identity_unconfirmed", False) is True

    def test_post_identity_failure_is_not_marked_identity_unconfirmed(
        self, qapp, monkeypatch, tmp_path
    ):
        """A meglevo HW-003 (kombinacios mod visszaigazolas) hiba MAR
        identify_all() UTAN tortenik -- a szerep-lekepezes ekkor MAR
        igazolt, tehat a flag NEM lehet True."""
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import ConfigurationError
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(Keithley2220PSU, "query_combination_mode", lambda self: "SER")
        cfg = _make_cfg(psu_mode="INDEPENDENT")

        with pytest.raises(ConfigurationError, match="HW-003") as excinfo:
            _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        assert getattr(excinfo.value, "safe_off_identity_unconfirmed", None) is False
        assert isinstance(getattr(excinfo.value, "safe_off_result", None), dict)

    def test_connect_worker_message_warns_on_identity_unconfirmed(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.gui.main_window import _InstrumentConnectWorker
        from Prog.src.exceptions import ConfigurationError
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(
            InstrumentManager, "identify_all",
            lambda self, expected_models=None: (_ for _ in ()).throw(
                ConfigurationError(
                    "Műszer-azonosítás sikertelen: felcserélt VISA "
                    "resource-string gyanús."
                )
            ),
        )
        cfg = _make_cfg(psu_mode="INDEPENDENT")
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        messages: list = []
        worker.connect_failed.connect(messages.append)

        worker.run()

        assert len(messages) == 1
        assert "felcserélt" in messages[0]
        assert "SAFE-OFF ÉRTELMEZHETETLEN" in messages[0]
        assert "ENERGIZÁLT" in messages[0]

    def test_connect_worker_message_has_no_identity_warning_after_confirmed_identity(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.gui.main_window import _InstrumentConnectWorker
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(Keithley2220PSU, "query_combination_mode", lambda self: "SER")
        monkeypatch.setattr(
            InstrumentManager, "safe_all_off",
            lambda self: {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True},
        )
        cfg = _make_cfg(psu_mode="INDEPENDENT")
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        messages: list = []
        worker.connect_failed.connect(messages.append)

        worker.run()

        assert len(messages) == 1
        assert "HW-003" in messages[0]
        assert "ÉRTELMEZHETETLEN" not in messages[0]
        assert "NEM igazolt" not in messages[0]

    def test_pre_identify_negotiate_failure_also_marked_identity_unconfirmed(
        self, qapp, monkeypatch, tmp_path
    ):
        """A BQ negotiate()-idozites (identify_all() ELOTTI hiba) is
        identity_unconfirmed=True-t kell adjon -- az azonositas ekkor meg
        el sem indult. A kivetel TIPUSA valtozatlan marad (l. a meglevo
        test_negotiate_timeout_closes_bq_serial_before_propagating
        tesztet, amit ez a valtozas nem erint)."""
        import serial  # type: ignore[import-untyped]

        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import InstrumentTimeoutError
        from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        fake = FakePicoSerial(simulate_no_response=True)
        monkeypatch.setattr(FakePicoSerial, "close", lambda self: None, raising=False)
        monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)
        cfg = _make_cfg(psu_mode="INDEPENDENT", test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        with pytest.raises(InstrumentTimeoutError) as excinfo:
            _construct_and_connect_instruments(cfg, needs_bq_transport=True)

        assert getattr(excinfo.value, "safe_off_identity_unconfirmed", False) is True

    def test_connect_worker_message_warns_on_uncertain_safe_off_after_identity_confirmed(
        self, qapp, monkeypatch, tmp_path
    ):
        """DECISION-1 (a fix-planner ajanlasa szerint): ugyanaz a root
        cause (a hibaut nem tesz allitast a kimeneti allapotrol) a
        MASIK, igazolt-identitasu agon is -- KULON figyelmeztetes,
        NEM az "ERTELMEZHETETLEN" szoveggel osszemosva (DFAM-018)."""
        from Prog.gui.main_window import _InstrumentConnectWorker
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(Keithley2220PSU, "query_combination_mode", lambda self: "SER")
        monkeypatch.setattr(
            InstrumentManager, "safe_all_off",
            lambda self: {"Load": True, "PSU": False, "DMM_V": True, "DMM_T": True},
        )
        cfg = _make_cfg(psu_mode="INDEPENDENT")
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        messages: list = []
        worker.connect_failed.connect(messages.append)

        worker.run()

        assert len(messages) == 1
        assert "NEM igazolt" in messages[0]
        assert "PSU" in messages[0]
        assert "ÉRTELMEZHETETLEN" not in messages[0]

    def test_load_uncertain_alone_is_reported(self, qapp, monkeypatch, tmp_path):
        """Fuggetlen MAX verifier F3 lelete: a meglevo teszt csak a PSU-t
        hagyta bizonytalanul -- a Load (amit CLAUDE.md 6. szakasza ELOBB
        kapcsol ki) sajat, kulon diszkriminalo teszt nelkul maradt."""
        from Prog.gui.main_window import _InstrumentConnectWorker
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(Keithley2220PSU, "query_combination_mode", lambda self: "SER")
        monkeypatch.setattr(
            InstrumentManager, "safe_all_off",
            lambda self: {"Load": False, "PSU": True, "DMM_V": True, "DMM_T": True},
        )
        cfg = _make_cfg(psu_mode="INDEPENDENT")
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        messages: list = []
        worker.connect_failed.connect(messages.append)

        worker.run()

        assert len(messages) == 1
        assert "NEM igazolt" in messages[0]
        assert "Load" in messages[0]

    def test_connect_all_failure_still_warns_fail_closed(self, qapp, monkeypatch, tmp_path):
        """Fuggetlen MAX verifier F1 lelete (Kor 18, Batch C11): a
        connect_all() SAJAT hibaja (a legyakoribb bring-up hiba: musszer
        kikapcsolva/rossz VISA-cim) a `try:`-en KIVUL fut, tehat a
        kivetelre SEMMILYEN uj attributum nem kerul -- korabban ez CSENDES
        maradt (fail-open pontosan a leggyakoribb hibanal), holott a
        legrosszabb feltetelezest kellene jeleznie (fail-closed, l.
        _on_finished()/checkpoint_panel.py azonos mintaja)."""
        from Prog.gui.main_window import _InstrumentConnectWorker
        from Prog.src.exceptions import InstrumentError
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(
            InstrumentManager, "connect_all",
            lambda self, cfg: (_ for _ in ()).throw(
                InstrumentError("PSU: nem válaszol (VISA timeout)")
            ),
        )
        cfg = _make_cfg(psu_mode="INDEPENDENT")
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        messages: list = []
        worker.connect_failed.connect(messages.append)

        worker.run()

        assert len(messages) == 1
        assert "nem válaszol" in messages[0]
        assert "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" in messages[0]
        assert "ÉRTELMEZHETETLEN" not in messages[0]
        assert "NEM igazolt" not in messages[0]

    def test_worker_message_does_not_crash_when_safe_all_off_returns_none(
        self, qapp, monkeypatch, tmp_path
    ):
        """Fuggetlen MAX verifier F2 lelete: a `safe_off_result or {}`
        guard nelkul (a producer- ES a worker-oldalon is) egy `None`-t
        visszaado safe_all_off() az EXCEPT-agban maga dobna AttributeError
        -t, ami elnyelné a connect_failed emit-et -- a GUI felakadna egy
        felig-csatlakozott, szivargo szal mellett."""
        from Prog.gui.main_window import _InstrumentConnectWorker
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(Keithley2220PSU, "query_combination_mode", lambda self: "SER")
        monkeypatch.setattr(
            InstrumentManager, "safe_all_off",
            lambda self: None,
        )
        cfg = _make_cfg(psu_mode="INDEPENDENT")
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        messages: list = []
        worker.connect_failed.connect(messages.append)

        worker.run()  # nem szabad kivetelt dobnia

        assert len(messages) == 1
        assert "HW-003" in messages[0]
        assert "NEM igazolt" in messages[0]


class _FakeResult:
    def __init__(self):
        self.status = "DONE"
        self.reason = ""
        self.total_charge_ah = 5.0
        self.total_discharge_ah = 0.0
        # Teljes kódbázis review 17. kör (2026-09-05), Metrológia/
        # Adatintegritás P2 lelet: a TestResult mostantól total_charge_wh/
        # total_discharge_wh mezőket is hordoz — l. extreme_dt_s_count
        # lenti kommentje ugyanerről a stub-frissítési kötelezettségről.
        self.total_charge_wh = 0.0
        self.total_discharge_wh = 0.0
        self.active_step_label = "golden_gg"
        self.safe_off_attempted = True
        self.safe_off_result = {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}
        self.communication_faults_count = 0
        # Teljes kódbázis review 9. kör (2026-08-30), Metrológia/
        # Adatintegritás P2 lelet: a TestResult mostantól extreme_dt_s_count/
        # max_observed_dt_s mezőket is hordoz — a stub-nak követnie kell a
        # valódi dataclass alapértékeit, különben _write_report() AttributeError-t
        # dob egy kézzel írt _FakeResult-tal futtatott teszten (a valódi
        # TestResult-ot használó tesztek ettől függetlenül mindig helyesek voltak).
        self.extreme_dt_s_count = 0
        self.max_observed_dt_s = 0.0
        # Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
        # Adatintegritás P2 lelet: a TestResult mostantól relax_dmm_fault_
        # count mezőt is hordoz — l. extreme_dt_s_count fenti kommentje
        # ugyanerről a stub-frissítési kötelezettségről.
        self.relax_dmm_fault_count = 0
        # Teljes kódbázis review 21. kör (2026-09-07), Session/Workflow/
        # Állapotgép P2 lelet: a TestResult mostantól temperature_out_of_
        # range_count mezőt is hordoz — l. extreme_dt_s_count fenti
        # kommentje ugyanerről a stub-frissítési kötelezettségről.
        self.temperature_out_of_range_count = 0
        # Review-kör 3 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb) P2,
        # DFAM-001 4. előfordulása: a TestResult mostantól temp_dmm_fault_
        # ticks mezőt is hordoz -- l. temperature_out_of_range_count fenti
        # kommentje ugyanerről a stub-frissítési kötelezettségről.
        self.temp_dmm_fault_ticks = 0
        # R16-B5b (DFAM-001): a TestResult mostantól error_queue_deferred_s
        # mezőt is hordoz (DMM-foglaltság miatti hibasor-halasztás eszközönkénti
        # összideje) -- l. extreme_dt_s_count fenti kommentje ugyanerről a
        # stub-frissítési kötelezettségről.
        self.error_queue_deferred_s = {}
        self.relax_missed_deadline_count = 0
        # Governance-v2 review-kör 1 (CF-18/DFAM-022, batch 4.3): a
        # TestResult mostantól total_session_duration_s mezőt is hordoz —
        # l. extreme_dt_s_count fenti kommentje ugyanerről a
        # stub-frissítési kötelezettségről.
        self.total_session_duration_s = 0.0
        # Governance-v2 review-kör 1 (CF-20/DFAM-021, batch 4.6): a
        # TestResult mostantól warning_code_counts mezőt is hordoz — l.
        # extreme_dt_s_count fenti kommentje ugyanerről a stub-frissítési
        # kötelezettségről.
        self.warning_code_counts: dict = {}


class _FakeLearningResultSummary:
    def __init__(self, result: str, quality_flags: tuple[str, ...] = ()):
        self.result = result
        self.quality_flags = quality_flags


class _FakeTraditionalLearningCtrl:
    def __init__(
        self, result_summary, charge_config=None, learning_config=None,
        relax_dmm_fault_total: int = 0, temp_dmm_fault_ticks: int = 0,
    ):
        self.result_summary = result_summary
        self.charge_config = charge_config
        self.learning_config = learning_config
        # Teljes kódbázis review 12. kör (2026-08-31), Szoftver/Architektúra
        # P2 lelet: _write_report() közvetlen (nem getattr) attribútum-
        # hozzáféréssel olvassa (l. _FakeGoldenGgCtrl azonos mintája fent),
        # tehát a stub-nak is mindig tartalmaznia kell.
        self.relax_dmm_fault_total = relax_dmm_fault_total
        # Review-kör 3 (2026-09-09), friss Metrológia M3-02 / Szoftver/
        # Architektúra F2 (P2): a TraditionalLearningController.
        # temp_dmm_fault_ticks property-je, ugyanazzal a közvetlen-
        # attribútum-hozzáférési mintával, mint a golden_gg testvér.
        self.temp_dmm_fault_ticks = temp_dmm_fault_ticks


class _FakeGoldenGgCtrl:
    def __init__(
        self, package_ready: bool, export_dir, export_zip_path,
        relax_dmm_fault_total: int = 0, charge_config=None,
        temp_dmm_fault_ticks: int = 0, quality_flags: tuple = (),
    ):
        self.package_ready_for_ti_submission = package_ready
        self.export_dir = export_dir
        self.export_zip_path = export_zip_path
        # Teljes kódbázis review 10. kör (2026-08-31), Metrológia/
        # Adatintegritás P2 lelet: a GoldenGgController.relax_dmm_fault_total
        # property-je — _write_report() közvetlen (nem getattr) attribútum-
        # hozzáféréssel olvassa, tehát a stub-nak is mindig tartalmaznia kell.
        self.relax_dmm_fault_total = relax_dmm_fault_total
        # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás NEW-2:
        # a GoldenGgController.temp_dmm_fault_ticks property-je, ugyanazzal
        # a közvetlen-attribútum-hozzáférési mintával.
        self.temp_dmm_fault_ticks = temp_dmm_fault_ticks
        # Teljes kódbázis review 15. kör (2026-09-04), Szoftver/Architektúra
        # P2 lelet: a GoldenGgController.charge_config property-je — l. a
        # testvér _FakeTraditionalLearningCtrl azonos mezője.
        self.charge_config = charge_config
        # Teljes kódbázis review 2. kör (2026-09-15, R2-11a, 14. writer-kör):
        # a GoldenGgController.quality_flags property-je — _write_report()
        # ugyanazzal a közvetlen-attribútum-hozzáférési mintával olvassa,
        # mint a fenti mezőket, tehát a stub-nak is mindig tartalmaznia
        # kell (üres tuple alapértékkel, hogy a MEGLÉVŐ hívóhelyek
        # változatlanul működjenek).
        self.quality_flags = quality_flags


class TestReleaseAudit20260823WriteReportBqFields:
    """Release-audit 2026-08-23 (2.2): _write_report() a
    _last_traditional_learning_ctrl/_last_golden_gg_ctrl referenciákból
    olvassa ki a BQ learning eredmény-klasszifikációt és a GOLDEN_GG
    export-státuszt a report.json-ba."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        return window

    def test_write_report_includes_bq_learning_result(self, qapp, monkeypatch, tmp_path):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
            _FakeLearningResultSummary("BQ_LEARNING_VERIFIED", ("QEN_NOT_CONFIRMED",))
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["bq_learning_result"] == "BQ_LEARNING_VERIFIED"
        assert report["bq_learning_quality_flags"] == ["QEN_NOT_CONFIRMED"]

    def test_write_report_includes_profile_and_config_snapshots(
        self, qapp, monkeypatch, tmp_path
    ):
        """DATA-006 (2026-08-24, session_fg1_20260823_200337 elemzés):
        korábban a profil/config ÉRTÉKEI (nem csak egy visszafejthetetlen
        hash) sehol nem kerültek a report.json-ba — utólag nem volt
        rekonstruálható a session mappából, milyen kapacitás-paraméterek
        voltak ténylegesen aktívak."""
        import json

        from Prog.src.charge_controller import ChargeConfig
        from Prog.src.traditional_learning_controller import LearningConfig

        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_profile_snapshot = {"nominal_capacity_Ah": 6.8, "model": "FG20721"}
        window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
            _FakeLearningResultSummary("BQ_LEARNING_VERIFIED"),
            charge_config=ChargeConfig(max_charge_Ah_factor=1.5),
            learning_config=LearningConfig(rebound_tolerance_V=0.1),
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["profile_snapshot"] == {"nominal_capacity_Ah": 6.8, "model": "FG20721"}
        assert report["charge_config_snapshot"]["max_charge_Ah_factor"] == 1.5
        assert report["learning_config_snapshot"]["rebound_tolerance_V"] == 0.1

    def test_write_report_includes_golden_gg_charge_config_snapshot(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 15. kör (2026-09-04),
        Szoftver/Architektúra P2 lelet] a GoldenGgController-ről hiányzott
        a charge_config property — a testvér TraditionalLearningController
        DATA-006 fixe (fent) sosem terjedt ki GOLDEN_GG session-re, holott
        a GoldenGgController.FULL_CHARGE fázisa ugyanazt a session-szintű
        ChargeConfig-ot használja."""
        import json

        from Prog.src.charge_controller import ChargeConfig

        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_golden_gg_ctrl = _FakeGoldenGgCtrl(
            False, None, None, charge_config=ChargeConfig(max_charge_Ah_factor=1.5),
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["charge_config_snapshot"]["max_charge_Ah_factor"] == 1.5

    def test_write_report_includes_chemid_charge_config_snapshot(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 16. kör (2026-09-04),
        Session/Workflow P2 lelet] a ChemIdCharacterizationController-ről
        is hiányzott a charge_config property — a golden_gg/traditional_
        learning mintáját követve, a maradék két testvér (chemid/ocv_soc)
        egyike."""
        import json

        from Prog.src.charge_controller import ChargeConfig

        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_chemid_ctrl = _FakeChemIdCtrl(
            charge_config=ChargeConfig(max_charge_Ah_factor=1.5),
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["charge_config_snapshot"]["max_charge_Ah_factor"] == 1.5

    def test_write_report_includes_ocv_soc_charge_config_snapshot(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 16. kör (2026-09-04),
        Session/Workflow P2 lelet] az OcvSocController-ről is hiányzott a
        charge_config property — a maradék, negyedik testvér."""
        import json

        from Prog.src.charge_controller import ChargeConfig

        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_ocv_soc_ctrl = _FakeOcvSocCtrl(
            charge_config=ChargeConfig(max_charge_Ah_factor=1.5),
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["charge_config_snapshot"]["max_charge_Ah_factor"] == 1.5

    def test_write_report_includes_active_internal_phase(self, qapp, monkeypatch, tmp_path):
        """[CF-19, governance-v2 kör 2, DFAM-023] a report.json "active_
        phase" mezője mindig az opak top-level lépéscímke -- ez a teszt azt
        igazolja, hogy MELLETTE megjelenik a controller TÉNYLEGES belső
        állapota/az abban eltöltött idő is, ha a controller exponálja
        (state/phase_elapsed_s property, l. ocv_soc_controller.py)."""
        import json
        from enum import Enum

        class _FakeState(Enum):
            STEP_RELAX = "STEP_RELAX"

        window = self._make_window(qapp, monkeypatch, tmp_path)
        ctrl = _FakeOcvSocCtrl()
        ctrl.state = _FakeState.STEP_RELAX
        ctrl.phase_elapsed_s = 42.5
        window._last_ocv_soc_ctrl = ctrl

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["active_internal_phase"] == "STEP_RELAX"
        assert report["active_internal_phase_elapsed_s"] == pytest.approx(42.5)

    def test_write_report_includes_iec_dc_resistance_active_internal_phase(
        self, qapp, monkeypatch, tmp_path
    ):
        """Governance-v2 kör 2: az IecDcResistanceController is az
        active_internal_phase/_elapsed_s öt lehetséges forrásának egyike
        -- a fenti OcvSoc-tesztet tükrözi."""
        import json
        from enum import Enum

        class _FakeIecState(Enum):
            LOAD_1 = "LOAD_1"

        window = self._make_window(qapp, monkeypatch, tmp_path)
        ctrl = _FakeIecDcResistanceCtrl()
        ctrl.state = _FakeIecState.LOAD_1
        ctrl.phase_elapsed_s = 12.0
        window._last_iec_dc_resistance_ctrl = ctrl

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["active_internal_phase"] == "LOAD_1"
        assert report["active_internal_phase_elapsed_s"] == pytest.approx(12.0)

    def test_write_report_includes_iec_dc_resistance_result_fields(
        self, qapp, monkeypatch, tmp_path
    ):
        """Governance-v2 kör 2: a Ri_DC_IEC_ohm/Isc_A egyetlen, session-
        szintű eredmény bekerül a report.json tetejére is, nem csak a
        dedikált iec_dc_resistance_result.csv-be."""
        import json

        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_iec_dc_resistance_ctrl = _FakeIecDcResistanceCtrl(
            result={
                "Ri_ohm": 0.0504, "Isc_A": 254.4, "quality": "OK",
                "timing_degraded": True, "synchronization_quality": "SOFTWARE_TICK_TIMED",
                "t_sample_1_s": 20.1, "t_sample_2_s": 6.0,
                "temperature_C": 21.3, "temperature_valid": True,
                "temperature_2_C": 21.4, "temperature_2_valid": True,
            },
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["Ri_DC_IEC_ohm"] == pytest.approx(0.0504)
        assert report["Isc_A"] == pytest.approx(254.4)
        # [regresszió, Kör 17 MAX-effort review, R17-12, DFAM-021] korábban
        # a quality/timing_degraded/synchronization_quality/t_sample_*_s
        # mezők KIZÁRÓLAG az iec_dc_resistance_result.csv-be jutottak el,
        # a fő report.json-ba nem -- most ott is elérhetők.
        assert report["Ri_DC_IEC_quality"] == "OK"
        assert report["Ri_DC_IEC_timing_degraded"] is True
        assert report["Ri_DC_IEC_synchronization_quality"] == "SOFTWARE_TICK_TIMED"
        assert report["Ri_DC_IEC_t_sample_1_s"] == pytest.approx(20.1)
        assert report["Ri_DC_IEC_t_sample_2_s"] == pytest.approx(6.0)
        # [regresszió, Kör 21 F13, METRO-R21-3, DFAM-021] korábban a 4
        # mérési hőmérséklet-mező (Ua/Ia pont + Ub/Ib pont, validity-
        # flagekkel) KIZÁRÓLAG az iec_dc_resistance_result.csv-be jutott
        # el -- a DC belső ellenállás erősen hőmérséklet-függő, a fő
        # report.json-nak is tartalmaznia kell.
        assert report["Ri_DC_IEC_temperature_C"] == pytest.approx(21.3)
        assert report["Ri_DC_IEC_temperature_valid"] is True
        assert report["Ri_DC_IEC_temperature_2_C"] == pytest.approx(21.4)
        assert report["Ri_DC_IEC_temperature_2_valid"] is True

    def test_write_report_iec_dc_resistance_result_fields_none_without_result(
        self, qapp, monkeypatch, tmp_path
    ):
        """Ha nem IEC_DC_INTERNAL_RESISTANCE session (vagy még nincs kész
        eredmény), a mezők None maradnak, nem hibáznak."""
        import json

        window = self._make_window(qapp, monkeypatch, tmp_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["Ri_DC_IEC_ohm"] is None
        assert report["Isc_A"] is None
        assert report["Ri_DC_IEC_quality"] is None
        assert report["Ri_DC_IEC_timing_degraded"] is None
        assert report["Ri_DC_IEC_synchronization_quality"] is None
        assert report["Ri_DC_IEC_t_sample_1_s"] is None
        assert report["Ri_DC_IEC_t_sample_2_s"] is None
        assert report["Ri_DC_IEC_temperature_C"] is None
        assert report["Ri_DC_IEC_temperature_valid"] is None
        assert report["Ri_DC_IEC_temperature_2_C"] is None
        assert report["Ri_DC_IEC_temperature_2_valid"] is None

    def test_write_report_active_internal_phase_none_without_composite_ctrl(
        self, qapp, monkeypatch, tmp_path
    ):
        """Ha egyik összetett controller sem futott (pl. CHARGE_ONLY/
        DISCHARGE_ONLY session), mindkét mező None marad, nem hibázik."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["active_internal_phase"] is None
        assert report["active_internal_phase_elapsed_s"] is None

    def test_write_report_does_not_crash_without_charge_or_learning_config(
        self, qapp, monkeypatch, tmp_path
    ):
        """A tlc test-double/jövőbeli variáns nem feltétlenül exponálja a
        charge_config/learning_config property-t — getattr védelemmel a
        _write_report() ekkor se dobjon kivételt (l. main_window.py)."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
            _FakeLearningResultSummary("BQ_LEARNING_VERIFIED")
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["charge_config_snapshot"] is None
        assert report["learning_config_snapshot"] is None

    def test_write_report_includes_provisioning_consistency_and_full_result_summary(
        self, qapp, monkeypatch, tmp_path
    ):
        """Block 6 (2026-08-25): a Block 2 konzisztencia-gate riportja és a
        Block 3 Cell BL restore evidence-et is hordozó teljes
        LearningResultSummary bekerül a report.json-ba, valódi
        dataclass-példányokból (nem a test-double _FakeLearningResultSummary-ból,
        ami csak .result/.quality_flags-et exponál)."""
        import json

        from Prog.src.bq_consistency_gate import (
            DfFieldComparison,
            ProvisioningConsistencyReport,
            ProvisioningGateDecision,
        )
        from Prog.src.traditional_learning_controller import (
            LEARNING_RESULT_VERIFIED,
            LearningResultSummary,
        )

        window = self._make_window(qapp, monkeypatch, tmp_path)
        gate_report = ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.CONSISTENT,
            reason="minden vizsgált DF-mező a profil-elvárt tolerancián belül",
            comparisons=(
                DfFieldComparison(
                    field_name="design_capacity_mAh", profile_expected=7000,
                    bq_readback=7000, unit="mAh", tolerance=50, within_tolerance=True,
                ),
            ),
        )
        result_summary = LearningResultSummary(
            result=LEARNING_RESULT_VERIFIED,
            cell_bl_original_mV=1950, cell_bl_restore_confirmed=True,
        )
        tlc = _FakeTraditionalLearningCtrl(result_summary)
        tlc.provisioning_consistency_report = gate_report
        window._last_traditional_learning_ctrl = tlc

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["provisioning_consistency"]["decision"] == "CONSISTENT"
        assert report["provisioning_consistency"]["comparisons"][0]["field_name"] == "design_capacity_mAh"
        assert report["learning_result_summary_full"]["result"] == LEARNING_RESULT_VERIFIED
        assert report["learning_result_summary_full"]["cell_bl_original_mV"] == 1950
        assert report["learning_result_summary_full"]["cell_bl_restore_confirmed"] is True

    def test_write_report_includes_golden_gg_export_status(self, qapp, monkeypatch, tmp_path):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        export_dir = str(tmp_path / "golden_gg_export")
        zip_path = str(tmp_path / "golden_gg_export" / "package.zip")
        window._last_golden_gg_ctrl = _FakeGoldenGgCtrl(True, export_dir, zip_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["golden_gg_package_ready_for_ti_submission"] is True
        assert report["golden_gg_export_dir"] == export_dir
        assert report["golden_gg_export_zip_path"] == zip_path

    def test_write_report_includes_chemid_export_status(self, qapp, monkeypatch, tmp_path):
        """[regresszió, D9/F-06, 2026-09-13, Riportok/ui_policy_audit_
        20260913.md] a sikeres GPCCHEM-export útvonala korábban sehol nem
        jelent meg a report.json-ban -- a GOLDEN_GG testvér (fent) már
        exponálta a sajátját, a CHEMID-é hiányzott."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        export_dir = str(tmp_path / "chemid_export")
        zip_path = str(tmp_path / "chemid_export" / "gpcchem_submission.zip")
        window._last_chemid_ctrl = _FakeChemIdCtrl(
            export_dir=export_dir, export_zip_path=zip_path,
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["chemid_export_dir"] == export_dir
        assert report["chemid_export_zip_path"] == zip_path

    def test_write_report_includes_chemid_relaxed_full_voltage(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kör 17 MAX-effort review, R17-24a, DFAM-021+
        DFAM-026] a RELAX_5H_FULL fázis végi feszültsége -- a teljes
        ChemID karakterizáció legjelentősebb OCV-adata -- korábban
        kiszámolódott, de sehova nem jutott el a report.json-ba."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_chemid_ctrl = _FakeChemIdCtrl(
            relaxed_full_voltage_V=13.42, relaxed_full_voltage_valid=True,
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["chemid_relaxed_full_voltage_V"] == pytest.approx(13.42)
        assert limitations["chemid_relaxed_full_voltage_valid"] is True

    def test_write_report_chemid_relaxed_full_voltage_none_without_chemid_session(
        self, qapp, monkeypatch, tmp_path
    ):
        """Nem-CHEMID session esetén (_last_chemid_ctrl None) a két mező
        None marad, nem hibázik."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["chemid_relaxed_full_voltage_V"] is None
        assert limitations["chemid_relaxed_full_voltage_valid"] is None

    def test_write_report_omits_bq_fields_for_non_bq_session(self, qapp, monkeypatch, tmp_path):
        """Nem-BQ session esetén (_last_*_ctrl referenciák None-ok) a mezők
        None-ok maradjanak, ne dobjon kivételt."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["bq_learning_result"] is None
        assert report["golden_gg_package_ready_for_ti_submission"] is None

    def test_write_report_includes_total_charge_and_discharge_wh(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 17. kör (2026-09-05),
        Metrológia/Adatintegritás P2 lelet] a TestResult mostantól
        total_charge_wh/total_discharge_wh mezőket is hordoz — a
        report.json-nak ezeket is tartalmaznia kell, az Ah-testvérpár
        mintáját követve."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        result = _FakeResult()
        result.total_charge_wh = 12.5
        result.total_discharge_wh = 8.25

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["total_charge_wh"] == pytest.approx(12.5)
        assert report["total_discharge_wh"] == pytest.approx(8.25)

    def test_write_report_includes_total_session_duration_s(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, CF-18/DFAM-022 (batch 4.3)] a TestResult mostantól
        total_session_duration_s mezőt is hordoz — a report.json-nak ezt is
        tartalmaznia kell, az Ah/Wh-testvérpár mintáját követve."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        result = _FakeResult()
        result.total_session_duration_s = 12345.6

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["total_session_duration_s"] == pytest.approx(12345.6)

    def test_write_report_includes_warning_code_counts(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Governance-v2 review-kör 1 (CF-20/DFAM-021, batch
        4.6)] a TestResult mostantól warning_code_counts mezőt is hordoz —
        a report.json MEASUREMENT_LIMITATIONS szekciójának ezt is
        tartalmaznia kell, a temperature_out_of_range_count mintáját
        követve."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        result = _FakeResult()
        result.warning_code_counts = {"TEMPERATURE_OUT_OF_RANGE": 7, "HEADROOM_APPROACHING": 2}

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["warning_code_counts"] == {
            "TEMPERATURE_OUT_OF_RANGE": 7, "HEADROOM_APPROACHING": 2,
        }

    def test_standalone_relax_temperature_monitor_lost_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, MAX verifier (review-kör 2, friss Szoftver/
        Architektúra F2, DFAM-018)] egy önálló (nem beágyazott) StepKind.
        RELAX alatti tartós hőmérséklet-DMM-kiesés a RelaxController-ben
        TUDATOSAN csak warning-ra degradálódik (nincs aktív energiaátadás
        relax alatt) -- ez a downgrade helyes, de korábban SEHOL nem
        jutott el a capacity_result_quality-ig, mert az csak a (voltage-
        DMM-specifikus) relax_dmm_fault_total-t nézte: egy DONE, 0
        kommunikációs hibával/extrém dt_s-sel záró session is "OK"-ot
        kapott, HOLOTT órákig nem volt hőmérséklet-felügyelet."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        result = _FakeResult()
        result.warning_code_counts = {"TEMPERATURE_MONITOR_LOST_CRITICAL": 12}

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"


class _FakeDischargeCtrl:
    def __init__(self, rb_timing_degraded: bool):
        self.rb_timing_degraded = rb_timing_degraded


class _FakeChemIdCtrl:
    def __init__(
        self, relax_dmm_fault_total: int = 0, charge_config=None,
        temp_dmm_fault_ticks: int = 0, export_dir=None, export_zip_path=None,
        relaxed_full_voltage_V=None, relaxed_full_voltage_valid: bool = False,
    ):
        self.relax_dmm_fault_total = relax_dmm_fault_total
        # Teljes kódbázis review 16. kör (2026-09-04), Session/Workflow P2
        # lelet: a ChemIdCharacterizationController.charge_config property-je
        # — l. a testvér _FakeGoldenGgCtrl/_FakeTraditionalLearningCtrl
        # azonos mezője.
        self.charge_config = charge_config
        # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás NEW-2:
        # l. _FakeGoldenGgCtrl.temp_dmm_fault_ticks azonos kommentje.
        self.temp_dmm_fault_ticks = temp_dmm_fault_ticks
        # D9/F-06 (2026-09-13): a ChemIdCharacterizationController.export_
        # dir/export_zip_path property-je — _write_report() közvetlen
        # (nem getattr) attribútum-hozzáféréssel olvassa, l. a testvér
        # _FakeGoldenGgCtrl azonos mezője.
        self.export_dir = export_dir
        self.export_zip_path = export_zip_path
        # Kör 17 MAX-effort review, R17-24a: a relaxed_full_voltage_V/
        # relaxed_full_voltage_valid property — l. chemid_controller.py
        # azonos hivatkozású docstringje.
        self.relaxed_full_voltage_V = relaxed_full_voltage_V
        self.relaxed_full_voltage_valid = relaxed_full_voltage_valid


class _FakeOcvSocCtrl:
    def __init__(
        self, relax_dmm_fault_total: int = 0, charge_config=None,
        temp_dmm_fault_ticks: int = 0, point_quality_counts=None,
    ):
        self.relax_dmm_fault_total = relax_dmm_fault_total
        # Teljes kódbázis review 16. kör (2026-09-04), Session/Workflow P2
        # lelet: az OcvSocController.charge_config property-je.
        self.charge_config = charge_config
        # Review-kör 3 (2026-09-09), friss Metrológia M3-02 / Szoftver/
        # Architektúra F2 (P2): az OcvSocController.temp_dmm_fault_ticks
        # property-je, ugyanazzal a közvetlen-attribútum-hozzáférési
        # mintával, mint a golden_gg testvér.
        self.temp_dmm_fault_ticks = temp_dmm_fault_ticks
        # Kor 18, Batch C12 (METRO-P2-1): az OcvSocController.
        # point_quality_counts property-je.
        self.point_quality_counts = point_quality_counts or {}


class _FakeIecDcResistanceCtrl:
    """Governance-v2 kör 2: az IecDcResistanceController öt testvér-
    controllerének (ggc/cic/tlc/osc/iec) minimális fake-je -- a
    _FakeOcvSocCtrl mintáját követi, plusz a saját `result` property
    (Ri_ohm/Isc_A a report.json-hoz, l. main_window.py::_write_report())."""

    def __init__(
        self, charge_config=None, result=None,
        relax_dmm_fault_total: int = 0, temp_dmm_fault_ticks: int = 0,
    ):
        self.charge_config = charge_config
        self.result = result
        # Review-kör 2 (2026-09-09), friss Szoftver/Architektúra F7 +
        # Metrológia/Adatintegritás NEW-2: _write_report() közvetlen (nem
        # getattr) attribútum-hozzáféréssel olvassa mindkettőt a
        # per-controller attribúciós blokkban (l. _FakeGoldenGgCtrl azonos
        # mintája) -- a stub-nak ezért mindig tartalmaznia kell.
        self.relax_dmm_fault_total = relax_dmm_fault_total
        self.temp_dmm_fault_ticks = temp_dmm_fault_ticks


class TestReleaseAudit20260823RbTimingDegradedInReport:
    """Release-audit 2026-08-23 (2.3): a DischargeController
    rb_timing_degraded jelzője a _discharge_ctrls_this_session listán
    keresztül jut el _write_report()-ba (session-szintű OR — több DISCHARGE
    lépés esetén egyik fázis degraded-státusza se veszhet el)."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        return window

    def test_discharge_ctrl_factory_appends_to_session_list(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="DISCHARGE_ONLY")

        assert window._discharge_ctrls_this_session == []
        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._discharge_ctrl_factory()

        assert window._discharge_ctrls_this_session == [ctrl]

    def test_default_discharge_current_uses_c10_capacity_basis(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Governance-v2 review-kör 1 (CF-16/MET-R1-04+16+18,
        DFAM-001 6. előfordulás + DFAM-017, batch 4.4)] amikor az operátor
        nem ad meg explicit discharge_current_A-t, az alapértelmezett
        kisütőáram korábban nyers `profile.nominal_capacity_Ah`-t osztott a
        rate-divisorral — a testvér ocv_soc_controller.py::
        _discharge_current_A (azonos minta) már a kanonikus
        `_c10_base_Ah`-t használja. A `BatteryProfile._c10_base_Ah`
        property-t class-szinten mockoljuk (a GUI-nak ma nincs mezője a
        C10_capacity_Ah beállítására, l. battery_profile.py docstringje),
        hogy diszkrimináló legyen a teszt a nominal_capacity_ah=7.2
        (SessionConfig alapérték) mellett is."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        from Prog.src.battery_profile import BatteryProfile
        monkeypatch.setattr(BatteryProfile, "_c10_base_Ah", property(lambda self: 4.0))
        window = MainWindow()
        cfg = _make_cfg(
            test_type="DISCHARGE_ONLY", discharge_current_A=0.0, discharge_rate_divisor=10,
        )

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._discharge_ctrl_factory()

        # 4.0/10=0.4, NEM nominal_capacity_ah(7.2)/10=0.72.
        assert ctrl._config.discharge_current_A == pytest.approx(0.4)

    def test_write_report_false_when_no_discharge_step_ran(self, qapp, monkeypatch, tmp_path):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["rb_timing_degraded"] is False

    def test_write_report_true_if_any_discharge_phase_degraded(self, qapp, monkeypatch, tmp_path):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._discharge_ctrls_this_session = [
            _FakeDischargeCtrl(rb_timing_degraded=False),
            _FakeDischargeCtrl(rb_timing_degraded=True),
        ]

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["rb_timing_degraded"] is True


class TestSession10GoldenGgChemidRelaxDmmFaultTotalInReport:
    """[regresszió, Teljes kódbázis review 10. kör (2026-08-31), Metrológia/
    Adatintegritás P2 lelet] a GoldenGgController/ChemIdCharacterizationController
    relax-fázisai alatti feszültség-DMM-olvasási hibák session-szintű összege
    korábban csak az export-zip belsejében jelent meg (csak sikeres exportnál)
    — a fő report.json-ban egyáltalán nem, FAULT-tal (export előtt) végződő
    session esetén nyomtalanul elveszett."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        return window

    def test_golden_gg_relax_dmm_fault_total_survives_fault_before_export(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_golden_gg_ctrl = _FakeGoldenGgCtrl(
            False, None, None, relax_dmm_fault_total=2,
        )
        result = _FakeResult()
        result.status = "FAULT"  # a session sosem érte el az EXPORT_PACKAGE-et

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["golden_gg_relax_dmm_fault_total"] == 2

    def test_chemid_relax_dmm_fault_total_survives_fault_before_export(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_chemid_ctrl = _FakeChemIdCtrl(relax_dmm_fault_total=3)
        result = _FakeResult()
        result.status = "FAULT"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["chemid_relax_dmm_fault_total"] == 3

    def test_no_golden_gg_or_chemid_ctrl_defaults_to_zero(self, qapp, monkeypatch, tmp_path):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["golden_gg_relax_dmm_fault_total"] == 0
        assert limitations["chemid_relax_dmm_fault_total"] == 0

    def test_ocv_soc_relax_dmm_fault_total_survives_fault_before_done(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, Teljes kódbázis review 14. kör (2026-09-04),
        Szoftver/Architektúra + Session/Workflow/Állapotgép P2 lelet] a
        golden_gg/chemid/traditional_learning mintáját követve — az
        OcvSocController volt a negyedik testvér, amelyről ez a bekötés
        hiányzott."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_ocv_soc_ctrl = _FakeOcvSocCtrl(relax_dmm_fault_total=5)
        result = _FakeResult()
        result.status = "FAULT"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["ocv_soc_relax_dmm_fault_total"] == 5

    def test_no_ocv_soc_ctrl_defaults_to_zero(self, qapp, monkeypatch, tmp_path):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["ocv_soc_relax_dmm_fault_total"] == 0


class TestReviewRound2TempDmmFaultTicksInReport:
    """[regresszió, MAX verifier (review-kör 2, 45ea824 P2-2), DFAM-021/026]
    a chemid_temp_dmm_fault_ticks/golden_gg_temp_dmm_fault_ticks/
    iec_dc_resistance_temp_dmm_fault_ticks/iec_dc_resistance_relax_dmm_
    fault_total session_meta mezők (Metrológia NEW-2 + Architektúra F7)
    a 45ea824 commitban regressziós teszt NÉLKÜL kerültek be -- egy
    mutációs teszt (a négy mező nullára kényszerítése + a temp_dmm_fault_
    ticks_total DEGRADED-gate-tag kivétele) egyetlen meglévő tesztet sem
    buktatott meg. Ez az osztály a TestSession10GoldenGgChemidRelaxDmm
    FaultTotalInReport 1:1 mintáját követve pótolja a hiányzó lefedettséget."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        return window

    def test_golden_gg_temp_dmm_fault_ticks_survives_fault_before_export(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_golden_gg_ctrl = _FakeGoldenGgCtrl(
            False, None, None, temp_dmm_fault_ticks=4,
        )
        result = _FakeResult()
        result.status = "FAULT"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["golden_gg_temp_dmm_fault_ticks"] == 4

    def test_chemid_temp_dmm_fault_ticks_survives_fault_before_export(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_chemid_ctrl = _FakeChemIdCtrl(temp_dmm_fault_ticks=6)
        result = _FakeResult()
        result.status = "FAULT"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["chemid_temp_dmm_fault_ticks"] == 6

    def test_iec_dc_resistance_relax_and_temp_ticks_survive_fault(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_iec_dc_resistance_ctrl = _FakeIecDcResistanceCtrl(
            relax_dmm_fault_total=7, temp_dmm_fault_ticks=8,
        )
        result = _FakeResult()
        result.status = "FAULT"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["iec_dc_resistance_relax_dmm_fault_total"] == 7
        assert limitations["iec_dc_resistance_temp_dmm_fault_ticks"] == 8

    def test_no_ctrl_defaults_temp_dmm_fault_ticks_to_zero(self, qapp, monkeypatch, tmp_path):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["golden_gg_temp_dmm_fault_ticks"] == 0
        assert limitations["chemid_temp_dmm_fault_ticks"] == 0
        assert limitations["iec_dc_resistance_relax_dmm_fault_total"] == 0
        assert limitations["iec_dc_resistance_temp_dmm_fault_ticks"] == 0

    def test_temp_dmm_fault_ticks_degrades_an_otherwise_ok_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        """A capacity_result_quality DEGRADED-gate-jének temp_dmm_fault_
        ticks_total > 0 tagját fedi -- egy DONE, 0 kommunikációs hibával/
        extrém dt_s-sel/relax-DMM-hibával záró session is DEGRADED-et kell
        kapjon, ha a hőmérséklet-DMM a session alatt legalább egyszer
        hibázott."""
        import json

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        window._last_chemid_ctrl = _FakeChemIdCtrl(temp_dmm_fault_ticks=1)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"

    def test_golden_gg_temp_dmm_fault_ticks_alone_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, MAX verifier (review-kör 2, f1c49b7 P3-2)] a fenti
        DEGRADED-teszt csak a chemid tagot fedte -- a temp_dmm_fault_
        ticks_total összeg golden_gg/iec tagjának ISOLÁLT (nem chemid-en
        keresztüli) törlése egyetlen korábbi teszten sem bukott volna."""
        import json

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        window._last_golden_gg_ctrl = _FakeGoldenGgCtrl(
            False, None, None, temp_dmm_fault_ticks=1,
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"

    def test_iec_dc_resistance_temp_dmm_fault_ticks_alone_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, MAX verifier (review-kör 2, f1c49b7 P3-2)] l. a
        golden_gg-s testvér docstringje -- itt az iec tag izolált fedése."""
        import json

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        window._last_iec_dc_resistance_ctrl = _FakeIecDcResistanceCtrl(temp_dmm_fault_ticks=1)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"


class TestReviewRound3TempDmmFaultTicksSiblingGapInReport:
    """[regresszió, review-kör 3 (2026-09-09), friss Metrológia M3-02 /
    Szoftver/Architektúra F2 (P2)] a temp_dmm_fault_ticks_total összeg és
    a per-controller session_meta kulcsok korábban csak golden_gg/chemid/
    iec_dc_resistance-ot fedték -- a traditional_learning/ocv_soc
    testvéreken a property MÁR létezett a controlleren (review-kör 3
    korábbi lépése), de a main_window.py összegzés/attribúció és a
    report_generator.py MEASUREMENT_LIMITATIONS mezői nem vették figyelembe.
    A TestReviewRound2TempDmmFaultTicksInReport 1:1 mintáját követi."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        return window

    def test_traditional_learning_temp_dmm_fault_ticks_survives_fault_before_export(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
            result_summary=None, temp_dmm_fault_ticks=3,
        )
        result = _FakeResult()
        result.status = "FAULT"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["traditional_learning_temp_dmm_fault_ticks"] == 3

    def test_ocv_soc_temp_dmm_fault_ticks_survives_fault_before_export(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_ocv_soc_ctrl = _FakeOcvSocCtrl(temp_dmm_fault_ticks=5)
        result = _FakeResult()
        result.status = "FAULT"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["ocv_soc_temp_dmm_fault_ticks"] == 5

    def test_no_ctrl_defaults_new_temp_dmm_fault_ticks_fields_to_zero(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        limitations = report["MEASUREMENT_LIMITATIONS"]
        assert limitations["traditional_learning_temp_dmm_fault_ticks"] == 0
        assert limitations["ocv_soc_temp_dmm_fault_ticks"] == 0

    def test_traditional_learning_temp_dmm_fault_ticks_alone_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        """A temp_dmm_fault_ticks_total DEGRADED-gate-tagját izoláltan a
        traditional_learning ágon fedi -- a golden_gg/chemid/iec ágak
        korábbi izolált tesztjeinek mintáját követve."""
        import json

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
            result_summary=None, temp_dmm_fault_ticks=1,
        )

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"

    def test_ocv_soc_temp_dmm_fault_ticks_alone_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        """L. a traditional_learning-es testvér docstringje -- itt az
        ocv_soc ág izolált fedése."""
        import json

        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        window._last_ocv_soc_ctrl = _FakeOcvSocCtrl(temp_dmm_fault_ticks=1)

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"


class TestSession11CapacityResultQualityConsidersRelaxDmmFaultTotal:
    """[regresszió, Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
    Adatintegritás P2 lelet] capacity_result_quality korábban NEM vette
    figyelembe a golden_gg/chemid relax-fázisok alatti DMM-hibaszámlálót
    (relax_dmm_fault_total, 10. kör óta a session_meta-ban) — a
    session_meta dict LÉTREHOZÁSA UTÁN, egy KÉSŐBBI feltételes blokkban
    került felvételre, miután a capacity_result_quality string már
    véglegesítve volt. Egy GOLDEN_GG/ChemID DONE session, ami 0 SCPI-
    hibával és 0 extrém dt_s-sel, de relax-fázisbeli DMM-olvasási hibával
    zárult, "OK"-ot kapott, holott a MEASUREMENT_LIMITATIONS ugyanabban a
    riportban ellentmondva > 0-t mutatott."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        return window

    def test_golden_gg_relax_dmm_fault_total_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_golden_gg_ctrl = _FakeGoldenGgCtrl(
            False, None, None, relax_dmm_fault_total=1,
        )
        result = _FakeResult()
        result.status = "DONE"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"

    def test_chemid_relax_dmm_fault_total_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_chemid_ctrl = _FakeChemIdCtrl(relax_dmm_fault_total=1)
        result = _FakeResult()
        result.status = "DONE"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"

    def test_zero_relax_dmm_fault_total_keeps_done_session_ok(self, qapp, monkeypatch, tmp_path):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_golden_gg_ctrl = _FakeGoldenGgCtrl(
            False, None, None, relax_dmm_fault_total=0,
        )
        result = _FakeResult()
        result.status = "DONE"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "OK"

    def test_traditional_learning_relax_dmm_fault_total_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, Teljes kódbázis review 12. kör (2026-08-31), Szoftver/
        Architektúra P2 lelet] a TraditionalLearningController RELAX_1/
        RELAX_2H fázisai ugyanazt a beágyazott RelaxController-t használják,
        mint golden_gg/chemid — a relax_dmm_fault_total property és a
        _write_report() bekötése hiányzott róla."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
            None, relax_dmm_fault_total=1,
        )
        result = _FakeResult()
        result.status = "DONE"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"
        assert (
            report["MEASUREMENT_LIMITATIONS"]["traditional_learning_relax_dmm_fault_total"] == 1
        )

    def test_ocv_soc_relax_dmm_fault_total_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, Teljes kódbázis review 14. kör (2026-09-04),
        Szoftver/Architektúra + Session/Workflow/Állapotgép P2 lelet] a
        golden_gg/chemid/traditional_learning mintáját követve — az
        OcvSocController-nek eddig hiányzó relax_dmm_fault_total property
        és _write_report()-bekötés miatt egy relax alatti DMM-kieséssel
        záruló, egyébként DONE OCV_SOC session tévesen "OK"-ot kapott."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._last_ocv_soc_ctrl = _FakeOcvSocCtrl(relax_dmm_fault_total=1)
        result = _FakeResult()
        result.status = "DONE"

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"
        assert report["MEASUREMENT_LIMITATIONS"]["ocv_soc_relax_dmm_fault_total"] == 1

    def test_temperature_out_of_range_count_degrades_done_session(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, Teljes kódbázis review 21. kör (2026-09-07), Session/
        Workflow/Állapotgép P2 lelet] a safety.check_test_temperature_range()
        (20. kör) által jelzett figyelmeztetés-számláló korábban nem
        befolyásolta a capacity_result_quality minősítést — egy egyébként
        DONE session, ami közben legalább egyszer a profil hőmérséklet-
        tartományán kívülre esett, tévesen "OK"-ot kapott."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        result = _FakeResult()
        result.status = "DONE"
        result.temperature_out_of_range_count = 1

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"
        assert report["MEASUREMENT_LIMITATIONS"]["temperature_out_of_range_count"] == 1

    def test_zero_temperature_out_of_range_count_keeps_done_session_ok(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        result = _FakeResult()
        result.status = "DONE"
        result.temperature_out_of_range_count = 0

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "OK"

    def test_temp_dmm_fault_ticks_degrades_done_session_on_standalone_charge_discharge(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, MAX verifier (review-kör 3, a4d9e82debe0bdfcb) P2,
        DFAM-001 4. előfordulása] a temp_dmm_fault_ticks_total (main_window.py)
        korábban KIZÁRÓLAG az 5 összetett controllert (chemid/golden_gg/iec_
        dc_resistance/ocv_soc/traditional_learning) fedte -- egy önálló
        StepKind.CHARGE/DISCHARGE lépés (bare Charge/DischargeController,
        nincs _last_*_ctrl referencia) hőmérséklet-DMM-kiesését a DEGRADED-
        gate emiatt sosem látta, mert a result.temp_dmm_fault_ticks (a
        temperature_out_of_range_count mintáját követő, MINDEN StepKind-et
        lefedő test_runner.py-aggregáció) hiányzott a gate-ből."""
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        result = _FakeResult()
        result.status = "DONE"
        result.temp_dmm_fault_ticks = 1

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "DEGRADED"
        assert report["MEASUREMENT_LIMITATIONS"]["temp_dmm_fault_ticks"] == 1

    def test_zero_temp_dmm_fault_ticks_keeps_done_session_ok(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        result = _FakeResult()
        result.status = "DONE"
        result.temp_dmm_fault_ticks = 0

        window._write_report(result)

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["MEASUREMENT_LIMITATIONS"]["capacity_result_quality"] == "OK"


class TestReleaseAudit20260823InstrumentLifecycle:
    """Release-audit 2026-08-23 (2.10/2.11/2.13): instrument/BQ soros port
    lifecycle-kezelés a GUI-rétegben — korábban egyik sem záródott le
    explicit módon hiba-/bezárás-ágakon."""

    def _make_window_with_connected_instruments(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)  # beállítja window._instruments-t (mockolt connect)
        return window

    # -- 2.10: disconnect_all() terminális _on_finished()-nél ------------ #

    def test_terminal_fault_state_calls_disconnect_all(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestResult
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)
        disconnect_calls = []
        window._instruments.disconnect_all = lambda: disconnect_calls.append(True)

        window._on_finished(TestResult(status="FAULT", reason="X"))

        assert disconnect_calls == [True]

    def test_terminal_done_state_calls_disconnect_all(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)
        disconnect_calls = []
        window._instruments.disconnect_all = lambda: disconnect_calls.append(True)

        window._on_finished(TestResult(status="DONE", total_charge_ah=1.0))

        assert disconnect_calls == [True]

    def test_non_terminal_resumable_checkpoint_does_not_disconnect(
        self, qapp, monkeypatch, tmp_path
    ):
        """CHECKPOINT_STOPPED + resume_possible=True: a session folytatható,
        a műszer-kapcsolatoknak élniük kell."""
        from Prog.src.test_runner import TestResult
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)
        disconnect_calls = []
        window._instruments.disconnect_all = lambda: disconnect_calls.append(True)

        window._on_finished(TestResult(status="CHECKPOINT_STOPPED", resume_possible=True))

        assert disconnect_calls == []

    def test_resumable_checkpoint_does_not_overwrite_checkpoint_status_message(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 5. kör (2026-08-29), GUI/UX
        P2 lelet] _on_finished() korábban feltétel nélkül "Kész |
        Töltve.../Kisütve..."-t írt a státusz-sorba egy RESUMABLE
        (keep_thread=True) CHECKPOINT_STOPPED esetén is — felülírva az
        _on_checkpoint_reached() által korábban beállított, valós
        (folytatható) állapotot leíró üzenetet."""
        from Prog.src.test_runner import TestResult
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)
        window._status_bar.showMessage("BQ checkpoint elérve — végezd el a BQ műveletet.")

        window._on_finished(TestResult(status="CHECKPOINT_STOPPED", resume_possible=True))

        assert "checkpoint" in window._status_bar.currentMessage().lower()
        assert "kész" not in window._status_bar.currentMessage().lower()

    def test_resumable_checkpoint_status_message_survives_real_worker_signal_path(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), GUI/UX/
        Állapot P2 lelet #2] A fenti test_resumable_checkpoint_does_not_
        overwrite_checkpoint_status_message KÖZVETLENÜL hívja window.
        _on_finished()-et, megkerülve a valós jelfolyamot — vakfolt. Éles
        futásban worker.py::_do_run() ELŐBB status_changed.emit("CHECKPOINT_
        STOPPED")-et ad (korábban közvetlenül self._status_bar.showMessage-
        re kötve, a nyers szöveget kiírva), és csak UTÁNA finished.emit()-et
        — a round-5 fix csak az utóbbit védte ki, a köztes nyers szöveget
        nem. Ez a teszt a TELJES MainWindow._start_test() szignál-huzalozást
        használja (test_fault_via_real_worker_signal_path_writes_report
        mintáját követve), hogy ezt a vakfoltot lefedje."""
        from Prog.gui.worker import TestRunnerWorker
        from Prog.src.test_runner import TestResult

        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)

        class _CheckpointRunner:
            def __init__(self):
                self.on_sample = None
                self.on_event = None
                self.on_step_changed = None
                self.stop_requested = False
                self.emergency_stop_requested = False
                self.emergency_stop_reason = ""

            def run(self, plan, start_step_index: int = 0):
                if self.on_event is not None:
                    self.on_event({
                        "event_code": "MANUAL_BQ_CHECKPOINT_REACHED",
                        "resume_possible": True,
                        "next_step_index": 3,
                    })
                return TestResult(
                    status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
                    resume_possible=True, next_step_index=3,
                )

            def request_stop(self):
                pass

            def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP"):
                pass

        window._worker = TestRunnerWorker(_CheckpointRunner(), None)
        window._worker.status_changed.connect(window._update_status_bar_from_worker_status)
        window._worker.checkpoint_reached.connect(window._on_checkpoint_reached)
        window._worker.finished.connect(window._on_finished)
        window._worker.run()  # szinkron hívás — nincs valós QThread a tesztben

        message = window._status_bar.currentMessage().lower()
        assert "checkpoint" in message
        assert message != "checkpoint_stopped"

    def test_terminal_checkpoint_still_shows_done_status_message(
        self, qapp, monkeypatch, tmp_path
    ):
        """Egy TERMINÁLIS (nem-resumable) CHECKPOINT_STOPPED esetén a "Kész"
        üzenet továbbra is helyes — a session ténylegesen véget ért, nincs
        élő worker, ami később felülírná."""
        from Prog.src.test_runner import TestResult
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)

        window._on_finished(
            TestResult(status="CHECKPOINT_STOPPED", resume_possible=False, total_charge_ah=1.5)
        )

        assert "kész" in window._status_bar.currentMessage().lower()

    def test_stopped_shows_distinct_status_message_not_kesz(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 6. kör (2026-08-29), GUI/UX/
        Állapot P2 lelet #2] _on_finished() a STOPPED (kézi Stop gomb)
        esetén is feltétel nélkül "Kész | Töltve.../Kisütve..."-t írt a
        státuszsorba, semmilyen megkülönböztető jelzés nélkül — DONE-nál
        van "Teszt befejezve" popup, FAULT-nál kritikus dialógus,
        STOPPED-nél viszont SEMMI. A report.json helyesen DEGRADED-et ír,
        ez tisztán GUI-kommunikációs rés volt."""
        from Prog.src.test_runner import TestResult
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)

        window._on_finished(TestResult(
            status="STOPPED", reason="USER_STOP_REQUESTED",
            total_charge_ah=0.2, total_discharge_ah=0.0,
            safe_off_attempted=True,
        ))

        message = window._status_bar.currentMessage().lower()
        assert "kész" not in message
        assert "leállítva" in message

    def test_terminal_checkpoint_still_names_outstanding_bq_operation(
        self, qapp, monkeypatch, tmp_path
    ):
        """Kör 21 F11 (F21-G4, DFAM-018): korábban a TERMINÁLIS (nem-
        resumable) CHECKPOINT_STOPPED "Kész"-e TELJESEN felülírta az
        _on_checkpoint_reached() BQ-teendő-emlékeztetőjét ("végezd el a BQ
        műveletet") -- mivel `resume_possible` ma STRUKTURÁLISAN mindig
        False (l. archívum §0.5), ez a felülírás MINDEN BQ checkpointon
        megtörtént. A "Kész" MARADHAT (a session tényleg véget ért, round-5
        döntés, l. fenti test), de a státuszsornak MEG KELL neveznie a
        fennálló BQ-műveletet."""
        from Prog.src.test_runner import TestResult
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)

        window._on_finished(
            TestResult(status="CHECKPOINT_STOPPED", resume_possible=False, total_charge_ah=1.5)
        )

        message = window._status_bar.currentMessage().lower()
        assert "kész" in message
        assert "bq" in message
        assert "művelet" in message

    def test_resumable_checkpoint_never_reaches_the_kesz_overwrite_at_all(
        self, qapp, monkeypatch, tmp_path
    ):
        """Counter-teszt: resume_possible=True eseten `keep_thread` (fent)
        True lesz, tehat a TELJES "Kesz" blokk (a `not keep_thread` miatt)
        sosem fut -- a BQ-emlekezteto szoveg NEM ADHATO HOZZA egy resumable
        agra, mert oda a "Kesz" maga sem er el. Explicit markerrel
        igazolva (NEM a widget indulasi szovegere hagyatkozva -- AZ maga
        IS tartalmazna "Kesz"-t, l. main_window.py:841-842 -- egy ilyen
        asszercio nem diszkriminalna): `_on_finished()` ezen az agon
        egyaltalan nem ir a statuszsorba, a korabban explicit beallitott
        marker valtozatlan marad."""
        from Prog.src.test_runner import TestResult
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)
        window._status_bar.showMessage("MARKER_BEFORE_ON_FINISHED")

        window._on_finished(
            TestResult(status="CHECKPOINT_STOPPED", resume_possible=True, total_charge_ah=1.5)
        )

        assert window._status_bar.currentMessage() == "MARKER_BEFORE_ON_FINISHED", (
            "resume_possible=True eseten a Kesz-blokk nem futhat -- ha ez "
            "megvaltozik, az a keep_thread szamitas vagy a guard sorrendjenek "
            "regressziojat jelezheti"
        )

    def test_done_status_message_unaffected_by_checkpoint_wording(
        self, qapp, monkeypatch, tmp_path
    ):
        """Counter-teszt: a fix NEM adhat BQ-szöveget egy valódi DONE-hoz
        -- a kiegészítés kizárólag CHECKPOINT_STOPPED-re vonatkozik."""
        from Prog.src.test_runner import TestResult
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)

        window._on_finished(TestResult(status="DONE", total_charge_ah=1.0, total_discharge_ah=1.0))

        message = window._status_bar.currentMessage().lower()
        assert "kész" in message
        assert "bq" not in message

    # -- 2.11: identify_all() hibánál rollback ---------------------------- #

    def test_identify_all_failure_triggers_disconnect_all(self, qapp, monkeypatch):
        from Prog.gui.main_window import _construct_and_connect_instruments
        _patch_hardware(monkeypatch)
        disconnect_calls = []
        monkeypatch.setattr(
            InstrumentManager, "identify_all",
            lambda self, expected_models=None: (_ for _ in ()).throw(RuntimeError("IDN timeout")),
        )
        monkeypatch.setattr(
            InstrumentManager, "disconnect_all",
            lambda self: disconnect_calls.append(True),
        )
        cfg = _make_cfg()

        with pytest.raises(RuntimeError, match="IDN timeout"):
            _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        assert disconnect_calls == [True]

    def test_identify_all_success_does_not_disconnect(self, qapp, monkeypatch):
        from Prog.gui.main_window import _construct_and_connect_instruments
        _patch_hardware(monkeypatch)
        disconnect_calls = []
        monkeypatch.setattr(
            InstrumentManager, "disconnect_all",
            lambda self: disconnect_calls.append(True),
        )
        cfg = _make_cfg()

        result = _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        assert result.instrument_identities is not None
        assert disconnect_calls == []

    def test_build_runner_exception_after_connect_disconnects_instruments(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, 2.10] ha _build_runner() a controller-építés
        közben hibázik (a hardveres connect UTÁN), a már megnyitott
        kapcsolatokat le kell zárni."""
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)
        # Review-kör 2 (2026-09-08), Szoftver/Architektúra F1 lelet: a
        # window._instruments (a fenti _make_window_with_connected_
        # instruments() ÁLTAL beállított, KORÁBBI session-hez tartozó
        # instrument-manager) SZÁNDÉKOSAN NEM kap disconnect_all() spy-t
        # itt -- a helyes fix a MOST, ebben a próbálkozásban ténylegesen
        # csatlakoztatott pre_connected.instruments-t zárja le, nem ezt a
        # régi objektumot (l. main_window.py::_on_connect_ready() except
        # ágának kommentje).
        old_session_disconnect_calls = []
        window._instruments.disconnect_all = lambda: old_session_disconnect_calls.append(True)
        window._build_runner = lambda cfg, pre_connected=None: (_ for _ in ()).throw(
            RuntimeError("controller construction failed")
        )
        window._pending_start_cfg = _make_cfg()
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        import types

        from Prog.gui.main_window import _PreConnectedInstruments

        new_attempt_calls = []
        bq_serial_closed = []
        pre_connected = _PreConnectedInstruments(
            instruments=types.SimpleNamespace(
                safe_all_off=lambda: new_attempt_calls.append("safe_all_off"),
                disconnect_all=lambda: new_attempt_calls.append("disconnect_all"),
            ),
            psu=None, load=None, dmm_v=None, dmm_t=None, bq_transport=None,
            session_bq_serial=types.SimpleNamespace(
                close=lambda: bq_serial_closed.append(True)
            ),
            instrument_identities={},
        )

        window._on_connect_ready(pre_connected=pre_connected)

        # A MOST megnyitott kapcsolatokat kell lezárni -- ez a valódi
        # erőforrás-szivárgás, amit a lelet azonosított.
        assert bq_serial_closed == [True]
        # Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-04 lelet:
        # a fizikai PSU/Load kimenetet safe_all_off()-fal KI KELL kapcsolni,
        # MIELŐTT a VISA-kapcsolat lezáródik (disconnect_all()).
        assert new_attempt_calls == ["safe_all_off", "disconnect_all"]
        # A régi, korábbi session instrument-manager-ét NEM szabad
        # (ismét, feleslegesen/tévesen) lezárni ezen az ágon.
        assert old_session_disconnect_calls == []

    def test_build_runner_exception_after_wp_ak_16_block_resets_bq_transport_flags(
        self, qapp, monkeypatch, tmp_path
    ):
        """[Kör 20 Batch E9, F20-G2, 2026-09-19] Ha _build_runner() a WP-AK-16
        blokk (self._session_bq_transport/self._session_owns_bq_transport
        beállítása) UTÁN, de a controller-építés befejezése ELŐTT hibázik, a
        MAX verifier F6 leletében (2026-09-15) NEM vizsgált GUI-állapot
        korábban LATCH-elve maradt: self._session_owns_bq_transport=True +
        a bq_diagnostics_panel session-owned módja, kizárva a Pico-
        előkészítést/kalibrációt/BQ Diagnosztika kapcsolódást -- KIZÁRÓLAG
        egy alkalmazás-újraindítás oldotta volna fel. Ez a teszt bukna a
        fix előtti kódon (a _close_session_bq_transport() hívás hiánya
        miatt a transport.disconnect() sosem futna le, és a flag/panel-mód
        sosem állna vissza)."""
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)
        window._instruments.disconnect_all = lambda: None

        disconnected = []

        class _FakeTransport:
            def disconnect(self):
                disconnected.append(True)

        panel_calls = []
        window._bq_diagnostics_panel.set_session_owned = (
            lambda active, session_label="": panel_calls.append((active, session_label))
        )

        def _fake_build_runner(cfg, pre_connected=None):
            # A valódi _build_runner() WP-AK-16 blokkjának 1:1 mása -- a
            # két mezőt EGYMÁS UTÁN, feltétel nélkül állítja be, majd a
            # session_owned módot is (l. main_window.py:3494-3509), MIELŐTT
            # a controller-építés (itt szimulálva) hibázna.
            window._session_bq_serial = pre_connected.session_bq_serial
            window._session_bq_transport = _FakeTransport()
            window._session_owns_bq_transport = True
            window._bq_diagnostics_panel.set_session_owned(True, session_label="TRADITIONAL_LEARNING")
            raise RuntimeError("controller construction failed after WP-AK-16 block")

        window._build_runner = _fake_build_runner
        window._pending_start_cfg = _make_cfg()
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        import types

        from Prog.gui.main_window import _PreConnectedInstruments

        bq_serial_closed = []
        pre_connected = _PreConnectedInstruments(
            instruments=types.SimpleNamespace(
                safe_all_off=lambda: None, disconnect_all=lambda: None,
            ),
            psu=None, load=None, dmm_v=None, dmm_t=None, bq_transport=None,
            session_bq_serial=types.SimpleNamespace(
                close=lambda: bq_serial_closed.append(True)
            ),
            instrument_identities={},
        )

        window._on_connect_ready(pre_connected=pre_connected)

        # A transport SAJÁT disconnect()-je lefutott -- ez a valódi
        # bizonyíték, hogy _close_session_bq_transport() (nem csak a nyers
        # serial.close()) meghívódott.
        assert disconnected == [True]
        assert window._session_owns_bq_transport is False
        assert window._session_bq_transport is None
        assert window._session_bq_serial is None
        # A panel visszaáll önálló módba -- (True, ...) HÍVÁS UTÁN (True,
        # session_label) majd (False, "") kell szerepeljen, ebben a
        # sorrendben.
        assert panel_calls == [(True, "TRADITIONAL_LEARNING"), (False, "")]
        # A meglévő, nyers session_bq_serial.close() védelmi ág (arra az
        # esetre, ha _build_runner() a WP-AK-16 blokk ELŐTT hibázna)
        # VÁLTOZATLANUL lefut -- a két hívás nem zárja ki egymást, és a
        # pyserial.Serial.close() ismételt hívása idempotens no-op.
        assert bq_serial_closed == [True]

    def test_build_runner_exception_before_wp_ak_16_block_does_not_call_close_session_bq_transport_incorrectly(
        self, qapp, monkeypatch, tmp_path
    ):
        """Counter-test a fenti mellé: ha _build_runner() a WP-AK-16 blokk
        ELŐTT hibázik (a mezők sosem lettek beállítva), az új
        _close_session_bq_transport() hívásnak NO-OP-nak kell lennie -- nem
        szabad kivételt dobnia, és nem szabad a session_owned módot
        (ami sosem volt True) hamisan False-ra "visszaállítania" (mert az
        sosem volt True)."""
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_connected_instruments(qapp, monkeypatch, tmp_path)
        window._instruments.disconnect_all = lambda: None

        panel_calls = []
        window._bq_diagnostics_panel.set_session_owned = (
            lambda active, session_label="": panel_calls.append((active, session_label))
        )
        window._build_runner = lambda cfg, pre_connected=None: (_ for _ in ()).throw(
            RuntimeError("controller construction failed before WP-AK-16 block")
        )
        window._pending_start_cfg = _make_cfg()
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

        import types

        from Prog.gui.main_window import _PreConnectedInstruments

        bq_serial_closed = []
        pre_connected = _PreConnectedInstruments(
            instruments=types.SimpleNamespace(
                safe_all_off=lambda: None, disconnect_all=lambda: None,
            ),
            psu=None, load=None, dmm_v=None, dmm_t=None, bq_transport=None,
            session_bq_serial=types.SimpleNamespace(
                close=lambda: bq_serial_closed.append(True)
            ),
            instrument_identities={},
        )

        window._on_connect_ready(pre_connected=pre_connected)  # nem dob kivételt

        assert window._session_owns_bq_transport is False
        assert panel_calls == []
        assert bq_serial_closed == [True]

    # -- 2.13: closeEvent() zárja a session BQ soros portot --------------- #

    def test_close_session_bq_transport_closes_and_resets_flags(
        self, qapp, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        disconnected = []

        # Review 2026-09-15 (C-02): _close_session_bq_transport() mostantól
        # a transport SAJÁT .disconnect()-jét hívja (nem a nyers serial
        # .close()-t) -- l. a metódus docstringje a teljes indoklásért.
        class _FakeTransport:
            def disconnect(self):
                disconnected.append(True)

        window._session_owns_bq_transport = True
        window._session_bq_transport = _FakeTransport()
        window._session_bq_serial = object()  # csak jelenlét-jelzésre, nem hívott

        window._close_session_bq_transport()

        assert disconnected == [True]
        assert window._session_bq_serial is None
        assert window._session_bq_transport is None
        assert window._session_owns_bq_transport is False

    def test_close_session_bq_transport_noop_when_not_owned(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._session_owns_bq_transport = False
        window._session_bq_serial = None
        window._session_bq_transport = None
        window._close_session_bq_transport()  # nem dob kivételt

    def test_close_event_no_active_worker_closes_session_bq_transport(
        self, qapp, monkeypatch, tmp_path
    ):
        """[a CRITICAL-hez közeli lelet lényege] CHECKPOINT_STOPPED után az
        ablakbezárás korábban sosem zárta le a session BQ soros portját."""
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        disconnected = []

        class _FakeTransport:
            def disconnect(self):
                disconnected.append(True)

        window._session_owns_bq_transport = True
        window._session_bq_transport = _FakeTransport()
        window._session_bq_serial = object()

        class _FakeEvent:
            def accept(self):
                pass

            def ignore(self):
                pass

        window.closeEvent(_FakeEvent())

        assert disconnected == [True]

    def test_close_event_ignored_when_bq_diagnostics_thread_does_not_stop_in_time(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, review-kör 4 (2026-09-09), MAX verifier
        (a153925b3b9fbce5c) P3-2, DFAM-016 sibling] a closeEvent() "nincs
        aktív teszt-worker" ága korábban feltétel nélkül hívta a
        _cleanup_bq_thread()-et (a BQ Diagnosztika szálra) -- ugyanaz a
        minta, mint a fő teszt-workernél (S4-01), csak a BQ diagnosztika-
        szálon. Ha a szál nem áll le időben, az ablak most már nem
        zárható be és a session BQ soros portja sem záródik le idő előtt."""
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        closed = []

        # MAX verifier F4 lelete (2026-09-15, C-02 után): a
        # _close_session_bq_transport() mostantól a
        # self._session_bq_transport.disconnect()-et hívja, NEM a nyers
        # self._session_bq_serial.close()-t -- enélkül a lenti kapu
        # (self._session_bq_transport is not None) SOHA nem tüzelt volna,
        # a `closed == []` állítás pedig TAUTOLOGIKUSSÁ vált volna
        # (bármit is csinál a closeEvent(), closed sosem változna).
        class _FakeTransport:
            def disconnect(self):
                closed.append(True)

        window._session_owns_bq_transport = True
        window._session_bq_transport = _FakeTransport()
        window._session_bq_serial = object()

        class _FakeBqThreadStillRunning:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return False

            def deleteLater(self):
                raise AssertionError(
                    "deleteLater() nem hívható egy még futó QThread-en"
                )

        window._bq_thread = _FakeBqThreadStillRunning()
        window._bq_worker = None

        class _FakeEvent:
            def __init__(self):
                self.ignored = False
                self.accepted = False

            def accept(self):
                self.accepted = True

            def ignore(self):
                self.ignored = True

        event = _FakeEvent()
        window.closeEvent(event)

        assert event.ignored is True
        assert event.accepted is False
        assert closed == []  # a session BQ port NEM záródott le idő előtt
        assert window._bq_thread is not None

    def test_bq_disconnect_requested_warns_and_keeps_state_when_thread_does_not_stop_in_time(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[regresszió, review-kör 5, GUI/UX/Állapot G5-02 (P2, DFAM-001)]
        _on_bq_disconnect_requested() korábban eldobta a _cleanup_bq_thread()
        visszatérési értékét -- timeoutnál a set_disconnected() ekkor is
        lefutott volna, hamis "Nincs kapcsolat"-ot mutatva egy MÉG ÉLŐ
        worker/soros port mellett, és a "Kapcsolódás" gomb újra
        engedélyezve egy csendes no-opra futott volna
        (_on_bq_connect_requested() early-return self._bq_worker is not
        None esetén)."""
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        warnings = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))

        class _FakeBqThreadStillRunning:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return False

            def deleteLater(self):
                raise AssertionError(
                    "deleteLater() nem hívható egy még futó QThread-en"
                )

        class _FakeStopRequestedSignal:
            def emit(self):
                pass

        class _FakeBqWorker:
            def __init__(self):
                self.stop_requested = _FakeStopRequestedSignal()

        window._bq_thread = _FakeBqThreadStillRunning()
        window._bq_worker = _FakeBqWorker()  # még "aktív" -- nem szabad None-ra esnie

        set_disconnected_calls = []
        window._bq_diagnostics_panel.set_disconnected = (
            lambda: set_disconnected_calls.append(True)
        )

        window._on_bq_disconnect_requested()

        assert len(warnings) == 1
        assert set_disconnected_calls == []  # a panel NEM állt "Nincs kapcsolat"-ra
        assert window._bq_worker is not None  # a következő connect-kísérlet nem lenne néma no-op

    def test_bq_connect_ready_disables_pico_provisioning_action(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[Kör 20 Batch E9 follow-up, NORMAL verifier lelete, 2026-09-19]
        _on_bq_connect_ready() a self._bq_worker beállítása UTÁN korábban
        NEM hívta a _refresh_calibration_blocked_state()-et -- a metódus
        ELEJÉN futó _cleanup_bq_connect_thread() MÁR meghívta ezt, de MÉG a
        self._bq_worker beállítása ELŐTT (a régi, None értékkel), ezért a
        Pico-előkészítő menüakció HAMISAN ENGEDÉLYEZETT maradt egy sikeres
        BQ Diagnosztika kapcsolódás UTÁN is, holott _on_pico_provisioning_
        requested() saját négy ága ezt a pontos állapotot (self._bq_worker
        is not None) blokkolná."""
        import Prog.gui.main_window as main_window_module
        from Prog.gui.main_window import _BqConnectResult

        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()

        class _FakeSignal:
            def connect(self, *a, **kw):
                pass

        class _FakeBqDiagnosticsWorker:
            def __init__(self, driver, serial_conn=None):
                self.snapshot_ready = _FakeSignal()
                self.connection_error = _FakeSignal()
                self.stop_requested = _FakeSignal()

            def moveToThread(self, thread):
                pass

            def start_polling(self):
                pass

        class _FakeQThread:
            def __init__(self):
                self.started = _FakeSignal()

            def start(self):
                pass

        monkeypatch.setattr(main_window_module, "BqDiagnosticsWorker", _FakeBqDiagnosticsWorker)
        monkeypatch.setattr(main_window_module, "QThread", _FakeQThread)

        assert window._pico_provisioning_action.isEnabled() is True

        result = _BqConnectResult(bq_serial=object(), driver=object(), port="COM10")
        window._on_bq_connect_ready(result)

        assert window._bq_worker is not None
        assert window._pico_provisioning_action.isEnabled() is False

    def test_bq_disconnect_requested_reenables_pico_provisioning_action(
        self, qapp, monkeypatch, tmp_path,
    ):
        """[Kör 20 Batch E9 follow-up, NORMAL verifier lelete, 2026-09-19]
        _on_bq_disconnect_requested() a self._bq_worker nullázása UTÁN
        (a _cleanup_bq_thread()-en keresztül) korábban NEM hívta a
        _refresh_calibration_blocked_state()-et -- a Pico-előkészítő
        menüakció TARTÓSAN letiltva maradt egy sikeres leválás után is,
        bármi más blokkoló ok nélkül."""
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()

        class _FakeBqThreadStopsInTime:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return True

            def deleteLater(self):
                pass

        class _FakeStopRequestedSignal:
            def emit(self):
                pass

        class _FakeBqWorker:
            def __init__(self):
                self.stop_requested = _FakeStopRequestedSignal()

            def deleteLater(self):
                pass

        window._bq_thread = _FakeBqThreadStopsInTime()
        window._bq_worker = _FakeBqWorker()
        window._refresh_calibration_blocked_state()  # a fenti "aktív" bq_worker letiltja
        assert window._pico_provisioning_action.isEnabled() is False

        window._on_bq_disconnect_requested()

        assert window._bq_worker is None
        assert window._pico_provisioning_action.isEnabled() is True


class TestReview2ndRoundCheckpointCloseFinalizesSession:
    """Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P1 lelet (F3): a
    checkpoint fülön a dokumentált, elsődleges kilépési út ("Teszt lezárása")
    korábban resumable checkpointnál (resume_possible=True, a tipikus eset)
    SEM műszer-lecsatlakozást, SEM BQ transport zárást, SEM session-active
    resetet, SEM report.json írást nem végzett — a session onnantól
    auditálhatatlanul "lógva" maradt (VISA kapcsolatok nyitva, "Eszközök
    keresése" véglegesen letiltva, session_recovery.py crashelt sessionként
    látta volna, mert checkpoint.json van, de report.json nincs)."""

    def _make_window_at_resumable_checkpoint(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestResult
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)  # beállítja _instruments/_session_dir/_config_panel-t
        disconnect_calls: list = []
        window._instruments.disconnect_all = lambda: disconnect_calls.append(True)
        # A resumable checkpoint elérése — worker.finished a valós Start-
        # folyamat nélkül, közvetlenül _on_finished()-en keresztül szimulálva
        # (ugyanaz a minta, mint TestReportWrittenForAllTerminalStates).
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True, total_charge_ah=0.4, total_discharge_ah=0.0,
        ))
        return window, disconnect_calls

    def test_checkpoint_stopped_resumable_does_not_finalize_yet(
        self, qapp, monkeypatch, tmp_path
    ):
        """Amíg a kezelő nem zárja le, a session-nek élnie kell — ez a
        meglévő, VÁLTOZATLAN viselkedés (l. TestReleaseAudit20260823
        InstrumentLifecycle.test_non_terminal_resumable_checkpoint_does_not_
        disconnect), csak itt a _pending_checkpoint_result mentését is
        igazolja."""
        window, disconnect_calls = self._make_window_at_resumable_checkpoint(
            qapp, monkeypatch, tmp_path
        )
        assert disconnect_calls == []
        assert not (window._session_dir / "report.json").exists()
        assert window._pending_checkpoint_result is not None

    def test_checkpoint_close_finalizes_resumable_session(self, qapp, monkeypatch, tmp_path):
        window, disconnect_calls = self._make_window_at_resumable_checkpoint(
            qapp, monkeypatch, tmp_path
        )

        window._on_checkpoint_close()

        assert disconnect_calls == [True]
        assert (window._session_dir / "report.json").exists()
        assert window._pending_checkpoint_result is None
        assert window._config_panel._search_btn.isEnabled() is True

    def test_checkpoint_close_report_reflects_checkpoint_status(
        self, qapp, monkeypatch, tmp_path
    ):
        import json

        window, _disconnect_calls = self._make_window_at_resumable_checkpoint(
            qapp, monkeypatch, tmp_path
        )
        window._on_checkpoint_close()

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["status"] == "CHECKPOINT_STOPPED"

    def test_checkpoint_stopped_resumable_disables_start_button(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 18. kör (2026-09-05), GUI/UX/
        Állapot P2 lelet] worker.py::_do_run() a keep_thread=True ágban IS
        kiadja a status_changed.emit("CHECKPOINT_STOPPED")-et, amit a
        LivePanel.set_status() fogad — mivel a status != "RUNNING", a Start
        gomb ott korábban visszaengedélyeződött, HOLOTT a worker/szál
        szándékosan életben marad (a session folytatható)."""
        window, _disconnect_calls = self._make_window_at_resumable_checkpoint(
            qapp, monkeypatch, tmp_path
        )
        assert window._live_panel._start_btn.isEnabled() is False

    def test_checkpoint_close_reenables_start_button(self, qapp, monkeypatch, tmp_path):
        window, _disconnect_calls = self._make_window_at_resumable_checkpoint(
            qapp, monkeypatch, tmp_path
        )
        window._on_checkpoint_close()
        assert window._live_panel._start_btn.isEnabled() is True

    def test_checkpoint_close_after_emergency_stop_reports_fault(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 23. kör (2026-09-07), Session/
        Workflow/Állapotgép P1 lelet] ha az operátor a checkpointnál
        parkolva előbb a "Safe Off" gombbal (_emergency_stop()) vészleállítást
        kér, majd "Teszt lezárása"-t nyom, ez az ág korábban SOSEM
        ellenőrizte a kérést — a report.json a régi, változatlan
        CHECKPOINT_STOPPED eredményből íródott, az explicit vészleállítás-
        kérés nyomtalanul eltűnt. Pontos testvérpárja a 19. kör
        worker.py::request_continue_from_checkpoint() fixjének (81741a4)."""
        import json
        import types

        window, _disconnect_calls = self._make_window_at_resumable_checkpoint(
            qapp, monkeypatch, tmp_path
        )
        # _make_window_at_resumable_checkpoint() a valós Start-folyamatot
        # (_start_test(), ami a self._worker-t ténylegesen létrehozza)
        # kihagyva közvetlenül _on_finished()-en keresztül szimulálja a
        # checkpointot — self._worker emiatt itt None. Egy minimális stub-
        # bal pótoljuk, ami pontosan azt a felületet adja, amit _emergency_
        # stop()/az _on_checkpoint_close() fix ténylegesen használ.

        class _FakeWorker:
            def __init__(self):
                self.runner = types.SimpleNamespace(
                    emergency_stop_requested=False, emergency_stop_reason="",
                )

            def request_emergency_stop(self, reason):
                self.runner.emergency_stop_requested = True
                self.runner.emergency_stop_reason = reason

            def request_continue_from_checkpoint(self):
                pass

            def deleteLater(self):
                pass

        window._worker = _FakeWorker()
        window._emergency_stop()  # Safe Off gomb — csak flaget állít, a runner dormant

        window._on_checkpoint_close()

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["status"] == "FAULT"
        assert "EMERGENCY_STOP_AT_CHECKPOINT" in report["reason"]
        assert report["MEASUREMENT_LIMITATIONS"]["emergency_stop_occurred"] is True

    def test_checkpoint_close_after_emergency_stop_does_not_inherit_stale_safe_off_attestation(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, MAX verifier NV-1, batch 4.1 follow-up] a
        `_run_manual_checkpoint()` (V-1 fix) mostantól `safe_off_
        attempted=True` + valós `safe_off_result`-tal tér vissza a
        CHECKPOINT_STOPPED eredményben — ez a checkpoint ELÉRÉSEKOR számolt
        bizonyíték. Az `_on_checkpoint_close()` `dataclasses_replace()`-a
        enélkül a javítás nélkül VÁLTOZATLANUL örökölné ezt a régi
        bizonyítékot a FAULT eredménybe/report.json-ba, holott a
        checkpointnál parkolva kért vészleállítás pillanatában egyetlen
        friss OFF parancs sem ment ki."""
        import json
        import types

        from Prog.src.test_runner import TestResult

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)
        window._instruments.disconnect_all = lambda: None
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True, total_charge_ah=0.4, total_discharge_ah=0.0,
            safe_off_attempted=True,
            safe_off_result={"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True},
        ))

        class _FakeWorker:
            def __init__(self):
                self.runner = types.SimpleNamespace(
                    emergency_stop_requested=False, emergency_stop_reason="",
                )

            def request_emergency_stop(self, reason):
                self.runner.emergency_stop_requested = True
                self.runner.emergency_stop_reason = reason

            def request_continue_from_checkpoint(self):
                pass

            def deleteLater(self):
                pass

        window._worker = _FakeWorker()
        window._emergency_stop()
        # Governance-v2 review-kör (MAX verifier V4-4/V4-1, batch 4.1b): a
        # lezárás mostantól TÉNYLEGES, friss safe-offot ad ki -- a stub
        # eredménye SZÁNDÉKOSAN megkülönböztethető a fenti checkpoint-kori
        # bizonyítéktól, hogy az öröklés ne lehessen álpozitív.
        window._instruments.safe_all_off = lambda: {
            "Load": True, "PSU": True, "DMM_V": False, "DMM_T": False,
        }

        window._on_checkpoint_close()

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["status"] == "FAULT"
        assert report["safe_off_attempted"] is True
        assert report["safe_off_result"] == {
            "Load": True, "PSU": True, "DMM_V": False, "DMM_T": False,
        }
        # A checkpoint-kori safe-off bizonyíték külön mezőben,
        # nem összekeverve a fentivel -- V4-1.
        assert report["checkpoint_safe_off_attempted"] is True
        assert report["checkpoint_safe_off_result"] == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }

    def test_checkpoint_close_without_emergency_stop_keeps_checkpoint_status(
        self, qapp, monkeypatch, tmp_path
    ):
        import json

        window, _disconnect_calls = self._make_window_at_resumable_checkpoint(
            qapp, monkeypatch, tmp_path
        )

        window._on_checkpoint_close()

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["status"] == "CHECKPOINT_STOPPED"

    def test_checkpoint_close_without_emergency_stop_does_not_carry_stale_safe_off_attestation(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, MAX verifier F2 (batch 4.1 follow-up, 3. kör)] a
        normál "Teszt lezárása" (NINCS vészleállítás-kérés) ág korábban
        VÁLTOZATLANUL vitte tovább a checkpoint ELÉRÉSEKOR (akár órákkal/
        napokkal korábban) számolt `safe_off_attempted`/`safe_off_result`
        mezőket a végleges report.json-ba — holott `_on_checkpoint_close()`
        SEM ezen az ágon nem ad ki friss OFF parancsot
        (`_finalize_terminal_session()` csak `disconnect_all()`-t hív).
        A mezőknek MOST már ezt a helyi, friss tényt kell tükrözniük
        MINDKÉT ágon, nem a checkpoint-kori (esetleg elévült) bizonyítékot."""
        import json

        from Prog.src.test_runner import TestResult

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)
        window._instruments.disconnect_all = lambda: None
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True, total_charge_ah=0.4, total_discharge_ah=0.0,
            safe_off_attempted=True,
            safe_off_result={"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True},
        ))

        window._instruments.safe_all_off = lambda: {
            "Load": True, "PSU": True, "DMM_V": False, "DMM_T": False,
        }

        window._on_checkpoint_close()  # NINCS előzetes _emergency_stop() hívás

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["status"] == "CHECKPOINT_STOPPED"
        # Governance-v2 review-kör (MAX verifier V4-4, batch 4.1b): a mezők a
        # LEZÁRÁSKOR ténylegesen kiadott, friss safe-off eredményét tükrözik
        # (korábban False/{} volt, mert egyetlen OFF parancs sem ment ki).
        assert report["safe_off_attempted"] is True
        assert report["safe_off_result"] == {
            "Load": True, "PSU": True, "DMM_V": False, "DMM_T": False,
        }
        assert report["checkpoint_safe_off_result"] == {
            "Load": True, "PSU": True, "DMM_V": True, "DMM_T": True,
        }

    def test_continue_requested_clears_pending_checkpoint_result(
        self, qapp, monkeypatch, tmp_path
    ):
        """Ha a kezelő inkább folytatja a sessiont (nem zárja le), a mentett
        checkpoint-eredmény elévül — enélkül egy elképzelhető (bár szűk)
        versenyhelyzetben egy elavult eredmény íródna ki, ha _on_checkpoint_
        close() mégis lefutna a folytatás után."""
        window, _disconnect_calls = self._make_window_at_resumable_checkpoint(
            qapp, monkeypatch, tmp_path
        )
        assert window._pending_checkpoint_result is not None
        window._worker = object()  # _on_continue_requested() csak akkor emit-el, ha van worker

        window._on_continue_requested()

        assert window._pending_checkpoint_result is None


class TestGovernanceV4CheckpointCloseFreshSafeOff:
    """[regresszió, governance-v2 review-kör, MAX verifier V4-4 + V4-1 (P2,
    DFAM-016), batch 4.1b] egy BQ checkpointnál PARKOLÓ session végleges
    lezárása ("Teszt lezárása" gomb, ill. ablakbezárás) a session TÉNYLEGES
    befejezése — CLAUDE.md §6 szerint ilyenkor kötelező a Load INPUT OFF +
    PSU ALL OUTPUTS OFF. Korábban ezen az úton EGYETLEN friss OFF parancs sem
    ment ki (`_finalize_terminal_session()` csak `disconnect_all()`-t hív,
    ami NEM ad ki OUTPUT/INPUT OFF-ot), a riport pedig `safe_off_attempted=
    False`/`{}`-t rögzített."""

    def _make_window_parked_at_checkpoint(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestResult
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)
        calls: list = []
        window._instruments.safe_all_off = lambda: (
            calls.append("safe_all_off")
            or {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}
        )
        window._instruments.disconnect_all = lambda: calls.append("disconnect_all")
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True, total_charge_ah=0.4, total_discharge_ah=0.0,
            safe_off_attempted=True,
            safe_off_result={"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True},
        ))
        return window, calls

    def test_checkpoint_close_issues_fresh_safe_off_before_disconnect(
        self, qapp, monkeypatch, tmp_path
    ):
        """Diszkrimináló: a régi kódon a `safe_all_off` egyáltalán nem
        szerepelne a hívási sorrendben. A SORREND is kötött — egy
        `disconnect_all()` UTÁN kiadott OFF parancs már nem érne el
        eszközt."""
        window, calls = self._make_window_parked_at_checkpoint(
            qapp, monkeypatch, tmp_path
        )

        window._on_checkpoint_close()

        assert calls == ["safe_all_off", "disconnect_all"]

    def test_checkpoint_close_logs_safe_off_performed_event(
        self, qapp, monkeypatch, tmp_path
    ):
        window, _calls = self._make_window_parked_at_checkpoint(
            qapp, monkeypatch, tmp_path
        )
        events: list = []
        window._live_panel.append_event = events.append

        window._on_checkpoint_close()

        assert any(e.get("event_code") == "SAFE_OFF_PERFORMED" for e in events)

    def test_uncertain_safe_off_is_critical_event_and_visible_in_report(
        self, qapp, monkeypatch, tmp_path
    ):
        """CLAUDE.md §10.4: egy nem igazoltan sikeres OFF sosem maradhat
        csendben — sem az élő eseménynaplóban, sem a riportban."""
        import json

        window, _calls = self._make_window_parked_at_checkpoint(
            qapp, monkeypatch, tmp_path
        )
        window._instruments.safe_all_off = lambda: {
            "Load": False, "PSU": True, "DMM_V": True, "DMM_T": True,
        }
        events: list = []
        window._live_panel.append_event = events.append

        window._on_checkpoint_close()

        uncertain = [
            e for e in events if e.get("event_code") == "SAFE_OFF_RESULT_UNCERTAIN"
        ]
        assert len(uncertain) == 1
        assert uncertain[0]["severity"] == "CRITICAL"
        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["safe_off_result"]["Load"] is False

    def test_missing_instruments_never_produce_false_positive_attestation(
        self, qapp, monkeypatch, tmp_path
    ):
        """Ha nincs élő műszer-kapcsolat, a riport NEM állíthatja, hogy
        safe-off történt."""
        import json

        window, _calls = self._make_window_parked_at_checkpoint(
            qapp, monkeypatch, tmp_path
        )
        window._instruments = None
        events: list = []
        window._live_panel.append_event = events.append

        window._on_checkpoint_close()

        assert any(
            e.get("event_code") == "SAFE_OFF_SKIPPED_NO_INSTRUMENTS" for e in events
        )
        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["safe_off_attempted"] is False
        assert report["safe_off_result"] == {}


class TestGovernanceV44CloseEventAtParkedCheckpoint:
    """[regresszió, governance-v2 review-kör, MAX verifier V4-4 (P2,
    blokkoló-jellegű biztonsági kommunikációs hiba)] egy BQ checkpointnál
    PARKOLÓ sessionnél a `closeEvent()` korábban a "Teszt fut" ágra esett: a
    dialógus HAMISAN azt ígérte, hogy "a safe_off automatikusan megtörténik",
    a Yes-ág pedig `_emergency_stop()`-ot hívott -- ami itt CSAK egy flaget
    állít a dormant runneren, amit soha semmi nem olvas ki. Eredmény: NEM ment
    ki OFF parancs, NEM történt disconnect_all(), NEM készült report.json."""

    def _make_parked_window(self, qapp, monkeypatch, tmp_path):
        import types

        from Prog.src.test_runner import TestResult
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        window._build_runner(cfg)
        calls: list = []
        window._instruments.safe_all_off = lambda: (
            calls.append("safe_all_off")
            or {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}
        )
        window._instruments.disconnect_all = lambda: calls.append("disconnect_all")
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True, total_charge_ah=0.4, total_discharge_ah=0.0,
            safe_off_attempted=True,
            safe_off_result={"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True},
        ))

        class _FakeWorker:
            def __init__(self):
                self.runner = types.SimpleNamespace(
                    emergency_stop_requested=False, emergency_stop_reason="",
                )

            def request_emergency_stop(self, reason):
                self.runner.emergency_stop_requested = True
                self.runner.emergency_stop_reason = reason

            def request_continue_from_checkpoint(self):
                pass

            def deleteLater(self):
                pass

        # A parkoló session worker-je SZÁNDÉKOSAN életben van (ez tartja
        # fenn a folytatás lehetőségét) -- pont ez vitte a closeEvent()-et a
        # "Teszt fut" ágra.
        window._worker = _FakeWorker()
        return window, calls

    class _FakeEvent:
        def __init__(self):
            self.ignored = False
            self.accepted = False

        def ignore(self):
            self.ignored = True

        def accept(self):
            self.accepted = True

    def test_close_at_parked_checkpoint_performs_safe_off_disconnect_and_report(
        self, qapp, monkeypatch, tmp_path
    ):
        from PySide6.QtWidgets import QMessageBox

        window, calls = self._make_parked_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        event = self._FakeEvent()

        window.closeEvent(event)

        assert calls == ["safe_all_off", "disconnect_all"]
        assert (window._session_dir / "report.json").exists()
        assert event.accepted is True
        assert event.ignored is False

    def test_close_dialog_does_not_falsely_promise_automatic_safe_off(
        self, qapp, monkeypatch, tmp_path
    ):
        """A finding lényege a HAMIS ÍGÉRET: az operátor jóhiszeműen
        otthagyhatta a bekapcsolt PSU-t/Load-ot. A szövegnek pontosan azt
        kell leírnia, amit az ág ténylegesen elvégez."""
        from PySide6.QtWidgets import QMessageBox

        window, _calls = self._make_parked_window(qapp, monkeypatch, tmp_path)
        texts: list = []

        def _question(_parent, _title, text, *a, **k):
            texts.append(text)
            return QMessageBox.StandardButton.Yes

        monkeypatch.setattr(QMessageBox, "question", _question)

        window.closeEvent(self._FakeEvent())

        assert len(texts) == 1
        assert "A safe_off automatikusan megtörténik." not in texts[0]
        assert "safe-off" in texts[0]
        assert "report.json" in texts[0]

    def test_close_at_parked_checkpoint_no_keeps_session_alive(
        self, qapp, monkeypatch, tmp_path
    ):
        from PySide6.QtWidgets import QMessageBox

        window, calls = self._make_parked_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )
        event = self._FakeEvent()

        window.closeEvent(event)

        assert event.ignored is True
        assert event.accepted is False
        assert calls == []
        assert not (window._session_dir / "report.json").exists()
        assert window._pending_checkpoint_result is not None

    def test_close_at_parked_checkpoint_blocked_when_thread_does_not_stop(
        self, qapp, monkeypatch, tmp_path
    ):
        """S4-01 kapu ezen az ágon is: ha a worker-szál nem állt le, az
        ablak NEM zárható be."""
        from PySide6.QtWidgets import QMessageBox

        window, _calls = self._make_parked_window(qapp, monkeypatch, tmp_path)

        class _FakeThreadStillRunning:
            def quit(self):
                pass

            def wait(self, timeout_ms):
                return False

            def deleteLater(self):
                raise AssertionError(
                    "deleteLater() nem hívható egy még futó QThread-en"
                )

        window._thread = _FakeThreadStillRunning()
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        warnings: list = []
        monkeypatch.setattr(
            QMessageBox, "warning", lambda *a, **k: warnings.append(a) or None
        )
        event = self._FakeEvent()

        window.closeEvent(event)

        assert event.ignored is True
        assert event.accepted is False
        assert len(warnings) == 1
        assert window._worker is not None


class TestReleaseAudit20260823DialogDefaultButtonNo:
    """Release-audit 2026-08-23 (2.14): a Start/figyelmeztetés/resume
    dialógusok explicit defaultButton=No-t kapjanak — enélkül egy véletlen
    Enter-lenyomás a Qt-alapértelmezés szerint a Yes-t (proceed) választaná."""

    def _make_window_with_valid_config(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cp = window._config_panel
        cp._model_edit.setText("TEST7AH")
        cp._capacity_spin.setValue(1.0)
        cp._psu_res_edit.setText("MOCK_PSU")
        cp._load_res_edit.setText("MOCK_LOAD")
        cp._dmm_v_res_edit.setText("MOCK_DMM_V")
        cp._dmm_t_res_edit.setText("MOCK_DMM_T")
        cp._psu_mode_combo.setCurrentText("INDEPENDENT")
        return window

    def test_start_summary_dialog_defaults_to_no(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QMessageBox
        window = self._make_window_with_valid_config(qapp, monkeypatch, tmp_path)

        calls = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: calls.append(a) or QMessageBox.StandardButton.No,
        )

        window._start_test()

        assert len(calls) == 1
        assert "Indítod a mérést?" in calls[0][2]
        assert calls[0][-1] == QMessageBox.StandardButton.No


class TestReleaseAudit20260823CheckpointChecklistLogging:
    """Release-audit 2026-08-23 (2.17): a BQ checkpoint-checklist állapota
    a "Teszt lezárása" gombnál bekerül az eseménynaplóba és egy külön
    fájlba a session mappájában."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"
        window._session_dir.mkdir(parents=True, exist_ok=True)
        return window

    def test_writes_checklist_file_with_all_items(self, qapp, monkeypatch, tmp_path):
        import json
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._checkpoint_panel._checkboxes[0].setChecked(True)

        window._log_checkpoint_checklist()

        checklist_path = window._session_dir / "bq_checkpoint_checklist.json"
        assert checklist_path.exists()
        state = json.loads(checklist_path.read_text(encoding="utf-8"))
        assert len(state) == len(window._checkpoint_panel._CHECKLIST_ITEMS)
        assert sum(1 for v in state.values() if v) == 1

    def test_appends_event_with_missing_items_summary(self, qapp, monkeypatch, tmp_path):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        events = []
        window._live_panel.append_event = lambda ev: events.append(ev)

        window._log_checkpoint_checklist()

        assert len(events) == 1
        assert events[0]["event_code"] == "BQ_CHECKPOINT_CHECKLIST_AT_CLOSE"
        assert "0/" in events[0]["event_message"]

    def test_noop_without_session_dir(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        assert window._session_dir is None
        window._log_checkpoint_checklist()  # nem dob kivételt

    def test_write_failure_surfaces_as_event_not_silently_swallowed(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Teljes kódbázis review 18. kör (2026-09-05), GUI/UX/
        Állapot P3 lelet] a checklist-fájl írási hibája korábban csendben
        elveszett (except Exception: pass) — CLAUDE.md §13 "logírási hiba
        látható kezelése", a _write_report()-nál (8. kör) már javított
        hibaosztály testvér-írási útja."""
        from pathlib import Path
        window = self._make_window(qapp, monkeypatch, tmp_path)
        events = []
        window._live_panel.append_event = lambda ev: events.append(ev)

        def _raise_on_write(self, *a, **k):
            raise OSError("simulated disk full")
        monkeypatch.setattr(Path, "write_text", _raise_on_write)

        window._log_checkpoint_checklist()

        assert len(events) == 2
        assert events[1]["event_code"] == "BQ_CHECKPOINT_CHECKLIST_WRITE_FAILED"
        assert "simulated disk full" in events[1]["event_message"]


class TestFeatureBqDiagnosticsLiveViewDuringSession:
    """Feature (2026-08-23, felhasználói kérés): a BQ Diagnosztika fül
    session alatt (TRADITIONAL_LEARNING/GOLDEN_GG) passzív élő nézetre
    vált — a session saját, periodikus gauge-lekérdezéséből (ctrl.
    on_gauge_sample) táplálva a gauge_snapshot_ready Qt-signalon keresztül,
    nem önálló kapcsolattal."""

    def test_golden_gg_ctrl_factory_wires_on_gauge_sample(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.72,
        )

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._golden_gg_ctrl_factory()

        assert ctrl.on_gauge_sample is not None

    def test_traditional_learning_ctrl_factory_wires_on_gauge_sample(
        self, qapp, monkeypatch, tmp_path
    ):
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._traditional_learning_ctrl_factory()

        assert ctrl.on_gauge_sample is not None

    def test_golden_gg_decisive_snapshot_reaches_logger(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Kor 18, Batch C7 R17-16] a ctrl.on_gauge_sample ->
        gauge_snapshot_ready -> logger.log_bq_diagnostics() wiring-lanc
        helyesseget igazolja -- a PSU/Load hardver-bringup hianya miatt
        NEM a valos _run_precond()-ot futtatja (azt a golden_gg_controller.
        py::_emit_gauge_sample_best_effort() hivasat a nem-GUI, mutacioval
        igazolt unit-tesztek fedik le, l. test_golden_gg_controller.py::
        TestPrecondAndCharge::test_precond_gg_source_snapshot_emits_
        on_gauge_sample), csak kozvetlenul hivja a callbacket, ugyanugy,
        mint a periodikus poll-ut mar bizonyitottan teszi."""
        from types import SimpleNamespace
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="GOLDEN_GG", bq_port="COM8", golden_gg_system_rate_current_A=0.72,
        )

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._golden_gg_ctrl_factory()

        log_calls = []
        window._worker = SimpleNamespace(
            runner=SimpleNamespace(
                logger=SimpleNamespace(log_bq_diagnostics=log_calls.append)
            )
        )

        # Kozvetlen callback-hivas (a ChemID GUI-teszt mintaja) -- a valos
        # bring-up (connect_all()) nem resze e tesztnek, csak a wiring
        # lanc (ctrl.on_gauge_sample -> gauge_snapshot_ready -> logger.
        # log_bq_diagnostics()) helyessege.
        sentinel_snapshot = object()
        ctrl.on_gauge_sample(sentinel_snapshot)

        assert log_calls == [sentinel_snapshot]

    def test_traditional_learning_decisive_snapshot_reaches_logger(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kor 18, Batch C7 R17-16] a TraditionalLearning
        Controller INITIAL_SNAPSHOT-ja a valos gauge_snapshot_ready
        Qt-signal lancon at eljut a logger.log_bq_diagnostics()-ig."""
        from types import SimpleNamespace
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._traditional_learning_ctrl_factory()

        log_calls = []
        window._worker = SimpleNamespace(
            runner=SimpleNamespace(
                logger=SimpleNamespace(log_bq_diagnostics=log_calls.append)
            )
        )

        ctrl._run_initial_snapshot()

        assert len(log_calls) == 1
        assert log_calls[0] is ctrl._initial_snapshot

    def test_build_runner_switches_panel_to_session_owned(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        self._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        window._build_runner(cfg)

        assert window._bq_diagnostics_panel._session_owned is True
        assert window._bq_diagnostics_panel._connect_btn.isEnabled() is False

    def test_gauge_snapshot_ready_signal_reaches_panel(self, qapp, monkeypatch, tmp_path):
        """A gauge_snapshot_ready.emit(...) (worker-szálról hívva a valós
        session alatt) a GUI-szálon a BqDiagnosticsPanel.update_snapshot()-ot
        hívja — itt szinkron emit()-tel (qapp event loop nem fut a teszt
        alatt, de a same-thread emit szinkron slot-hívássá egyszerűsödik)."""
        from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
        from Prog.src.bq_diagnostics import poll_bq_diagnostics
        from Prog.tests.mock_drivers.mock_bq34z110_transport import (
            MockBq34z110Transport,
        )

        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        transport = MockBq34z110Transport()
        driver = Bq34z110ReadOnlyDriver(transport=transport)
        transport.set_register(driver.STD_CMD_VOLTAGE, 12700)
        snapshot = poll_bq_diagnostics(driver)

        # R1 F-29 előfeltétel: a valós úton a _build_runner() session-módba
        # állítja a panelt (l. test_build_runner_switches_panel_to_session_owned).
        window._bq_diagnostics_panel.set_session_owned(True, session_label="GOLDEN_GG")
        window.gauge_snapshot_ready.emit(snapshot)

        assert window._bq_diagnostics_panel._value_labels["voltage_mV"].text() == "12700"

    def test_close_session_bq_transport_restores_panel_to_standalone_mode(
        self, qapp, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()

        class _FakeTransport:
            def disconnect(self):
                pass

        window._session_owns_bq_transport = True
        window._session_bq_transport = _FakeTransport()
        window._session_bq_serial = object()
        window._bq_diagnostics_panel.set_session_owned(True, session_label="GOLDEN_GG")

        window._close_session_bq_transport()

        assert window._bq_diagnostics_panel._connect_btn.isEnabled() is True
        assert window._bq_diagnostics_panel._session_owned is False

    def _patch_bq_serial(self, monkeypatch):
        import serial  # type: ignore[import-untyped]

        class _FakeSerial:
            def __init__(self, *a, **k):
                pass

            def close(self):
                pass

        monkeypatch.setattr(serial, "Serial", _FakeSerial)

        from Prog.drivers.pico_bq_transport import PicoBQTransport
        monkeypatch.setattr(PicoBQTransport, "negotiate", lambda self, *a, **k: None)
        return _FakeSerial


# ------------------------------------------------------------------ #
# Governance-v2 review-kör 1 (CF-07, DFAM-011+012, batch 4.2): a       #
# controllerek SAJÁT on_event-je korábban KIZÁRÓLAG a loggerbe írt —  #
# a runner.on_event lánc (worker._handle_event -> event_ready ->      #
# LivePanel.append_event) sosem kapta meg, tehát az operátor élőben   #
# nem látta ezeket az eseményeket (csak session után, a logfájlban).  #
# Kritikus, mert a batch 4.1 CF-01 fix CONTROLLER_ABORT_FAILED/       #
# EMERGENCY_STOP eseményei is emiatt maradtak volna láthatatlanok.    #
# ------------------------------------------------------------------ #

class TestCf07ControllerEventsReachLivePanel:
    @staticmethod
    def _patch_bq_serial(monkeypatch):
        import serial  # type: ignore[import-untyped]

        class _FakeSerial:
            def __init__(self, *a, **k):
                pass

            def close(self):
                pass

        monkeypatch.setattr(serial, "Serial", _FakeSerial)
        from Prog.drivers.pico_bq_transport import PicoBQTransport
        monkeypatch.setattr(PicoBQTransport, "negotiate", lambda self, *a, **k: None)

    def test_logger_failure_does_not_prevent_live_panel_forwarding(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, MAX verifier F-3 (CF-07 utóellenőrzés, batch 4.2)]
        a `_forward_controller_event()` a testvér batch 4.1 V-7 mintáját
        követve a `logger.log_event()`-et try/except-be csomagolja — egy
        itteni I/O hiba enélkül megakadályozta volna a `runner.on_event`
        felé történő továbbítást is, pont akkor hagyva az operátort
        jelzés nélkül, amikor a naplózó réteg is meghibásodott."""
        from Prog.src.test_runner import StepKind, TestStep

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()

        runner, _plan, _ = window._build_runner(cfg)
        received = []
        runner.on_event = received.append
        step = TestStep(StepKind.RELAX, "relax_after_charge")
        ctrl = runner._relax_ctrl_factory(step)

        def _raise_on_log_event(*a, **k):
            raise OSError("simulated disk full")
        import Prog.src.logger as logger_module
        monkeypatch.setattr(logger_module.Logger, "log_event", _raise_on_log_event)

        event = {"event_code": "RELAX_DMM_FAULT", "event_message": "x"}
        ctrl.on_event(event)  # nem szabad kivételt dobnia

        assert received == [event]

    def test_critical_severity_event_forwarded_as_is_critical_to_logger(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, review-kör 2 (2026-09-09), friss Szoftver/
        Architektúra F5 (DFAM-001)] a controller-eseményekben szereplő
        `severity: 'CRITICAL'` (pl. RelaxController::RELAX_TEMPERATURE_
        DMM_FAULT) korábban eldobódott a _forward_controller_event()
        logger.log_event()-hívásánál -- az events.csv is_critical=False-t
        rögzített egy elvben kritikus eseményre, holott az élő panel
        (event_log_widget.py) a severity-t helyesen pirosra színezte."""
        from Prog.src.test_runner import StepKind, TestStep

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()

        runner, _plan, _ = window._build_runner(cfg)
        step = TestStep(StepKind.RELAX, "relax_after_charge")
        ctrl = runner._relax_ctrl_factory(step)

        log_calls = []
        import Prog.src.logger as logger_module
        monkeypatch.setattr(
            logger_module.Logger, "log_event",
            lambda self, event_code, message, is_critical=False: log_calls.append(
                (event_code, is_critical)
            ),
        )

        ctrl.on_event({
            "event_code": "RELAX_TEMPERATURE_DMM_FAULT",
            "event_message": "Hőmérséklet-DMM kiesés relaxáció közben",
            "severity": "CRITICAL",
        })
        assert log_calls == [("RELAX_TEMPERATURE_DMM_FAULT", True)]

        log_calls.clear()
        ctrl.on_event({"event_code": "RELAX_DMM_FAULT", "event_message": "x"})
        assert log_calls == [("RELAX_DMM_FAULT", False)]

    def test_relax_ctrl_factory_forwards_on_event_to_runner(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import StepKind, TestStep

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()

        runner, _plan, _ = window._build_runner(cfg)
        received = []
        runner.on_event = received.append
        step = TestStep(StepKind.RELAX, "relax_after_charge")
        ctrl = runner._relax_ctrl_factory(step)

        event = {"event_code": "RELAX_DMM_FAULT", "event_message": "x"}
        ctrl.on_event(event)

        assert received == [event]

    def test_relax_ctrl_duration_uses_structured_relax_purpose_not_label_text(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Governance-v2 review-kör 1 (CF-33/DFAM-025
        implicit_dispatch_on_unstructured_label, batch 4.5)] a relax-
        időtartam korábban a `step.label` SZÖVEGÉBEN keresett "discharge"
        substringgel dőlt el — egy tetszőleges, a "discharge" szót NEM
        tartalmazó címke (pl. egy jövőbeli átnevezés) a rövidebb (2h)
        alapértelmezett relax-időt kapta volna a szükséges 5h helyett,
        annak ellenére, hogy a step szemantikailag kisütés utáni relax.
        A `relax_purpose="post_discharge"` mező EXPLICIT, a label
        szövegétől független jelző."""
        from Prog.src.test_runner import StepKind, TestStep

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()

        runner, _plan, _ = window._build_runner(cfg)

        # A címke SZÁNDÉKOSAN nem tartalmazza a "discharge" szót, mégis
        # post_discharge célú relax -- ez a diszkrimináló eset.
        step = TestStep(StepKind.RELAX, "arbitrary_relabeled_step", relax_purpose="post_discharge")
        ctrl = runner._relax_ctrl_factory(step)
        assert ctrl._config.min_relax_s == pytest.approx(18000.0)

        # Fordítva: "discharge" szót TARTALMAZÓ címke, de relax_purpose
        # NINCS post_discharge-re állítva -- a rövid, NEM a hosszú relax-
        # időt kell kapnia, NEM a címke szövege alapján. [Kör 19 Batch D4,
        # SW19-01 javítás után: `_make_cfg()` alapértelmezett test_type-ja
        # CHARACTERIZATION, aminek "relax_after_charge" mezőcsoportja
        # LÁTHATÓ ui_policy.py-ban -- a rövid ág mostantól a KONFIGURÁLT
        # cfg.relax_after_charge_s-t adja (SessionConfig alapértéke
        # 600.0), NEM a hardcode-olt 7200.0-t -- l. a lenti, kifejezetten
        # erre a dispatchre írt teszteket a teljes indoklásért.]
        step2 = TestStep(StepKind.RELAX, "post_discharge_something", relax_purpose="")
        ctrl2 = runner._relax_ctrl_factory(step2)
        assert ctrl2._config.min_relax_s == pytest.approx(600.0)

    def test_relax_ctrl_characterization_honors_configured_relax_after_charge_s(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kör 19 Batch D4, SW19-01] a `_make_relax_ctrl()`
        korábban `elif cfg.test_type == "CHARGE_ONLY":` ágon dispatch-elt
        -- CHARACTERIZATION módban ui_policy.py SZERINT a "relax_after_
        charge" mezőcsoport UGYANÚGY látható/szerkeszthető (a Start-
        dialógus meg is erősíti az operátor által beállított értéket),
        de a régi `elif` ezt SOHA nem ismerte fel, mindig a hardcode-olt
        7200.0-ra esve -- 12x eltérés az operátor 600.0-ás GUI-
        alapértékénél, holott a Start-dialógus a FIGYELMEN KÍVÜL HAGYOTT
        értéket erősítette meg. Ez a teszt egy NEM-alapértelmezett
        relax_after_charge_s-t állít be, hogy a diszkrimináció ne
        véletlenül essen egybe egy alapértékkel."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHARACTERIZATION", relax_after_charge_s=1234.0)

        runner, _plan, _ = window._build_runner(cfg)
        from Prog.src.test_runner import StepKind, TestStep
        step = TestStep(StepKind.RELAX, "relax_after_charge", relax_purpose="")
        ctrl = runner._relax_ctrl_factory(step)

        assert ctrl._config.min_relax_s == pytest.approx(1234.0)

    def test_relax_ctrl_charge_only_still_honors_configured_relax_after_charge_s(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kör 19 Batch D4, SW19-01] a régi, explicit
        `cfg.test_type == "CHARGE_ONLY"` ág is helyesen működött -- ez a
        teszt bizonyítja, hogy az új, ui_policy-alapú dispatch NEM
        regresszált a CHARGE_ONLY esetre."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHARGE_ONLY", relax_after_charge_s=999.0)

        runner, _plan, _ = window._build_runner(cfg)
        from Prog.src.test_runner import StepKind, TestStep
        step = TestStep(StepKind.RELAX, "relax_after_charge", relax_purpose="")
        ctrl = runner._relax_ctrl_factory(step)

        assert ctrl._config.min_relax_s == pytest.approx(999.0)

    def test_relax_ctrl_post_discharge_purpose_unaffected_by_test_type(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kör 19 Batch D4, SW19-01] a `relax_purpose ==
        "post_discharge"` ág elsőbbséget élvez, FÜGGETLENÜL a test_type-
        tól/a ui_policy-alapú kapunk -- ez mindkét (CHARACTERIZATION és
        CHARGE_ONLY) módban is a fix 18000.0-t kell adja, nem a
        konfigurált relax_after_charge_s-t."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHARACTERIZATION", relax_after_charge_s=1234.0)

        runner, _plan, _ = window._build_runner(cfg)
        from Prog.src.test_runner import StepKind, TestStep
        step = TestStep(StepKind.RELAX, "relax_after_discharge", relax_purpose="post_discharge")
        ctrl = runner._relax_ctrl_factory(step)

        assert ctrl._config.min_relax_s == pytest.approx(18000.0)

    def test_chemid_ctrl_factory_forwards_on_event_to_runner(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHEMID_CHARACTERIZATION_PBA")

        runner, _plan, _ = window._build_runner(cfg)
        received = []
        runner.on_event = received.append
        ctrl = runner._chemid_ctrl_factory()

        event = {"event_code": "CHEMID_TEST_EVENT", "event_message": "x"}
        ctrl.on_event(event)

        assert received == [event]

    def test_chemid_relax_dmm_fault_event_reaches_logger(self, qapp, monkeypatch, tmp_path):
        """[regresszió, Kor 18, Batch C7 R17-14] a ChemID inline relax
        DMM-hiba esemenye a valos _forward_controller_event() lancon
        (ctrl.on_event -> logger.log_event) at eljut a loggerig -- a
        ctrl.on_event(...) direkt hivas, NEM worker/Qt-signal-lanc (azt a
        mar meglevo test_chemid_ctrl_factory_forwards_on_event_to_runner
        fedi le); ugyanaz a mintak, mint a RelaxController/OcvSocController
        testvereire mar bizonyitott (l. test_critical_severity_event_
        forwarded_...)."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="CHEMID_CHARACTERIZATION_PBA")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._chemid_ctrl_factory()

        log_calls = []
        import Prog.src.logger as logger_module
        monkeypatch.setattr(
            logger_module.Logger, "log_event",
            lambda self, event_code, message, is_critical=False: log_calls.append(
                (event_code, is_critical)
            ),
        )

        ctrl.on_event({
            "event_code": "CHEMID_RELAX_DMM_FAULT",
            "event_message": "DMM olvasási hiba relaxáció közben (ChemID RELAX_5H_FULL, hiba #1)",
        })

        assert ("CHEMID_RELAX_DMM_FAULT", False) in log_calls

    def test_traditional_learning_ctrl_factory_forwards_on_event_to_runner(
        self, qapp, monkeypatch, tmp_path
    ):
        self._patch_bq_serial(monkeypatch)
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", bq_port="COM8")

        runner, _plan, _ = window._build_runner(cfg)
        received = []
        runner.on_event = received.append
        ctrl = runner._traditional_learning_ctrl_factory()

        event = {"event_code": "LEARNING_BL_THRESHOLD_RESTORE_FAILED", "event_message": "x"}
        ctrl.on_event(event)

        assert received == [event]

    def test_ocv_soc_ctrl_factory_has_on_event_and_forwards_to_runner(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, MAX verifier CF-07] az OcvSocController korábban
        EGYÁLTALÁN NEM rendelkezett on_event-tel — a relax-fázis DMM-
        kiesése élőben láthatatlan maradt, csak a session-végi
        relax_dmm_fault_total számlálóban jelent meg."""
        _patch_hardware(monkeypatch)
        _use_hil_verified_ocv_soc_pulse_interval(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._ocv_soc_ctrl_factory()
        assert ctrl.on_event is not None

        received = []
        runner.on_event = received.append
        event = {"event_code": "OCV_SOC_RELAX_DMM_FAULT", "event_message": "x"}
        ctrl.on_event(event)

        assert received == [event]

    def test_iec_dc_resistance_ctrl_factory_forwards_on_event_to_runner(
        self, qapp, monkeypatch, tmp_path
    ):
        """Governance-v2 kör 2: az IecDcResistanceController on_event-je
        a testvér ctrl_factory-k (chemid/traditional_learning/ocv_soc)
        mintáját követi -- az i10_discharge_current_A a profil YAML-ból
        jön, l. lenti battery_profiles/FIAMM_12V.yaml felülírás."""
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        profiles_dir = tmp_path / "Prog" / "config" / "battery_profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "FIAMM_12V.yaml").write_text(
            "chemistry: AGM_VRLA\nmanufacturer: FIAMM\ncell_count: 6\n"
            "nominal_voltage_V: 12.0\ni10_discharge_current_A: 0.62\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="IEC_DC_INTERNAL_RESISTANCE")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._iec_dc_resistance_ctrl_factory()
        assert ctrl.on_event is not None
        assert ctrl.on_result is not None

        received = []
        runner.on_event = received.append
        event = {"event_code": "IEC_DC_RESISTANCE_EVENT", "event_message": "x"}
        ctrl.on_event(event)

        assert received == [event]

    def test_iec_dc_resistance_ctrl_factory_raises_without_profile_i10(
        self, qapp, monkeypatch, tmp_path
    ):
        """[Kör 21 F17, SRC21-04 sibling-korrekció] a valós
        Prog/config/battery_profiles/FIAMM_12V.yaml 2026-09-12 óta
        TARTALMAZ i10_discharge_current_A-t (0,67A, l. CLAIM_LEDGER.yaml
        BAT-FG20721-I10-001) -- ez a teszt korábban hallgatólagosan ARRA
        a valós fájlra hagyatkozott (csak `exe_dir`-t patch-elte, a
        `bundle_dir()`-en át olvasott profile_yaml_path-ot NEM, l.
        main_window.py:3500-3502), így a valós fájl i10-mezőjének
        hozzáadása után hamisan zöld maradt (a hívás már nem dob).
        Explicit, önálló profillal ellenőrizzük: a controller
        konstruktora ConfigurationError-t dob, ha a PROFIL nem
        tartalmaz i10_discharge_current_A-t, nem találgat nominal_
        capacity_Ah/10-ből."""
        from Prog.src.exceptions import ConfigurationError

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        profiles_dir = tmp_path / "Prog" / "config" / "battery_profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "FIAMM_12V.yaml").write_text(
            "chemistry: AGM_VRLA\nmanufacturer: FIAMM\ncell_count: 6\n"
            "nominal_voltage_V: 12.0\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "bundle_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="IEC_DC_INTERNAL_RESISTANCE")

        runner, _plan, _ = window._build_runner(cfg)

        with pytest.raises(ConfigurationError):
            runner._iec_dc_resistance_ctrl_factory()

    def test_ocv_soc_controller_fires_on_event_on_relax_dmm_fault(self):
        """[regresszió, MAX verifier CF-07] az OcvSocController belső
        _run_relax_phase()-a ténylegesen meghívja a friss on_event-et DMM-
        kiesésnél a relax fázisban — a RelaxController RELAX_DMM_FAULT
        mintáját követve. Diszkrimináló: az OcvSocController::__init__
        on_event hozzáadása/on_event hívása nélkül ez a teszt bukna
        (AttributeError vagy soha meg nem hívott spy)."""
        from unittest.mock import MagicMock

        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.ocv_soc_controller import OcvSocConfig, OcvSocController
        from Prog.src.safety import PsuMode, SafetyManager

        profile = BatteryProfile(
            battery_name="Test", manufacturer="FIAMM", model="FG",
            nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.0,
        )
        safety = SafetyManager(profile=profile, psu_mode=PsuMode.INDEPENDENT)
        psu, load = MagicMock(), MagicMock()
        dmm_v, dmm_t = MagicMock(), MagicMock()
        dmm_t.read_temperature.return_value = 25.0
        ctrl = OcvSocController(psu, load, dmm_v, dmm_t, profile, safety, OcvSocConfig())

        events = []
        ctrl.on_event = events.append
        ctrl._dmm_valid = False  # DMM-kiesést szimulálunk a relax fázisban

        ctrl._run_relax_phase(dt_s=1.0)

        assert len(events) == 1
        assert events[0]["event_code"] == "OCV_SOC_RELAX_DMM_FAULT"


class TestR3B3ProducerSeverityReachesLivePanel:
    """[regresszió, R3-B3 (R3-GUI-1, P3), DFAM-018 RECURRING] teljes GUI-lánc
    a VALÓDI `_build_runner()` factory-controlleréből: producer ->
    `_forward_controller_event()` -> runner.on_event ->
    `TestRunnerWorker._handle_event()` -> `event_ready` signal ->
    LivePanel eseménynapló. A semleges kódú, de negatív jelentésű
    LEARNING_DONE (nem VERIFIED) és OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY
    korábban INFO-ként, színtelenül jelent meg; a producer explicit WARNING
    severity-je sárga hátteret ad. A perzisztens is_critical=False
    változatlan. Unit/offscreen GUI bizonyíték, HIL nem kell."""

    @staticmethod
    def _window_runner(monkeypatch, tmp_path, **cfg_overrides):
        from Prog.gui.worker import TestRunnerWorker

        _patch_hardware(monkeypatch)
        TestCf07ControllerEventsReachLivePanel._patch_bq_serial(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        runner, _plan, _ = window._build_runner(_make_cfg(**cfg_overrides))
        # runner.on_event = worker._handle_event (a worker referenciáját a
        # hívó tartja meg, különben a signal-forrás felszabadulhat).
        worker = TestRunnerWorker(runner, None)
        worker.event_ready.connect(window._live_panel.append_event)
        return window, runner, worker

    @staticmethod
    def _items(window, code):
        lst = window._live_panel._event_log._list
        needle = " " + code + " "
        return [lst.item(i) for i in range(lst.count()) if needle in lst.item(i).text()]

    @staticmethod
    def _finish_learning(monkeypatch, window, ctrl):
        """Q-38: a factory-controller valodi, korlatos zaro toltesi utja."""
        import dataclasses

        from Prog.src.traditional_learning_controller import LearningState
        from Prog.tests.test_traditional_learning_controller import (
            _b23b_finish_recharge,
            make_controller,
        )

        # Csak az I/O-fuggosegek mockok; a factory es az on_event-lanc valodi.
        io_ctrl, *_ = make_controller()
        for name in ("_psu", "_load", "_dmm_v", "_dmm_t", "_bq_transport", "_bq_driver"):
            monkeypatch.setattr(ctrl, name, getattr(io_ctrl, name))
        ctrl._final_charge_config = dataclasses.replace(
            ctrl._final_charge_config, fast_loop_enabled=False,
        )
        ctrl._tlc_completion["effective_charge_config"] = dataclasses.asdict(ctrl._final_charge_config)
        checkpoints = []
        ctrl.persist_tlc_completion = checkpoints.append
        ctrl._enter(LearningState.FINAL_SNAPSHOT)
        ctrl.advance(0.0)
        assert ctrl.state == LearningState.FINAL_RECHARGE, ctrl.fault_reason
        assert not ctrl._psu.called("output_on")
        assert TestR3B3ProducerSeverityReachesLivePanel._items(window, "LEARNING_DONE") == []
        _b23b_finish_recharge(ctrl, max_ticks=20)
        assert ctrl._psu.called("output_on"), "A zaro toltes tenylegesen elindult"
        assert [record["final_recharge"]["status"] for record in checkpoints] == [
            "NOT_STARTED", "RUNNING", "COMPLETED",
        ]
        assert checkpoints[-1]["final_recharge"]["off_readback"] == {"Load": False, "PSU": False}
        assert not ctrl._load.input_commanded_on and not ctrl._psu.output_commanded_on

    def test_non_verified_learning_done_is_yellow_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.src.traditional_learning_controller import LEARNING_RESULT_VERIFIED

        window, runner, _worker = self._window_runner(
            monkeypatch, tmp_path, test_type="TRADITIONAL_LEARNING", bq_port="COM8",
        )
        log_calls = []
        real_log_event = Logger.log_event

        def _spy_log_event(self, event_code, message, is_critical=False):
            log_calls.append((event_code, is_critical))
            return real_log_event(self, event_code, message, is_critical=is_critical)

        monkeypatch.setattr(Logger, "log_event", _spy_log_event)
        ctrl = runner._traditional_learning_ctrl_factory()
        self._finish_learning(monkeypatch, window, ctrl)

        assert ctrl.result_summary.result != LEARNING_RESULT_VERIFIED
        items = self._items(window, "LEARNING_DONE")
        assert len(items) == 1
        assert "WARNING" in items[0].text()
        assert items[0].background().color().name() == "#fff8e1"
        # A perzisztens szemantika változatlan: a forwarder csak CRITICAL-ra
        # ír is_critical=True-t.
        assert [c for c in log_calls if c[0] == "LEARNING_DONE"] == [
            ("LEARNING_DONE", False)
        ]

    def test_verified_learning_done_stays_info_without_background(
        self, qapp, monkeypatch, tmp_path
    ):
        """Negatív kontroll: VERIFIED eredménynél nincs explicit severity --
        INFO, háttér nélkül, a Q-38 szerinti igazolt zaro toltes utan."""
        import dataclasses

        from PySide6.QtCore import Qt

        from Prog.src.traditional_learning_controller import LEARNING_RESULT_VERIFIED

        window, runner, _worker = self._window_runner(
            monkeypatch, tmp_path, test_type="TRADITIONAL_LEARNING", bq_port="COM8",
        )
        log_calls = []
        real_log_event = Logger.log_event

        def _spy_log_event(self, event_code, message, is_critical=False):
            log_calls.append((event_code, is_critical))
            return real_log_event(self, event_code, message, is_critical=is_critical)

        monkeypatch.setattr(Logger, "log_event", _spy_log_event)
        ctrl = runner._traditional_learning_ctrl_factory()
        real_classify = ctrl._classify_result
        monkeypatch.setattr(
            ctrl, "_classify_result",
            lambda: dataclasses.replace(real_classify(), result=LEARNING_RESULT_VERIFIED),
        )
        self._finish_learning(monkeypatch, window, ctrl)

        items = self._items(window, "LEARNING_DONE")
        assert len(items) == 1
        assert "INFO" in items[0].text()
        assert items[0].background().style() == Qt.BrushStyle.NoBrush
        assert [c for c in log_calls if c[0] == "LEARNING_DONE"] == [
            ("LEARNING_DONE", False)
        ]

    def test_ocv_partial_energy_event_is_yellow_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.src.ocv_soc_controller import OcvSocState

        _use_hil_verified_ocv_soc_pulse_interval(monkeypatch)
        window, runner, _worker = self._window_runner(
            monkeypatch, tmp_path, test_type="OCV_SOC_CHARACTERIZATION",
        )
        ctrl = runner._ocv_soc_ctrl_factory()
        ctrl._state = OcvSocState.IMPULSE_ACQUIRE
        ctrl.emergency_stop("USER_EMERGENCY_STOP")

        items = self._items(window, "OCV_SOC_PULSE_ABORTED_PARTIAL_ENERGY")
        assert len(items) == 1
        assert "WARNING" in items[0].text()
        assert items[0].background().color().name() == "#fff8e1"


class TestR4B1ReboundAcceptedSeverityReachesLivePanel:
    """[regresszió, R4-B1 (P3), DFAM-018 RECURRING] teljes GUI-lánc a VALÓDI
    `_build_runner()` factory-controlleréből (a TestR3B3ProducerSeverity-
    ReachesLivePanel mintája): producer (`_run_rebound_check()` valódi
    elfogadási ága) -> `_forward_controller_event()` -> runner.on_event ->
    `TestRunnerWorker._handle_event()` -> `event_ready` signal -> LivePanel
    eseménynapló. retry > 0 (REBOUND_ACCEPTED_AFTER_RETRY quality flag):
    WARNING, sárga háttér; retry == 0: INFO, háttér nélkül. A perzisztens
    is_critical=False mindkét esetben változatlan. Unit/offscreen GUI
    bizonyíték, HIL nem kell."""

    @staticmethod
    def _accept_rebound(monkeypatch, tmp_path, retries):
        from Prog.src.traditional_learning_controller import LearningState

        window, runner, worker = TestR3B3ProducerSeverityReachesLivePanel._window_runner(
            monkeypatch, tmp_path, test_type="TRADITIONAL_LEARNING", bq_port="COM8",
        )
        log_calls = []
        real_log_event = Logger.log_event

        def _spy_log_event(self, event_code, message, is_critical=False):
            log_calls.append((event_code, is_critical))
            return real_log_event(self, event_code, message, is_critical=is_critical)

        monkeypatch.setattr(Logger, "log_event", _spy_log_event)
        ctrl = runner._traditional_learning_ctrl_factory()
        # Előállapot: REBOUND_CHECK_1, `retries` korábbi sikertelen próba,
        # a settle letelt, a mért rebound pontosan a célon -> a VALÓDI
        # elfogadási ág fut (dt_s=0.0: nincs BQ-poll).
        ctrl._state = LearningState.REBOUND_CHECK_1
        ctrl._rebound_retry_count = retries
        ctrl._phase_elapsed_s = ctrl._config.rebound_settle_s
        ctrl._dmm_valid = True
        ctrl._u_batt = (
            ctrl._config.learning_rebound_target_V_per_cell * ctrl._profile.cell_count
        )
        ctrl._run_rebound_check(0.0, LearningState.DISCHARGE_1, LearningState.RELAX_1)
        assert ctrl.state == LearningState.RELAX_1
        assert ctrl._rebound_retries_1 == retries
        assert any(
            f.startswith("REBOUND_ACCEPTED_AFTER_RETRY_AT_DISCHARGE_1(")
            for f in ctrl.quality_flags
        ) is (retries > 0)
        items = TestR3B3ProducerSeverityReachesLivePanel._items(
            window, "LEARNING_REBOUND_ACCEPTED"
        )
        assert len(items) == 1
        # A perzisztens szemantika változatlan: a forwarder csak CRITICAL-ra
        # ír is_critical=True-t.
        assert [c for c in log_calls if c[0] == "LEARNING_REBOUND_ACCEPTED"] == [
            ("LEARNING_REBOUND_ACCEPTED", False)
        ]
        return items[0], worker

    @pytest.mark.parametrize("retries", [1, 2], ids=["retry1", "retry2"])
    def test_rebound_accepted_after_retry_is_yellow_warning(
        self, qapp, monkeypatch, tmp_path, retries
    ):
        item, _worker = self._accept_rebound(monkeypatch, tmp_path, retries)
        assert "WARNING" in item.text()
        assert item.background().color().name() == "#fff8e1"

    def test_rebound_accepted_first_attempt_stays_info_without_background(
        self, qapp, monkeypatch, tmp_path
    ):
        """Negatív kontroll: retry == 0 -> nincs explicit severity -- INFO,
        háttér nélkül (a HEAD-en is átmegy, a fix nem rontja el)."""
        from PySide6.QtCore import Qt

        item, _worker = self._accept_rebound(monkeypatch, tmp_path, 0)
        assert "INFO" in item.text()
        assert item.background().style() == Qt.BrushStyle.NoBrush


class TestReviewRound4GuiF5DeviceErrorCounterSync:
    """[regresszió, review-kör 4/5 GUI F5 (P2, DFAM-012 rokona)] a
    DeviceErrorPanel "Törlés" gombja korábban csak a saját (háttérfül)
    listáját/számlálóját nullázta -- a LivePanel kompakt "Eszközhiba"
    számlálója nem értesült erről, tartósan eltérő értéket mutatva egy
    mid-session Törlés-kattintás után. Ez a teszt a TÉNYLEGES MainWindow
    bekötést fedi (nem csak az egyes panelek izolált unit tesztjét) --
    mindkét panel a konstruktorban jön létre, a kapcsolat is ott köttetik."""

    def test_device_error_panel_clear_resets_live_panel_counter(self, qapp, monkeypatch):
        window = MainWindow()
        window._live_panel.append_device_error({"device": "PSU", "error": "err"})
        assert window._live_panel._device_err_lbl.text() == "1"

        window._device_error_panel.clear()

        assert window._live_panel._device_err_lbl.text() == "0"


class TestC12PendingBqDfRestoreStartupWarning:
    """[teljes kódbázis review kör 7 (2026-09-15), C-12] a startup-review
    dialógusnak (`_review_incomplete_sessions_on_startup()`) fel kell
    tüntetnie a `checkpoint.json`-ban talált `pending_bq_df_restore`
    maradványt -- ez az egyetlen operátor-szintű jelzés arról, hogy a
    fizikai BQ34Z110 chip valószínűleg ideiglenes learning-értéken és
    unsealed állapotban maradt egy megszakadt session után."""

    def test_dialog_text_contains_pending_bq_df_restore_warning(self, qapp, monkeypatch, tmp_path):
        import json

        from Prog.gui import main_window as main_window_module

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20260915_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "status": "RUNNING",
                "step": "precharge",
                "safe_boundary": False,
                "pending_bq_df_restore": {
                    "field": "cell_bl_set_volt_mV",
                    "original_mV": 1950,
                    "restore_confirmed": None,
                    "bq_unsealed": True,
                },
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        captured = {}

        def _fake_information(parent, title, text):
            captured["text"] = text
            captured["severity"] = "information"

        def _fake_warning(parent, title, text):
            captured["text"] = text
            captured["severity"] = "warning"

        monkeypatch.setattr(
            main_window_module.QMessageBox, "information", _fake_information
        )
        # Kör 19 D8e (GUI-R19-04, DFAM-018): a dialógus mostantól .warning()-ot
        # hív, ha a felsorolt sessionök bármelyike ⚠-jelölt (safety-releváns)
        # sort hordoz -- mindkét konstruktort patch-eljük, hogy a tartalom-
        # alapú asszerciók a hívott konstruktortól függetlenül működjenek,
        # és a `severity` kulcs alapján külön ellenőrizhető legyen, MELYIK
        # tüzelt (l. lent az egyes tesztek saját asszercióit).
        monkeypatch.setattr(
            main_window_module.QMessageBox, "warning", _fake_warning
        )

        MainWindow()

        assert "text" in captured, "a dialogus nem jelent meg egy felfedezett megszakadt sessionre"
        assert "1950" in captured["text"]
        assert "BQ Data Flash MARADVÁNY" in captured["text"]
        # Kör 19 D8e (GUI-R19-04): BQ DF-maradvány -- CRITICAL safety-tartalom,
        # .warning()-on kell mennie, nem .information()-ön.
        assert captured["severity"] == "warning"

    def test_dialog_text_omits_warning_when_no_pending_restore(self, qapp, monkeypatch, tmp_path):
        import json

        from Prog.gui import main_window as main_window_module

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20260915_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "status": "RUNNING", "step": "relax", "safe_boundary": True,
                # Kör 19 D8e (GUI-R19-04): explicit, TELJESEN igazolt safe-off
                # -- e nélkül a hiányzó last_safe_off_result kulcs ÖNMAGÁBAN
                # is ⚠-jelölt "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" sort váltana ki,
                # ami ezt a tesztet (aminek célja a BQ DF-sor HIÁNYÁNAK
                # elszigetelt vizsgálata) hamisan .warning()-útra terelné.
                "last_safe_off_result": {"PSU": True, "Load": True, "DMM_V": True, "DMM_T": True},
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        captured = {}

        def _fake_information(parent, title, text):
            captured["text"] = text
            captured["severity"] = "information"

        def _fake_warning(parent, title, text):
            captured["text"] = text
            captured["severity"] = "warning"

        monkeypatch.setattr(
            main_window_module.QMessageBox, "information", _fake_information
        )
        # Kör 19 D8e (GUI-R19-04, DFAM-018): a dialógus mostantól .warning()-ot
        # hív, ha a felsorolt sessionök bármelyike ⚠-jelölt (safety-releváns)
        # sort hordoz -- mindkét konstruktort patch-eljük, hogy a tartalom-
        # alapú asszerciók a hívott konstruktortól függetlenül működjenek,
        # és a `severity` kulcs alapján külön ellenőrizhető legyen, MELYIK
        # tüzelt (l. lent az egyes tesztek saját asszercióit).
        monkeypatch.setattr(
            main_window_module.QMessageBox, "warning", _fake_warning
        )

        MainWindow()

        assert "text" in captured
        assert "BQ Data Flash MARADVÁNY" not in captured["text"]
        # Kör 19 D8e (GUI-R19-04): nincs ⚠-jelölt sor -- .information() marad.
        assert captured["severity"] == "information"


class TestR211bPendingBqUnsealedStartupWarning:
    """R2-11b (2026-09-15, Teljes kódbázis review 2. kör, 14. writer-kör,
    DFAM-016 + DFAM-026): ugyanaz a startup-review dialógus, KÜLÖN,
    SZÁNDÉKOSAN ELKÜLÖNÍTETT sorral a GoldenGgController tisztán
    hozzáférés-állapotbeli (BQ SEAL) maradványára -- l. a testvér
    TestC12PendingBqDfRestoreStartupWarning osztály ugyanerről a
    mintáról a TraditionalLearning Cell BL Data Flash maradványra."""

    def test_dialog_text_contains_pending_bq_unsealed_warning(self, qapp, monkeypatch, tmp_path):
        import json

        from Prog.gui import main_window as main_window_module

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20260915_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "status": "RUNNING",
                "step": "system_rate_discharge",
                "safe_boundary": False,
                "pending_bq_unsealed": {
                    "controller": "GOLDEN_GG",
                    "reason": "SOURCE_SNAPSHOT_READ_REQUIRES_UNSEALED",
                    "bq_unsealed": True,
                },
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        captured = {}

        def _fake_information(parent, title, text):
            captured["text"] = text
            captured["severity"] = "information"

        def _fake_warning(parent, title, text):
            captured["text"] = text
            captured["severity"] = "warning"

        monkeypatch.setattr(
            main_window_module.QMessageBox, "information", _fake_information
        )
        # Kör 19 D8e (GUI-R19-04, DFAM-018): a dialógus mostantól .warning()-ot
        # hív, ha a felsorolt sessionök bármelyike ⚠-jelölt (safety-releváns)
        # sort hordoz -- mindkét konstruktort patch-eljük, hogy a tartalom-
        # alapú asszerciók a hívott konstruktortól függetlenül működjenek,
        # és a `severity` kulcs alapján külön ellenőrizhető legyen, MELYIK
        # tüzelt (l. lent az egyes tesztek saját asszercióit).
        monkeypatch.setattr(
            main_window_module.QMessageBox, "warning", _fake_warning
        )

        MainWindow()

        assert "text" in captured, "a dialogus nem jelent meg egy felfedezett megszakadt sessionre"
        assert "BQ SEAL MARADVÁNY" in captured["text"]
        assert "GOLDEN_GG" in captured["text"]
        # Kör 19 D8e (GUI-R19-04): BQ seal-maradvány -- .warning()-on kell mennie.
        assert captured["severity"] == "warning"

    def test_dialog_text_for_restored_cell_bl_reseal_unconfirmed_does_not_claim_df_unmodified(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, R1 F-17] a TLC CELL_BL_RESTORED_RESEAL_UNCONFIRMED
        maradványánál a Data Flash TÉNYLEGESEN módosult (temp-write, majd
        megerősített restore) -- a korábbi "Data Flash érték ezen az úton
        NEM módosult" mondat erre az útra HAMIS volt. Az új szöveg csak azt
        állítja, hogy nyitott (vissza nem állított) DF-maradvány nincs."""
        import json

        from Prog.gui import main_window as main_window_module

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20261026_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "status": "RUNNING",
                "step": "traditional_learning",
                "safe_boundary": False,
                "pending_bq_df_restore": None,
                "pending_bq_unsealed": {
                    "controller": "TRADITIONAL_LEARNING",
                    "reason": "CELL_BL_RESTORED_RESEAL_UNCONFIRMED",
                    "bq_unsealed": True,
                },
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        captured = {}

        def _fake_information(parent, title, text):
            captured["text"] = text
            captured["severity"] = "information"

        def _fake_warning(parent, title, text):
            captured["text"] = text
            captured["severity"] = "warning"

        monkeypatch.setattr(
            main_window_module.QMessageBox, "information", _fake_information
        )
        monkeypatch.setattr(
            main_window_module.QMessageBox, "warning", _fake_warning
        )

        MainWindow()

        assert "text" in captured, "a dialogus nem jelent meg egy felfedezett megszakadt sessionre"
        assert "BQ SEAL MARADVÁNY" in captured["text"]
        assert "CELL_BL_RESTORED_RESEAL_UNCONFIRMED" in captured["text"]
        assert "NEM módosult" not in captured["text"]
        assert (
            "Nyitott (vissza nem állított) Data Flash-érték-maradvány ezen az úton nincs"
            in captured["text"]
        )
        assert captured["severity"] == "warning"

    def test_dialog_text_omits_warning_when_no_pending_unsealed(
        self, qapp, monkeypatch, tmp_path
    ):
        import json

        from Prog.gui import main_window as main_window_module

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20260915_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "status": "RUNNING", "step": "relax", "safe_boundary": True,
                # Kör 19 D8e (GUI-R19-04): l. a testvér
                # test_dialog_text_omits_warning_when_no_pending_restore
                # azonos hivatkozású kommentje ugyanerről az indoklásról.
                "last_safe_off_result": {"PSU": True, "Load": True, "DMM_V": True, "DMM_T": True},
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        captured = {}

        def _fake_information(parent, title, text):
            captured["text"] = text
            captured["severity"] = "information"

        def _fake_warning(parent, title, text):
            captured["text"] = text
            captured["severity"] = "warning"

        monkeypatch.setattr(
            main_window_module.QMessageBox, "information", _fake_information
        )
        # Kör 19 D8e (GUI-R19-04, DFAM-018): a dialógus mostantól .warning()-ot
        # hív, ha a felsorolt sessionök bármelyike ⚠-jelölt (safety-releváns)
        # sort hordoz -- mindkét konstruktort patch-eljük, hogy a tartalom-
        # alapú asszerciók a hívott konstruktortól függetlenül működjenek,
        # és a `severity` kulcs alapján külön ellenőrizhető legyen, MELYIK
        # tüzelt (l. lent az egyes tesztek saját asszercióit).
        monkeypatch.setattr(
            main_window_module.QMessageBox, "warning", _fake_warning
        )

        MainWindow()

        assert "text" in captured
        assert "BQ SEAL MARADVÁNY" not in captured["text"]
        # Kör 19 D8e (GUI-R19-04): nincs ⚠-jelölt sor -- .information() marad.
        assert captured["severity"] == "information"

    def test_both_residuals_render_as_two_distinct_lines(self, qapp, monkeypatch, tmp_path):
        """DFAM-018 semantic-collapse-avoidance guard: a Cell BL Data
        Flash-maradvány és a BQ seal-állapot-maradvány KÉT FÜGGETLEN sor
        -- egy checkpoint, ami mindkettőt hordozza (elvben nem fordulhat
        elő egyszerre, mert a két mező két KÜLÖNBÖZŐ controller-típushoz
        tartozik, de a dialógus-renderelő kódnak akkor is FÜGGETLENÜL
        kell kezelnie őket), mindkét figyelmeztetést meg kell jelenítse,
        nem csak az egyiket."""
        import json

        from Prog.gui import main_window as main_window_module

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20260915_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "status": "RUNNING",
                "step": "system_rate_discharge",
                "safe_boundary": False,
                "pending_bq_df_restore": {
                    "field": "cell_bl_set_volt_mV",
                    "original_mV": 1950,
                    "restore_confirmed": None,
                    "bq_unsealed": True,
                },
                "pending_bq_unsealed": {
                    "controller": "GOLDEN_GG",
                    "reason": "SOURCE_SNAPSHOT_READ_REQUIRES_UNSEALED",
                    "bq_unsealed": True,
                },
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        captured = {}

        def _fake_information(parent, title, text):
            captured["text"] = text
            captured["severity"] = "information"

        def _fake_warning(parent, title, text):
            captured["text"] = text
            captured["severity"] = "warning"

        monkeypatch.setattr(
            main_window_module.QMessageBox, "information", _fake_information
        )
        # Kör 19 D8e (GUI-R19-04, DFAM-018): a dialógus mostantól .warning()-ot
        # hív, ha a felsorolt sessionök bármelyike ⚠-jelölt (safety-releváns)
        # sort hordoz -- mindkét konstruktort patch-eljük, hogy a tartalom-
        # alapú asszerciók a hívott konstruktortól függetlenül működjenek,
        # és a `severity` kulcs alapján külön ellenőrizhető legyen, MELYIK
        # tüzelt (l. lent az egyes tesztek saját asszercióit).
        monkeypatch.setattr(
            main_window_module.QMessageBox, "warning", _fake_warning
        )

        MainWindow()

        assert "text" in captured
        assert "BQ Data Flash MARADVÁNY" in captured["text"]
        assert "BQ SEAL MARADVÁNY" in captured["text"]
        # Kör 19 D8e (GUI-R19-04): mindkét maradvány ⚠-jelölt -- .warning().
        assert captured["severity"] == "warning"

    def test_dialog_text_renders_tlc_controller_name_with_zero_main_window_diff(
        self, qapp, monkeypatch, tmp_path
    ):
        """[regresszió, Kör 17 MAX-effort review, R17-08] a startup-
        dialógus KÖZÖS, controller-agnosztikus renderelője (a `controller`/
        `reason` mezőket generikusan írja ki) -- ez a teszt empirikusan
        bizonyítja, hogy a TraditionalLearningController-forrású
        pending_bq_unsealed ugyanúgy megjelenik, mint a GOLDEN_GG-forrású,
        NULLA main_window.py-diff mellett (a fix csak a controller-oldali
        property-t adta hozzá, l. R17-08)."""
        import json

        from Prog.gui import main_window as main_window_module

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20260915_000000"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "status": "RUNNING",
                "step": "traditional_learning",
                "safe_boundary": False,
                "pending_bq_unsealed": {
                    "controller": "TRADITIONAL_LEARNING",
                    "reason": "PRECOND_DF_READ_REQUIRES_UNSEALED",
                    "bq_unsealed": True,
                },
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        captured = {}

        def _fake_information(parent, title, text):
            captured["text"] = text
            captured["severity"] = "information"

        def _fake_warning(parent, title, text):
            captured["text"] = text
            captured["severity"] = "warning"

        monkeypatch.setattr(
            main_window_module.QMessageBox, "information", _fake_information
        )
        # Kör 19 D8e (GUI-R19-04, DFAM-018): a dialógus mostantól .warning()-ot
        # hív, ha a felsorolt sessionök bármelyike ⚠-jelölt (safety-releváns)
        # sort hordoz -- mindkét konstruktort patch-eljük, hogy a tartalom-
        # alapú asszerciók a hívott konstruktortól függetlenül működjenek,
        # és a `severity` kulcs alapján külön ellenőrizhető legyen, MELYIK
        # tüzelt (l. lent az egyes tesztek saját asszercióit).
        monkeypatch.setattr(
            main_window_module.QMessageBox, "warning", _fake_warning
        )

        MainWindow()

        assert "text" in captured, "a dialogus nem jelent meg egy felfedezett megszakadt sessionre"
        assert "BQ SEAL MARADVÁNY" in captured["text"]
        assert "TRADITIONAL_LEARNING" in captured["text"]
        # DFAM-018: ez a checkpoint NEM hordoz pending_bq_df_restore-t --
        # a DF-maradvány sor NEM jelenhet meg egy tisztán seal-allapot
        # maradvany mellett.
        assert "BQ Data Flash MARADVÁNY" not in captured["text"]
        # Kör 19 D8e (GUI-R19-04): BQ seal-maradvány -- .warning()-on kell mennie.
        assert captured["severity"] == "warning"


class TestR1703LastSafeOffResultStartupWarning:
    """[regresszió, Kör 17 MAX-effort review, R17-03+R17-17+V4-1,
    DFAM-016+018+026] a startup-review dialógus KÜLÖN, SZÁNDÉKOSAN
    ELKÜLÖNÍTETT sorral a PSU/Load KIMENETI állapotának bizonytalanságára
    -- a fenti két BQ-sortól KÜLÖN (DFAM-018), mert eltérő fizikai
    kockázatot jeleznek (chip biztonsági állapota vs. energizált kimenet)."""

    def _make_checkpoint(self, tmp_path, **overrides) -> None:
        import json

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20260916_000000"
        session_dir.mkdir(parents=True)
        base = {"status": "RUNNING", "step": "system_rate_discharge", "safe_boundary": False}
        base.update(overrides)
        (session_dir / "checkpoint.json").write_text(
            json.dumps(base), encoding="utf-8",
        )

    def _capture_dialog(self, qapp, monkeypatch, tmp_path) -> dict:
        from Prog.gui import main_window as main_window_module

        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        captured = {}

        def _fake_information(parent, title, text):
            captured["text"] = text
            captured["severity"] = "information"

        def _fake_warning(parent, title, text):
            captured["text"] = text
            captured["severity"] = "warning"

        monkeypatch.setattr(
            main_window_module.QMessageBox, "information", _fake_information
        )
        # Kör 19 D8e (GUI-R19-04, DFAM-018): a dialógus mostantól .warning()-ot
        # hív, ha a felsorolt sessionök bármelyike ⚠-jelölt (safety-releváns)
        # sort hordoz -- mindkét konstruktort patch-eljük, hogy a tartalom-
        # alapú asszerciók a hívott konstruktortól függetlenül működjenek,
        # és a `severity` kulcs alapján külön ellenőrizhető legyen, MELYIK
        # tüzelt (l. lent az egyes tesztek saját asszercióit).
        monkeypatch.setattr(
            main_window_module.QMessageBox, "warning", _fake_warning
        )
        MainWindow()
        return captured

    def test_startup_dialog_warns_when_checkpoint_has_no_safe_off_record(
        self, qapp, monkeypatch, tmp_path
    ):
        """A kulcs teljes hianya (regi formatumu checkpoint VAGY egy
        crash a garantalt safe-off ELOTT) -- a legsulyosabb, bizonyitek
        nelkuli eset."""
        self._make_checkpoint(tmp_path)  # nincs last_safe_off_result kulcs
        captured = self._capture_dialog(qapp, monkeypatch, tmp_path)

        assert "text" in captured
        assert "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" in captured["text"]
        # Kör 19 D8e (GUI-R19-04): energizálva maradhatott kimenet -- .warning().
        assert captured["severity"] == "warning"

    def test_startup_dialog_warns_on_uncertain_safe_off_record(
        self, qapp, monkeypatch, tmp_path
    ):
        self._make_checkpoint(
            tmp_path,
            last_safe_off_result={"PSU": False, "Load": True, "DMM_V": True, "DMM_T": True},
        )
        captured = self._capture_dialog(qapp, monkeypatch, tmp_path)

        assert "text" in captured
        assert "SAFE-OFF BIZONYTALAN" in captured["text"]
        assert "PSU" in captured["text"]
        # R1 B7S V-1: F-42 óta a False readback-eredetű is lehet -> a szöveg
        # nem állíthatja, hogy "parancs-szintű, nem hardveres readback".
        assert "safe-off utáni állapotellenőrzés" in captured["text"]
        assert "nem hardveres readback" not in captured["text"]
        # Kör 19 D8e (GUI-R19-04): bizonytalan safe-off -- .warning().
        assert captured["severity"] == "warning"

    def test_startup_dialog_omits_line_when_safe_off_confirmed(
        self, qapp, monkeypatch, tmp_path
    ):
        self._make_checkpoint(
            tmp_path,
            last_safe_off_result={"PSU": True, "Load": True, "DMM_V": True, "DMM_T": True},
        )
        captured = self._capture_dialog(qapp, monkeypatch, tmp_path)

        assert "text" in captured
        assert "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" not in captured["text"]
        assert "SAFE-OFF BIZONYTALAN" not in captured["text"]
        # Kör 19 D8e (GUI-R19-04): nincs ⚠-jelölt sor -- .information() marad.
        assert captured["severity"] == "information"

    def test_startup_dialog_safe_off_line_distinct_from_bq_residual_lines(
        self, qapp, monkeypatch, tmp_path
    ):
        """DFAM-018: egy checkpoint, ami EGYSZERRE hordoz BQ seal-
        maradványt ÉS bizonytalan safe-off-ot, MINDKÉT sort MEGJELENÍTI,
        NEM olvasztja össze egybe."""
        self._make_checkpoint(
            tmp_path,
            pending_bq_unsealed={
                "controller": "GOLDEN_GG",
                "reason": "SOURCE_SNAPSHOT_READ_REQUIRES_UNSEALED",
                "bq_unsealed": True,
            },
            last_safe_off_result={"PSU": False, "Load": True, "DMM_V": True, "DMM_T": True},
        )
        captured = self._capture_dialog(qapp, monkeypatch, tmp_path)

        assert "text" in captured
        assert "BQ SEAL MARADVÁNY" in captured["text"]
        assert "SAFE-OFF BIZONYTALAN" in captured["text"]
        # Kör 19 D8e (GUI-R19-04): mindkét ⚠-jelölt sor -- .warning().
        assert captured["severity"] == "warning"


class TestR1706UnreadableCheckpointStartupWarning:
    """[regresszió, Kor 18, Batch C7 R17-06, DFAM-018+DFAM-004] a startup-
    review dialogus KULON sort jelenit meg, ha a checkpoint.json nem
    olvashato (csonka/serult) -- ez korabban csendben ugyanugy nezett ki,
    mint egy ep, maradvany nelkuli checkpoint, elrejtve a BQ/safe-off
    figyelmezteteseket is."""

    def _capture_dialog(self, qapp, monkeypatch, tmp_path) -> dict:
        from Prog.gui import main_window as main_window_module

        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        captured = {}

        def _fake_information(parent, title, text):
            captured["text"] = text
            captured["severity"] = "information"

        def _fake_warning(parent, title, text):
            captured["text"] = text
            captured["severity"] = "warning"

        monkeypatch.setattr(
            main_window_module.QMessageBox, "information", _fake_information
        )
        # Kör 19 D8e (GUI-R19-04, DFAM-018): a dialógus mostantól .warning()-ot
        # hív, ha a felsorolt sessionök bármelyike ⚠-jelölt (safety-releváns)
        # sort hordoz -- mindkét konstruktort patch-eljük, hogy a tartalom-
        # alapú asszerciók a hívott konstruktortól függetlenül működjenek,
        # és a `severity` kulcs alapján külön ellenőrizhető legyen, MELYIK
        # tüzelt (l. lent az egyes tesztek saját asszercióit).
        monkeypatch.setattr(
            main_window_module.QMessageBox, "warning", _fake_warning
        )
        MainWindow()
        return captured

    def test_unreadable_checkpoint_renders_residual_warning(self, qapp, monkeypatch, tmp_path):
        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20260916_000001"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            '{"status": "RUN', encoding="utf-8"
        )
        captured = self._capture_dialog(qapp, monkeypatch, tmp_path)

        assert "text" in captured
        assert "CHECKPOINT NEM OLVASHATÓ" in captured["text"]
        # A discriminator: az egyeb (BQ/safe-off) maradvany-sorok NEM
        # tuzelhetnek egy `{}`-bol -- a CONTINUE biztositja, hogy ne is
        # probaljanak.
        assert "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" not in captured["text"]
        assert "BQ Data Flash MARADVÁNY" not in captured["text"]
        assert "BQ SEAL MARADVÁNY" not in captured["text"]
        # Kör 19 D8e (GUI-R19-04): olvashatatlan checkpoint -- .warning().
        assert captured["severity"] == "warning"

    def test_valid_residualless_checkpoint_renders_no_unreadable_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        """A discriminator masik fele: egy TELJESEN ep, maradvany-mentes
        checkpoint NEM kaphatja meg a "NEM OLVASHATÓ" jelzest."""
        import json

        sessions_root = tmp_path / "Mérések" / "Sessions"
        session_dir = sessions_root / "session_TEST_20260916_000002"
        session_dir.mkdir(parents=True)
        (session_dir / "checkpoint.json").write_text(
            json.dumps({
                "status": "RUNNING", "step": "relax", "safe_boundary": True,
                # Kör 19 D8e (GUI-R19-04): l. TestC12PendingBqDfRestoreStartupWarning
                # ::test_dialog_text_omits_warning_when_no_pending_restore
                # azonos hivatkozású kommentje ugyanerről az indoklásról.
                "last_safe_off_result": {"PSU": True, "Load": True, "DMM_V": True, "DMM_T": True},
            }),
            encoding="utf-8",
        )
        captured = self._capture_dialog(qapp, monkeypatch, tmp_path)

        assert "text" in captured
        assert "CHECKPOINT NEM OLVASHATÓ" not in captured["text"]
        # Kör 19 D8e (GUI-R19-04): nincs ⚠-jelölt sor -- .information() marad.
        assert captured["severity"] == "information"


class TestR1715StartDialogHashCriticalFields:
    """[regresszió, Kor 18, Batch C7 R17-15, DFAM-003+DFAM-017] a Start-
    megerosito dialogus mostantol a 9, korabban rejtett, hash-kritikus
    SessionConfig mezot is megjeleniti."""

    def test_start_summary_renders_every_hash_critical_field(self, qapp):
        from Prog.gui.main_window import _format_start_summary

        cfg = _make_cfg(
            sample_id="SN-R17-15",
            battery_model="SENTINEL-MODEL-XYZ",
            nominal_capacity_ah=12.34,
            temperature_compensation_mode="ENABLED",
            temperature_sensor_mode="NTC_10K",
            discharge_rate_divisor=137,  # nem-tipikus sentinel -- l. P3-2
            golden_gg_system_rate_current_A=0.99,
            ocv_soc_step_percent=3.5,
            relax_after_charge_s=1234.0,
        )
        text = _format_start_summary({}, cfg)

        # Fuggetlen verifier P3-2 lelete: a puszta substring-kereses (pl.
        # "7") nem diszkriminalo, ha egy MASIK mezo erteke is tartalmazza
        # -- ezert itt a CIMKE+ertek parost keressuk, nem az izolalt erteket.
        for expected in (
            "sample_id='SN-R17-15'", "modell='SENTINEL-MODEL-XYZ'",
            "nominál=12.34 Ah", "hőkompenzáció=ENABLED",
            "hőszenzor=NTC_10K", "kisütés-osztó=137",
            "GG rate-áram=0.99 A", "OCV/SOC lépés=3.5%",
            "relax töltés után=1234.0 s",
        ):
            assert expected in text, f"{expected!r} hianyzik a Start-osszefoglalobol"

    def test_start_summary_without_cfg_is_unchanged(self, qapp):
        """Backward-compat pin: cfg=None (a meglevo hivohelyek/tesztek
        allapota) nem valt hibat, es NEM add hozza az uj sorokat."""
        from Prog.gui.main_window import _format_start_summary

        text = _format_start_summary({"test_type": "CHARACTERIZATION"})

        assert "Minta/akku:" not in text
        assert "Mérőlánc/rács:" not in text


class TestKor20BatchE9PicoProvisioningActionWidgetState:
    """[Kör 20 Batch E9, F20-G4, 2026-09-19] A "Pico előkészítése…" menüakció
    korábban SOHA nem kapott setEnabled()-et -- mind a négy kizárási
    feltételét kizárólag kattintás-idejű QMessageBox.warning érvényesítette
    (l. _on_pico_provisioning_requested()). A _refresh_calibration_blocked_
    state() bővítve most a menüakció widget-állapotát is beállítja, SAJÁT,
    négy feltétellel (nem a kalibrációs `blocked` bool újrafelhasználásával,
    ami hibás okokból tiltana/engedne)."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        return MainWindow()

    def test_enabled_at_startup_when_nothing_active(self, qapp, monkeypatch, tmp_path):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        assert window._pico_provisioning_action.isEnabled() is True

    def test_disabled_when_worker_active(self, qapp, monkeypatch, tmp_path):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._worker = object()
        window._refresh_calibration_blocked_state()
        assert window._pico_provisioning_action.isEnabled() is False

    def test_disabled_when_bq_worker_active(self, qapp, monkeypatch, tmp_path):
        # Counter-eset a kalibrációs `blocked` bool-hoz képest: a kalibráció
        # NEM érzékeny _bq_worker-re (l. _refresh_calibration_blocked_state()
        # docstringje), de a Pico-előkészítés IGEN -- ha ezt a SAJÁT
        # feltétel-halmazt véletlenül a `blocked` bool-ra cserélnék, ez a
        # teszt bukna.
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._bq_worker = object()
        window._refresh_calibration_blocked_state()
        assert window._pico_provisioning_action.isEnabled() is False

    def test_disabled_when_session_owns_bq_transport(self, qapp, monkeypatch, tmp_path):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._session_owns_bq_transport = True
        window._refresh_calibration_blocked_state()
        assert window._pico_provisioning_action.isEnabled() is False

    def test_disabled_when_bq_connect_worker_active(self, qapp, monkeypatch, tmp_path):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._bq_connect_worker = object()
        window._refresh_calibration_blocked_state()
        assert window._pico_provisioning_action.isEnabled() is False

    def test_disabled_when_connect_worker_active(self, qapp, monkeypatch, tmp_path):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._connect_worker = object()
        window._refresh_calibration_blocked_state()
        assert window._pico_provisioning_action.isEnabled() is False

    def test_reenabled_after_condition_clears(self, qapp, monkeypatch, tmp_path):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        window._worker = object()
        window._refresh_calibration_blocked_state()
        assert window._pico_provisioning_action.isEnabled() is False
        window._worker = None
        window._refresh_calibration_blocked_state()
        assert window._pico_provisioning_action.isEnabled() is True

    def test_calibration_button_gate_unaffected_by_bq_worker(self, qapp, monkeypatch, tmp_path):
        """Counter-test: a testvér, kalibrációs gomb SAJÁT (szűkebb, 3
        feltételes) kapuja NEM változott -- a `_bq_worker` egyedül nem
        blokkolja a kalibrációt (l. a metódus docstringje), csak a Pico-
        előkészítést. Ha a két feltétel-halmaz véletlenül összeolvadna, ez
        a teszt bukna."""
        window = self._make_window(qapp, monkeypatch, tmp_path)
        panel_calls = []
        window._bq_diagnostics_panel.set_calibration_blocked_by_other_activity = (
            lambda blocked: panel_calls.append(blocked)
        )
        window._bq_worker = object()
        window._refresh_calibration_blocked_state()
        assert panel_calls == [False]
        assert window._pico_provisioning_action.isEnabled() is False


_F19_HIL_MIN_RAW = "+4.04496000E-01"


class TestF19OcvSocSampleTimerPreflight:
    """[regresszio, R1 B5, F-19, DFAM-009] Az OCV_SOC session korabban a
    gyari pulse_sample_interval_s=0,2 s-mal elindult, pedig a valos 34465A
    SAMP:TIM? MIN-je az impulzusmeres DCV-beallitasan (range 100, NPLC=10)
    0,404496 s (CLAIM_LEDGER INSTR-DMM-SAMPTIM-001) -- a hiba csak az ELSO
    impulzusnal, az oras elotoltes es relaxacio utan, FAULT-kent jelent
    meg. Most a bring-up (configure_dcv utan, minden ON elott) csak olvasva
    lekerdezi a MIN-t, es a meglevo except-agon (safe_all_off ->
    disconnect_all) elutasit."""

    def _setup(self, monkeypatch, tmp_path) -> dict:
        from Prog.drivers.device_load import Keithley2380Load
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        counters = {
            "psu_output_on": 0, "load_input_on": 0,
            "safe_all_off": 0, "disconnect_all": 0,
        }

        def _bump(key):
            counters[key] += 1

        monkeypatch.setattr(Keithley2220PSU, "output_on", lambda self: _bump("psu_output_on"))
        monkeypatch.setattr(
            Keithley2380Load, "input_on",
            lambda self, clear_first=True: _bump("load_input_on"),
        )

        def _safe_all_off(self):
            _bump("safe_all_off")
            return {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}

        monkeypatch.setattr(InstrumentManager, "safe_all_off", _safe_all_off)
        monkeypatch.setattr(
            InstrumentManager, "disconnect_all", lambda self: _bump("disconnect_all")
        )
        return counters

    # -- T1 ---------------------------------------------------------------
    def test_worker_path_factory_config_rejected_before_energizing(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.gui.main_window import _InstrumentConnectWorker
        counters = self._setup(monkeypatch, tmp_path)
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        failed: list = []
        ready: list = []
        worker.connect_failed.connect(failed.append)
        worker.connect_ready.connect(ready.append)

        worker.run()

        assert ready == []
        assert len(failed) == 1
        message = failed[0]
        assert "OCV_SOC_PREFLIGHT" in message
        assert "SAMPLE_TIMER_MIN_EXCEEDS_REQUESTED_INTERVAL" in message
        assert "0.404496" in message
        assert "Q-6" in message
        assert counters["psu_output_on"] == 0
        assert counters["load_input_on"] == 0
        assert counters["safe_all_off"] == 1
        assert counters["disconnect_all"] == 1
        # A gyari alapertek valtozatlan (Q-6 felhasznaloi dontes).
        from Prog.gui.main_window import _ocv_soc_config_for
        assert _ocv_soc_config_for(cfg).pulse_sample_interval_s == pytest.approx(0.2)

    # -- T2 ---------------------------------------------------------------
    def test_sync_build_runner_raises_and_creates_no_session_dir(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.src.exceptions import ConfigurationError
        counters = self._setup(monkeypatch, tmp_path)
        window = MainWindow()
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")

        with pytest.raises(
            ConfigurationError, match="SAMPLE_TIMER_MIN_EXCEEDS_REQUESTED_INTERVAL"
        ) as excinfo:
            window._build_runner(cfg)

        assert getattr(excinfo.value, "safe_off_attempted", False) is True
        assert getattr(excinfo.value, "safe_off_identity_unconfirmed", None) is False
        sessions_root = tmp_path / "Mérések" / "Sessions"
        if sessions_root.exists():
            assert not any(p.name.startswith("session_") for p in sessions_root.iterdir())
        assert counters["psu_output_on"] == 0
        assert counters["load_input_on"] == 0
        assert counters["safe_all_off"] == 1
        assert counters["disconnect_all"] == 1

    # -- T3 ---------------------------------------------------------------
    def test_gui_chain_worker_failed_signal_reaches_on_connect_failed(
        self, qapp, monkeypatch, tmp_path
    ):
        from PySide6.QtWidgets import QMessageBox

        from Prog.gui.main_window import _InstrumentConnectWorker
        counters = self._setup(monkeypatch, tmp_path)
        criticals: list = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            lambda *a, **k: criticals.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = MainWindow()
        # Ha a bring-up atengedne, a session-epitesig sem szabad eljutni
        # (a regi kodon ez a spy jelzi a hibat, valodi runner-szal nelkul).
        build_calls: list = []
        window._build_runner = lambda cfg, pre_connected=None: (
            build_calls.append(cfg) or (_ for _ in ()).throw(
                RuntimeError("_build_runner nem futhat elutasitott preflight utan")
            )
        )
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")
        # A _start_test() altal a worker inditasa elott beallitott allapot.
        window._pending_start_cfg = cfg
        window._live_panel.set_start_enabled(False)
        window._live_panel.set_emergency_stop_enabled(True)
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        window._connect_worker = worker
        worker.connect_ready.connect(window._on_connect_ready)
        worker.connect_failed.connect(window._on_connect_failed)

        worker.run()

        assert build_calls == []
        assert len(criticals) == 1
        assert "0.404496" in criticals[0]
        assert "Q-6" in criticals[0]
        assert window._connect_worker is None
        assert window._worker is None
        assert window._live_panel._start_btn.isEnabled() is True
        assert counters["psu_output_on"] == 0
        assert counters["load_input_on"] == 0

    # -- T4 ---------------------------------------------------------------
    def test_preflight_after_configure_dcv_before_psu_mode_and_read_only(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.gui.main_window import _construct_and_connect_instruments
        self._setup(monkeypatch, tmp_path)
        _use_hil_verified_ocv_soc_pulse_interval(monkeypatch)
        calls: list = []
        def _configure_dcv(self, range_V, nplc):
            self._test_nplc = nplc
            calls.append(("configure_dcv", id(self), range_V, nplc))

        monkeypatch.setattr(Keysight34465ADMM, "configure_dcv", _configure_dcv)

        def _query(self):
            calls.append(("query_min", id(self)))
            return _F19_HIL_MIN_RAW

        monkeypatch.setattr(Keysight34465ADMM, "query_sample_timer_capability", _query)
        for name in ("set_nplc", "configure_timed_acquisition", "arm_timed_acquisition"):
            monkeypatch.setattr(
                Keysight34465ADMM, name,
                lambda self, *a, _n=name, **k: calls.append(("FORBIDDEN_" + _n, id(self))),
            )

        def _set_mode_independent(self):
            calls.append(("set_mode_independent", id(self)))
            self._test_combination_mode = "OFF"

        monkeypatch.setattr(Keithley2220PSU, "set_mode_independent", _set_mode_independent)
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")

        result = _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        names = [c[0] for c in calls]
        assert names.count("query_min") == 1
        assert not any(n.startswith("FORBIDDEN_") for n in names), names
        dcv = next(c for c in calls if c[0] == "configure_dcv")
        query = next(c for c in calls if c[0] == "query_min")
        assert dcv[1] == query[1] == id(result.dmm_v)
        assert dcv[2] == 100 and dcv[3] == 1
        assert [c[3] for c in calls if c[0] == "configure_dcv"] == [1, 10]
        assert result.dmm_v.get_nplc() == 10
        assert result.dmm_v._test_autozero is True
        assert (
            names.index("configure_dcv") < names.index("query_min")
            < names.index("set_mode_independent")
        )

    # -- T5 ---------------------------------------------------------------
    @pytest.mark.parametrize(
        "raw, expected_code",
        [
            (None, "SAMPLE_TIMER_CAPABILITY_QUERY_FAILED"),
            ("NaN", "SAMPLE_TIMER_CAPABILITY_INVALID_RESPONSE"),
            ("inf", "SAMPLE_TIMER_CAPABILITY_INVALID_RESPONSE"),
            ("0", "SAMPLE_TIMER_CAPABILITY_INVALID_RESPONSE"),
            ("-1", "SAMPLE_TIMER_CAPABILITY_INVALID_RESPONSE"),
            ("GARBAGE", "SAMPLE_TIMER_CAPABILITY_INVALID_RESPONSE"),
            ("", "SAMPLE_TIMER_CAPABILITY_INVALID_RESPONSE"),
        ],
    )
    def test_fail_closed_on_unusable_capability_response(
        self, qapp, monkeypatch, tmp_path, raw, expected_code
    ):
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import ConfigurationError, InstrumentTimeoutError
        counters = self._setup(monkeypatch, tmp_path)
        # A kuszob itt teljesitheto lenne (0,41 s) -- csak a valasz
        # hasznalhatatlansaga utasithat el.
        _use_hil_verified_ocv_soc_pulse_interval(monkeypatch)

        def _query(self):
            if raw is None:
                raise InstrumentTimeoutError("simulated SAMP:TIM? MIN timeout")
            return raw

        monkeypatch.setattr(Keysight34465ADMM, "query_sample_timer_capability", _query)
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")

        with pytest.raises(ConfigurationError) as excinfo:
            _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        assert expected_code in str(excinfo.value)
        assert "OCV_SOC_PREFLIGHT" in str(excinfo.value)
        assert getattr(excinfo.value, "safe_off_attempted", False) is True
        assert counters["safe_all_off"] == 1
        assert counters["disconnect_all"] == 1
        assert counters["psu_output_on"] == 0
        assert counters["load_input_on"] == 0

    # -- T6 ---------------------------------------------------------------
    @pytest.mark.parametrize(
        "interval_s, accepted",
        [(0.404496, True), (0.41, True), (0.404495, False)],
    )
    def test_boundary_min_equal_to_requested_is_accepted(
        self, qapp, monkeypatch, tmp_path, interval_s, accepted
    ):
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import ConfigurationError
        counters = self._setup(monkeypatch, tmp_path)
        _use_ocv_soc_pulse_interval(monkeypatch, interval_s)
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")

        if accepted:
            result = _construct_and_connect_instruments(cfg, needs_bq_transport=False)
            assert result.instruments is not None
            assert counters["safe_all_off"] == 0
        else:
            with pytest.raises(
                ConfigurationError, match="SAMPLE_TIMER_MIN_EXCEEDS_REQUESTED_INTERVAL"
            ):
                _construct_and_connect_instruments(cfg, needs_bq_transport=False)
            assert counters["safe_all_off"] == 1

    # -- T7 ---------------------------------------------------------------
    def test_other_test_types_do_not_query_sample_timer_min(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.test_runner import TestType
        self._setup(monkeypatch, tmp_path)
        _use_hil_verified_ocv_soc_pulse_interval(monkeypatch)
        queried: list = []

        def _query(self):
            queried.append(id(self))
            return _F19_HIL_MIN_RAW

        monkeypatch.setattr(Keysight34465ADMM, "query_sample_timer_capability", _query)
        for test_type in TestType:
            queried.clear()
            cfg = _make_cfg(test_type=test_type.value)
            _construct_and_connect_instruments(cfg, needs_bq_transport=False)
            expected = 1 if test_type == TestType.OCV_SOC_CHARACTERIZATION else 0
            assert len(queried) == expected, test_type.value

    # -- T8 ---------------------------------------------------------------
    @pytest.mark.parametrize(
        "raw",
        [
            "+4.04496000E-01", "+2.00000000E-01", "+1.99E-01", "+4.1E-01",
            "NaN", "inf", "0", "-1", "GARBAGE", "", None,
        ],
    )
    @pytest.mark.parametrize("requested_s", [0.2, 0.404496, 0.41])
    def test_decision_parity_with_pulse_service_capability_gate(self, raw, requested_s):
        from Prog.gui.main_window import _preflight_ocv_soc_sample_timer
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.exceptions import ConfigurationError
        from Prog.src.pulse_measurement_service import PulseMeasurementService
        from Prog.src.safety import PsuMode, SafetyManager
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad

        def _dmm():
            if raw is None:
                return MockDMM(simulate_sample_timer_query_failure=True)
            return MockDMM(sample_timer_min_raw_override=raw)

        service = PulseMeasurementService(
            MockLoad(), _dmm(), 10.8, sample_interval_s=requested_s,
            safety=SafetyManager(
                profile=BatteryProfile(
                    battery_name="TEST", manufacturer="FIAMM", model="FG20721",
                    nominal_voltage_V=12.0, cell_count=6, nominal_capacity_Ah=7.2,
                ),
                psu_mode=PsuMode.INDEPENDENT,
            ),
            max_voltage_V=14.55,
            max_load_power_W=SafetyManager.max_load_power_W,
        )
        service_ok, service_reason = service._verify_sample_timer_capability()
        try:
            _preflight_ocv_soc_sample_timer(_dmm(), requested_s)
            preflight_ok, message = True, ""
        except ConfigurationError as exc:
            preflight_ok, message = False, str(exc)

        assert preflight_ok == service_ok
        if not service_ok:
            assert service_reason.split(":")[0] in message

    # -- T9 ---------------------------------------------------------------
    def test_preflight_dcv_setting_matches_impulse_measurement_setting(
        self, qapp, monkeypatch, tmp_path
    ):
        """B15a óta az impulzus 1 NPLC/AZ ON, a relax-alap továbbra is 10.
        A preflight MIN-lekérdezése és a szolgáltatás ugyanazt az impulzus-
        beállítást használja; a 0,2 s-ot nem a relax MIN-jéhez mérjük."""
        from Prog.src.ocv_soc_controller import OcvSocState
        from Prog.src.test_runner import (
            _FAST_SUBSTATE_NAMES,
            _SLOW_BASE_NPLC_STEP_KINDS,
            StepKind,
        )
        self._setup(monkeypatch, tmp_path)
        dcv_nplc: list = []
        queried_settings: list = []

        def _configure_dcv(self, range_V, nplc):
            self._test_nplc = nplc
            dcv_nplc.append(nplc)

        def _query(self):
            queried_settings.append((self.get_nplc(), self._test_autozero))
            # Eltérő MIN a két konfiguráción: a régi 10 PLC-s kapu itt bukik.
            return "+4.04496000E-02" if self.get_nplc() == 1 else _F19_HIL_MIN_RAW

        monkeypatch.setattr(Keysight34465ADMM, "configure_dcv", _configure_dcv)
        monkeypatch.setattr(Keysight34465ADMM, "query_sample_timer_capability", _query)
        window = MainWindow()
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._ocv_soc_ctrl_factory()

        assert dcv_nplc == [1, 10]
        assert queried_settings == [(1.0, True)]
        assert runner._config.relax_ocv_nplc == pytest.approx(10.0)
        assert ctrl._dmm_v.get_nplc() == pytest.approx(10.0)
        assert StepKind.OCV_SOC in _SLOW_BASE_NPLC_STEP_KINDS
        assert not any(
            name.startswith("IMPULSE") for name in _FAST_SUBSTATE_NAMES[StepKind.OCV_SOC]
        )
        assert OcvSocState.PRECHARGE.name in _FAST_SUBSTATE_NAMES[StepKind.OCV_SOC]
        assert ctrl._pulse_service._acquisition_nplc == pytest.approx(1.0)
        assert ctrl._pulse_service._require_autozero_on is True
        assert ctrl._pulse_service._sample_interval_s == pytest.approx(0.2)
        assert ctrl._config.pulse_sample_interval_s == pytest.approx(0.2)

    def test_preflight_rejects_slow_impulse_even_if_relax_min_would_pass(
        self, qapp, monkeypatch, tmp_path
    ):
        """A hibás konfiguráció választása hamis PASS-t sem eredményezhet."""
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import ConfigurationError

        counters = self._setup(monkeypatch, tmp_path)
        queried: list = []

        def _query(self):
            queried.append((self.get_nplc(), self._test_autozero))
            return "0.21" if self.get_nplc() == 1 else "0.01"

        monkeypatch.setattr(Keysight34465ADMM, "query_sample_timer_capability", _query)
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")
        with pytest.raises(
            ConfigurationError, match="SAMPLE_TIMER_MIN_EXCEEDS_REQUESTED_INTERVAL"
        ) as excinfo:
            _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        assert queried == [(1.0, True)]
        assert "NPLC=1, AZ ON" in str(excinfo.value)
        assert counters == {
            "psu_output_on": 0, "load_input_on": 0,
            "safe_all_off": 1, "disconnect_all": 1,
        }

    @pytest.mark.parametrize("failure", ["autozero", "readback", "restore"])
    def test_preflight_configuration_failure_stays_safe(
        self, qapp, monkeypatch, tmp_path, failure
    ):
        """AZ/NPLC/restore hiba: nincs ON, és mindkét cleanup megtörténik."""
        from Prog.gui.main_window import _construct_and_connect_instruments
        from Prog.src.exceptions import ConfigurationError, InstrumentError

        counters = self._setup(monkeypatch, tmp_path)
        queried: list = []
        configured: list = []
        error = InstrumentError("szimulált preflight-konfigurációs hiba")

        def _configure_dcv(self, range_V, nplc):
            configured.append(nplc)
            if failure == "restore" and nplc == 10:
                raise error
            self._test_nplc = nplc

        def _enable_autozero(self):
            if failure == "autozero":
                raise error
            self._test_autozero = True

        monkeypatch.setattr(Keysight34465ADMM, "configure_dcv", _configure_dcv)
        monkeypatch.setattr(Keysight34465ADMM, "enable_autozero", _enable_autozero)
        if failure == "readback":
            monkeypatch.setattr(Keysight34465ADMM, "get_nplc", lambda self: 10.0)
        monkeypatch.setattr(
            Keysight34465ADMM, "query_sample_timer_capability",
            lambda self: queried.append(True) or "0.04",
        )
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")
        with pytest.raises((ConfigurationError, InstrumentError)) as excinfo:
            _construct_and_connect_instruments(cfg, needs_bq_transport=False)

        if failure == "readback":
            assert "OCV_SOC_PREFLIGHT NPLC readback" in str(excinfo.value)
        else:
            assert excinfo.value is error
        assert configured == [1, 10]
        assert queried == ([True] if failure == "restore" else [])
        assert counters == {
            "psu_output_on": 0, "load_input_on": 0,
            "safe_all_off": 1, "disconnect_all": 1,
        }

    # -- T10 --------------------------------------------------------------
    def test_preflight_and_runner_share_one_ocv_soc_config_source(
        self, qapp, monkeypatch, tmp_path
    ):
        import dataclasses

        import Prog.gui.main_window as main_window_module
        source = Path(main_window_module.__file__).read_text(encoding="utf-8")
        assert source.count("OcvSocConfig(") == 1

        self._setup(monkeypatch, tmp_path)
        original_config_for = main_window_module._ocv_soc_config_for
        original_preflight = main_window_module._preflight_ocv_soc_sample_timer
        config_calls: list = []
        preflight_requested: list = []

        def _config_for(cfg):
            config_calls.append(cfg)
            return dataclasses.replace(original_config_for(cfg), pulse_sample_interval_s=0.41)

        def _preflight(dmm_v, requested_interval_s):
            preflight_requested.append(requested_interval_s)
            return original_preflight(dmm_v, requested_interval_s)

        monkeypatch.setattr(main_window_module, "_ocv_soc_config_for", _config_for)
        monkeypatch.setattr(main_window_module, "_preflight_ocv_soc_sample_timer", _preflight)
        window = MainWindow()
        cfg = _make_cfg(test_type="OCV_SOC_CHARACTERIZATION", ocv_soc_step_percent=10.0)

        runner, _plan, _ = window._build_runner(cfg)
        ctrl = runner._ocv_soc_ctrl_factory()

        assert len(config_calls) == 2
        assert all(c is cfg for c in config_calls)
        assert preflight_requested == [pytest.approx(0.41)]
        assert ctrl._config.pulse_sample_interval_s == pytest.approx(0.41)
        assert ctrl._config.step_percent == pytest.approx(10.0)
        assert ctrl._config.discharge_rate_divisor == cfg.discharge_rate_divisor

    @pytest.mark.parametrize("failure", ["queue", "query"])
    def test_preflight_checks_errors_before_minimum(
        self, qapp, monkeypatch, tmp_path, failure,
    ):
        """AZ/NPLC után a hibasor elutasítása is restore + safe-off utat kér."""
        from Prog.gui.main_window import _InstrumentConnectWorker
        from Prog.src.exceptions import InstrumentError

        counters = self._setup(monkeypatch, tmp_path)
        checked, minimum, configured = [], [], []

        def configure(self, range_V, nplc):
            configured.append(nplc)
            self._test_nplc = nplc

        def errors(self):
            checked.append((self.get_nplc(), self._test_autozero))
            if failure == "query":
                raise InstrumentError("F5_ERROR_QUEUE_QUERY_FAILED")
            return ["-221,F5_ERROR_QUEUE_REJECTED"]

        monkeypatch.setattr(Keysight34465ADMM, "configure_dcv", configure)
        monkeypatch.setattr(Keysight34465ADMM, "check_error", errors)
        monkeypatch.setattr(
            Keysight34465ADMM, "query_sample_timer_capability",
            lambda self: minimum.append(True) or "0.04",
        )
        worker = _InstrumentConnectWorker(
            _make_cfg(test_type="OCV_SOC_CHARACTERIZATION"), needs_bq_transport=False,
        )
        failed, ready = [], []
        worker.connect_failed.connect(failed.append)
        worker.connect_ready.connect(ready.append)
        worker.run()

        assert checked == [(1.0, True)]
        assert minimum == []
        assert configured == [1, 10]
        assert ready == [] and len(failed) == 1
        assert ("F5_ERROR_QUEUE_QUERY_FAILED" if failure == "query"
                else "F5_ERROR_QUEUE_REJECTED") in failed[0]
        assert counters == {
            "psu_output_on": 0, "load_input_on": 0,
            "safe_all_off": 1, "disconnect_all": 1,
        }


class TestF19MockDmmSampleTimerFidelity:
    """[regresszio, R1 B5, DFAM-006] A MockDMM SAMP:TIM? MIN-je korabban
    NPLC/50 + 20 us = 0,20002 s volt (a kodeval azonos, ellenorizetlen
    feltetelezes), a get_sample_timer_s() pedig mindig a kert erteket adta
    (tautologia). M1: a HIL-en mert MIN alapbol; M2: opt-in csendes
    elutasitas allapotmegorzessel. NEM modellezett: a -221 al-tartomany
    (0,3 s -> MIN) es a 0,45-0,99 s -> 0,41 s kvantalas."""

    # -- T11 --------------------------------------------------------------
    def test_default_min_is_hil_measured_value_at_nplc10_50hz(self):
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        dmm = MockDMM()
        assert float(dmm.query_sample_timer_capability()) == pytest.approx(0.404496, abs=1e-9)

    def test_unmeasured_configuration_keeps_aperture_model(self):
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        dmm = MockDMM()
        dmm.set_nplc(0.02)
        assert float(dmm.query_sample_timer_capability()) == pytest.approx(
            0.02 / 50.0 + 20e-6
        )

    # -- T12 --------------------------------------------------------------
    def test_silent_rejection_keeps_previous_timer_and_empty_error_queue(self):
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        dmm = MockDMM(simulate_hil_sample_timer_silent_rejection=True)
        dmm.configure_timed_acquisition(sample_count=3, interval_s=0.2)
        assert dmm.get_sample_timer_s() == pytest.approx(1.0)
        assert dmm.check_error() == []

        dmm.configure_timed_acquisition(sample_count=3, interval_s=0.41)
        assert dmm.get_sample_timer_s() == pytest.approx(0.41)
        assert dmm.check_error() == []

    def test_silent_rejection_preserves_previously_accepted_timer(self):
        """FINAL B5/2: elfogadott 0,41 -> elutasitott 0,2 -> a readback
        tovabbra is 0,41, ures hibasorral; a reset_to_single_sample() sem
        torli ezt az allapotot."""
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        dmm = MockDMM(simulate_hil_sample_timer_silent_rejection=True)
        dmm.configure_timed_acquisition(sample_count=3, interval_s=0.41)
        dmm.configure_timed_acquisition(sample_count=3, interval_s=0.2)
        assert dmm.get_sample_timer_s() == pytest.approx(0.41)
        assert dmm.check_error() == []

        dmm.reset_to_single_sample()
        assert dmm.get_sample_timer_s() == pytest.approx(0.41)
        assert dmm.check_error() == []

    # -- T13 --------------------------------------------------------------
    def test_residual_tautology_when_flag_off_characterization(self):
        """KARAKTERIZALO teszt (nem helyesseg-allitas): flag nelkul a mock
        a regi, "jol viselkedo" muszert modellezi -- a kert ertek jon vissza
        a MIN alatt is. Ez a nyilvantartott DFAM-006 maradek (~83 legacy
        teszt erre epit); a teljes lezaras NEM allithato."""
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        dmm = MockDMM()
        dmm.configure_timed_acquisition(sample_count=3, interval_s=0.2)
        assert dmm.get_sample_timer_s() == pytest.approx(0.2)
        assert dmm.check_error() == []


# --------------------------------------------------------------------------- #
# R1 B6 -- GUI safety-lathatosag (F-06, F-08, F-09 + NS-B6-1, F-31)           #
# --------------------------------------------------------------------------- #

# A meglevo worker-ag (3) operatori szovege (main_window.py
# _InstrumentConnectWorker.run(), Kor 18 C11) -- FUGGETLENUL leirva, nem a
# produkcios helperbol importalva, hogy a pontos-szoveg assert ne legyen
# tautologikus.
_B6_ABORT_STATUS = "Kapcsolódás megszakítva vészleállítás miatt — a session nem indult el."
_B6_UNCERTAIN_TITLE = "VÉSZLEÁLLÍTÁS — bizonytalan safe-off"


def _b6_not_confirmed_text(devices: str) -> str:
    return (
        "\n\nFIGYELEM — a biztonságos lekapcsolás NEM igazolt: "
        + devices
        + ". A kimenet ENERGIZÁLT maradhatott; ellenőrizd "
        "fizikailag a műszereket. (Parancs-szintű eredmény, "
        "nem hardveres readback.)"
    )


_B6_ALL_OK = {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}
_B6_PSU_UNCERTAIN = {"Load": True, "PSU": False, "DMM_V": True, "DMM_T": True}
_B6_LOAD_UNCERTAIN = {"Load": False, "PSU": True, "DMM_V": True, "DMM_T": True}
_B6_F31_SUFFIX = " — FIGYELEM: minősített eredmény, l. a záró üzenetet és a report.json-t"


def _b6_record_criticals(monkeypatch, calls: list) -> list:
    """QMessageBox.critical spy: a hivasi sorrendbe "critical"-t ir, es
    (cim, szoveg) parokat gyujt."""
    from PySide6.QtWidgets import QMessageBox

    dialogs: list = []

    def _critical(*a, **k):
        calls.append("critical")
        dialogs.append((a[1], a[2]))

    monkeypatch.setattr(QMessageBox, "critical", _critical)
    return dialogs


def _b6_pre_connected(calls: list, safe_off_result):
    import types

    from Prog.gui.main_window import _PreConnectedInstruments

    def _safe_all_off():
        calls.append("safe_all_off")
        return safe_off_result

    instruments = types.SimpleNamespace(
        safe_all_off=_safe_all_off,
        disconnect_all=lambda: calls.append("disconnect_all"),
    )
    return _PreConnectedInstruments(
        instruments=instruments,
        psu=None, load=None, dmm_v=None, dmm_t=None, bq_transport=None,
        session_bq_serial=types.SimpleNamespace(close=lambda: calls.append("serial_close")),
        instrument_identities={},
    )


class TestR1B6F06AbortBranchSafeOffResultShown:
    """[regresszio, R1 B6, F-06, DFAM-016/018] a _on_connect_ready()
    abort-aga (kapcsolodas kozbeni veszleallitas) eldobta a safe_all_off()
    eredmenyet -- egy nem igazolt OFF csendben maradt. Most a figyelmeztetes
    a safe-off -> disconnect -> portzaras UTAN, kritikus dialogusban jelenik
    meg, a worker-ag (3) szovegevel azonosan."""

    def _window(self, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._connect_abort_requested = True
        window._pending_start_cfg = _make_cfg()
        return window

    @pytest.mark.parametrize(
        ("safe_off_result", "devices"),
        [(_B6_PSU_UNCERTAIN, "PSU"), (_B6_LOAD_UNCERTAIN, "Load")],
        ids=["psu_uncertain", "load_uncertain"],
    )
    def test_uncertain_safe_off_shows_critical_after_cleanup(
        self, qapp, monkeypatch, tmp_path, safe_off_result, devices
    ):
        window = self._window(monkeypatch, tmp_path)
        calls: list = []
        dialogs = _b6_record_criticals(monkeypatch, calls)

        window._on_connect_ready(_b6_pre_connected(calls, safe_off_result))

        assert calls == ["safe_all_off", "disconnect_all", "serial_close", "critical"]
        assert dialogs == [(
            _B6_UNCERTAIN_TITLE, _B6_ABORT_STATUS + _b6_not_confirmed_text(devices),
        )]
        assert window._status_bar.currentMessage() == _B6_ABORT_STATUS
        assert window._worker is None
        assert window._connect_abort_requested is False

    def test_confirmed_safe_off_shows_no_dialog(self, qapp, monkeypatch, tmp_path):
        window = self._window(monkeypatch, tmp_path)
        calls: list = []
        dialogs = _b6_record_criticals(monkeypatch, calls)

        window._on_connect_ready(_b6_pre_connected(calls, dict(_B6_ALL_OK)))

        assert calls == ["safe_all_off", "disconnect_all", "serial_close"]
        assert dialogs == []
        assert window._status_bar.currentMessage() == _B6_ABORT_STATUS

    def test_non_dict_result_is_fail_closed(self, qapp, monkeypatch, tmp_path):
        """None (nincs eredmeny) -> mindket eszkoz bizonytalan, nem hallgatunk."""
        window = self._window(monkeypatch, tmp_path)
        calls: list = []
        dialogs = _b6_record_criticals(monkeypatch, calls)

        window._on_connect_ready(_b6_pre_connected(calls, None))

        assert dialogs == [(
            _B6_UNCERTAIN_TITLE, _B6_ABORT_STATUS + _b6_not_confirmed_text("PSU, Load"),
        )]


class TestR1B6F06BuildRunnerFailureSafeOffResultShown:
    """[regresszio, R1 B6, F-06] a _on_connect_ready() _build_runner()-hiba
    aga szinten eldobta a safe_all_off() eredmenyet -- a hibadialogus csak
    str(exc)-et mutatott."""

    def _window(self, monkeypatch, tmp_path, calls):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._pending_start_cfg = _make_cfg()
        window._build_runner = lambda cfg, pre_connected=None: (_ for _ in ()).throw(
            RuntimeError("BOOM")
        )
        original_close = window._close_session_bq_transport

        def _spy_close():
            calls.append("close_session_bq_transport")
            original_close()

        window._close_session_bq_transport = _spy_close
        return window

    @pytest.mark.parametrize(
        ("safe_off_result", "devices"),
        [(_B6_PSU_UNCERTAIN, "PSU"), (_B6_LOAD_UNCERTAIN, "Load")],
        ids=["psu_uncertain", "load_uncertain"],
    )
    def test_uncertain_safe_off_appended_to_error_dialog_after_cleanup(
        self, qapp, monkeypatch, tmp_path, safe_off_result, devices
    ):
        calls: list = []
        window = self._window(monkeypatch, tmp_path, calls)
        dialogs = _b6_record_criticals(monkeypatch, calls)

        window._on_connect_ready(_b6_pre_connected(calls, safe_off_result))

        assert calls == [
            "safe_all_off", "disconnect_all", "close_session_bq_transport",
            "serial_close", "critical",
        ]
        assert dialogs == [("Inicializálási hiba", "BOOM" + _b6_not_confirmed_text(devices))]

    def test_confirmed_safe_off_dialog_is_exactly_str_exc(self, qapp, monkeypatch, tmp_path):
        calls: list = []
        window = self._window(monkeypatch, tmp_path, calls)
        dialogs = _b6_record_criticals(monkeypatch, calls)

        window._on_connect_ready(_b6_pre_connected(calls, dict(_B6_ALL_OK)))

        assert dialogs == [("Inicializálási hiba", "BOOM")]


class TestR1B6F06SameWarningTextOnAllThreeBringUpPaths:
    """[R1 B6, F-06] a harom bring-up hibaut (worker-ag (3), abort-ag,
    _build_runner-hiba ag) ugyanazt a "NEM igazolt" szoveget adja."""

    def test_worker_abort_and_build_failure_paths_share_the_warning_text(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.gui.main_window import _InstrumentConnectWorker

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        expected = _b6_not_confirmed_text("PSU")

        # (3) worker-ag: identify_all() utani hiba, igazolt azonositas mellett.
        monkeypatch.setattr(
            InstrumentManager, "safe_all_off", lambda self: dict(_B6_PSU_UNCERTAIN)
        )
        monkeypatch.setattr(InstrumentManager, "disconnect_all", lambda self: None)
        monkeypatch.setattr(
            Keysight34465ADMM, "configure_dcv",
            lambda self, range_V, nplc: (_ for _ in ()).throw(RuntimeError("DCV_FAIL")),
        )
        worker = _InstrumentConnectWorker(_make_cfg(), needs_bq_transport=False)
        failed: list = []
        worker.connect_failed.connect(failed.append)
        worker.run()
        assert failed == ["DCV_FAIL" + expected]

        calls: list = []
        dialogs = _b6_record_criticals(monkeypatch, calls)
        window = MainWindow()
        window._connect_abort_requested = True
        window._pending_start_cfg = _make_cfg()
        window._on_connect_ready(_b6_pre_connected(calls, dict(_B6_PSU_UNCERTAIN)))
        window._pending_start_cfg = _make_cfg()
        window._build_runner = lambda cfg, pre_connected=None: (_ for _ in ()).throw(
            RuntimeError("BOOM")
        )
        window._on_connect_ready(_b6_pre_connected(calls, dict(_B6_PSU_UNCERTAIN)))

        assert [text for _title, text in dialogs] == [
            _B6_ABORT_STATUS + expected, "BOOM" + expected,
        ]


class TestR1B6F09NsB61BuildFailureInstrumentsReference:
    """[regresszio, R1 B6, NS-B6-1 (F-09 testvere, DFAM-018)] ha a
    _build_runner() a `self._instruments = pre_connected.instruments`
    ertekadas UTAN bukik (pl. Logger), a hiba-ag lezarja a managert, de a
    referencia rajta maradt -- egy kesobbi idle excepthook egy lezart
    manageren adna ki safe-offot (hamis "NEM igazolt" CRITICAL)."""

    def test_failure_after_assignment_nulls_reference_and_keeps_uncertainty(
        self, qapp, monkeypatch, tmp_path
    ):
        import Prog.src.logger as logger_module
        from Prog.gui.main_window import _construct_and_connect_instruments

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg()
        pre_connected = _construct_and_connect_instruments(cfg, needs_bq_transport=False)
        manager = pre_connected.instruments
        calls: list = []
        manager.safe_all_off = lambda: calls.append("safe_all_off") or dict(_B6_PSU_UNCERTAIN)
        manager.disconnect_all = lambda: calls.append("disconnect_all")
        assigned_before_failure: list = []

        def _failing_logger(*a, **k):
            assigned_before_failure.append(window._instruments is manager)
            raise RuntimeError("LOGGER_BOOM")

        monkeypatch.setattr(logger_module, "Logger", _failing_logger)
        dialogs = _b6_record_criticals(monkeypatch, calls)
        window._pending_start_cfg = cfg

        window._on_connect_ready(pre_connected)

        # Elofeltetel: a hiba TENYLEGESEN az ertekadas utan kovetkezett be.
        assert assigned_before_failure == [True]
        assert calls == ["safe_all_off", "disconnect_all", "critical"]
        assert window._instruments is None
        assert dialogs == [("Inicializálási hiba", "LOGGER_BOOM" + _b6_not_confirmed_text("PSU"))]
        assert window._worker is None

    def test_early_failure_does_not_null_other_manager_reference(
        self, qapp, monkeypatch, tmp_path
    ):
        """Kontroll: korai (az ertekadas elotti) hibanal a MASIK (korabbi)
        manager referenciajat nem szabad nullazni -- az identitasfeltetel
        ezt vedi."""
        import types

        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        other_manager = types.SimpleNamespace(
            safe_all_off=lambda: dict(_B6_ALL_OK), disconnect_all=lambda: None,
        )
        window._instruments = other_manager
        window._pending_start_cfg = _make_cfg()
        window._build_runner = lambda cfg, pre_connected=None: (_ for _ in ()).throw(
            RuntimeError("EARLY_BOOM")
        )
        calls: list = []
        _b6_record_criticals(monkeypatch, calls)

        window._on_connect_ready(_b6_pre_connected(calls, dict(_B6_ALL_OK)))

        assert window._instruments is other_manager


class TestR1B6F09TerminalFinalizeNullsInstruments:
    """[regresszio, R1 B6, F-09, DFAM-016] a _finalize_terminal_session()
    disconnect_all() utan a self._instruments a lezart managerre mutatott."""

    def _window(self, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._build_runner(_make_cfg())
        return window

    @pytest.mark.parametrize(
        ("status", "resume_possible"),
        [("DONE", False), ("FAULT", False), ("STOPPED", False), ("CHECKPOINT_STOPPED", False)],
        ids=["done", "fault", "stopped", "terminal_checkpoint"],
    )
    def test_terminal_states_null_instruments_after_disconnect(
        self, qapp, monkeypatch, tmp_path, status, resume_possible
    ):
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        manager = window._instruments
        calls: list = []
        manager.disconnect_all = lambda: calls.append(("disconnect_all", window._instruments))

        window._on_finished(TestResult(
            status=status, reason="X", resume_possible=resume_possible,
            safe_off_attempted=True, safe_off_result=dict(_B6_ALL_OK),
        ))

        assert calls == [("disconnect_all", manager)]
        assert window._instruments is None

    def test_done_uncertainty_survives_nulling_in_report_and_popup(
        self, qapp, monkeypatch, tmp_path
    ):
        import json

        from PySide6.QtWidgets import QMessageBox

        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        window._instruments.disconnect_all = lambda: None
        popups: list = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: popups.append(a[2]))

        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True, safe_off_result=dict(_B6_PSU_UNCERTAIN),
        ))

        assert window._instruments is None
        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert report["safe_off_attempted"] is True
        assert report["safe_off_result"]["PSU"] is False
        assert len(popups) == 1
        assert "lekapcsolás NEM igazolt: PSU" in popups[0]
        # R1 B7S V-1: a záró popup sem állíthat kizárólag parancs-szintű eredményt.
        assert "safe-off utáni állapotellenőrzés" in popups[0]
        assert "nem hardveres readback" not in popups[0]

    def test_resumable_checkpoint_keeps_instruments(self, qapp, monkeypatch, tmp_path):
        """Kontroll: folytathato checkpointnal a session el, a referencia marad."""
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        manager = window._instruments
        window._on_finished(TestResult(status="CHECKPOINT_STOPPED", resume_possible=True))

        assert window._instruments is manager

    def test_checkpoint_close_fresh_safe_off_happens_before_nulling(
        self, qapp, monkeypatch, tmp_path
    ):
        """FINAL B6/2: a "Teszt lezarasa" friss safe-offja meg az el
        managerrel tortenik, a nullazas csak a disconnect utan."""
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        manager = window._instruments
        calls: list = []
        manager.safe_all_off = lambda: (
            calls.append(("safe_all_off", window._instruments)) or dict(_B6_ALL_OK)
        )
        manager.disconnect_all = lambda: calls.append(("disconnect_all", window._instruments))
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True, safe_off_attempted=True, safe_off_result=dict(_B6_ALL_OK),
        ))
        assert window._instruments is manager

        window._on_checkpoint_close()

        assert calls == [("safe_all_off", manager), ("disconnect_all", manager)]
        assert window._instruments is None


class _B6ScriptedRunner:
    """Minimalis TestRunner-dublor a valodi TestRunnerWorker-lanchoz
    (worker -> signal -> handler): opcionalis checkpoint-esemeny, majd a
    megadott TestResult."""

    def __init__(self, result, checkpoint_event=None):
        self._result = result
        self._checkpoint_event = checkpoint_event
        self.on_sample = None
        self.on_event = None
        self.on_step_changed = None
        self.stop_requested = False
        self.emergency_stop_requested = False
        self.emergency_stop_reason = ""

    def run(self, plan, start_step_index: int = 0):
        if self._checkpoint_event is not None and self.on_event is not None:
            self.on_event(dict(self._checkpoint_event))
        return self._result

    def request_stop(self):
        pass

    def request_emergency_stop(self, reason: str = "USER_EMERGENCY_STOP"):
        pass


def _b6_run_real_worker(window, runner):
    """A _on_connect_ready() bekotesenek a B6-ot erinto resze, valodi
    TestRunnerWorker-rel, szinkron run()-nal (nincs QThread)."""
    from Prog.gui.worker import TestRunnerWorker

    window._worker = TestRunnerWorker(runner, None)
    window._worker.status_changed.connect(window._update_status_bar_from_worker_status)
    window._worker.finished.connect(window._on_finished)
    window._worker.checkpoint_reached.connect(window._checkpoint_panel.show_checkpoint)
    window._worker.checkpoint_reached.connect(window._on_checkpoint_reached)
    window._worker.run()


class TestR1B6F08CheckpointPanelSafeOffDisabledAfterFinalize:
    """[regresszio, R1 B6, F-08, DFAM-001] a terminalis BQ checkpoint utan a
    checkpoint panel piros "Safe Off" gombja aktiv maradt, pedig a session
    lezarult, a muszer-kapcsolatok bontva -- a gomb hatastalan volt."""

    def _window(self, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._build_runner(_make_cfg())
        window._instruments.disconnect_all = lambda: None
        return window

    def test_real_worker_terminal_checkpoint_disables_safe_off_keeps_warning(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        panel = window._checkpoint_panel
        event = {
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED", "step_name": "manual_bq_checkpoint",
            "next_step_index": 5, "resume_possible": False,
            "total_charge_ah": 1.0, "total_discharge_ah": 0.5,
            "safe_off_result": dict(_B6_PSU_UNCERTAIN),
        }
        result = TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=False, total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True, safe_off_result=dict(_B6_PSU_UNCERTAIN),
        )

        _b6_run_real_worker(window, _B6ScriptedRunner(result, checkpoint_event=event))

        assert panel._emstop_btn.isEnabledTo(panel) is False
        assert "lezárult" in panel._emstop_btn.toolTip()
        # FINAL B6/3: a bizonytalansagjelzo cimke megmarad.
        assert panel._safe_off_warning_lbl.isVisibleTo(panel) is True
        assert "NEM igazolt: PSU" in panel._safe_off_warning_lbl.text()
        # A "Teszt lezarasa" gomb (a ful bezarasa) erintetlen.
        assert panel._close_btn.isEnabledTo(panel) is True

        # Kovetkezo, ELO session checkpointja: a gomb ujra hasznalhato.
        window._build_runner(_make_cfg())
        next_event = dict(event, resume_possible=True, safe_off_result=dict(_B6_ALL_OK))
        next_result = TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True, safe_off_attempted=True, safe_off_result=dict(_B6_ALL_OK),
        )
        _b6_run_real_worker(window, _B6ScriptedRunner(next_result, checkpoint_event=next_event))

        assert panel._emstop_btn.isEnabledTo(panel) is True
        assert panel._emstop_btn.toolTip() == ""

    def test_checkpoint_close_path_also_disables_safe_off(self, qapp, monkeypatch, tmp_path):
        """A _finalize_terminal_session() masik hivoja (_on_checkpoint_close)."""
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        window._instruments.safe_all_off = lambda: dict(_B6_ALL_OK)
        panel = window._checkpoint_panel
        event = {
            "event_code": "MANUAL_BQ_CHECKPOINT_REACHED", "step_name": "cp",
            "next_step_index": 3, "resume_possible": True,
            "safe_off_result": dict(_B6_ALL_OK),
        }
        result = TestResult(
            status="CHECKPOINT_STOPPED", reason="MANUAL_BQ_CHECKPOINT_REACHED",
            resume_possible=True, safe_off_attempted=True, safe_off_result=dict(_B6_ALL_OK),
        )
        _b6_run_real_worker(window, _B6ScriptedRunner(result, checkpoint_event=event))
        assert panel._emstop_btn.isEnabledTo(panel) is True

        window._on_checkpoint_close()

        # isEnabledTo(panel): a gomb SAJAT allapota -- a _on_checkpoint_close()
        # a checkpoint-fulet is letiltja, ami miatt az isEnabled() a szulo
        # miatt mar a regi kodon is False lenne (nem diszkriminalna).
        assert panel._emstop_btn.isEnabledTo(panel) is False


class TestR1B6F31DurableStatusBarQualifier:
    """[regresszio, R1 B6, F-31, DFAM-018] degradalt/bizonytalan safe-off
    DONE utan a tartos statuszsor feltetel nelkul "Kesz | ..." maradt -- a
    popup bezarasa utan semmi nem jelezte a minositett eredmenyt."""

    def _window(self, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._build_runner(_make_cfg())
        window._instruments.disconnect_all = lambda: None
        return window

    def test_done_with_uncertain_safe_off_gets_suffix(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True, safe_off_result=dict(_B6_PSU_UNCERTAIN),
        ))

        assert window._status_bar.currentMessage() == (
            "Kész | Töltve: 1.0000 Ah | Kisütve: 0.5000 Ah" + _B6_F31_SUFFIX
        )

    def test_clean_done_has_exact_message_without_suffix(self, qapp, monkeypatch, tmp_path):
        """Kontroll: nem degradalt DONE (mockolt, OK minositesu session_meta)."""
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        window._finalize_terminal_session = lambda result: {"capacity_result_quality": "OK"}
        window._on_finished(TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True, safe_off_result=dict(_B6_ALL_OK),
        ))

        assert window._status_bar.currentMessage() == (
            "Kész | Töltve: 1.0000 Ah | Kisütve: 0.5000 Ah"
        )

    def test_terminal_checkpoint_degraded_keeps_bq_reminder_and_gets_suffix(
        self, qapp, monkeypatch, tmp_path
    ):
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="CHECKPOINT_STOPPED", resume_possible=False, total_charge_ah=1.5,
            safe_off_attempted=True, safe_off_result=dict(_B6_LOAD_UNCERTAIN),
        ))

        assert window._status_bar.currentMessage() == (
            "Kész | Töltve: 1.5000 Ah | Kisütve: 0.0000 Ah — végezd el a BQ "
            "műveletet, majd zárd le a sessiont." + _B6_F31_SUFFIX
        )

    def test_stopped_degraded_gets_no_suffix(self, qapp, monkeypatch, tmp_path):
        """D-B6-3: az utotag csak a "Kesz"-cimkeju statuszokra."""
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        window._on_finished(TestResult(
            status="STOPPED", reason="USER_STOP_REQUESTED",
            total_charge_ah=0.2, total_discharge_ah=0.0,
            safe_off_attempted=True, safe_off_result=dict(_B6_PSU_UNCERTAIN),
        ))

        assert window._status_bar.currentMessage() == (
            "Leállítva (kézi Stop) | Töltve: 0.2000 Ah | Kisütve: 0.0000 Ah"
        )

    def test_fault_leaves_status_bar_marker_unchanged(self, qapp, monkeypatch, tmp_path):
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        window._status_bar.showMessage("MARKER_BEFORE_ON_FINISHED")
        window._on_finished(TestResult(
            status="FAULT", reason="X",
            safe_off_attempted=True, safe_off_result=dict(_B6_PSU_UNCERTAIN),
        ))

        assert window._status_bar.currentMessage() == "MARKER_BEFORE_ON_FINISHED"

    def test_real_worker_chain_done_ends_with_suffix(self, qapp, monkeypatch, tmp_path):
        """worker.status_changed("DONE") -> _update_status_bar_from_worker_status,
        majd finished -> _on_finished: a VEGSO statuszsor-szoveg az utotagos."""
        from Prog.src.test_runner import TestResult

        window = self._window(monkeypatch, tmp_path)
        result = TestResult(
            status="DONE", total_charge_ah=1.0, total_discharge_ah=0.5,
            safe_off_attempted=True, safe_off_result=dict(_B6_PSU_UNCERTAIN),
        )
        seen: list = []
        window._status_bar.messageChanged.connect(seen.append)

        _b6_run_real_worker(window, _B6ScriptedRunner(result))

        assert "DONE" in seen
        assert window._status_bar.currentMessage() == (
            "Kész | Töltve: 1.0000 Ah | Kisütve: 0.5000 Ah" + _B6_F31_SUFFIX
        )


class TestR1B8cF25DischargeConfigSnapshotInReport:
    """[regresszió, R1 B8c, F-25 (P2), DFAM-021, D-B8-6] a DISCHARGE_ONLY
    session report.json-jából korábban nem volt rekonstruálható a
    ténylegesen alkalmazott kisütési áram-SETPOINT és cutoff -- nem
    alapértelmezett policy (pl. C/20-preset: 1,75 V/cella) mellett a
    profile_snapshot 1,80 V/cellát mutatott. Mostantól a
    _discharge_ctrls_this_session utolsó controllerének config_snapshot()-ja
    `discharge_config_snapshot`-ként a report.json-ba kerül (setpoint, nem
    műszer-readback)."""

    def test_discharge_only_175_per_cell_snapshot_in_report_json(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="DISCHARGE_ONLY",
            discharge_current_A=0.36,
            discharge_terminate_voltage_V=6 * 1.75,
        )
        _runner, _plan, _ = window._build_runner(cfg)
        ctrl = _runner._discharge_ctrl_factory()
        assert window._discharge_ctrls_this_session == [ctrl]

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        snapshot = report["discharge_config_snapshot"]
        assert snapshot["effective_terminate_voltage_V"] == pytest.approx(10.5)
        assert snapshot["effective_discharge_current_set_A"] == pytest.approx(0.36)
        assert snapshot["terminate_voltage_V_override"] == pytest.approx(10.5)
        assert snapshot["discharge_current_A"] == pytest.approx(0.36)
        # a profile_snapshot maga a profil-alapértéket mutatja -- éppen ezért
        # kell a külön, effektív snapshot.
        assert report["profile_snapshot"]["terminate_voltage_per_cell_V"] == pytest.approx(1.80)

    def test_discharge_only_zero_sentinel_resolves_to_profile_in_report(
        self, qapp, monkeypatch, tmp_path,
    ):
        import json
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        cfg = _make_cfg(
            test_type="DISCHARGE_ONLY", discharge_current_A=0.5,
            discharge_terminate_voltage_V=0.0,
        )
        _runner, _plan, _ = window._build_runner(cfg)
        ctrl = _runner._discharge_ctrl_factory()

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        snapshot = report["discharge_config_snapshot"]
        assert snapshot["terminate_voltage_V_override"] == 0.0
        assert snapshot["effective_terminate_voltage_V"] == pytest.approx(
            ctrl._profile.terminate_voltage_pack_V,
        )
        assert snapshot["effective_terminate_voltage_V"] == pytest.approx(6 * 1.80)

    def test_no_discharge_step_snapshot_is_none(self, qapp, monkeypatch, tmp_path):
        import json
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._session_dir = tmp_path / "session_x"

        window._write_report(_FakeResult())

        report = json.loads((window._session_dir / "report.json").read_text(encoding="utf-8"))
        assert "discharge_config_snapshot" in report
        assert report["discharge_config_snapshot"] is None


# ------------------------------------------------------------------ #
# R1 F-29 (DFAM-018+DFAM-001): a BQ Diagnosztika panel késő            #
# (bontás/session-vég UTÁN kézbesített) jelei -- teljes lánc: valódi   #
# QThread + BqDiagnosticsWorker ill. keresztszálas gauge_snapshot_ready #
# -> queued kézbesítés -> BqDiagnosticsPanel handler.                   #
# ------------------------------------------------------------------ #

class TestR1F29BqDiagnosticsLateSignalFullChain:
    @staticmethod
    def _make_driver(voltage_mV: int = 12500, qmax=None, update_status=None):
        from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
        from Prog.tests.mock_drivers.mock_bq34z110_transport import (
            MockBq34z110Transport,
        )
        transport = MockBq34z110Transport()
        driver = Bq34z110ReadOnlyDriver(transport=transport)
        transport.set_register(driver.STD_CMD_VOLTAGE, voltage_mV)
        if qmax is not None:
            block = bytearray(32)
            block[0:2] = qmax.to_bytes(2, "big")
            block[4] = update_status
            transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, bytes(block))
        return driver

    @staticmethod
    def _install_spy_worker(monkeypatch):
        """A VALÓDI BqDiagnosticsWorker alosztálya, amely a konstruktorban
        (tehát a szál indulása ELŐTT) DirectConnection-spyt köt a két
        kimenő jelre: a spy a worker SAJÁT szálán fut, a GUI-szál
        eseményhurkának pumpálása nélkül igazolja a kibocsátást."""
        import threading

        from PySide6.QtCore import Qt

        import Prog.gui.main_window as main_window_module
        from Prog.gui.bq_diagnostics_worker import BqDiagnosticsWorker

        spy = {"snapshot": threading.Event(), "error": threading.Event()}

        class _SpyBqDiagnosticsWorker(BqDiagnosticsWorker):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.snapshot_ready.connect(
                    lambda _s: spy["snapshot"].set(), Qt.ConnectionType.DirectConnection,
                )
                self.connection_error.connect(
                    lambda _m: spy["error"].set(), Qt.ConnectionType.DirectConnection,
                )
                # A teszt a pollokat explicit vezérli; időzített harmadik nem futhat.
                self.snapshot_ready.connect(self.stop_polling, Qt.ConnectionType.DirectConnection)
                self.connection_error.connect(self.stop_polling, Qt.ConnectionType.DirectConnection)

        monkeypatch.setattr(main_window_module, "BqDiagnosticsWorker", _SpyBqDiagnosticsWorker)
        return spy

    @staticmethod
    def _pump(qapp, rounds: int = 10) -> None:
        for _ in range(rounds):
            qapp.processEvents()

    @staticmethod
    def _stop_bq_thread_if_running(window) -> None:
        # Timeout/assert-bukás esetén is garantált száltakarítás.
        if window._bq_thread is not None:
            window._cleanup_bq_thread()

    def _connect_standalone(self, monkeypatch, tmp_path, driver):
        from Prog.gui.main_window import _BqConnectResult
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        spy = self._install_spy_worker(monkeypatch)
        window = MainWindow()
        window._on_bq_connect_ready(
            _BqConnectResult(bq_serial=None, driver=driver, port="COM7")
        )
        return window, spy

    # -- (d) önálló út: valódi worker-snapshot a bontás alatt a sorban -- #

    def test_worker_snapshot_queued_during_standalone_disconnect_is_not_rendered(
        self, qapp, monkeypatch, tmp_path,
    ):
        window, spy = self._connect_standalone(monkeypatch, tmp_path, self._make_driver())
        try:
            spy["snapshot"].wait(timeout=5.0)
            assert spy["snapshot"].is_set()  # igazolt emisszió a bontás ELŐTT
            panel = window._bq_diagnostics_panel
            # GUI-pumpálás nem történt: a snapshot még a GUI-sorban várakozik.
            assert panel._value_labels["voltage_mV"].text() == "—"

            window._on_bq_disconnect_requested()
            assert window._bq_worker is None
            assert window._bq_thread is None

            self._pump(qapp)  # most kézbesül a késő snapshot_ready

            assert panel._value_labels["voltage_mV"].text() == "—"
            assert panel._last_success_perf is None
            assert panel._last_field_values == {}
            assert "frissítés" not in panel._freshness_lbl.text()
            assert panel._status_lbl.text() == "Nincs kapcsolat"
        finally:
            self._stop_bq_thread_if_running(window)

    # -- késő connection_error: valódi worker-hibainjektálás ------------- #

    @staticmethod
    def _patch_worker_poll(monkeypatch, script):
        """A worker modul poll_bq_diagnostics()-át cseréli: a `script`
        listából soronként 'raise' -> kivétel (a worker connection_error
        ága), egyébként a megadott snapshot."""
        import Prog.gui.bq_diagnostics_worker as worker_module

        steps = list(script)

        def _fake_poll(_driver):
            step = steps.pop(0) if steps else "raise"
            if step == "raise":
                raise OSError("simulated serial I/O hiba")
            return step

        monkeypatch.setattr(worker_module, "poll_bq_diagnostics", _fake_poll)

    def test_worker_connection_error_queued_during_disconnect_keeps_no_connection(
        self, qapp, monkeypatch, tmp_path,
    ):
        self._patch_worker_poll(monkeypatch, ["raise"])
        window, spy = self._connect_standalone(monkeypatch, tmp_path, self._make_driver())
        try:
            spy["error"].wait(timeout=5.0)
            assert spy["error"].is_set()  # igazolt hibajel-emisszió a bontás ELŐTT
            panel = window._bq_diagnostics_panel
            assert panel._status_lbl.text() == "Kapcsolódva: COM7"  # még nincs kézbesítve

            window._on_bq_disconnect_requested()
            assert window._bq_worker is None

            self._pump(qapp)  # most kézbesül a késő connection_error

            assert panel._status_lbl.text() == "Nincs kapcsolat"
            assert panel._connect_btn.isEnabled() is True
        finally:
            self._stop_bq_thread_if_running(window)

    def test_worker_connection_error_on_active_connection_then_successful_poll_recovers(
        self, qapp, monkeypatch, tmp_path,
    ):
        """Pozitív kontroll: aktív önálló kapcsolaton a valódi worker
        hibajele megjelenik, és a következő sikeres poll helyreállítja."""
        from PySide6.QtCore import QMetaObject, Qt

        from Prog.src.bq_diagnostics import poll_bq_diagnostics

        good_snapshot = poll_bq_diagnostics(self._make_driver(voltage_mV=12600))
        self._patch_worker_poll(monkeypatch, ["raise", good_snapshot])
        window, spy = self._connect_standalone(monkeypatch, tmp_path, self._make_driver())
        try:
            spy["error"].wait(timeout=5.0)
            assert spy["error"].is_set()
            # A spy az emit közben jelez; a barrier a GUI-sorba állítást is megvárja.
            QMetaObject.invokeMethod(
                window._bq_worker, "stop_polling", Qt.ConnectionType.BlockingQueuedConnection,
            )
            self._pump(qapp)
            panel = window._bq_diagnostics_panel
            assert panel._status_lbl.text() == "Kapcsolódási hiba: simulated serial I/O hiba"

            QMetaObject.invokeMethod(
                window._bq_worker, "poll_once", Qt.ConnectionType.BlockingQueuedConnection,
            )
            spy["snapshot"].wait(timeout=5.0)
            assert spy["snapshot"].is_set()
            self._pump(qapp)

            assert panel._status_lbl.text() == "Kapcsolódva: COM7"
            assert panel._value_labels["voltage_mV"].text() == "12600"
        finally:
            self._stop_bq_thread_if_running(window)

    # -- (e) session-út: keresztszálas gauge_snapshot_ready ------------- #

    @staticmethod
    def _enter_fake_session(monkeypatch, tmp_path):
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()

        class _FakeTransport:
            def disconnect(self):
                pass

        window._session_owns_bq_transport = True
        window._session_bq_transport = _FakeTransport()
        window._session_bq_serial = object()
        window._bq_diagnostics_panel.set_session_owned(True, session_label="GOLDEN_GG")
        return window

    @staticmethod
    def _emit_from_worker_thread(fn) -> None:
        import threading
        t = threading.Thread(target=fn)
        t.start()
        t.join(timeout=5.0)
        assert not t.is_alive()

    def test_gauge_snapshot_queued_across_session_teardown_is_not_rendered(
        self, qapp, monkeypatch, tmp_path,
    ):
        from Prog.src.bq_diagnostics import poll_bq_diagnostics

        window = self._enter_fake_session(monkeypatch, tmp_path)
        panel = window._bq_diagnostics_panel
        snapshot = poll_bq_diagnostics(self._make_driver(voltage_mV=12700))

        self._emit_from_worker_thread(lambda: window.gauge_snapshot_ready.emit(snapshot))
        assert panel._value_labels["voltage_mV"].text() == "—"  # még a GUI-sorban
        window._close_session_bq_transport()  # session-vég
        self._pump(qapp)

        assert panel._session_owned is False
        assert panel._value_labels["voltage_mV"].text() == "—"
        assert panel._last_success_perf is None
        assert panel._status_lbl.text() == "Nincs kapcsolat"

        # Pozitív kontroll: aktív session alatt ugyanez a keresztszálas
        # emit renderel.
        panel.set_session_owned(True, session_label="GOLDEN_GG")
        self._emit_from_worker_thread(lambda: window.gauge_snapshot_ready.emit(snapshot))
        self._pump(qapp)
        assert panel._value_labels["voltage_mV"].text() == "12700"

    def test_final_session_snapshot_queued_before_finished_is_logged_and_kept(
        self, qapp, monkeypatch, tmp_path,
    ):
        """A session-runner a végső gauge-snapshotot és a `finished`-et
        ugyanarról a szálról, ebben a sorrendben emittálja; a queued
        kézbesítés FIFO, tehát a végső snapshot a session-lezárás ELŐTT
        jut a panelhez -- és a lezárás után az evidence megmarad."""
        from PySide6.QtCore import QObject, Qt, Signal

        from Prog.src.bq_diagnostics import poll_bq_diagnostics

        window = self._enter_fake_session(monkeypatch, tmp_path)
        panel = window._bq_diagnostics_panel
        first = poll_bq_diagnostics(self._make_driver(qmax=7000, update_status=0x04))
        final = poll_bq_diagnostics(self._make_driver(qmax=7000, update_status=0x05))

        class _RunnerFinishedEmitter(QObject):
            finished = Signal()

        emitter = _RunnerFinishedEmitter()
        emitter.finished.connect(
            window._close_session_bq_transport, Qt.ConnectionType.QueuedConnection,
        )

        def _runner_thread():
            window.gauge_snapshot_ready.emit(first)
            window.gauge_snapshot_ready.emit(final)
            emitter.finished.emit()

        self._emit_from_worker_thread(_runner_thread)
        assert panel._session_owned is True  # a lezárás is még a sorban
        self._pump(qapp)

        assert panel._session_owned is False
        lst = panel._session_event_log._list
        texts = [lst.item(i).text() for i in range(lst.count())]
        assert len(texts) == 1, texts
        assert "BQ_UPDATE_STATUS_CHANGED" in texts[0]
        assert "0x0004" in texts[0] and "0x0005" in texts[0]


# ---------------------------------------------------------------------------
# R2-B3 (R2-SES-01 = R2-GUI-01, DFAM-001/032): bring-up fazisjelzo, resume-modal
# alatti veszhook, kapu utani Qt-reentrancia, fail-closed kapu, failed-ag reset.
# ---------------------------------------------------------------------------

RESUME_TITLE = "Megszakadt session folytatható"
OK4 = {"Load": True, "PSU": True, "DMM_V": True, "DMM_T": True}


def _fill_config(window):
    cp = window._config_panel
    cp._model_edit.setText("TEST7AH")
    cp._capacity_spin.setValue(6.0)
    cp._psu_res_edit.setText("MOCK_PSU")
    cp._load_res_edit.setText("MOCK_LOAD")
    cp._dmm_v_res_edit.setText("MOCK_DMM_V")
    cp._dmm_t_res_edit.setText("MOCK_DMM_T")
    cp._psu_mode_combo.setCurrentText("INDEPENDENT")


def _pump_until(qapp, cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert cond()


def _fire():
    try:
        raise RuntimeError("kivetel a GUI-szalon")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())


def _chain_setup(monkeypatch, tmp_path, reply, fire):
    _patch_hardware(monkeypatch)
    monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
    calls: list = []
    monkeypatch.setattr(InstrumentManager, "safe_all_off",
                        lambda self: (calls.append(("safe_all_off", id(self))) or dict(OK4)))
    monkeypatch.setattr(InstrumentManager, "disconnect_all",
                        lambda self: calls.append(("disconnect_all", id(self))))
    window = MainWindow()
    _fill_config(window)
    parent = window._sessions_root() / "session_TEST7AH_20200101_000000"
    parent.mkdir(parents=True)
    monkeypatch.setattr(sr, "find_resumable_session", lambda *a, **k: {
        "session_dir": parent, "checkpoint": {"next_safe_step_index": 0},
        "unreadable": False, "evaluation": None})
    prev: list = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: prev.append(a))
    _install_emergency_excepthook(window)
    at_hook: dict = {}

    def _question(parent_w, title, *a, **k):
        if title == RESUME_TITLE:
            at_hook["bring_up"] = getattr(window, "_bring_up_in_progress", None)
            at_hook["calls_before"] = list(calls)
            if fire:
                _fire()
            at_hook["calls_after_hook"] = list(calls)
            at_hook["abort_after_hook"] = window._connect_abort_requested
            return reply
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", _question)
    criticals: list = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a[1:3]))
    trw = MagicMock()
    monkeypatch.setattr(mw, "TestRunnerWorker", trw)
    return window, parent, calls, prev, at_hook, criticals, trw


def _done(window, at_hook):
    return ("calls_after_hook" in at_hook and window._connect_worker is None
            and not getattr(window, "_bring_up_in_progress", False))


def _stop_thread(window):
    if window._thread is not None:
        window._thread.quit()
        window._thread.wait(2000)


YES_NO = [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No]


class TestR2B3ResumeModalHookChain:
    @pytest.mark.parametrize("reply", YES_NO)
    def test_hook_during_resume_modal_aborts_before_marker(self, qapp, monkeypatch, tmp_path, reply):
        window, parent, calls, prev, at_hook, criticals, trw = _chain_setup(
            monkeypatch, tmp_path, reply, fire=True)
        try:
            window._start_test()
            _pump_until(qapp, lambda: _done(window, at_hook))
            names = [n for n, _ in calls]
            assert window._worker is None
            assert trw.call_count == 0
            assert at_hook["calls_after_hook"] == at_hook["calls_before"]
            assert at_hook["abort_after_hook"] is True
            assert at_hook["bring_up"] is True
            assert names == ["safe_all_off", "disconnect_all"]
            assert len({i for _, i in calls}) == 1
            assert not (parent / "report.json").exists()
            assert sorted(p.name for p in window._sessions_root().iterdir()) == [parent.name]
            assert window._instruments is None
            assert window._bring_up_in_progress is False
            assert window._connect_abort_requested is False
            assert window._live_panel._start_btn.isEnabled() is True
            assert window._status_bar.currentMessage() == "Inicializálási hiba."
            assert len(criticals) == 1 and "VÉSZLEÁLLÍTÁS" in criticals[0][1]
            assert len(prev) == 1
        finally:
            _stop_thread(window)

    @pytest.mark.parametrize("reply", YES_NO)
    def test_control_without_hook_session_starts(self, qapp, monkeypatch, tmp_path, reply):
        window, parent, calls, _prev, at_hook, criticals, trw = _chain_setup(
            monkeypatch, tmp_path, reply, fire=False)
        try:
            window._start_test()
            _pump_until(qapp, lambda: _done(window, at_hook))
            assert window._worker is not None
            assert trw.call_count == 1
            assert calls == []
            assert (parent / "report.json").exists() is (reply == QMessageBox.StandardButton.Yes)
            assert criticals == []
            assert getattr(window, "_bring_up_in_progress", False) is False
        finally:
            _stop_thread(window)


class TestR2B3BringUpFlagLifecycle:
    def _window(self, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        return MainWindow()

    def test_hook_with_only_bring_up_flag_sets_abort_without_io(self, qapp, monkeypatch, tmp_path):
        window = self._window(monkeypatch, tmp_path)
        window._build_runner(_make_cfg())
        io: list = []
        window._instruments.safe_all_off = lambda: io.append(1) or {}
        window._bring_up_in_progress = True
        prev: list = []
        monkeypatch.setattr(sys, "excepthook", lambda *a: prev.append(a))
        _install_emergency_excepthook(window)
        _fire()
        assert io == []
        assert window._connect_abort_requested is True
        assert len(prev) == 1

    def test_connect_failed_resets_flag(self, qapp, monkeypatch, tmp_path):
        window = self._window(monkeypatch, tmp_path)
        window._bring_up_in_progress = True
        window._on_connect_failed("x")
        assert window._bring_up_in_progress is False

    @pytest.mark.parametrize("exit_path", ["abort", "cfg_none", "build_fail", "unexpected"])
    def test_on_connect_ready_resets_flag_on_every_exit(self, qapp, monkeypatch, tmp_path, exit_path):
        window = self._window(monkeypatch, tmp_path)
        calls: list = []
        window._bring_up_in_progress = True
        window._pending_start_cfg = None if exit_path == "cfg_none" else _make_cfg()
        if exit_path == "abort":
            window._connect_abort_requested = True
        if exit_path == "build_fail":
            def _bf(cfg, pre_connected=None):
                raise RuntimeError("b")
            window._build_runner = _bf
        pre = _b6_pre_connected(calls, {"Load": True, "PSU": True})
        if exit_path == "unexpected":
            def _boom():
                raise RuntimeError("varatlan")
            window._cleanup_connect_thread = _boom
            with pytest.raises(RuntimeError):
                window._on_connect_ready(pre)
        else:
            window._on_connect_ready(pre)
        assert window._bring_up_in_progress is False
        assert window._connect_abort_requested is False

    def test_success_path_resets_flag_and_idle_hook_not_stuck(self, qapp, monkeypatch, tmp_path):
        from Prog.gui.main_window import _construct_and_connect_instruments

        window = self._window(monkeypatch, tmp_path)
        monkeypatch.setattr(mw, "QThread", MagicMock())
        monkeypatch.setattr(mw, "TestRunnerWorker", MagicMock())
        cfg = _make_cfg()
        window._pending_start_cfg = cfg
        window._bring_up_in_progress = True
        window._on_connect_ready(_construct_and_connect_instruments(cfg, False))
        assert window._worker is not None
        assert window._bring_up_in_progress is False
        window._worker = None
        io: list = []
        window._instruments.safe_all_off = lambda: io.append(1) or {"Load": True, "PSU": True}
        prev: list = []
        monkeypatch.setattr(sys, "excepthook", lambda *a: prev.append(a))
        _install_emergency_excepthook(window)
        _fire()
        assert io == [1]
        assert window._connect_abort_requested is False


# ---- R2-B3 v2: Codex B3-PLAN-01 / B3-PLAN-02 + backstop -------------------


class _Emitter(QObject):
    sig = Signal()


def _raising_slot_once(log: list):
    state = {"done": False}

    def _slot():
        if not state["done"]:
            state["done"] = True
            log.append("nested_slot_raised")
            raise RuntimeError("beagyazott Qt-slot kivetel")
    return _slot


def _ready_setup(monkeypatch, tmp_path):
    from Prog.gui.main_window import _construct_and_connect_instruments

    _patch_hardware(monkeypatch)
    monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
    log: list = []
    monkeypatch.setattr(InstrumentManager, "safe_all_off",
                        lambda self: (log.append("safe_all_off") or dict(OK4)))
    monkeypatch.setattr(InstrumentManager, "disconnect_all",
                        lambda self: log.append("disconnect_all"))
    monkeypatch.setattr(sr, "find_resumable_session", lambda *a, **k: None)
    thread_cls = MagicMock()
    thread_cls.return_value.start.side_effect = lambda: log.append("thread_start")
    monkeypatch.setattr(mw, "QThread", thread_cls)
    trw = MagicMock()
    trw.return_value.request_emergency_stop.side_effect = (
        lambda reason: log.append(("request_emergency_stop", reason)))
    monkeypatch.setattr(mw, "TestRunnerWorker", trw)
    criticals: list = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: criticals.append(a[1:3]))
    window = MainWindow()
    cfg = _make_cfg()
    window._pending_start_cfg = cfg
    window._bring_up_in_progress = True  # _start_test() allapota (connect-szal utan)
    prev: list = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: prev.append(a))
    _install_emergency_excepthook(window)
    pre = _construct_and_connect_instruments(cfg, False)
    return window, pre, log, trw, criticals, prev


class TestR2B3PostGateQtReentrancy:
    """[Codex B3-PLAN-02] a kapu UTANI GUI-elokeszites szinkron Qt-jelenek
    slotjabol jovo kivetel (hook -> abort) nem veszhet el."""

    @pytest.mark.parametrize("emitter", ["device_error_cleared", "tabs_current_changed"])
    def test_nested_slot_abort_is_enforced_before_session(self, qapp, monkeypatch, tmp_path, emitter):
        window, pre, log, trw, criticals, prev = _ready_setup(monkeypatch, tmp_path)
        slot = _raising_slot_once(log)
        if emitter == "device_error_cleared":
            window._device_error_panel.cleared.connect(slot)
        else:
            window._tabs.setCurrentIndex(0)
            window._tabs.currentChanged.connect(slot)

        window._on_connect_ready(pre)

        assert "nested_slot_raised" in log
        assert len(prev) == 1
        assert trw.call_count == 0
        assert window._worker is None
        assert "thread_start" not in log
        assert [e for e in log if e in ("safe_all_off", "disconnect_all")] == [
            "safe_all_off", "disconnect_all"]
        assert not window._sessions_root().exists() or list(window._sessions_root().iterdir()) == []
        assert window._bring_up_in_progress is False
        assert window._connect_abort_requested is False
        assert len(criticals) == 1 and "VÉSZLEÁLLÍTÁS" in criticals[0][1]

    def test_control_no_nested_exception_session_starts(self, qapp, monkeypatch, tmp_path):
        window, pre, log, trw, criticals, prev = _ready_setup(monkeypatch, tmp_path)
        window._on_connect_ready(pre)
        assert trw.call_count == 1
        assert log == ["thread_start"]
        assert criticals == [] and prev == []

    def test_backstop_abort_in_residual_window_stops_runner_before_thread_start(
        self, qapp, monkeypatch, tmp_path
    ):
        """A kapu es a worker-hozzarendeles kozti (ma Qt-callback-mentes) ablakba
        injektalt VALODI beagyazott Qt-slot kivetel: a worker-inditas elott a
        runner sajat veszleallitasi utjara kerul (nincs elveszett abort)."""
        window, pre, log, trw, _criticals, _prev = _ready_setup(monkeypatch, tmp_path)
        em = _Emitter()
        em.sig.connect(_raising_slot_once(log))
        inner = trw.side_effect

        def _ctor(*a, **k):
            em.sig.emit()  # beagyazott kivetel: hook -> _worker meg None
            return trw.return_value

        trw.side_effect = _ctor
        window._on_connect_ready(pre)
        trw.side_effect = inner

        stops = [e for e in log if isinstance(e, tuple)]
        assert len(stops) == 1
        assert stops[0][1].startswith("UNHANDLED_EXCEPTION: ")
        assert log.index(stops[0]) < log.index("thread_start")
        assert "safe_all_off" not in log  # a hook nem adott ki muszer-I/O-t
        assert window._bring_up_in_progress is False
        assert window._connect_abort_requested is False


class TestR2B3ConnectFailedFlagReset:
    """[Codex B3-PLAN-01, DFAM-032] _on_connect_failed() kivetel eseten is
    nullazza mindket flaget."""

    @pytest.mark.parametrize("raise_in_cleanup", [False, True])
    def test_flags_reset_on_failed_path(self, qapp, monkeypatch, tmp_path, raise_in_cleanup):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._bring_up_in_progress = True
        window._connect_abort_requested = True
        if raise_in_cleanup:
            def _boom():
                raise RuntimeError("GUI-frissites hiba a cleanup vegen")
            window._refresh_calibration_blocked_state = _boom
            with pytest.raises(RuntimeError):
                window._on_connect_failed("x")
        else:
            window._on_connect_failed("x")
        assert window._bring_up_in_progress is False
        assert window._connect_abort_requested is False


# ---------------------------------------------------------------------------
# R2-B6 (R2-GUI-02/03/04, DFAM-018/001): tartos F-31 minosito utotag a Close-on,
# riport-kudarcnal nincs report.json-hivatkozas, elfogadott Start = session-hatar
# (checkpoint-ful tiltas + utotag-reset), bring-up alatti regi Close hatastalan.
# ---------------------------------------------------------------------------

F31 = " — FIGYELEM: minősített eredmény, l. a záró üzenetet és a report.json-t"
NOREP = " — FIGYELEM: a report.json NEM készült el, l. a záró üzenetet"
CLOSED = "Teszt lezárva — konfigurálj új mérést."
PSU_BAD = {"Load": True, "PSU": False, "DMM_V": True, "DMM_T": True}


def _terminal_checkpoint(monkeypatch, tmp_path, sor, meta="real"):
    _patch_hardware(monkeypatch)
    monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
    window = MainWindow()
    window._build_runner(_make_cfg(test_type="BQ_LEARNING_PHYSICAL"))
    window._instruments.disconnect_all = lambda: None
    if meta != "real":
        window._finalize_terminal_session = lambda result: meta
    event = {"event_code": "MANUAL_BQ_CHECKPOINT_REACHED", "resume_possible": False,
             "safe_off_result": dict(sor)}
    window._checkpoint_panel.show_checkpoint(event)
    window._on_checkpoint_reached(event)
    window._on_finished(_R2TestResult(status="CHECKPOINT_STOPPED", resume_possible=False,
                                   safe_off_attempted=True, safe_off_result=dict(sor),
                                   total_charge_ah=1.0, total_discharge_ah=1.0))
    return window


def _accepted_start(window, monkeypatch, answer=QMessageBox.StandardButton.Yes):
    _fill_config(window)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answer)

    def _stop(cfg, pre_connected=None):
        raise RuntimeError("stop before real TestRunnerWorker/QThread in test")

    window._build_runner = _stop
    window._start_test()


def _pump_connect_done(qapp, window):
    deadline = time.monotonic() + 5.0
    while window._connect_worker is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert window._connect_worker is None


class TestR2B6Gui02Gui03DurableQualifier:
    def test_close_keeps_f31_suffix(self, qapp, monkeypatch, tmp_path):
        window = _terminal_checkpoint(monkeypatch, tmp_path, PSU_BAD)
        assert window._status_bar.currentMessage().endswith(F31)
        window._on_checkpoint_close()
        assert window._status_bar.currentMessage() == CLOSED + F31

    def test_clean_close_control_exact(self, qapp, monkeypatch, tmp_path):
        window = _terminal_checkpoint(monkeypatch, tmp_path, OK4,
                                      meta={"capacity_result_quality": "OK"})
        window._on_checkpoint_close()
        assert window._status_bar.currentMessage() == CLOSED

    def test_close_after_report_failure_keeps_no_report_suffix(self, qapp, monkeypatch, tmp_path):
        window = _terminal_checkpoint(monkeypatch, tmp_path, OK4, meta=None)
        assert window._status_bar.currentMessage().endswith(NOREP)
        window._on_checkpoint_close()
        assert window._status_bar.currentMessage() == CLOSED + NOREP

    def test_done_report_failure_does_not_reference_report_json(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)

        def _boom(self, meta):
            raise OSError("lemez megtelt (szimulalt)")

        monkeypatch.setattr(report_generator.ReportGenerator, "generate", _boom)
        window = MainWindow()
        window._build_runner(_make_cfg(test_type="CHARGE_ONLY"))
        window._instruments.disconnect_all = lambda: None
        window._on_finished(_R2TestResult(status="DONE", safe_off_attempted=True,
                                       safe_off_result=dict(OK4),
                                       total_charge_ah=1.0, total_discharge_ah=0.0))
        assert not (window._session_dir / "report.json").exists()
        assert window._status_bar.currentMessage() == (
            "Kész | Töltve: 1.0000 Ah | Kisütve: 0.0000 Ah" + NOREP)


class TestR2B6Gui04CheckpointLifecycle:
    def test_accepted_start_disables_tab_and_clears_suffix(self, qapp, monkeypatch, tmp_path):
        window = _terminal_checkpoint(monkeypatch, tmp_path, PSU_BAD)
        assert window._tabs.isTabEnabled(window._checkpoint_tab_index) is True
        _accepted_start(window, monkeypatch)
        try:
            assert window._tabs.isTabEnabled(window._checkpoint_tab_index) is False
            assert getattr(window, "_terminal_status_suffix", "") == ""
        finally:
            _pump_connect_done(qapp, window)
        assert window._tabs.isTabEnabled(window._checkpoint_tab_index) is False

    def test_close_during_bring_up_is_inert(self, qapp, monkeypatch, tmp_path):
        window = _terminal_checkpoint(monkeypatch, tmp_path, PSU_BAD)
        old_dir = window._session_dir
        _accepted_start(window, monkeypatch)
        try:
            window._on_checkpoint_close()
            assert window._status_bar.currentMessage() == "Csatlakozás a műszerekhez…"
            assert window._live_panel._start_btn.isEnabled() is False
            assert not (old_dir / "bq_checkpoint_checklist.json").exists()
        finally:
            _pump_connect_done(qapp, window)

    def test_rejected_start_keeps_old_checkpoint_close(self, qapp, monkeypatch, tmp_path):
        window = _terminal_checkpoint(monkeypatch, tmp_path, PSU_BAD)
        _accepted_start(window, monkeypatch, answer=QMessageBox.StandardButton.No)
        assert window._connect_worker is None
        assert window._tabs.isTabEnabled(window._checkpoint_tab_index) is True
        window._on_checkpoint_close()
        assert window._status_bar.currentMessage() == CLOSED + F31
        assert (window._session_dir / "bq_checkpoint_checklist.json").exists()


class TestR13B4PsuOffFailureGuiChain:
    """Real worker -> manager -> PSU ALL OFF failure; Load is still attempted.
    No runner starts, Start is restored and rollback sends OFF. Unit/mock only.
    """

    def test_off_failure_reaches_operator_and_session_does_not_start(
        self, qapp, monkeypatch, tmp_path
    ):
        import re
        import types

        from PySide6.QtWidgets import QMessageBox

        from Prog.drivers.device_load import Keithley2380Load
        from Prog.gui.main_window import _InstrumentConnectWorker

        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        res = MagicMock(name="psu_visa")
        def fail_connect_off(cmd, *a, **k):
            if cmd == "OUTP OFF;*OPC?" and sum(
                c.args[0] == cmd for c in res.query.call_args_list
            ) <= 3:
                raise TimeoutError("simulated ALL OFF timeout")
            return '+0,"No error"'

        res.query.side_effect = fail_connect_off
        opened: list = []

        def _open_resource(resource, *a, **k):
            opened.append(resource)
            return res

        monkeypatch.setitem(
            sys.modules, "pyvisa",
            types.SimpleNamespace(
                ResourceManager=lambda: MagicMock(open_resource=_open_resource)
            ),
        )
        load_connect = MagicMock(name="load_connect")
        monkeypatch.setattr(Keithley2380Load, "connect", load_connect)
        dmm_connect = MagicMock(name="dmm_connect")
        monkeypatch.setattr(Keysight34465ADMM, "connect", dmm_connect)
        criticals: list = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            lambda *a, **k: criticals.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = MainWindow()
        build_calls: list = []
        window._build_runner = lambda cfg, pre_connected=None: build_calls.append(cfg)
        cfg = _make_cfg(psu_mode="INDEPENDENT")
        window._pending_start_cfg = cfg
        window._live_panel.set_start_enabled(False)
        window._live_panel.set_emergency_stop_enabled(True)
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        window._connect_worker = worker
        ready: list = []
        failed: list = []
        worker.connect_ready.connect(ready.append)
        worker.connect_failed.connect(failed.append)
        worker.connect_ready.connect(window._on_connect_ready)
        worker.connect_failed.connect(window._on_connect_failed)

        worker.run()

        assert ready == []
        assert len(failed) == 1
        assert "OUTP OFF;*OPC?" in failed[0]
        assert "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" in failed[0]
        assert criticals == failed
        assert build_calls == []
        assert window._worker is None
        assert window._connect_worker is None
        assert window._live_panel._start_btn.isEnabled() is True
        assert opened == [cfg.psu_resource]
        # R6-B1 (R5-B1-N3): a PSU-hiba után a Load connect()-je is lefut.
        load_connect.assert_called_once_with(cfg.load_resource)
        dmm_connect.assert_not_called()
        sent = [c.args[0] for c in res.query.call_args_list] + [
            c.args[0] for c in res.write.call_args_list
        ]
        on = re.compile(r"(^|;|:)\s*(OUTP|OUTP:ENAB)\s+(ON|1)\b", re.IGNORECASE)
        assert not any(on.search(m) for m in sent)
        # Three failed connect OFF attempts, then successful rollback OFF.
        assert [c.args[0] for c in res.query.call_args_list].count("OUTP OFF;*OPC?") == 4
        res.close.assert_called_once_with()


class _VisaLikeOpenError(Exception):
    """R6-B1 verifier N-1: a pyvisa VisaIOError MRO-ja Error -> Exception, NEM
    OSError (és nem InstrumentError) -- az open_failure ág ilyennel dobjon,
    különben egy `except (InstrumentError, OSError)`-ra szűkített connect_all()
    mutáns (amely a kikapcsolt/nem található PSU-nál újra kihagyná a Load OFF-ot)
    túlélné a tesztet."""


class TestR6B1PsuGateFailureStillTurnsLoadOffGuiChain:
    """[regresszio, R6 B1: R5-B1-N3, DFAM-016 x DFAM-050] GUI-lanc: valodi
    _InstrumentConnectWorker -> valodi _construct_and_connect_instruments()
    -> valodi InstrumentManager.connect_all() -> valodi Keithley2220PSU es
    Keithley2380Load (allapottarto VISA-double-ok, resource szerint
    routolva; a Load bemenete egy korabbi sessionbol ON). A PSU connect()
    bukik; elvaras: a Load sajat INP OFF-ja + a rollback INP OFF-ja kimegy,
    a Load double vegallapota OFF, ON-parancs nem megy ki, a DMM-hez nem
    jut el, egyetlen connect_failed, connect_ready es _build_runner nincs,
    a Start ujra engedelyezett. Unit/mock szint; fizikai OFF-ot nem bizonyit."""

    @pytest.mark.parametrize("psu_case", ["off_timeout", "open_failure"])
    def test_psu_failure_load_off_and_session_does_not_start(
        self, qapp, monkeypatch, tmp_path, psu_case
    ):
        import types

        from Prog.gui.main_window import _InstrumentConnectWorker
        from Prog.tests.test_device_load import _StickyLoadResource
        from Prog.tests.test_device_psu import _energizing, _StatefulConnect2220Resource

        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        cfg = _make_cfg(psu_mode="INDEPENDENT")
        if psu_case == "off_timeout":
            psu_res = _StatefulConnect2220Resource()
            psu_res.fail_before_execute = lambda msg: (
                msg == "OUTP OFF;*OPC?" and psu_res.messages.count(msg) <= 3
            )
        else:
            psu_res = _VisaLikeOpenError("VI_ERROR_RSRC_NFOUND")
        load_res = _StickyLoadResource(inp=True, short=True, timer=True)
        routes = {cfg.psu_resource: psu_res, cfg.load_resource: load_res}
        opened: list = []

        def _open_resource(resource, *a, **k):
            opened.append(resource)
            target = routes[resource]
            if isinstance(target, BaseException):
                raise target
            return target

        monkeypatch.setitem(
            sys.modules, "pyvisa",
            types.SimpleNamespace(
                ResourceManager=lambda: MagicMock(open_resource=_open_resource)
            ),
        )
        dmm_connect = MagicMock(name="dmm_connect")
        monkeypatch.setattr(Keysight34465ADMM, "connect", dmm_connect)
        criticals: list = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            lambda *a, **k: criticals.append(a[2] if len(a) > 2 else k.get("text", "")),
        )
        window = MainWindow()
        build_calls: list = []
        window._build_runner = lambda c, pre_connected=None: build_calls.append(c)
        window._pending_start_cfg = cfg
        window._live_panel.set_start_enabled(False)
        window._live_panel.set_emergency_stop_enabled(True)
        worker = _InstrumentConnectWorker(cfg, needs_bq_transport=False)
        window._connect_worker = worker
        ready: list = []
        failed: list = []
        worker.connect_ready.connect(ready.append)
        worker.connect_failed.connect(failed.append)
        worker.connect_ready.connect(window._on_connect_ready)
        worker.connect_failed.connect(window._on_connect_failed)

        worker.run()

        assert ready == []
        assert len(failed) == 1
        # Fail-closed szoveg rogzitese (R6 FINAL B1, Codex-ajanlas): a
        # connect_all()-kivetel nem hordoz safe_off_attempted-et.
        assert "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" in failed[0]
        assert criticals == failed
        assert build_calls == []
        assert window._worker is None
        assert window._connect_worker is None
        assert window._live_panel._start_btn.isEnabled() is True
        assert opened == [cfg.psu_resource, cfg.load_resource]
        dmm_connect.assert_not_called()
        # Load: a sajat connect()-beli INP OFF + a rollback safe_off() INP OFF.
        assert load_res.msgs("Q").count("INP OFF;*OPC?") == 2
        assert load_res.state["INP"] is False
        assert load_res.closed is True
        assert not any("INP ON" in m.upper() for _k, m in load_res.log)
        if psu_case != "open_failure":
            assert psu_res.output_state is False and psu_res.energized == set()
            assert psu_res.closed is True
            assert _energizing(psu_res.messages) == []


class TestR9B4SocEstimateMethod:
    """R9-B4: controller -> _write_report() -> JSON, tenyleges kapacitasalap."""

    def _make_window(self, qapp, monkeypatch, tmp_path):
        _patch_hardware(monkeypatch)
        _use_hil_verified_ocv_soc_pulse_interval(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        return MainWindow()

    def _build_ocv(self, window, monkeypatch, measured_capacity_Ah):
        from Prog.src import ocv_soc_controller as ocv_module

        original = ocv_module.OcvSocController
        if measured_capacity_Ah is not None:
            def _make_controller(*args, **kwargs):
                return original(
                    *args, measured_capacity_Ah=measured_capacity_Ah, **kwargs
                )

            monkeypatch.setattr(ocv_module, "OcvSocController", _make_controller)

        runner, _plan, _start = window._build_runner(
            _make_cfg(test_type="OCV_SOC_CHARACTERIZATION")
        )
        try:
            ctrl = runner._ocv_soc_ctrl_factory()
            assert isinstance(ctrl, original)
            assert window._last_ocv_soc_ctrl is ctrl
            return ctrl
        finally:
            runner._logger.close()

    def _report(self, window):
        import json

        meta = window._write_report(_R2TestResult(status="DONE"))
        assert meta is not None
        return json.loads(
            (window._session_dir / "report.json").read_text(encoding="utf-8")
        )

    @pytest.mark.parametrize(
        "measured_capacity_Ah, quality, method",
        [
            (
                None, "NOMINAL_CAPACITY_BASED_SOC",
                "coulomb_counting_from_nominal_capacity",
            ),
            (
                6.0, "MEASURED_CAPACITY_BASED",
                "coulomb_counting_from_measured_capacity",
            ),
        ],
    )
    def test_ocv_controller_basis_reaches_report(
        self, qapp, monkeypatch, tmp_path, measured_capacity_Ah, quality, method
    ):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        ctrl = self._build_ocv(window, monkeypatch, measured_capacity_Ah)
        assert ctrl.capacity_basis_quality == quality
        assert self._report(window)["SOC_estimate_method"] == method

    def test_charge_only_without_ocv_controller_has_no_soc_method(
        self, qapp, monkeypatch, tmp_path
    ):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        runner, _plan, _start = window._build_runner(
            _make_cfg(test_type="CHARGE_ONLY")
        )
        runner._logger.close()
        assert window._last_ocv_soc_ctrl is None
        assert self._report(window)["SOC_estimate_method"] is None

    @pytest.mark.parametrize("measured_capacity_Ah", [None, 6.0])
    def test_new_session_does_not_reuse_previous_ocv_controller(
        self, qapp, monkeypatch, tmp_path, measured_capacity_Ah
    ):
        window = self._make_window(qapp, monkeypatch, tmp_path)
        self._build_ocv(window, monkeypatch, measured_capacity_Ah)
        previous_report = self._report(window)
        assert previous_report["SOC_estimate_method"] == (
            "coulomb_counting_from_nominal_capacity"
            if measured_capacity_Ah is None
            else "coulomb_counting_from_measured_capacity"
        )

        runner, _plan, _start = window._build_runner(
            _make_cfg(test_type="CHARGE_ONLY", battery_model="NEXT_SESSION")
        )
        runner._logger.close()
        assert window._last_ocv_soc_ctrl is None
        assert self._report(window)["SOC_estimate_method"] is None


class TestR11B3RelaxTemperatureEventChain:
    """R11-B3: factory -> logger es worker -> LivePanel, esemenyhurok nelkul."""

    @pytest.mark.parametrize(
        ("mode", "charge_override", "discharge_override"),
        [("ENABLED", 0.0, 0.0), ("MONITOR_ONLY", 0.8, 0.7)],
    )
    def test_one_log_and_live_event_per_fault_transition(
        self, qapp, monkeypatch, mode, charge_override, discharge_override,
    ):
        import Prog.src.logger as logger_module
        from Prog.gui.worker import TestRunnerWorker
        from Prog.src.test_runner import StepKind, TestStep
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        monkeypatch.setattr(
            app_paths, "exe_dir", lambda: Path(__file__).with_name("r11_b3_memory"),
        )
        monkeypatch.setattr(sr, "discover_incomplete_sessions", lambda *_a, **_k: [])
        monkeypatch.setattr(sr, "find_resumable_session", lambda *_a, **_k: None)
        logger = MagicMock(spec=Logger)
        monkeypatch.setattr(logger_module, "Logger", lambda *_a, **_k: logger)
        psu, load = MockPSU(), MockLoad()
        dmm_v = MockDMM(voltage_V=12.5)
        dmm_t = MockDMM(temperature_C=22.0, simulate_temp_failure=True)
        instruments = InstrumentManager(psu, load, dmm_v, dmm_t)
        connected = mw._PreConnectedInstruments(
            instruments=instruments, psu=psu, load=load, dmm_v=dmm_v, dmm_t=dmm_t,
            bq_transport=None, session_bq_serial=None, instrument_identities={},
        )
        window = MainWindow()
        cfg = _make_cfg(
            test_type="CHARGE_ONLY", temperature_compensation_mode=mode,
            relax_after_charge_s=1000.0, charge_current_A_override=charge_override,
            discharge_current_A=discharge_override,
        )
        runner, plan, _ = window._build_runner(cfg, pre_connected=connected)
        worker = TestRunnerWorker(runner, plan)
        worker.event_ready.connect(window._live_panel.append_event)
        received = []
        worker.event_ready.connect(received.append)
        ctrl = runner._relax_ctrl_factory(TestStep(StepKind.RELAX, "relax_after_charge"))
        logger.log_event.reset_mock()
        event_list = window._live_panel._event_log._list

        first_fault_tick = 1 if mode == "ENABLED" else 30
        for tick in range(1, 101):
            ctrl.advance(2.0)
            expected = int(tick >= first_fault_tick)
            assert logger.log_event.call_count == expected
            assert len(received) == expected
            assert event_list.count() == expected
        first = logger.log_event.call_args
        assert first.args[0] == "RELAX_TEMPERATURE_DMM_FAULT"
        assert first.kwargs == {"is_critical": True}
        assert received[0]["severity"] == "CRITICAL"
        assert "CRITICAL" in event_list.item(0).text()

        dmm_t.simulate_temp_failure = False
        ctrl.advance(2.0)
        assert logger.log_event.call_count == 1
        dmm_t.simulate_temp_failure = True
        for tick in range(1, 101):
            ctrl.advance(2.0)
            expected = 1 + int(tick >= first_fault_tick)
            assert logger.log_event.call_count == expected
            assert len(received) == expected
            assert event_list.count() == expected

        assert ctrl.temp_dmm_fault_ticks == 200
        assert ctrl.last_warning_code == "TEMPERATURE_MONITOR_LOST_CRITICAL"
        assert all(e["event_code"] == "RELAX_TEMPERATURE_DMM_FAULT" for e in received)
        assert all(e["severity"] == "CRITICAL" for e in received)
        assert not psu.output_commanded_on
        assert not load.input_commanded_on
        assert psu.calls == []
        assert load.calls == []


class TestR16B5bCleanup:
    @staticmethod
    def make(monkeypatch):
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock

        import Prog.gui.main_window as module
        from Prog.drivers.device_dmm import DMMMeasurementBusy
        from Prog.src.test_runner import TestResult

        monkeypatch.setattr(module, "_PENDING_DISCONNECTS", [])
        manager = Mock()
        manager.disconnect_all.side_effect = DMMMeasurementBusy("foglalt")
        manager.safe_all_off.return_value = {"Load": True, "PSU": True}
        port = Mock()
        events, files = [], {}
        class MemoryPath:
            parent = property(lambda self: self)
            def __truediv__(self, name):
                assert name == "report.json"
                return self
            def mkdir(self, **kwargs):
                pass
            def write_text(self, text, **kwargs):
                files["report.json"] = json.loads(text)

        window = SimpleNamespace(
            _instruments=manager, _checkpoint_panel=Mock(), _config_panel=Mock(),
            _live_panel=Mock(), _status_bar=Mock(), _close_session_bq_transport=port.close,
            _session_dir=MemoryPath(), _discharge_ctrls_this_session=[],
            _last_golden_gg_ctrl=None, _last_chemid_ctrl=None,
            _last_traditional_learning_ctrl=None, _last_ocv_soc_ctrl=None,
            _last_iec_dc_resistance_ctrl=None,
        )
        window._live_panel.append_event.side_effect = events.append
        window._write_report = lambda result: module.MainWindow._write_report(window, result)
        return module, window, manager, port, events, files, TestResult

    @pytest.mark.parametrize("broken_panel", [False, True])
    def test_busy_finalize_closes_port_panel_and_writes_report(self, monkeypatch, broken_panel):
        module, window, manager, port, events, files, result_type = self.make(monkeypatch)
        if broken_panel:
            window._checkpoint_panel.set_session_finalized.side_effect = RuntimeError("panelhiba")
        result = result_type(
            status="FAULT", reason="eredeti hiba", error_queue_deferred_s={"DMM_V": 12.0},
            relax_missed_deadline_count=7,
        )
        meta = module.MainWindow._finalize_terminal_session(window, result)
        port.close.assert_called_once()
        window._config_panel.set_session_active.assert_called_once_with(False)
        assert meta["reason"] == "eredeti hiba"
        assert files["report.json"]["error_queue_deferred_s"] == {"DMM_V": 12.0}
        assert meta["error_queue_deferred_s"] == {"DMM_V": 12.0}
        assert files["report.json"]["relax_missed_deadline_count"] == 7
        assert meta["relax_missed_deadline_count"] == 7
        assert events and all(event["severity"] == "CRITICAL" for event in events)
        assert module._PENDING_DISCONNECTS == [manager]
        manager.disconnect_all.side_effect = None
        assert module._retry_pending_disconnects(events.append)
        assert module._PENDING_DISCONNECTS == []

    @pytest.mark.parametrize("abort", [False, True])
    def test_busy_bringup_cleanup_preserves_original_error(self, monkeypatch, abort):
        from types import SimpleNamespace
        from unittest.mock import Mock
        module, window, manager, port, events, _, _ = self.make(monkeypatch)
        window._pending_start_cfg = SimpleNamespace(runner_tick_s=5)
        window._cleanup_connect_thread = Mock()
        window._connect_abort_requested = abort
        window._device_error_panel = Mock()
        window._tabs = Mock()
        original = ArithmeticError("eredeti build-hiba")
        window._build_runner = Mock(side_effect=original)
        messages = []
        monkeypatch.setattr(module.QMessageBox, "critical", lambda *a: messages.append(a[-1]))
        connected = SimpleNamespace(instruments=manager, session_bq_serial=port)
        module.MainWindow._on_connect_ready_impl(window, connected)
        assert port.close.called
        window._config_panel.set_session_active.assert_called_once_with(False)
        assert events[0]["severity"] == "CRITICAL"
        if not abort:
            assert "eredeti build-hiba" in messages[0]

    def test_constructor_busy_keeps_original_exception_and_closes_bq(self, monkeypatch):
        from unittest.mock import Mock

        import Prog.drivers.device_dmm as dmm_module
        import Prog.drivers.device_load as load_module
        import Prog.drivers.device_psu as psu_module
        import Prog.drivers.pico_bq_transport as bq_module
        import Prog.src.instrument_manager as manager_module
        module, _, manager, port, _, _, _ = self.make(monkeypatch)
        original = ArithmeticError("eredeti azonosítási hiba")
        manager.identify_all.side_effect = original
        transport = Mock(serial=port)
        monkeypatch.setattr(dmm_module, "Keysight34465ADMM", Mock())
        monkeypatch.setattr(load_module, "Keithley2380Load", Mock())
        monkeypatch.setattr(psu_module, "Keithley2220PSU", Mock())
        monkeypatch.setattr(manager_module, "InstrumentManager", lambda *a: manager)
        monkeypatch.setattr(bq_module, "PicoBQTransport", lambda: transport)
        with pytest.raises(ArithmeticError) as caught:
            module._construct_and_connect_instruments(_make_cfg(), True)
        assert caught.value is original
        port.close.assert_called_once()
        assert original.safe_off_attempted
        assert module._PENDING_DISCONNECTS == [manager]

    def test_worker_record_runner_event_gui_signal_chain(self, monkeypatch):
        from Prog.gui.worker import TestRunnerWorker
        from Prog.src.test_runner import TestPlan, TestType
        from Prog.tests.test_test_runner import TestR16B5bRunner

        runner, ctrl, rig = TestR16B5bRunner.make()
        worker = TestRunnerWorker(runner, TestPlan(test_type=TestType.CHARGE_ONLY, steps=[]))
        received = []
        worker.event_ready.connect(received.append)
        ctrl.on_event = lambda event: runner.on_event(event)
        ctrl.attach_sampling_service(rig.service)
        rig.hardware.read_hook = lambda: (_ for _ in ()).throw(RuntimeError("rekordhiba"))
        try:
            rig.start()
            ctrl.collect_sampling_records()
        finally:
            rig.stop()
        assert received[0]["event_code"] == "RELAX_DMM_FAULT"
        assert "rekordhiba" in received[0]["event_message"]


class TestR16B5bF2Gui:
    @pytest.mark.parametrize("stop_kind", ["emergency", "graceful"])
    def test_stuck_worker_report_and_checkpoint_keep_the_same_reason(self, monkeypatch, stop_kind):
        from Prog.tests.test_test_runner import TestR16B5bF2

        result, checkpoint = TestR16B5bF2.stuck_run(monkeypatch, stop_kind)
        module, window, _manager, _port, _events, files, _result_type = TestR16B5bCleanup.make(monkeypatch)
        module.MainWindow._write_report(window, result)
        assert files["report.json"]["reason"] == checkpoint["reason"] == result.reason

    def test_start_retries_pending_disconnect_before_reading_configuration(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import Mock

        module, _window, manager, _port, events, _files, _result_type = TestR16B5bCleanup.make(monkeypatch)
        module._PENDING_DISCONNECTS.append(manager)
        config_panel = Mock()
        sentinel = RuntimeError("konfigurációig eljutott")
        config_panel.get_session_config.side_effect = sentinel
        window = SimpleNamespace(
            _worker=None, _connect_worker=None, _config_panel=config_panel,
            _live_panel=SimpleNamespace(append_event=events.append),
        )
        module.MainWindow._start_test(window)
        config_panel.get_session_config.assert_not_called()
        assert module._PENDING_DISCONNECTS == [manager]
        assert events[-1]["severity"] == "CRITICAL"
        manager.disconnect_all.side_effect = None
        with pytest.raises(RuntimeError) as caught:
            module.MainWindow._start_test(window)
        assert caught.value is sentinel
        assert module._PENDING_DISCONNECTS == []
        config_panel.get_session_config.assert_called_once()

    @pytest.mark.parametrize("with_seed", [False, True])
    def test_build_resume_to_runner_checkpoint_and_real_report_in_memory(self, monkeypatch, with_seed):
        from io import StringIO
        from types import SimpleNamespace

        import Prog.src.logger as logger_module
        import Prog.src.test_runner as runner_module
        from Prog.tests.test_test_runner import _f26_mock_logger_runner

        class MemoryPath:
            name = "memória_session"

            def __truediv__(self, name):
                return self

            def with_suffix(self, suffix):
                return self

            def open(self, mode, **kwargs):
                assert mode == "w"

                class MemoryFile(StringIO):
                    def fileno(self):
                        return 0

                return MemoryFile()

            def replace(self, target):
                return target

        memory = MemoryPath()
        checkpoint = {"next_safe_step_index": 1}
        if with_seed:
            checkpoint["error_queue_deferred_s"] = {"DMM_V": 120.0}
            checkpoint["relax_missed_deadline_count"] = 7
        _unused, logger, _dmm, _events = _f26_mock_logger_runner(runner_module.TestRunnerConfig())
        manager = _unused._instruments
        connected = SimpleNamespace(
            psu=manager.psu, load=manager.load, dmm_v=manager.dmm_voltage,
            dmm_t=manager.dmm_temperature, instruments=manager,
            instrument_identities={}, bq_transport=None, session_bq_serial=None,
        )
        budget_calls: list[float] = []
        window = SimpleNamespace(
            _bq_worker=None, _connect_abort_requested=False, _sessions_root=lambda: memory,
            _live_panel=SimpleNamespace(set_fast_loop_budget_s=budget_calls.append),
        )
        monkeypatch.setattr(sr, "find_resumable_session", lambda *args, **kwargs: {
            "session_dir": memory, "checkpoint": checkpoint,
        })
        monkeypatch.setattr(logger_module, "Logger", lambda *args: logger)
        monkeypatch.setattr(mw.os, "fsync", lambda fd: None)
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
        cfg = _make_cfg()
        runner, plan, start = mw.MainWindow._build_runner(window, cfg, connected)
        expected = 120.0 if with_seed else 0.0
        assert start == 1
        # ST1: CHARACTERIZATION@1 -- a hátralévő RELAX/DISCHARGE/RELAX nem tölt.
        assert budget_calls == [0.0]
        assert runner._config.initial_relax_missed_deadline_count == (7 if with_seed else 0)
        if with_seed:
            checkpoint["error_queue_deferred_s"]["DMM_V"] = 900.0
            checkpoint["relax_missed_deadline_count"] = 900
        now = [100000.0]
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: now[0])
        monkeypatch.setattr(manager, "poll_device_errors", lambda **kwargs: [
            {"device": "DMM_V", "status": "HALASZTVA", "error": "foglalt"},
        ])
        runner._poll_device_errors()
        now[0] += 5.0
        runner.request_stop()
        result = runner.run(plan, start)
        assert result.error_queue_deferred_s == {"DMM_V": expected + 5.0}
        assert runner._config.initial_error_queue_deferred_s == ({"DMM_V": 120.0} if with_seed else {})
        assert logger.write_checkpoint.call_args.args[0]["error_queue_deferred_s"] == {"DMM_V": expected + 5.0}
        module, report_window, _manager, _port, _events, files, _type = TestR16B5bCleanup.make(monkeypatch)
        module.MainWindow._write_report(report_window, result)
        assert files["report.json"]["error_queue_deferred_s"] == {"DMM_V": expected + 5.0}
        expected_deadlines = 7 if with_seed else 0
        assert result.relax_missed_deadline_count == expected_deadlines
        assert logger.write_checkpoint.call_args.args[0]["relax_missed_deadline_count"] == expected_deadlines
        assert files["report.json"]["relax_missed_deadline_count"] == expected_deadlines


    def test_config_panel_failure_still_finishes_port_and_report(self, monkeypatch):
        module, window, _manager, port, events, files, result_type = TestR16B5bCleanup.make(monkeypatch)
        window._config_panel.set_session_active.side_effect = RuntimeError("konfigurációs panelhiba")
        result = result_type(status="FAULT", reason="eredeti ok")
        module.MainWindow._finalize_terminal_session(window, result)
        port.close.assert_called_once()
        assert files["report.json"]["reason"] == "eredeti ok"
        assert any("konfigurációs panelhiba" in ev["event_message"] for ev in events)


class TestR16B5bF4Gui:
    @pytest.mark.parametrize("stop_kind", ["emergency", "graceful"])
    def test_done_drain_timeout_reason_reaches_real_gui_report(self, monkeypatch, stop_kind):
        from Prog.tests.test_test_runner import TestR16B5bF4

        result, checkpoint = TestR16B5bF4.done_drain_run(monkeypatch, stop_kind)
        module, window, _manager, _port, _events, files, _type = TestR16B5bCleanup.make(monkeypatch)
        module.MainWindow._write_report(window, result)
        assert files["report.json"]["status"] == checkpoint["status"] == result.status
        assert files["report.json"]["reason"] == checkpoint["reason"] == result.reason


class TestR16B5bF5Gui:
    @pytest.mark.parametrize("order", ("none", "stop", "abort", "stop_abort", "abort_stop"))
    @pytest.mark.parametrize("stage", ("before", "drain", "handler", "after"))
    @pytest.mark.parametrize("release", ("ok", "timeout", "late"))
    @pytest.mark.parametrize("primary", (False, True))
    @pytest.mark.parametrize(
        ("emergency_reason", "graceful_reason"),
        [("GUI_ABORT_F5", "USER_STOP_REQUESTED"), ("", ""), (None, None)],
    )
    def test_matrix_reason_reaches_real_gui_report(
        self, monkeypatch, order, stage, release, primary, emergency_reason, graceful_reason,
    ):
        from Prog.tests.test_test_runner import TestR16B5bF5

        result, checkpoint = TestR16B5bF5.run_case(
            monkeypatch, order, stage, release, primary,
            emergency_reason=emergency_reason, graceful_reason=graceful_reason,
        )
        module, window, _manager, _port, _events, files, _type = TestR16B5bCleanup.make(monkeypatch)
        module.MainWindow._write_report(window, result)
        assert files["report.json"]["status"] == checkpoint["status"] == result.status
        assert files["report.json"]["reason"] == checkpoint.get("reason", "") == result.reason

    @pytest.mark.parametrize("boundary", ("DONE", "CHECKPOINT", "GRACEFUL"))
    @pytest.mark.parametrize("order", ("stop", "abort", "stop_abort", "abort_stop"))
    @pytest.mark.parametrize(
        ("emergency_reason", "graceful_reason"),
        [("POLL_ABORT_F5", "USER_STOP_REQUESTED"), ("", ""), (None, None)],
    )
    def test_final_poll_reason_reaches_real_gui_report(
        self, monkeypatch, boundary, order, emergency_reason, graceful_reason,
    ):
        from Prog.tests.test_test_runner import TestR16B5bF5

        result, checkpoint = TestR16B5bF5.run_final_poll_case(
            monkeypatch, boundary, order, emergency_reason, graceful_reason,
        )
        module, window, _manager, _port, _events, files, _type = TestR16B5bCleanup.make(monkeypatch)
        module.MainWindow._write_report(window, result)
        assert files["report.json"]["status"] == checkpoint["status"] == result.status
        assert files["report.json"]["reason"] == checkpoint["reason"] == result.reason



class TestR16B5bF7Gui:
    @pytest.mark.parametrize("reason", ("", None))
    def test_empty_primary_fault_reaches_real_gui_report(self, monkeypatch, reason):
        from Prog.tests.test_test_runner import TestR16B5bF7

        result, checkpoint = TestR16B5bF7.run_empty_primary_case(reason)
        module, window, _manager, _port, _events, files, _type = TestR16B5bCleanup.make(monkeypatch)
        module.MainWindow._write_report(window, result)
        assert files["report.json"]["status"] == checkpoint["status"] == result.status == "FAULT"
        assert files["report.json"]["reason"] == checkpoint["reason"] == result.reason == "STEP_FAULT"


class TestR16B16aFactoryOrigin:
    @pytest.mark.parametrize("mode", ["DISCHARGE_ONLY", "CHARACTERIZATION", "BQ_LEARNING_PHYSICAL"])
    def test_snapshot_reaches_controller_factory(self, qapp, monkeypatch, mode):
        import Prog.src.logger as logger_module
        from Prog.src.battery_profile import DischargeRateOrigin
        from Prog.tests.mock_drivers.mock_dmm import MockDMM
        from Prog.tests.mock_drivers.mock_load import MockLoad
        from Prog.tests.mock_drivers.mock_psu import MockPSU

        monkeypatch.setattr(logger_module, "Logger", lambda *a, **k: MagicMock())
        monkeypatch.setattr(sr, "find_resumable_session", lambda *a, **k: None)
        window = MainWindow()
        monkeypatch.setattr(window, "_sessions_root", lambda: Path.cwd())
        psu, load, dmm_v, dmm_t = MockPSU(), MockLoad(), MockDMM(), MockDMM()
        instruments = InstrumentManager(psu, load, dmm_v, dmm_t)
        connected = mw._PreConnectedInstruments(
            instruments, psu, load, dmm_v, dmm_t, None, None,
            {"PSU": "MOCK", "Load": "MOCK", "DMM_V": "MOCK", "DMM_T": "MOCK"},
        )
        origin = DischargeRateOrigin("C/20", "NOMINAL_C10_CAPACITY", 7.2)
        cfg = _make_cfg(
            test_type=mode, battery_model="FG20721", discharge_current_A=0.36,
            discharge_terminate_voltage_V=10.5, discharge_rate_origin=origin,
        )
        runner, _plan, _extra = window._build_runner(cfg, pre_connected=connected)
        ctrl = runner._discharge_ctrl_factory()
        assert ctrl._config.rate_origin == origin
        assert ctrl.config_snapshot()["rate_origin"] == {
            "rate_policy": "C/20", "capacity_basis": "NOMINAL_C10_CAPACITY", "capacity_Ah": 7.2,
        }
        dmm_v.voltage_V = 12.5
        for _ in range(3):
            ctrl.advance(1.0)
        assert load.input_commanded_on

    @pytest.mark.parametrize("change", ["rate", "model", "profile"])
    def test_unsafe_revalidation_blocks_actual_start_before_summary(self, qapp, monkeypatch, change):
        window = MainWindow()
        cp = window._config_panel
        cp._test_type_combo.setCurrentText("DISCHARGE_ONLY")
        cp._model_edit.setText("FG20721")
        cp._capacity_spin.setValue(7.2)
        for widget in (cp._psu_res_edit, cp._load_res_edit, cp._dmm_v_res_edit, cp._dmm_t_res_edit):
            widget.setText("MOCK")
        cp._apply_discharge_preset(20, 1.75)
        cp.refresh_validation()
        assert cp.is_start_allowed()
        if change == "rate":
            cp._on_discharge_current_spin_changed(0.36)
        elif change == "model":
            cp._model_edit.setText("OTHER")
        else:
            # Profilváltás YAML-értékét memóriában injektáljuk, fájlírás nélkül.
            monkeypatch.setattr(
                cp, "_apply_profile_yaml_overrides",
                lambda name: cp._model.set_terminate_voltage_per_cell_default_V(1.60),
            )
            cp._profile_combo.setCurrentText("FIAMM_24V")
            cp._wiring_confirmed_cb.setChecked(True)
        warnings, questions = [], []
        monkeypatch.setattr(mw, "_retry_pending_disconnects", lambda *a: True)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: questions.append(a) or QMessageBox.StandardButton.No,
        )
        window._start_test()
        assert warnings
        assert not questions
        assert window._worker is None and window._connect_worker is None


class TestR16B16bGuiPulseMode:
    def test_gui_config_explicitly_rejects_hardware_pulse(self, monkeypatch):
        from Prog.gui.main_window import _ocv_soc_config_for
        from Prog.src.ocv_soc_controller import OcvSocConfig
        from Prog.src.pulse_measurement_service import PulseDischargeError
        config = OcvSocConfig(pulse_trigger_mode="HARDWARE_PULSE_SOFTWARE_TRIGGERED")
        monkeypatch.setattr("Prog.src.ocv_soc_controller.OcvSocConfig", lambda **kw: config)
        with pytest.raises(PulseDischargeError, match="HARDWARE_PULSE_HIL_PENDING"):
            _ocv_soc_config_for(_make_cfg(test_type="OCV_SOC_CHARACTERIZATION"))


class TestR16B7BqProfileSource:
    @pytest.mark.parametrize("name,energy", [("FIAMM_12V", 8040), ("FIAMM_24V", 16080)])
    def test_source_values_reach_runner_before_instruments(self, monkeypatch, name, energy):
        from types import SimpleNamespace

        import Prog.src.safety as safety_module

        captured = []

        class Captured(Exception):
            pass

        def capture(*, profile, **kwargs):
            captured.append(profile)
            raise Captured

        monkeypatch.setattr(safety_module, "SafetyManager", capture)
        cfg = _make_cfg(test_type="TRADITIONAL_LEARNING", battery_profile_name=name, bq_port="COM8")
        with pytest.raises(Captured):
            MainWindow._build_runner(SimpleNamespace(_bq_worker=None), cfg)
        profile = captured[0]
        assert profile.C10_capacity_Ah == 6.7
        assert profile.design_capacity_mAh == 6700
        assert profile.design_energy_cWh == energy
        assert profile.qmax_cell0_initial_mAh == 7200
        assert profile.bq_learning_discharge_current_A == pytest.approx(0.67)

    # A bemenet C10: OTHER forrásolt méret mellett; p=3 a 24 V-os energiakorlát felett.
    @pytest.mark.parametrize("model,nominal", [("OTHER", 6.7), ("FG20721", 7.0), ("FG20721", 20.1)])
    def test_unsourced_or_oversized_pack_blocks_before_connect(self, monkeypatch, model, nominal):
        from types import SimpleNamespace

        from Prog.src.exceptions import ProfileValidationError

        connect = MagicMock()
        monkeypatch.setattr(mw, "_construct_and_connect_instruments", connect)
        cfg = _make_cfg(
            test_type="TRADITIONAL_LEARNING", battery_profile_name="FIAMM_24V",
            battery_model=model, nominal_capacity_ah=nominal, bq_port="COM8",
        )
        with pytest.raises(ProfileValidationError):
            MainWindow._build_runner(SimpleNamespace(_bq_worker=None), cfg)
        connect.assert_not_called()


class TestR16B7F1WidgetCapacity:
    @pytest.mark.parametrize("mode", ["TRADITIONAL_LEARNING", "GOLDEN_GG"])
    @pytest.mark.parametrize("name,base_energy", [("FIAMM_12V", 8040), ("FIAMM_24V", 16080)])
    @pytest.mark.parametrize("parallel", [1, 2])
    def test_c10_widget_session_builder_currents_agree(
        self, qapp, monkeypatch, mode, name, base_energy, parallel,
    ):
        from types import SimpleNamespace

        from Prog.gui.panels.config_panel import ConfigPanel
        from Prog.src.bq_consistency_gate import build_initial_df_values

        panel = ConfigPanel()
        try:
            panel._profile_combo.setCurrentText(name)
            panel._model_edit.setText("FG20721")
            panel._test_type_combo.setCurrentText(mode)
            panel._capacity_spin.setValue(6.7 * parallel)
            cfg = panel.get_session_config()
            summary = panel.build_start_summary()
            captured = []

            class Captured(Exception):
                pass

            def capture(*, profile, **kwargs):
                captured.append(profile)
                raise Captured

            monkeypatch.setattr("Prog.src.safety.SafetyManager", capture)
            connect = MagicMock()
            monkeypatch.setattr(mw, "_construct_and_connect_instruments", connect)
            with pytest.raises(Captured):
                MainWindow._build_runner(SimpleNamespace(_bq_worker=None), cfg)
            connect.assert_not_called()
            profile = captured[0]
            assert cfg.nominal_capacity_ah == pytest.approx(6.7 * parallel)
            assert profile.C10_capacity_Ah == pytest.approx(6.7 * parallel)
            assert profile.nominal_capacity_Ah == pytest.approx(7.2 * parallel)
            assert profile.effective_max_charge_A == pytest.approx(1.675 * parallel)
            assert profile.effective_taper_A == pytest.approx(0.201 * parallel)
            assert summary["requested_charge_current_A"] == pytest.approx(profile.effective_max_charge_A)
            assert panel._model.taper_current_A == pytest.approx(profile.effective_taper_A)
            assert f"{profile.effective_max_charge_A:.2f} A" in panel._charge_current_label.text()
            values = build_initial_df_values(profile)
            assert values["design_capacity_mAh"] == 6700 * parallel
            assert values["design_energy_cWh"] == base_energy * parallel
            assert values["qmax_cell0_initial_mAh"] == 7200 * parallel
            assert profile.bq_learning_discharge_current_A == pytest.approx(0.67 * parallel)
        finally:
            panel.close()
            panel.deleteLater()

    @pytest.mark.parametrize("capacity", [6.8, 7.2, 13.5])
    @pytest.mark.parametrize("name", ["FIAMM_12V", "FIAMM_24V"])
    def test_unsourced_widget_capacity_blocks_before_connect(self, qapp, monkeypatch, capacity, name):
        from types import SimpleNamespace

        from Prog.gui.panels.config_panel import ConfigPanel
        from Prog.src.exceptions import ProfileValidationError

        panel = ConfigPanel()
        try:
            panel._profile_combo.setCurrentText(name)
            panel._model_edit.setText("FG20721")
            panel._test_type_combo.setCurrentText("TRADITIONAL_LEARNING")
            panel._capacity_spin.setValue(capacity)
            connect = MagicMock()
            monkeypatch.setattr(mw, "_construct_and_connect_instruments", connect)
            with pytest.raises(ProfileValidationError, match="SOURCE_REQUIRED"):
                MainWindow._build_runner(
                    SimpleNamespace(_bq_worker=None), panel.get_session_config(),
                )
            connect.assert_not_called()
        finally:
            panel.close()
            panel.deleteLater()

    def test_normal_mode_keeps_gui_capacity_meaning(self, qapp, monkeypatch):
        from types import SimpleNamespace

        from Prog.gui.panels.config_panel import ConfigPanel

        panel = ConfigPanel()
        try:
            panel._model_edit.setText("FG20721")
            panel._test_type_combo.setCurrentText("CHARGE_ONLY")
            panel._capacity_spin.setValue(7.2)
            captured = []

            class Captured(Exception):
                pass

            def capture(*, profile, **kwargs):
                captured.append(profile)
                raise Captured

            monkeypatch.setattr("Prog.src.safety.SafetyManager", capture)
            with pytest.raises(Captured):
                MainWindow._build_runner(
                    SimpleNamespace(_bq_worker=None), panel.get_session_config(),
                )
            assert captured[0].C10_capacity_Ah is None
            assert captured[0].nominal_capacity_Ah == 7.2
            assert captured[0].effective_max_charge_A == pytest.approx(1.8)
        finally:
            panel.close()
            panel.deleteLater()


class TestR16B27CheckpointMappingSchema:
    """K8: a valodi startup/resume olvasok, csak memoriabeli JSON-adatokkal."""

    FIELDS = (
        "pending_bq_df_restore", "pending_bq_unsealed", "last_safe_off_result",
        "instrument_identities", "warning_code_counts", "error_queue_deferred_s",
    )

    @pytest.fixture
    def checkpoint_tree(self, monkeypatch):
        import json
        from types import SimpleNamespace

        root = Path("R:/R16-B27-checkpoints")
        older = root / "session_TEST_20261111_000000"
        latest = root / "session_TEST_20261112_000000"
        identities = {"PSU": "MOCK_PSU", "Load": "MOCK_LOAD"}
        base = {
            "safe_boundary": True, "next_safe_step_index": 1,
            "config_hash": "config", "profile_hash": "profile",
            "instrument_identities": identities, "sample_id": "S001",
            "step": "relax", "pending_bq_df_restore": None,
            "pending_bq_unsealed": None,
            "last_safe_off_result": {"PSU": True, "Load": True},
            "warning_code_counts": {}, "error_queue_deferred_s": {},
        }
        documents = {
            older / "checkpoint.json": json.dumps(base),
            latest / "checkpoint.json": json.dumps(base),
        }
        directories = {root, older, latest}
        original_exists = Path.exists
        original_is_dir = Path.is_dir
        original_iterdir = Path.iterdir
        original_read_text = Path.read_text

        def in_tree(path):
            return path == root or root in path.parents

        monkeypatch.setattr(
            Path, "exists",
            lambda path: path in directories or path in documents
            if in_tree(path) else original_exists(path),
        )
        monkeypatch.setattr(
            Path, "is_dir",
            lambda path: path in directories if in_tree(path) else original_is_dir(path),
        )
        monkeypatch.setattr(
            Path, "iterdir",
            lambda path: iter((older, latest)) if path == root else original_iterdir(path),
        )

        def read_text(path, *args, **kwargs):
            if in_tree(path):
                return documents[path]
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        warning = MagicMock()
        information = MagicMock()
        monkeypatch.setattr(QMessageBox, "warning", warning)
        monkeypatch.setattr(QMessageBox, "information", information)
        owner = SimpleNamespace(
            _sessions_root=lambda: root,
            _mark_incomplete_sessions_reviewed=MagicMock(),
        )
        return SimpleNamespace(
            root=root, latest=latest, documents=documents, base=base,
            identities=identities, owner=owner, warning=warning, information=information,
        )

    @staticmethod
    def _find_resume(tree):
        return sr.find_resumable_session(
            tree.root, "config", "profile", tree.identities, "S001", total_step_count=3,
        )

    @pytest.mark.parametrize("field", FIELDS)
    @pytest.mark.parametrize(
        "value", [[1], "damaged", 1, [], "", 0, False],
        ids=["list", "string", "number", "empty-list", "empty-string", "zero", "false"],
    )
    def test_corrupt_mapping_warns_and_blocks_latest_without_fallback(
        self, checkpoint_tree, field, value,
    ):
        import json

        tree = checkpoint_tree
        tree.documents[tree.latest / "checkpoint.json"] = json.dumps(
            {**tree.base, field: value},
        )
        # A tenyleges discovery -> startup-render lanc; a regi kodon a harom
        # startup-almezo truthy erteke itt AttributeError-t okoz.
        MainWindow._review_incomplete_sessions_on_startup(tree.owner)
        tree.warning.assert_called_once()
        message = tree.warning.call_args.args[2]
        assert tree.latest.name in message
        assert "CHECKPOINT NEM OLVASHAT\u00d3" in message
        assert "UNSEALED" in message and "PSU/Load" in message
        tree.information.assert_not_called()
        entries = sr.discover_incomplete_sessions(tree.root)
        latest = next(entry for entry in entries if entry["session_dir"] == tree.latest)
        assert latest["unreadable"] is True
        assert latest["checkpoint"] == {}
        assert self._find_resume(tree) is None
        assert not sr.evaluate_resume(
            latest["checkpoint"], "config", "profile", tree.identities, "S001", 3,
        ).allowed
        tree.owner._mark_incomplete_sessions_reviewed.assert_called_once()

    @pytest.mark.parametrize("field", FIELDS)
    @pytest.mark.parametrize("kind", ["missing", "none", "empty-dict", "valid-dict"])
    def test_missing_none_and_dict_keep_existing_startup_and_resume_behavior(
        self, checkpoint_tree, field, kind,
    ):
        import json

        tree = checkpoint_tree
        payload = dict(tree.base)
        valid = {
            "pending_bq_df_restore": {"field": "Cell BL", "original_mV": 1700},
            "pending_bq_unsealed": {"controller": "GOLDEN_GG", "reason": "snapshot"},
            "last_safe_off_result": {"PSU": True, "Load": True},
            "instrument_identities": tree.identities,
            "warning_code_counts": {"WARNING": 2},
            "error_queue_deferred_s": {"PSU": 1.0},
        }
        if kind == "missing":
            del payload[field]
        else:
            payload[field] = {"none": None, "empty-dict": {}, "valid-dict": valid[field]}[kind]
        tree.documents[tree.latest / "checkpoint.json"] = json.dumps(payload)
        MainWindow._review_incomplete_sessions_on_startup(tree.owner)
        dialogs = tree.warning.call_args_list + tree.information.call_args_list
        assert len(dialogs) == 1
        assert "CHECKPOINT NEM OLVASHAT\u00d3" not in dialogs[0].args[2]
        warns = (
            field == "last_safe_off_result" and kind != "valid-dict"
        ) or (
            field in {"pending_bq_df_restore", "pending_bq_unsealed"} and kind == "valid-dict"
        )
        assert tree.warning.call_count == int(warns)
        assert tree.information.call_count == int(not warns)
        latest = next(
            entry for entry in sr.discover_incomplete_sessions(tree.root)
            if entry["session_dir"] == tree.latest
        )
        assert latest["unreadable"] is False
        assert latest["checkpoint"] == payload
        match = self._find_resume(tree)
        blocked = field == "instrument_identities" and kind != "valid-dict"
        assert (match is None) == blocked
        if match is not None:
            assert match["session_dir"] == tree.latest
            assert match["checkpoint"] == payload


@pytest.mark.parametrize("status", ["COMPLETED", "INCOMPLETE", "RUNNING", "MISSING"])
@pytest.mark.parametrize("report_ok", [False, True])
def test_b23b_worker_finished_gui_recharge_semantics(qapp, monkeypatch, status, report_ok):
    from types import SimpleNamespace

    from Prog.gui.worker import TestRunnerWorker
    from Prog.src.test_runner import TestPlan, TestResult

    monkeypatch.setattr(MainWindow, "_review_incomplete_sessions_on_startup", lambda self: None)
    window = MainWindow()
    window._active_test_type = "TRADITIONAL_LEARNING"
    record = None if status == "MISSING" else {
        "learning_evidence": {"result_summary": {"result": "BQ_LEARNING_VERIFIED"}},
        "final_recharge": {
            "status": status,
            "completed_at_utc": "2026-11-12T10:00:00+00:00" if status == "COMPLETED" else None,
            "next_campaign_not_before_utc": "2026-11-14T10:00:00+00:00" if status == "COMPLETED" else None,
            "off_readback": {"Load": False, "PSU": False},
        },
    }
    window._last_traditional_learning_ctrl = SimpleNamespace(tlc_completion=record)
    messages, runs = [], []
    for method in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, method, lambda *a, **k: messages.append(a[2]))
    monkeypatch.setattr(
        window, "_finalize_terminal_session",
        lambda result: {"capacity_result_quality": "OK", "bq_learning_result": "BQ_LEARNING_VERIFIED"} if report_ok else None,
    )
    class Runner:
        def run(self, plan, start_step_index=0):
            runs.append(True)
            assert len(runs) == 1, "B23b GUI runner bound"
            return TestResult(status="DONE", safe_off_attempted=True, safe_off_result={"PSU": True, "Load": True})
    runner = Runner()
    worker = TestRunnerWorker(runner, TestPlan.traditional_learning())
    worker.finished.connect(window._on_finished)
    try:
        worker.run()
        assert len(runs) == 1, "Real worker -> finished -> GUI trigger"
        assert len(messages) == 1
        text = messages[0]
        if status == "COMPLETED":
            assert "záró újratöltés nincs igazolva" not in text
            assert "48 h" in text and "2026-11-14T10:00:00+00:00" in text
        else:
            assert "záró újratöltés nincs igazolva" in text
            assert "kisütött állapotban maradt" not in text
    finally:
        window.close()
        window.deleteLater()


def test_b23b_gui_report_uses_frozen_learning_evidence(qapp, monkeypatch):
    import json

    from Prog.src.test_runner import TestResult

    monkeypatch.setattr(MainWindow, "_review_incomplete_sessions_on_startup", lambda self: None)
    window = MainWindow()
    record = {
        "learning_evidence": {"result_summary": {"result": "BQ_LEARNING_VERIFIED", "quality_flags": []}},
        "final_recharge": {"status": "INCOMPLETE", "completed_at_utc": None},
    }
    from Prog.src.traditional_learning_controller import LearningResultSummary
    window._last_traditional_learning_ctrl = _FakeTraditionalLearningCtrl(
        LearningResultSummary(result="BQ_LEARNING_FAILED"),
    )
    window._last_traditional_learning_ctrl.tlc_completion = record
    window._session_dir = Path("B23b_memory_session")
    written = []
    def write(path, text, **kwargs):
        assert path == Path("B23b_memory_session/report.json")
        written.append(json.loads(text))
        return len(text)
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: None)
    monkeypatch.setattr(Path, "write_text", write)
    try:
        meta = window._write_report(TestResult(status="FAULT"))
        assert meta is not None
        assert written[0]["tlc_completion"] == record
        assert written[0]["bq_learning_result"] == "BQ_LEARNING_VERIFIED"
        assert written[0]["learning_result_summary_full"] == record["learning_evidence"]["result_summary"]
    finally:
        window._session_dir = None
        window.close()
        window.deleteLater()


def test_b23b_startup_preserves_bq_evidence_and_warns_about_final_charge(qapp, monkeypatch):
    review = MainWindow._review_incomplete_sessions_on_startup
    monkeypatch.setattr(MainWindow, "_review_incomplete_sessions_on_startup", lambda self: None)
    window = MainWindow()
    record = {
        "learning_evidence": {"result_summary": {"result": "BQ_LEARNING_VERIFIED"}},
        "final_recharge": {"status": "RUNNING", "completed_at_utc": None},
    }
    cp = {
        "safe_boundary": False, "step": "TLC", "tlc_completion": record,
        "last_safe_off_result": {"PSU": True, "Load": True},
    }
    monkeypatch.setattr(sr, "discover_incomplete_sessions", lambda root: [
        {"checkpoint": cp, "session_dir": Path("B23b_memory_session")},
    ])
    monkeypatch.setattr(window, "_mark_incomplete_sessions_reviewed", lambda entries: None)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: messages.append(a[2]))
    try:
        review(window)
        assert len(messages) == 1
        assert "záró újratöltés nincs igazolva" in messages[0]
        assert "BQ-bizonyíték megmaradt" in messages[0]
        assert "aktív töltés nem folytatható" in messages[0]
        assert cp["tlc_completion"] == record
    finally:
        window.close()
        window.deleteLater()


@pytest.mark.parametrize("corruption", ["missing", "timestamp", "off", "interval", "no_record"])
def test_b23b_gui_requires_a_proven_recharge_end(corruption):
    record = {"final_recharge": {
        "status": "COMPLETED",
        "completed_at_utc": "2026-11-12T10:00:00+00:00",
        "next_campaign_not_before_utc": "2026-11-14T10:00:00+00:00",
        "off_readback": {"Load": False, "PSU": False},
    }}
    assert MainWindow._tlc_recharge_completed(record)
    final = record["final_recharge"]
    if corruption == "missing":
        final["completed_at_utc"] = None
    elif corruption == "timestamp":
        final["completed_at_utc"] = "not a timestamp"
    elif corruption == "off":
        final["off_readback"]["PSU"] = 0
    elif corruption == "interval":
        final["next_campaign_not_before_utc"] = "2026-11-12T10:00:00+00:00"
    else:
        record = None
    assert not MainWindow._tlc_recharge_completed(record)


@pytest.mark.parametrize("outcome", ["emergency_stop", "dmm_fault", "completed"])
def test_b23b_final_recharge_severity_reaches_live_panel(qapp, monkeypatch, outcome):
    """DFAM-018: valodi factory -> forwarder -> worker -> LivePanel, csak memoria."""
    import dataclasses
    import io
    import sqlite3
    from copy import deepcopy

    from PySide6.QtCore import Qt

    from Prog.src.traditional_learning_controller import LearningState
    from Prog.tests.test_traditional_learning_controller import (
        _b23b_finish_recharge,
        make_controller,
    )

    # A Logger valodi marad; csak a fajl- es SQLite-hatter memoriabeli.
    root = Path.cwd() / "B23b_fix3_memory"
    assert not root.exists()
    real_open, real_mkdir, real_connect = Path.open, Path.mkdir, sqlite3.connect

    def memory_open(path, *args, **kwargs):
        if path.is_relative_to(root):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    def memory_mkdir(path, *args, **kwargs):
        if not path.is_relative_to(root):
            return real_mkdir(path, *args, **kwargs)

    def memory_connect(database, *args, **kwargs):
        if Path(database).is_relative_to(root):
            database = ":memory:"
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(Path, "open", memory_open)
    monkeypatch.setattr(Path, "mkdir", memory_mkdir)
    monkeypatch.setattr(sqlite3, "connect", memory_connect)
    window, runner, worker = TestR3B3ProducerSeverityReachesLivePanel._window_runner(
        monkeypatch, root, test_type="TRADITIONAL_LEARNING", bq_port="COM8",
    )
    log_calls, events = [], []
    real_log_event = Logger.log_event

    def spy_log_event(logger, event_code, message, is_critical=False):
        log_calls.append((event_code, is_critical))
        return real_log_event(logger, event_code, message, is_critical=is_critical)

    monkeypatch.setattr(Logger, "log_event", spy_log_event)
    worker.event_ready.connect(events.append)
    try:
        ctrl = runner._traditional_learning_ctrl_factory()
        io_ctrl, *_ = make_controller()
        for name in ("_psu", "_load", "_dmm_v", "_dmm_t", "_bq_transport", "_bq_driver"):
            monkeypatch.setattr(ctrl, name, getattr(io_ctrl, name))
        ctrl._final_charge_config = dataclasses.replace(
            ctrl._final_charge_config, fast_loop_enabled=False,
        )
        checkpoints = []
        ctrl.persist_tlc_completion = checkpoints.append
        ctrl._enter(LearningState.FINAL_SNAPSHOT)
        ctrl.advance(0.0)
        assert ctrl.state == LearningState.FINAL_RECHARGE, ctrl.fault_reason
        assert not ctrl._psu.called("output_on")
        evidence = deepcopy(ctrl.tlc_completion["learning_evidence"])
        for _ in range(8):
            ctrl.advance(0.1)
            if ctrl._psu.output_commanded_on:
                break
        assert ctrl.state == LearningState.FINAL_RECHARGE, ctrl.fault_reason
        assert ctrl._psu.output_commanded_on, "Aktiv zaro toltes a hibainjektalas elott"
        assert not ctrl._load.input_commanded_on
        assert checkpoints[-1]["final_recharge"]["status"] == "RUNNING"

        if outcome == "completed":
            _b23b_finish_recharge(ctrl, max_ticks=20)
            assert ctrl.state == LearningState.DONE
            terminal_code = "LEARNING_FINAL_RECHARGE_COMPLETED"
            expected_status = "COMPLETED"
        else:
            if outcome == "emergency_stop":
                ctrl.emergency_stop("USER_EMERGENCY_STOP")
            else:
                ctrl._dmm_v.set_dmm_invalid()
            for _ in range(8):
                if ctrl.state == LearningState.FAULT:
                    break
                ctrl.advance(1.0)
            assert ctrl.state == LearningState.FAULT, "Korlatos hibamegszakitas"
            reason = "USER_EMERGENCY_STOP" if outcome == "emergency_stop" else "DMM_FEEDBACK_LOST"
            assert reason in ctrl.fault_reason
            assert not any(e["event_code"] == "LEARNING_DONE" for e in events)
            terminal_code = "LEARNING_FINAL_RECHARGE_INCOMPLETE"
            expected_status = "INCOMPLETE"

        assert [r["final_recharge"]["status"] for r in checkpoints] == [
            "NOT_STARTED", "RUNNING", expected_status,
        ]
        assert ctrl.tlc_completion["learning_evidence"] == evidence
        assert not ctrl._psu.read_output_state() and not ctrl._load.read_input_state()
        assert not ctrl._psu.output_commanded_on and not ctrl._load.input_commanded_on
        on_count = ctrl._psu.calls.count("output_on()")
        ctrl.advance(1.0)
        assert ctrl._psu.calls.count("output_on()") == on_count == 1

        for code in ("LEARNING_EVIDENCE_CAPTURED", "LEARNING_FINAL_RECHARGE_STARTED", terminal_code):
            items = TestR3B3ProducerSeverityReachesLivePanel._items(window, code)
            assert len(items) == 1, code
            if code == "LEARNING_FINAL_RECHARGE_INCOMPLETE":
                assert [e.get("severity") for e in events if e["event_code"] == code] == ["WARNING"]
                assert "WARNING" in items[0].text()
                assert items[0].background().color().name() == "#fff8e1"
            else:
                assert "INFO" in items[0].text()
                assert items[0].background().style() == Qt.BrushStyle.NoBrush
            assert [call for call in log_calls if call[0] == code] == [(code, False)]
    finally:
        runner._logger.close()
        window.close()
        window.deleteLater()


def test_b23b_fix4_startup_warns_for_legacy_tlc_checkpoint(qapp, monkeypatch):
    from copy import deepcopy

    review = MainWindow._review_incomplete_sessions_on_startup
    monkeypatch.setattr(MainWindow, "_review_incomplete_sessions_on_startup", lambda self: None)
    window = MainWindow()
    cp = {
        "safe_boundary": False, "step": "traditional_learning",
        "last_safe_off_result": {"PSU": True, "Load": True},
    }
    original = deepcopy(cp)
    entry = {"checkpoint": cp, "session_dir": Path("B23b_fix4_legacy_memory_session")}
    discovered, reviewed, warnings, information, questions = [], [], [], [], []

    def discover(root):
        discovered.append(root)
        return [entry]

    def question(*args, **kwargs):
        questions.append(args)
        raise AssertionError("Legacy TLC startup must not offer active continuation")

    monkeypatch.setattr(sr, "discover_incomplete_sessions", discover)
    monkeypatch.setattr(window, "_mark_incomplete_sessions_reviewed", lambda entries: reviewed.extend(entries))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    monkeypatch.setattr(QMessageBox, "information", lambda *args: information.append(args[2]))
    monkeypatch.setattr(QMessageBox, "question", question)
    try:
        review(window)  # Synchronous, exactly one review; no event loop or polling.
        assert len(discovered) == 1 and reviewed == [entry], "The legacy checkpoint must be reviewed"
        assert len(warnings) == 1 and information == []
        assert "záró újratöltés nincs igazolva" in warnings[0]
        assert "BQ-bizonyítékot ellenőrizni kell" in warnings[0]
        assert "aktív töltés nem folytatható" in warnings[0]
        assert "BQ-bizonyíték megmaradt" not in warnings[0]
        assert questions == [] and window._worker is None
        assert "tlc_completion" not in cp and cp == original
    finally:
        window.close()
        window.deleteLater()


class TestST1LiveStaleFastLoopBudget:
    """ST1 (G1-UD-7, DFAM-028): a _build_runner() a LivePanel
    elévültküszöbébe a session tényleges shared_charge_cfg fast-loop keretét
    köti be (0.0, ha a ténylegesen futó lépések között nincs töltő lépés). A
    korábbi 2 × runner_tick_s küszöb a fast loop több másodperces normál
    mintaközénél tickenként jelzett."""

    @staticmethod
    def _window(monkeypatch, tmp_path) -> MainWindow:
        _patch_hardware(monkeypatch)
        monkeypatch.setattr(app_paths, "exe_dir", lambda: tmp_path)
        window = MainWindow()
        window._live_panel.set_expected_tick_interval_ms(2000)  # a :1733 hívás mintája, r = 2 s
        return window

    @staticmethod
    def _style_at(window: MainWindow, age_s: float) -> str:
        panel = window._live_panel
        panel._last_sample_perf = time.perf_counter() - age_s
        panel._refresh_staleness()
        return panel._step_time_lbl.styleSheet()

    @pytest.mark.parametrize(("overrides", "base_age_s", "stale_age_s"), [
        pytest.param({}, 11.5, 12.5, id="default"),
        pytest.param({"fast_loop_period_s": 0.3}, 15.5, 16.5, id="period_0_3"),
        pytest.param({"fast_loop_enabled": False}, None, 4.5, id="fast_loop_disabled"),
    ])
    def test_build_runner_budget_follows_shared_charge_config(
        self, qapp, monkeypatch, tmp_path, overrides, base_age_s, stale_age_s,
    ):
        import Prog.src.charge_controller as charge_module
        from Prog.gui.panels.live_panel import (
            _LIVE_LABEL_BASE_STYLE,
            _LIVE_LABEL_STALE_STYLE,
        )

        real_config = charge_module.ChargeConfig
        monkeypatch.setattr(
            charge_module, "ChargeConfig", lambda **kwargs: real_config(**{**kwargs, **overrides}),
        )
        window = self._window(monkeypatch, tmp_path)

        runner, _plan, _start = window._build_runner(_make_cfg(test_type="CHARGE_ONLY"))

        charge_cfg = runner._charge_ctrl_factory()._config
        for key, value in overrides.items():
            assert getattr(charge_cfg, key) == value
        if base_age_s is not None:
            assert self._style_at(window, base_age_s) == _LIVE_LABEL_BASE_STYLE
        assert self._style_at(window, stale_age_s) == _LIVE_LABEL_STALE_STYLE

    def test_discharge_only_keeps_nominal_threshold(self, qapp, monkeypatch, tmp_path):
        from Prog.gui.panels.live_panel import _LIVE_LABEL_STALE_STYLE

        window = self._window(monkeypatch, tmp_path)

        window._build_runner(_make_cfg(test_type="DISCHARGE_ONLY"))

        assert self._style_at(window, 4.5) == _LIVE_LABEL_STALE_STYLE

    def test_discharge_after_charge_session_resets_budget(self, qapp, monkeypatch, tmp_path):
        from Prog.gui.panels.live_panel import _LIVE_LABEL_STALE_STYLE

        window = self._window(monkeypatch, tmp_path)
        window._build_runner(_make_cfg(test_type="CHARGE_ONLY", battery_model="ST1CHARGE"))

        window._build_runner(_make_cfg(test_type="DISCHARGE_ONLY", battery_model="ST1DISCHARGE"))

        assert self._style_at(window, 4.5) == _LIVE_LABEL_STALE_STYLE

    def test_resume_with_remaining_charge_step_keeps_budget(self, qapp, monkeypatch, tmp_path):
        """BQ_LEARNING_PHYSICAL@3: a hátralévő lépések között van CHARGE
        (charge_2), tehát a resume sem nullázhatja a keretet."""
        from Prog.gui.panels.live_panel import _LIVE_LABEL_BASE_STYLE

        window = self._window(monkeypatch, tmp_path)
        parent = window._sessions_root() / "session_TEST7AH_20200101_000000"
        parent.mkdir(parents=True)
        monkeypatch.setattr(sr, "find_resumable_session", lambda *a, **k: {
            "session_dir": parent, "checkpoint": {"next_safe_step_index": 3},
            "unreadable": False, "evaluation": None,
        })
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

        runner, _plan, start = window._build_runner(_make_cfg(test_type="BQ_LEARNING_PHYSICAL"))

        assert start == 3
        assert runner._config.parent_session_id == parent.name
        assert self._style_at(window, 11.5) == _LIVE_LABEL_BASE_STYLE
