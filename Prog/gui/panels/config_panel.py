"""
ConfigPanel — SessionConfig dataclass + Qt konfiguráló panel.
SessionConfig: GUI által kitöltött paraméterek, validate() visszaadja a hibákat.
Qt widget: Task 4-ben kerül ide.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

import yaml
from PySide6.QtWidgets import (
    QWidget, QFormLayout, QLineEdit, QDoubleSpinBox, QComboBox,
    QCheckBox, QGroupBox, QVBoxLayout, QPushButton, QLabel, QScrollArea,
)
from PySide6.QtCore import Signal


@dataclass
class SessionConfig:
    # Akkumulátor
    battery_profile_name: str = "FIAMM_12V"
    battery_model: str = ""
    nominal_capacity_ah: float = 0.0
    sample_id: str = ""

    # Műszerek
    psu_resource: str = ""
    load_resource: str = ""
    dmm_voltage_resource: str = ""
    dmm_temperature_resource: str = ""

    # PSU mód
    psu_mode: str = "INDEPENDENT"
    hardware_wiring_confirmed: bool = False

    # Teszt
    test_type: str = "CHARACTERIZATION"
    runner_tick_s: float = 2.0
    taper_hold_s: float = 600.0
    discharge_rate_divisor: int = 5
    ocv_soc_step_percent: float = 5.0

    # Hőkompenzáció
    temperature_compensation_mode: str = "MONITOR_ONLY"

    # Kiterjesztett paraméterek
    relax_after_charge_s: float = 600.0
    charge_current_A_override: float = 0.0
    discharge_current_A: float = 0.0
    discharge_terminate_voltage_V: float = 0.0

    def validate(self) -> list[str]:
        """Visszaadja a validációs hibaüzenetek listáját. Üres lista = OK."""
        errors: list[str] = []

        if self.nominal_capacity_ah <= 0:
            errors.append("nominal_capacity_ah > 0 kötelező")
        if not self.battery_model.strip():
            errors.append("battery_model nem lehet üres")
        if not self.psu_resource.strip():
            errors.append("psu_resource nem lehet üres")
        if not self.load_resource.strip():
            errors.append("load_resource nem lehet üres")
        if not self.dmm_voltage_resource.strip():
            errors.append("dmm_voltage_resource nem lehet üres")
        if not self.dmm_temperature_resource.strip():
            errors.append("dmm_temperature_resource nem lehet üres")
        if self.psu_mode in ("PARALLEL", "SERIES") and not self.hardware_wiring_confirmed:
            errors.append(
                f"hardware_wiring_confirmed = True kötelező {self.psu_mode} módban"
            )
        if self.battery_profile_name == "FIAMM_24V" and self.psu_mode != "SERIES":
            errors.append("FIAMM_24V (24V pack) csak SERIES PSU módban indítható")

        # Végfeszültség ellenőrzés (ha be van állítva)
        if self.discharge_terminate_voltage_V > 0:
            cell_count = _PROFILE_DEFAULTS.get(
                self.battery_profile_name, {}
            ).get("cell_count", 6)
            min_v = cell_count * 1.75  # FIAMM AGM: C/20 = 1.75V/cella, alatta maradandó károsodás
            if self.discharge_terminate_voltage_V < min_v:
                errors.append(
                    f"Végfeszültség ({self.discharge_terminate_voltage_V:.2f}V) "
                    f"< 1.75V/cella minimum ({min_v:.2f}V)"
                )

        # Töltőáram override ellenőrzés
        if self.charge_current_A_override > 0:
            psu_max = 3.0 if self.psu_mode == "PARALLEL" else 1.5
            if self.charge_current_A_override > psu_max:
                errors.append(
                    f"Töltőáram ({self.charge_current_A_override:.2f}A) "
                    f"> PSU {self.psu_mode} limit ({psu_max:.1f}A)"
                )

        return errors


_PROFILE_DEFAULTS = {
    "FIAMM_12V": {"nominal_voltage_V": 12.0, "cell_count": 6,
                  "battery_name": "FIAMM AGM", "manufacturer": "FIAMM"},
    "FIAMM_24V": {"nominal_voltage_V": 24.0, "cell_count": 12,
                  "battery_name": "FIAMM AGM 24V", "manufacturer": "FIAMM"},
}



class ConfigPanel(QWidget):
    """Konfiguráló panel — YAML betöltés + GUI override → SessionConfig."""

    config_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()
        self._load_yaml()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        root = QVBoxLayout(container)
        scroll.setWidget(container)
        outer.addWidget(scroll)

        # --- Akkumulátor ---
        batt_box = QGroupBox("Akkumulátor")
        batt_form = QFormLayout(batt_box)

        self._profile_combo = QComboBox()
        self._profile_combo.addItems(["FIAMM_12V", "FIAMM_24V"])
        self._profile_combo.currentTextChanged.connect(self._on_profile_changed)
        batt_form.addRow("Profil:", self._profile_combo)

        self._model_edit = QLineEdit()
        self._model_edit.setPlaceholderText("pl. FG20721")
        batt_form.addRow("Modell:", self._model_edit)

        self._capacity_spin = QDoubleSpinBox()
        self._capacity_spin.setRange(0.1, 1000.0)
        self._capacity_spin.setDecimals(1)
        self._capacity_spin.setSuffix(" Ah")
        batt_form.addRow("Névleges kapacitás:", self._capacity_spin)

        self._sample_id_edit = QLineEdit()
        self._sample_id_edit.setPlaceholderText("opcionális azonosító")
        batt_form.addRow("Sample ID:", self._sample_id_edit)

        self._cell_count_label = QLabel("6")
        batt_form.addRow("Cellaszám (csak olvasható):", self._cell_count_label)

        root.addWidget(batt_box)

        # --- Műszerek ---
        instr_box = QGroupBox("Műszerek (VISA resource stringek)")
        instr_form = QFormLayout(instr_box)

        self._psu_res_edit = QLineEdit()
        self._psu_res_edit.setPlaceholderText("USB0::0x05E6::0x2220::...")
        instr_form.addRow("PSU:", self._psu_res_edit)

        self._load_res_edit = QLineEdit()
        self._load_res_edit.setPlaceholderText("USB0::0x05E6::0x2380::...")
        instr_form.addRow("Load:", self._load_res_edit)

        self._dmm_v_res_edit = QLineEdit()
        self._dmm_v_res_edit.setPlaceholderText("TCPIP0::192.168.x.x::inst0::INSTR")
        instr_form.addRow("DMM feszültség:", self._dmm_v_res_edit)

        self._dmm_t_res_edit = QLineEdit()
        self._dmm_t_res_edit.setPlaceholderText("TCPIP0::192.168.x.x::inst0::INSTR")
        instr_form.addRow("DMM hőmérséklet:", self._dmm_t_res_edit)

        search_btn = QPushButton("Eszközök keresése…")
        search_btn.clicked.connect(self._open_device_search)
        instr_form.addRow("", search_btn)

        save_btn = QPushButton("Resource stringek mentése (local_config.yaml)")
        save_btn.clicked.connect(self._save_local_config)
        instr_form.addRow("", save_btn)

        root.addWidget(instr_box)

        # --- PSU mód ---
        psu_box = QGroupBox("PSU mód")
        psu_form = QFormLayout(psu_box)

        self._psu_mode_combo = QComboBox()
        self._psu_mode_combo.addItems(["INDEPENDENT", "PARALLEL", "SERIES"])
        self._psu_mode_combo.currentTextChanged.connect(self._on_psu_mode_changed)
        psu_form.addRow("Mód:", self._psu_mode_combo)

        self._psu_mode_info_label = QLabel("Max: 30V / 1.5A per csatorna")
        psu_form.addRow("", self._psu_mode_info_label)

        self._wiring_confirmed_cb = QCheckBox("Fizikai bekötés megerősítve")
        self._wiring_confirmed_cb.setEnabled(False)
        psu_form.addRow("", self._wiring_confirmed_cb)

        root.addWidget(psu_box)

        # --- Teszt ---
        test_box = QGroupBox("Teszt")
        test_form = QFormLayout(test_box)

        self._test_type_combo = QComboBox()
        self._test_type_combo.addItems(
            ["CHARACTERIZATION", "BQ_LEARNING_PHYSICAL", "OCV_SOC_CHARACTERIZATION"]
        )
        test_form.addRow("Teszttípus:", self._test_type_combo)

        self._tick_spin = QDoubleSpinBox()
        self._tick_spin.setRange(0.5, 30.0)
        self._tick_spin.setDecimals(1)
        self._tick_spin.setValue(2.0)
        self._tick_spin.setSuffix(" s")
        test_form.addRow("Runner tick:", self._tick_spin)

        self._taper_spin = QDoubleSpinBox()
        self._taper_spin.setRange(60.0, 7200.0)
        self._taper_spin.setDecimals(0)
        self._taper_spin.setValue(600.0)
        self._taper_spin.setSuffix(" s")
        test_form.addRow("Taper hold:", self._taper_spin)

        self._discharge_rate_combo = QComboBox()
        self._discharge_rate_combo.addItems(["C/5 (gyors)", "C/10 (közepes)", "C/20 (lassú)"])
        test_form.addRow("Kisütési ráta:", self._discharge_rate_combo)

        self._ocv_soc_step_spin = QDoubleSpinBox()
        self._ocv_soc_step_spin.setRange(1.0, 20.0)
        self._ocv_soc_step_spin.setDecimals(1)
        self._ocv_soc_step_spin.setValue(5.0)
        self._ocv_soc_step_spin.setSuffix(" %")
        test_form.addRow("OCV-SOC lépés:", self._ocv_soc_step_spin)

        root.addWidget(test_box)

        # --- Hőkompenzáció ---
        temp_box = QGroupBox("Hőkompenzáció")
        temp_form = QFormLayout(temp_box)
        self._temp_comp_combo = QComboBox()
        self._temp_comp_combo.addItems(["OFF", "MONITOR_ONLY", "ENABLED"])
        self._temp_comp_combo.setCurrentText("MONITOR_ONLY")
        temp_form.addRow("Mód:", self._temp_comp_combo)
        root.addWidget(temp_box)

        root.addStretch()

    def _on_profile_changed(self, name: str) -> None:
        defaults = _PROFILE_DEFAULTS.get(name, {})
        self._cell_count_label.setText(str(defaults.get("cell_count", "?")))
        if name == "FIAMM_24V":
            self._psu_mode_combo.setCurrentText("SERIES")
            self._wiring_confirmed_cb.setEnabled(True)

    def _on_psu_mode_changed(self, mode: str) -> None:
        needs_wiring = mode in ("PARALLEL", "SERIES")
        self._wiring_confirmed_cb.setEnabled(needs_wiring)
        if not needs_wiring:
            self._wiring_confirmed_cb.setChecked(False)
        info = {
            "INDEPENDENT": "Max: 30V / 1.5A per csatorna",
            "PARALLEL":    "Max: 30V / 3.0A",
            "SERIES":      "Max: 60V / 1.5A",
        }
        self._psu_mode_info_label.setText(info.get(mode, ""))

    def _load_yaml(self) -> None:
        from Prog import app_paths
        cfg: dict[str, Any] = {}
        for path in (app_paths.default_config_path(), app_paths.local_config_path()):
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                cfg.update(loaded)

        instr = cfg.get("instruments", {})
        psu_res = instr.get("psu", {}).get("resource", "")
        if psu_res and psu_res != "PLACEHOLDER":
            self._psu_res_edit.setText(psu_res)
        load_res = instr.get("load", {}).get("resource", "")
        if load_res and load_res != "PLACEHOLDER":
            self._load_res_edit.setText(load_res)
        dmm_v_res = instr.get("dmm_voltage", {}).get("resource", "")
        if dmm_v_res and dmm_v_res != "PLACEHOLDER":
            self._dmm_v_res_edit.setText(dmm_v_res)
        dmm_t_res = instr.get("dmm_temperature", {}).get("resource", "")
        if dmm_t_res and dmm_t_res != "PLACEHOLDER":
            self._dmm_t_res_edit.setText(dmm_t_res)

        psu_mode = instr.get("psu", {}).get("combination_mode", "INDEPENDENT")
        idx = self._psu_mode_combo.findText(psu_mode)
        if idx >= 0:
            self._psu_mode_combo.setCurrentIndex(idx)

        temp_mode = cfg.get("temperature_compensation_mode", "MONITOR_ONLY")
        idx = self._temp_comp_combo.findText(temp_mode)
        if idx >= 0:
            self._temp_comp_combo.setCurrentIndex(idx)

    def _save_local_config(self) -> None:
        from Prog import app_paths
        data = {
            "instruments": {
                "psu":             {"resource": self._psu_res_edit.text()},
                "load":            {"resource": self._load_res_edit.text()},
                "dmm_voltage":     {"resource": self._dmm_v_res_edit.text()},
                "dmm_temperature": {"resource": self._dmm_t_res_edit.text()},
            }
        }
        path = app_paths.local_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True)

    def _open_device_search(self) -> None:
        from Prog.gui.panels.device_search_dialog import DeviceSearchDialog
        dlg = DeviceSearchDialog(self)
        dlg.exec()

    def get_session_config(self) -> SessionConfig:
        discharge_rate_map = {
            "C/5 (gyors)": 5,
            "C/10 (közepes)": 10,
            "C/20 (lassú)": 20,
        }
        discharge_divisor = discharge_rate_map.get(
            self._discharge_rate_combo.currentText(), 5
        )
        return SessionConfig(
            battery_profile_name=self._profile_combo.currentText(),
            battery_model=self._model_edit.text().strip(),
            nominal_capacity_ah=self._capacity_spin.value(),
            sample_id=self._sample_id_edit.text().strip(),
            psu_resource=self._psu_res_edit.text().strip(),
            load_resource=self._load_res_edit.text().strip(),
            dmm_voltage_resource=self._dmm_v_res_edit.text().strip(),
            dmm_temperature_resource=self._dmm_t_res_edit.text().strip(),
            psu_mode=self._psu_mode_combo.currentText(),
            hardware_wiring_confirmed=self._wiring_confirmed_cb.isChecked(),
            test_type=self._test_type_combo.currentText(),
            runner_tick_s=self._tick_spin.value(),
            taper_hold_s=self._taper_spin.value(),
            discharge_rate_divisor=discharge_divisor,
            ocv_soc_step_percent=self._ocv_soc_step_spin.value(),
            temperature_compensation_mode=self._temp_comp_combo.currentText(),
        )
