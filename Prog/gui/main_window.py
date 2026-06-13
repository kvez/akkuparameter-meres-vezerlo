"""
MainWindow — QTabWidget koordinátor + QThread életciklus + objektumgráf factory.
A ConfigPanel-tól SessionConfig-ot kap, abból építi a teljes TestRunner stack-et.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QStatusBar, QTabWidget,
    QVBoxLayout, QWidget,
)

from Prog import app_paths
from Prog.gui.panels.config_panel import ConfigPanel, SessionConfig
from Prog.gui.panels.device_error_panel import DeviceErrorPanel
from Prog.gui.panels.live_panel import LivePanel
from Prog.gui.panels.checkpoint_panel import CheckpointPanel
from Prog.gui.worker import TestRunnerWorker


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Akkuteszter — Labor műszerfal")
        self.resize(1280, 900)

        self._thread: QThread | None = None
        self._worker: TestRunnerWorker | None = None
        self._instruments = None      # FIX-02: closeEvent safe_all_off
        self._session_dir: Path | None = None  # FIX-08: report.json helye

        self._tabs = QTabWidget()

        # Dedicated header bar above tabs
        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(72)
        hbox = QHBoxLayout(header)
        hbox.setContentsMargins(8, 4, 8, 4)

        logo_path = app_paths.resources_dir() / "psnd.png"
        if logo_path.exists():
            self.setWindowIcon(QIcon(str(logo_path)))
            pixmap = QPixmap(str(logo_path)).scaledToHeight(
                60, Qt.TransformationMode.SmoothTransformation
            )
            logo_label = QLabel()
            logo_label.setPixmap(pixmap)
            logo_label.setToolTip("PSND Elektronika")
            hbox.addWidget(logo_label)

        title_label = QLabel("Akkuteszter — Labor műszerfal")
        font = title_label.font()
        font.setBold(True)
        font.setPointSize(14)
        title_label.setFont(font)
        hbox.addWidget(title_label)
        hbox.addStretch()

        vbox.addWidget(header)
        vbox.addWidget(self._tabs)
        self.setCentralWidget(container)

        self._config_panel = ConfigPanel()
        self._live_panel = LivePanel()
        self._checkpoint_panel = CheckpointPanel()
        self._device_error_panel = DeviceErrorPanel()

        self._tabs.addTab(self._config_panel, "Konfiguráció")
        self._tabs.addTab(self._live_panel, "Élő mérés")
        self._checkpoint_tab_index = self._tabs.addTab(
            self._checkpoint_panel, "BQ Checkpoint"
        )
        self._tabs.setTabEnabled(self._checkpoint_tab_index, False)
        self._tabs.addTab(self._device_error_panel, "Eszköz hibák")

        self._live_panel.start_requested.connect(self._start_test)
        self._live_panel.stop_requested.connect(self._stop_test)
        self._live_panel.emergency_stop_requested.connect(self._emergency_stop)

        self._checkpoint_panel.close_requested.connect(self._on_checkpoint_close)
        self._checkpoint_panel.emergency_stop_requested.connect(self._emergency_stop)
        self._checkpoint_panel.continue_requested.connect(self._on_continue_requested)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage(
            "Kész — konfiguráld a paramétereket és nyomj Start-ot.")

    # ------------------------------------------------------------------ #
    # Ablak lezárás (FIX-02)                                              #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            reply = QMessageBox.question(
                self, "Teszt fut",
                "Teszt folyamatban van. Biztosan bezárod az ablakot?\n"
                "(A safe_off automatikusan megtörténik.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._emergency_stop()
                self._cleanup_thread()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

    # ------------------------------------------------------------------ #
    # Start / Stop                                                        #
    # ------------------------------------------------------------------ #

    def _start_test(self) -> None:
        if self._worker is not None:
            return
        cfg = self._config_panel.get_session_config()
        errors = cfg.validate()
        if errors:
            QMessageBox.warning(self, "Konfiguráció hiba", "\n".join(errors))
            return

        warnings = cfg.get_warnings()
        if warnings:
            reply = QMessageBox.question(
                self, "Figyelmeztetés",
                "\n".join(warnings) + "\n\nFolytassuk?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
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

        self._tabs.setTabEnabled(self._checkpoint_tab_index, False)   # reset
        self._worker.event_ready.connect(self._live_panel.append_event)
        self._worker.device_error_ready.connect(
            self._device_error_panel.append_device_error
        )
        self._worker.step_changed.connect(self._on_step_changed)
        self._worker.step_changed.connect(self._live_panel.set_step)
        self._worker.checkpoint_reached.connect(
            self._checkpoint_panel.show_checkpoint
        )
        self._worker.checkpoint_reached.connect(self._on_checkpoint_reached)

        self._worker.status_changed.connect(self._on_status_changed)

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
        keep_thread = (result.status == "CHECKPOINT_STOPPED"
                       and result.resume_possible)
        if not keep_thread:
            self._cleanup_thread()
        self._status_bar.showMessage(
            f"Kész | Töltve: {result.total_charge_ah:.4f} Ah | "
            f"Kisütve: {result.total_discharge_ah:.4f} Ah"
        )
        if result.status == "DONE":
            # FIX-08: riport generálás
            self._write_report(result)
            QMessageBox.information(
                self, "Teszt befejezve",
                f"Teszt kész — {result.status}\n"
                f"Töltve: {result.total_charge_ah:.4f} Ah\n"
                f"Kisütve: {result.total_discharge_ah:.4f} Ah",
            )
        elif result.status == "CHECKPOINT_STOPPED":
            pass  # tab és status bar már frissítve az _on_checkpoint_reached-ben

    def _write_report(self, result) -> None:
        """Összefoglaló report.json írása a session mappába."""
        if self._session_dir is None:
            return
        try:
            from Prog.src.report_generator import ReportGenerator
            rg = ReportGenerator()
            session_meta = {
                "psu_mode": getattr(self, "_psu_mode_str", "UNKNOWN"),
                "total_charge_ah": result.total_charge_ah,
                "total_discharge_ah": result.total_discharge_ah,
                "capacity_result_quality": "OK",
                "emergency_stop_occurred": (result.status == "FAULT"),
                "communication_faults_count": 0,
            }
            report = rg.generate(session_meta)
            report_path = self._session_dir / "report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        except Exception as exc:
            self._status_bar.showMessage(f"Riport generálás hiba: {exc}")

    def _on_step_changed(self, payload: dict) -> None:
        status = payload.get("runner_status", "")
        label  = payload.get("step_label", "")
        self._status_bar.showMessage(f"{status} — {label}")

    def _on_checkpoint_reached(self, event: dict) -> None:
        self._tabs.setTabEnabled(self._checkpoint_tab_index, True)
        self._tabs.setCurrentIndex(self._checkpoint_tab_index)
        if event.get("resume_possible", False):
            msg = "BQ checkpoint elérve — végezd el a BQ műveletet, majd folytathatod."
        else:
            msg = "BQ checkpoint elérve — végezd el a BQ műveletet, majd zárd le a sessiont."
        self._status_bar.showMessage(msg)

    def _on_checkpoint_close(self) -> None:
        self._tabs.setTabEnabled(self._checkpoint_tab_index, False)
        self._tabs.setCurrentIndex(0)
        self._status_bar.showMessage("Teszt lezárva — konfigurálj új mérést.")
        self._cleanup_thread()

    def _on_continue_requested(self) -> None:
        if self._worker is None:
            return
        self._worker.request_continue_from_checkpoint()
        self._live_panel.reset_plots()
        self._checkpoint_panel.set_continuing()
        self._status_bar.showMessage("Folytatás indítása...")

    def _on_status_changed(self, status: str) -> None:
        if (status == "RUNNING"
                and self._tabs.currentIndex() == self._checkpoint_tab_index):
            self._tabs.setCurrentIndex(1)

    def _on_fault(self, reason: str) -> None:
        self._cleanup_thread()
        self._status_bar.showMessage(f"HIBA: {reason}")
        QMessageBox.critical(
            self, "Teszt hiba", f"A teszt leállt:\n{reason}")

    def _cleanup_thread(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread.deleteLater()
            self._thread = None
        if self._worker:
            self._worker.deleteLater()
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
        from Prog.src.ocv_soc_controller import OcvSocController, OcvSocConfig
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
            cell_count=int(meta["cell_count"]),
            nominal_capacity_Ah=cfg.nominal_capacity_ah,
        )

        psu_mode  = PsuMode(cfg.psu_mode)
        temp_mode = TempCompMode(cfg.temperature_compensation_mode)
        safety = SafetyManager(
            profile=profile,
            psu_mode=psu_mode,
            temp_comp_mode=temp_mode,
            is_bq_mode=(cfg.test_type == "BQ_LEARNING_PHYSICAL"),
        )

        psu   = Keithley2220PSU()
        load  = Keithley2380Load()
        dmm_v = Keysight34465ADMM()
        dmm_t = Keysight34465ADMM()

        self._instruments = InstrumentManager(psu, load, dmm_v, dmm_t)
        self._psu_mode_str: str = cfg.psu_mode  # H-02: riporthoz

        instr_cfg = InstrumentConfig(
            psu_resource=cfg.psu_resource,
            load_resource=cfg.load_resource,
            dmm_voltage_resource=cfg.dmm_voltage_resource,
            dmm_temperature_resource=cfg.dmm_temperature_resource,
        )
        self._instruments.connect_all(instr_cfg)

        # P0-1: DMM mérési módok konfigurálása — connect után kötelező
        dmm_v.configure_dcv(range_V=100, nplc=10)
        dmm_t.configure_temp_4wire_pt100(nplc=10)

        # P0-2: PSU kombináció mód tényleges beállítása a műszeren
        if psu_mode == PsuMode.INDEPENDENT:
            psu.set_mode_independent()
        elif psu_mode == PsuMode.PARALLEL:
            psu.set_mode_parallel()
        elif psu_mode == PsuMode.SERIES:
            psu.set_mode_series()

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._session_dir = app_paths.exe_dir() / "Mérések" / "Sessions" / f"session_{cfg.battery_model}_{stamp}"
        logger = Logger(self._session_dir, LogConfig())

        discharge_A = (
            cfg.discharge_current_A
            if cfg.discharge_current_A > 0
            else profile.nominal_capacity_Ah / cfg.discharge_rate_divisor
        )

        def _make_charge_ctrl():
            return ChargeController(
                psu, load, dmm_v, dmm_t, profile, safety,
                ChargeConfig(
                    taper_hold_s=cfg.taper_hold_s,
                    charge_current_A_override=cfg.charge_current_A_override,
                ),
            )

        def _make_discharge_ctrl():
            return DischargeController(
                psu, load, dmm_v, dmm_t, profile, safety,
                DischargeConfig(
                    discharge_current_A=discharge_A,
                    terminate_voltage_V_override=cfg.discharge_terminate_voltage_V,
                ),
            )

        _DEFAULT_RELAX_S = 7200.0  # RelaxConfig.min_relax_s default

        _POST_DISCHARGE_RELAX_S = 18000.0   # 5h: FIAMM spec / BQ Learning

        def _make_relax_ctrl(step):
            if "discharge" in step.label.lower():
                relax_s = _POST_DISCHARGE_RELAX_S
            elif cfg.test_type == "CHARGE_ONLY":
                relax_s = cfg.relax_after_charge_s
            else:
                relax_s = _DEFAULT_RELAX_S   # 2h post-charge
            rc = RelaxController(dmm_v, RelaxConfig(min_relax_s=relax_s))
            rc.on_event = lambda ev: logger.log_event(
                ev.get("event_code", "RELAX_EVENT"),
                ev.get("event_message", ""),
            )
            return rc

        ocv_soc_config = OcvSocConfig(
            discharge_rate_divisor=cfg.discharge_rate_divisor,
            step_percent=cfg.ocv_soc_step_percent,
        )

        def _make_ocv_soc_ctrl():
            ctrl = OcvSocController(
                psu, load, dmm_v, dmm_t, profile, safety, ocv_soc_config
            )
            ctrl.on_soc_point = lambda data: logger.log_ocv_soc_point(data)
            return ctrl

        runner = TestRunner(
            instrument_manager=self._instruments,
            safety=safety,
            logger=logger,
            profile=profile,
            config=TestRunnerConfig(
                runner_tick_s=cfg.runner_tick_s,
                test_name=cfg.battery_model or "unnamed",
                sleep_enabled=True,
            ),
            charge_ctrl_factory=_make_charge_ctrl,
            discharge_ctrl_factory=_make_discharge_ctrl,
            relax_ctrl_factory=_make_relax_ctrl,
            ocv_soc_ctrl_factory=_make_ocv_soc_ctrl,
        )

        if cfg.test_type == "CHARGE_ONLY":
            test_plan = TestPlan.charge_only()
        elif cfg.test_type == "DISCHARGE_ONLY":
            test_plan = TestPlan.discharge_only()
        elif cfg.test_type == "CHARACTERIZATION":
            test_plan = TestPlan.characterization()
        elif cfg.test_type == "OCV_SOC_CHARACTERIZATION":
            test_plan = TestPlan.ocv_soc_characterization()
        else:
            test_plan = TestPlan.bq_learning_physical()
        return runner, test_plan
