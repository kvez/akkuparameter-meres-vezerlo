"""
MainWindow — QTabWidget koordinátor + QThread életciklus + objektumgráf factory.
A ConfigPanel-tól SessionConfig-ot kap, abból építi a teljes TestRunner stack-et.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QStatusBar,
)

from Prog.gui.panels.config_panel import ConfigPanel, SessionConfig
from Prog.gui.panels.live_panel import LivePanel
from Prog.gui.worker import TestRunnerWorker


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Akkuteszter — Labor műszerfal")
        self.resize(1280, 900)

        self._thread: QThread | None = None
        self._worker: TestRunnerWorker | None = None

        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)

        self._config_panel = ConfigPanel()
        self._live_panel = LivePanel()

        self._tabs.addTab(self._config_panel, "Konfiguráció")
        self._tabs.addTab(self._live_panel, "Élő mérés")

        self._live_panel.start_requested.connect(self._start_test)
        self._live_panel.stop_requested.connect(self._stop_test)
        self._live_panel.emergency_stop_requested.connect(self._emergency_stop)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage(
            "Kész — konfiguráld a paramétereket és nyomj Start-ot.")

    # ------------------------------------------------------------------ #
    # Start / Stop                                                        #
    # ------------------------------------------------------------------ #

    def _start_test(self) -> None:
        cfg = self._config_panel.get_session_config()
        errors = cfg.validate()
        if errors:
            QMessageBox.warning(self, "Konfiguráció hiba", "\n".join(errors))
            return

        try:
            runner, test_plan = self._build_runner(cfg)
        except Exception as exc:
            QMessageBox.critical(self, "Inicializálási hiba", str(exc))
            return

        self._live_panel.reset_plots()
        self._tabs.setCurrentIndex(1)

        self._thread = QThread()
        self._worker = TestRunnerWorker(runner, test_plan)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.sample_ready.connect(self._live_panel.update_sample)
        self._worker.status_changed.connect(self._live_panel.set_status)
        self._worker.status_changed.connect(self._status_bar.showMessage)
        self._worker.fault.connect(self._live_panel.set_fault)
        self._worker.fault.connect(self._on_fault)
        self._worker.finished.connect(self._on_finished)

        self._thread.start()
        self._status_bar.showMessage("Teszt elindítva…")

    def _stop_test(self) -> None:
        if self._worker:
            self._worker.request_stop()
            self._status_bar.showMessage("Leállítás folyamatban…")

    def _emergency_stop(self) -> None:
        if self._worker:
            self._worker.request_emergency_stop("USER_EMERGENCY_STOP")
            self._status_bar.showMessage("VÉSZLEÁLLÍTÁS kérve…")

    # ------------------------------------------------------------------ #
    # Worker events                                                       #
    # ------------------------------------------------------------------ #

    def _on_finished(self, result) -> None:
        self._cleanup_thread()
        self._status_bar.showMessage(
            f"Kész | Töltve: {result.total_charge_ah:.4f} Ah | "
            f"Kisütve: {result.total_discharge_ah:.4f} Ah"
        )
        if result.status == "DONE":
            QMessageBox.information(
                self, "Teszt befejezve",
                f"Teszt kész — {result.status}\n"
                f"Töltve: {result.total_charge_ah:.4f} Ah\n"
                f"Kisütve: {result.total_discharge_ah:.4f} Ah",
            )

    def _on_fault(self, reason: str) -> None:
        self._cleanup_thread()
        self._status_bar.showMessage(f"HIBA: {reason}")
        QMessageBox.critical(
            self, "Teszt hiba", f"A teszt leállt:\n{reason}")

    def _cleanup_thread(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        self._worker = None

    # ------------------------------------------------------------------ #
    # Objektumgráf factory                                               #
    # ------------------------------------------------------------------ #

    def _build_runner(self, cfg: SessionConfig):
        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.safety import SafetyManager, PsuMode, TempCompMode
        from Prog.src.instrument_manager import InstrumentManager, InstrumentConfig
        from Prog.src.logger import Logger, LogConfig
        from Prog.src.charge_controller import ChargeController, ChargeConfig
        from Prog.src.discharge_controller import DischargeController, DischargeConfig
        from Prog.src.relax_controller import RelaxController, RelaxConfig
        from Prog.src.test_runner import TestRunner, TestRunnerConfig, TestPlan
        from Prog.drivers.device_psu import Keithley2220PSU
        from Prog.drivers.device_load import Keithley2380Load
        from Prog.drivers.device_dmm import Keysight34465ADMM

        _PROFILE_META = {
            "FIAMM_12V": {"nominal_voltage_V": 12.0, "cell_count": 6},
            "FIAMM_24V": {"nominal_voltage_V": 24.0, "cell_count": 12},
        }
        meta = _PROFILE_META.get(cfg.battery_profile_name, _PROFILE_META["FIAMM_12V"])

        profile = BatteryProfile(
            battery_name=cfg.battery_profile_name,
            manufacturer="FIAMM",
            model=cfg.battery_model,
            nominal_voltage_V=meta["nominal_voltage_V"],
            cell_count=meta["cell_count"],
            nominal_capacity_Ah=cfg.nominal_capacity_ah,
        )

        psu_mode  = PsuMode(cfg.psu_mode)
        temp_mode = TempCompMode(cfg.temperature_compensation_mode)
        safety = SafetyManager(
            profile=profile,
            psu_mode=psu_mode,
            temp_comp_mode=temp_mode,
        )

        psu   = Keithley2220PSU()
        load  = Keithley2380Load()
        dmm_v = Keysight34465ADMM()
        dmm_t = Keysight34465ADMM()

        instruments = InstrumentManager(psu, load, dmm_v, dmm_t)
        instr_cfg = InstrumentConfig(
            psu_resource=cfg.psu_resource,
            load_resource=cfg.load_resource,
            dmm_voltage_resource=cfg.dmm_voltage_resource,
            dmm_temperature_resource=cfg.dmm_temperature_resource,
        )
        instruments.connect_all(instr_cfg)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        session_dir = Path("Mérések") / f"session_{cfg.battery_model}_{stamp}"
        logger = Logger(session_dir, LogConfig())

        charge_ctrl = ChargeController(
            psu, load, dmm_v, dmm_t, profile, safety,
            ChargeConfig(taper_hold_s=cfg.taper_hold_s),
        )
        discharge_ctrl = DischargeController(
            psu, load, dmm_v, dmm_t, profile, safety, DischargeConfig(),
        )
        relax_ctrl = RelaxController(dmm_v, RelaxConfig())

        runner = TestRunner(
            instrument_manager=instruments,
            safety=safety,
            logger=logger,
            profile=profile,
            config=TestRunnerConfig(
                runner_tick_s=cfg.runner_tick_s,
                test_name=cfg.battery_model or "unnamed",
                sleep_enabled=True,
            ),
            charge_controller=charge_ctrl,
            discharge_controller=discharge_ctrl,
            relax_controller=relax_ctrl,
        )

        test_plan = (
            TestPlan.characterization()
            if cfg.test_type == "CHARACTERIZATION"
            else TestPlan.bq_learning_physical()
        )
        return runner, test_plan
