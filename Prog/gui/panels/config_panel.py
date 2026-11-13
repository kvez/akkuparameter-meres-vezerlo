"""
ConfigPanel — SessionConfig dataclass + Qt konfiguráló panel.
SessionConfig: GUI által kitöltött paraméterek, validate() visszaadja a hibákat.
Qt widget: Task 4-ben kerül ide.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

import yaml
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from Prog.src.battery_profile import (
    UNKNOWN_DISCHARGE_RATE_ORIGIN,
    DischargeRateOrigin,
    discharge_rate_origin_for_mode,
    normal_discharge_minimum_per_cell,
)


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """`override` rekurzívan a `base`-re olvad -- csak azok a levél-kulcsok
    íródnak felül, amik TÉNYLEGESEN szerepelnek `override`-ban; minden más,
    `base`-ban meglévő kulcs (akár mélyen beágyazott) érintetlen marad.

    Kör 21 Batch F8 (ARC21-4(b), DFAM-017): a korábbi `cfg.update(loaded)`
    (sekély merge) a `default_config.yaml` egy egész NESTED blokkját (pl.
    `instruments.psu`) TELJESEN felülírta a `local_config.yaml` UGYANAZON
    kulcsú blokkjával, akkor is, ha az utóbbi nem tartalmazott minden
    almezőt -- egy `default_config.yaml`-ban definiált, de a
    `local_config.yaml`-ból hiányzó almező (pl. `combination_mode`)
    csendben elveszett, holott a hívó a `default_config.yaml`-t
    ALAPÉRTÉKKÉNT olvasta be elsőként, PONT azért, hogy a hiányzó mezőket
    onnan pótolja."""
    result = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge_dicts(existing, value)
        else:
            result[key] = value
    return result


# R2-B8 (R2-ARCH-01, DFAM-046): a battery_model a session-mappa nevébe kerül
# (main_window.py _build_runner(): session_{battery_model}_{stamp}). Az NTFS
# útvonal-komponens korlátja 255 UTF-16 kódegység; a fix rész
# "session_" (8) + "_" (1) + stamp "%Y%m%d_%H%M%S" (15) = 24, így a
# battery_model legfeljebb 255 - 24 = 231 UTF-16 kódegység lehet.
_BATTERY_MODEL_MAX_UTF16_UNITS = 231


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
    # Kör 19 D8g (SW19-05, DFAM-017): a régi alapérték (5) SOHA nem volt
    # elérhető a GUI-n át -- get_session_config() (lent) MINDIG explicit
    # 10-et ad át, felülírva ezt. Az egyetlen konstrukció, ahol a
    # dataclass-alapérték ténylegesen számít, egy KÖZVETLEN, GUI-n kívüli
    # SessionConfig()-hívás (pl. Prog/tools/ szkript) -- ott a "5" csendben
    # ELTÉRT volna az OcvSocConfig.discharge_rate_divisor testvér-mezőjének
    # (ocv_soc_controller.py) 10-es alapértékétől. Mostantól mindkettő
    # 10 -- egységes alapérték, zéró viselkedésváltozás a GUI-úton (ahol a
    # tényleges érték már eddig is 10 volt). KÖVETKEZMÉNY: ez a mező tagja
    # a CONFIG_HASH_FIELDS-nek (session_recovery.py) -- egy KÖZVETLEN
    # SessionConfig() konstrukció (nem a GUI-n át), ami a régi 5-ös
    # alapértékre támaszkodott, MOST eltérő config_hash-t kapna, ami egy
    # ilyen konstrukcióból korábban készült, safe_boundary=True, még
    # folyamatban lévő checkpoint resume-ját blokkolná (ugyanaz a
    # következményosztály, mint R2-04-F2/DFAM-003). A jelenlegi lemezen lévő
    # egyetlen ilyen konstrukció (Prog/tools/d10_run_two_traditional_
    # learning_cycles.py) checkpoint-jai MIND safe_boundary=False-ok --
    # ezeket a resume-kapu MÁR ma is elutasítja, FÜGGETLENÜL ettől a
    # változástól, tehát a konkrét lemezállapotot nem érinti.
    discharge_rate_divisor: int = 10
    ocv_soc_step_percent: float = 5.0

    # Hőkompenzáció
    temperature_compensation_mode: str = "MONITOR_ONLY"
    # Hőmérséklet-szenzor típus — RTD_4W_PT100 a gyári (4-eres PT100), a
    # THERMISTOR_2W_NTC_10K_B3950 pótlás a sérült RTD helyett (2-eres, 10kΩ@25°C,
    # B=3950K NTC, szoftveres Beta-átszámítás — l. device_dmm.py).
    temperature_sensor_mode: str = "RTD_4W_PT100"

    # Kiterjesztett paraméterek
    relax_after_charge_s: float = 600.0
    charge_current_A_override: float = 0.0
    discharge_current_A: float = 0.0
    discharge_terminate_voltage_V: float = 0.0
    # Belső bizonyítékadat; nincs hozzá új kezelői mező.
    discharge_rate_origin: DischargeRateOrigin = UNKNOWN_DISCHARGE_RATE_ORIGIN

    # WP-AK-16: BQ34Z110 traditional learning / Golden GG — TRADITIONAL_LEARNING
    # és GOLDEN_GG test_type esetén a session-szintű PicoBQTransport ehhez a
    # porthoz csatlakozik (l. main_window.py::_build_runner()).
    bq_port: str = ""
    # SUP03-GG-007: nincs biztonságos alapérték, explicit meg kell adni.
    golden_gg_system_rate_current_A: float = 0.0

    def validate(self) -> list[str]:
        """Visszaadja a validációs hibaüzenetek listáját. Üres lista = OK."""
        errors: list[str] = []

        if self.nominal_capacity_ah <= 0:
            errors.append("nominal_capacity_ah > 0 kötelező")
        if not self.battery_model.strip():
            errors.append("battery_model nem lehet üres")
        # R1 F-18: a battery_model a session-mappa nevébe kerül
        # (main_window.py::_build_runner(): session_{battery_model}_{stamp});
        # egy elválasztó a Sessions gyökér alá beágyazott (vagy '..'-tal
        # azon kívüli) mappát hozna létre, amit a nem rekurzív discovery/
        # resume nem lát.
        if "/" in self.battery_model or "\\" in self.battery_model:
            errors.append(
                "battery_model nem tartalmazhat útvonal-elválasztót ('/' vagy '\\')"
            )
        # R2-B8 (R2-ARCH-01): a Windows-fájlnévben tiltott karakterek
        # ('<>:"|?*' és U+0000..U+001F) és a komponens-hosszkorlát ellenőrzése nélkül a
        # session-mappa mkdir-je csak a Start UTÁN bukna. A '\x7f' NEM tiltott
        # (NTFS-en a mkdir sikerül). A 'surrogatepass' miatt egy magányos
        # surrogate sem dob UnicodeEncodeError-t a validate()-ben.
        forbidden = sorted(
            {c for c in self.battery_model if c in '<>:"|?*' or ord(c) < 32}
        )
        if forbidden:
            errors.append(
                "battery_model nem tartalmazhat Windows-fájlnévben tiltott karaktert "
                f"('<', '>', ':', '\"', '|', '?', '*' vagy vezérlőkarakter): {forbidden!r}"
            )
        if (
            len(self.battery_model.encode("utf-16-le", "surrogatepass")) // 2
            > _BATTERY_MODEL_MAX_UTF16_UNITS
        ):
            errors.append(
                "battery_model túl hosszú a session-mappanévhez (legfeljebb "
                f"{_BATTERY_MODEL_MAX_UTF16_UNITS} UTF-16 kódegység)"
            )
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

        # D9/F-01 (2026-09-13, Riportok/ui_policy_audit_20260913.md): ez a
        # két ellenőrzés korábban egy SAJÁT, a ui_policy.py registrytől
        # FÜGGETLEN, hardkódolt (test_type in (...)) forrásból élt --
        # ugyanaz a DFAM-017 (configuration_multiple_sources_of_truth)
        # mintázat, mint a többi, itt már javított testvér-eset
        # (PROFILE_META/PSU_HW_MAX_CURRENT_A, l. fent/lent). A
        # ui_policy.is_group_required() SUP02-ARCH-003 szerint a
        # kötelezőségi szabályok egyetlen, központi forrása kellene legyen
        # -- most az is. Nulla viselkedésváltozás: a registry ugyanazt a
        # 2 modot/csoportot jelöli kötelezőnek, mint a korábbi hardkódolt
        # lista (l. ui_policy.py TRADITIONAL_LEARNING/GOLDEN_GG
        # required_groups).
        #
        # A registry TOVÁBBI (11) required_groups-bejegyzése (charge_
        # current/discharge_rate/terminate_voltage/pulse_current/
        # capacity_basis/chemid_c20_current/chemid_manufacturer_cutoff)
        # SZÁNDÉKOSAN NEM kerül itt hard ">0"-enforcement alá: ezek mind
        # policy-vezérelt, MINDIG valamilyen auto/default értéket kapnak
        # (pl. a kisütőáram spinbox "auto (C/5)" speciális értéke,
        # DynamicSessionModel pulse_current_policy default C5, stb.) --
        # egy ">0 kötelező" check itt egy LEGITIM auto-módot utasítana el
        # hibaként. Ezeknél a "required" a registryben azt jelenti, hogy a
        # csoport látható/mérlegelhető kell legyen indítás előtt (l.
        # is_group_visible()+a GUI gate-ek), NEM azt, hogy a kezelőnek
        # explicit felül kellene írnia. capacity_basis-t egyébként a fenti
        # nominal_capacity_ah-check már MINDEN módban (nem csak OCV_SOC-nál)
        # lefedi.
        from Prog.gui.ui_policy import is_group_required
        if is_group_required(self.test_type, "bq_port") and not self.bq_port.strip():
            errors.append("bq_port nem lehet üres TRADITIONAL_LEARNING/GOLDEN_GG módban")
        if (
            is_group_required(self.test_type, "golden_gg_system_rate_current")
            and self.golden_gg_system_rate_current_A <= 0
        ):
            errors.append(
                "golden_gg_system_rate_current_A > 0 kötelező (SUP03-GG-007: nincs "
                "automatikus C/10-/C/20-származtatás)"
            )

        from Prog.gui.ui_policy import is_group_visible
        if is_group_visible(self.test_type, "terminate_voltage"):
            from Prog.gui.derived_parameters import PROFILE_META
            meta = PROFILE_META.get(self.battery_profile_name, {})
            minimum = normal_discharge_minimum_per_cell(
                manufacturer=meta.get("manufacturer", ""), model=self.battery_model,
                current_A=self.discharge_current_A, origin=self.discharge_rate_origin,
            ) * meta.get("cell_count", 6)
            requested = self.discharge_terminate_voltage_V
            if not isfinite(requested) or requested < 0.0 or (requested != 0.0 and requested < minimum):
                errors.append(
                    f"Végfeszültség ({requested!r}V) < GOV-029 minimum ({minimum:.2f}V)"
                )
            if not isfinite(self.discharge_current_A) or self.discharge_current_A < 0.0:
                errors.append("GOV-029: érvénytelen discharge_current_A")
            origin = self.discharge_rate_origin
            if origin.capacity_Ah != 0.0 and (
                not isfinite(origin.capacity_Ah) or (
                    origin.capacity_basis == "NOMINAL_C10_CAPACITY"
                    and origin.capacity_Ah != self.nominal_capacity_ah
                )
            ):
                errors.append("GOV-029: eltérő kapacitásbázis-eredet")

        # Töltőáram override ellenőrzés
        if self.charge_current_A_override > 0:
            # Governance-v2 review-kör 1 (CF-15/DFAM-017 3. előfordulás,
            # batch 4.5): korábban inline "3.0 if PARALLEL else 1.5" —
            # ugyanaz az érték, mint a kanonikus derived_parameters.
            # PSU_HW_MAX_CURRENT_A (amit a ConfigPanel.max_charge_current_A()
            # már importál, l. lent), de itt egy SAJÁT, testvér-inkonzisztens
            # másolatként élt.
            from Prog.gui.derived_parameters import PSU_HW_MAX_CURRENT_A
            psu_max = PSU_HW_MAX_CURRENT_A.get(self.psu_mode, 1.5)
            if self.charge_current_A_override > psu_max:
                errors.append(
                    f"Töltőáram ({self.charge_current_A_override:.2f}A) "
                    f"> PSU {self.psu_mode} limit ({psu_max:.1f}A)"
                )

        return errors

    def get_warnings(self) -> list[str]:
        """Nem blokkoló figyelmeztetések — indítható, de érdemes figyelni."""
        warnings: list[str] = []
        if self.discharge_terminate_voltage_V > 0:
            from Prog.gui.derived_parameters import PROFILE_META
            cell_count = PROFILE_META.get(
                self.battery_profile_name, {}
            ).get("cell_count", 6)
            warn_v = cell_count * 1.75
            if self.discharge_terminate_voltage_V < warn_v:
                warnings.append(
                    f"Végfeszültség ({self.discharge_terminate_voltage_V:.2f}V) "
                    f"< az igazolt FG20721 C/20 minimuma ({warn_v:.2f}V). "
                    "GOV-029: más vagy bizonytalan rátánál 1.80V/cella a minimum; "
                    "a minimum alatti érték normál kisütésben blokkoló hiba."
                )
        return warnings


# Governance-v2 review-kör 1 (CF-15/DFAM-017 3. előfordulás, batch 4.5):
# a korábbi, itt élt _PROFILE_DEFAULTS dict megszűnt — a kanonikus forrás
# mostantól derived_parameters.PROFILE_META (l. validate()/get_warnings()/
# _on_profile_changed() -- Kör 19 D8b/SW19-10: a korábbi "get_errors()"
# hivatkozás lógó volt, ilyen metódus sosem létezett itt; a hibalistát a
# validate() adja vissza). A _PROFILE_DEFAULTS "battery_name"/"manufacturer"
# mezői sosem lettek kiolvasva ebben a fájlban (holt adat) — a
# nominal_voltage_V/cell_count az egyetlen ténylegesen használt kulcsok,
# mindkettő megvan a PROFILE_META-ban, azonos értékkel.



class ConfigPanel(QWidget):
    """Konfiguráló panel — YAML betöltés + GUI override → SessionConfig.

    WP-AK-03 (SUP02 production GUI integráció): a reaktív mezők (töltőáram,
    kisütőáram, végfeszültség) mostantól a `DynamicSessionModel`-en (l.
    `Prog/gui/derived_parameters.py`) keresztül számolnak, nem ad-hoc
    widget-logikán. Egyirányú adatfolyam (SUP02-REACT-003, nincs signal
    recursion): widget-esemény → `self._model.set_X(...)` → `_render_from_model()`
    (jelblokkolással) írja vissza a widgeteket a model aktuális állapotára.
    A model önmaga sosem olvas widgetből."""

    config_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()
        self._load_yaml()

        from Prog.gui.derived_parameters import DynamicSessionModel
        self._model = DynamicSessionModel(
            battery_profile_name=self._profile_combo.currentText(),
            c10_capacity_Ah=self._capacity_spin.value(),
            test_type=self._test_type_combo.currentText(),
            psu_mode=self._psu_mode_combo.currentText(),
        )
        # SUP02-POLICY-001/002: alapértelmezett policy-k a jelenlegi (WP-AK-03
        # előtti) widget-alapértékek/címkék szerint — "auto (C/5)" a kisütési
        # áramnál, "1.80V/cella" a végfeszültségnél (C5/C10 preset alapértéke).
        from Prog.gui.derived_parameters import (
            DischargeRatePolicy,
            TerminateVoltagePolicy,
        )
        with self._model.batch_update():
            self._model.set_discharge_rate_policy(DischargeRatePolicy.C5)
            # Kör 20 Batch E5 (SW20-2): korábban V_1_80_PER_CELL -- azaz az
            # "auto (profil default)" felirat mögött HARDKÓDOLT 1.80 V/cella
            # állt, függetlenül a profil-YAML terminate_voltage_per_cell_V
            # mezőjétől, amit a TÉNYLEGESEN alkalmazott cutoff
            # (BatteryProfile.terminate_voltage_pack_V) használ. A
            # PROFILE_DEFAULT policy a modell YAML-ból feltöltött
            # terminate_voltage_per_cell_default_V-jét olvassa. A két gyári
            # profilnál (1.80) az eredmény bitre azonos.
            self._model.set_terminate_voltage_policy(TerminateVoltagePolicy.PROFILE_DEFAULT)
        # Kör 20 Batch E5 (SW20-2): a profil-YAML override-oknak a
        # KONSTRUKCIÓKORI _render_from_model() ELŐTT kell megtörténniük,
        # különben a végfeszültség-spin a YAML-tól eltérő alapértéket
        # mutatná az első render és az első profilváltás között.
        self._apply_profile_yaml_overrides(self._profile_combo.currentText())
        self._render_from_model()
        self._update_visibility()
        self._refresh_i10_source_label(self._profile_combo.currentText())
        self._refresh_chemid_readonly_labels(self._profile_combo.currentText())
        self._refresh_charge_safety_limit_label(self._profile_combo.currentText())
        self._refresh_ocv_soc_partial_discharge_rate_label()

    def _build_ui(self) -> None:
        from Prog.gui.derived_parameters import LOAD_HW_MAX_CURRENT_A

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
        self._model_edit.textChanged.connect(self._on_battery_model_changed)
        batt_form.addRow("Modell:", self._model_edit)

        self._capacity_spin = QDoubleSpinBox()
        self._capacity_spin.setRange(0.1, 1000.0)
        self._capacity_spin.setDecimals(1)
        self._capacity_spin.setSuffix(" Ah")
        self._capacity_spin.setToolTip(
            "Az adatlap szerinti 10 órás kapacitás. "
            "C-ráta számítás alapja (0.25×C10 = max töltőáram)."
        )
        batt_form.addRow("C10 kapacitás (Ah):", self._capacity_spin)

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

        self._search_btn = QPushButton("Eszközök keresése…")
        self._search_btn.clicked.connect(self._open_device_search)
        instr_form.addRow("", self._search_btn)

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

        # MAX verifier P2-1 pontosítás (batch 4.5): ez a kezdőérték csak
        # bootstrap-placeholder — __init__() a _build_ui() UTÁN azonnal
        # meghívja _render_from_model()-t, ami a kanonikus PSU_HW_MAX_
        # VOLTAGE_V/CURRENT_A-ból számolt, tényleges szöveggel felülírja,
        # mielőtt a widget egyáltalán megjelenne (paint event előtt).
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
            ["CHARACTERIZATION", "BQ_LEARNING_PHYSICAL", "OCV_SOC_CHARACTERIZATION",
             "CHEMID_CHARACTERIZATION_PBA", "CHARGE_ONLY", "DISCHARGE_ONLY",
             "TRADITIONAL_LEARNING", "GOLDEN_GG", "IEC_DC_INTERNAL_RESISTANCE"]
        )
        test_form.addRow("Teszttípus:", self._test_type_combo)

        # WP-AK-16: BQ soros port (TRADITIONAL_LEARNING/GOLDEN_GG) — a session
        # saját PicoBQTransport-ját ez a port azonosítja, l. main_window.py.
        self._bq_port_edit = QLineEdit()
        self._bq_port_edit.setPlaceholderText("pl. COM8")
        test_form.addRow("BQ (Pico) port:", self._bq_port_edit)

        # WP-AK-16/SUP03-GG-007: nincs automatikus C/10-/C/20-származtatás.
        self._golden_gg_current_spin = QDoubleSpinBox()
        self._golden_gg_current_spin.setRange(0.0, 3.0)
        self._golden_gg_current_spin.setDecimals(2)
        self._golden_gg_current_spin.setSingleStep(0.01)
        self._golden_gg_current_spin.setSuffix(" A")
        test_form.addRow("Golden GG rendszer-kisütőáram:", self._golden_gg_current_spin)

        # Governance-v2 kör 2: IEC 60896-21 §6.3 -- Ia=4×I10/Ib=20×I10 minden
        # időzítést a forrás rögzít (nincs GUI-szabad numerikus paraméter),
        # az EGYETLEN mód-specifikus elem a profilból jövő I10 csak-olvasható
        # megjelenítése, hogy a kezelő indítás előtt lássa, melyik értékből
        # számolódik Ia/Ib -- l. ui_policy.py "i10_source_display" csoport.
        self._i10_source_label = QLabel("-")
        test_form.addRow("I10 forrás (profil):", self._i10_source_label)

        # D9/F-02 (2026-09-13, Riportok/ui_policy_audit_20260913.md): a
        # ui_policy.py CHEMID_CHARACTERIZATION_PBA policy a "chemid_c20_
        # current"/"chemid_manufacturer_cutoff" csoportokat LÁTHATÓNAK ÉS
        # KÖTELEZŐNEK deklarálja, de ezeknek korábban NEM volt GUI-
        # megjelenítése -- a chemid_controller.py (213-215./795. sor)
        # ténylegesen a profil C10-kapacitásából (/20) és a profil YAML
        # terminate_voltage_per_cell_V×cell_count-jából számolja ezeket,
        # GUI-bemenet nélkül -- az operátor indítás előtt NEM látta,
        # milyen kisütőáramra/cutoff-feszültségre indul egy ~20-24 órás,
        # valós energiaátadású ciklus. Csak-olvasható, az i10_source_label
        # PONTOS mintájára (ugyanabból a profil-YAML-ből, amit a
        # _build_runner() is olvas).
        self._chemid_c20_current_label = QLabel("-")
        test_form.addRow("C/20 kisütőáram (profil):", self._chemid_c20_current_label)
        self._chemid_cutoff_label = QLabel("-")
        test_form.addRow("Gyártói cutoff (profil):", self._chemid_cutoff_label)
        # R1 B8c, F-24 (P2, DFAM-026): OCV_SOC alatt a kisütési lépcsők
        # cutoffja a profilból jön (ocv_soc_controller.py `profile.
        # terminate_voltage_pack_V`), a szerkeszthető "Végfeszültség" sornak
        # ott nincs fogyasztója -- csak-olvasható megjelenítés a ChemID
        # cutoff-sor mintájára, UGYANABBÓL az értékből (l.
        # _refresh_chemid_readonly_labels(), nincs új számítás).
        self._ocv_soc_profile_cutoff_label = QLabel("-")
        test_form.addRow(
            "Végfeszültség (profil, OCV_SOC):", self._ocv_soc_profile_cutoff_label
        )

        # D9/F-06 (2026-09-13, Riportok/ui_policy_audit_20260913.md): a
        # `chemid_conditioning`/`chemid_relax_5h` csoportok LÁTHATÓNAK
        # deklarálva, de a ChemIdConfig() kondicionálási/relax-időtartamai
        # (fix konstansok, `main_window.py`-ban SOHA nem override-olva GUI-
        # ból) korábban sehol nem jelentek meg -- az operátor nem látta,
        # mekkora az ELŐRE MEGHATÁROZOTT minimumidő egy ~24h+ CHEMID-ciklus
        # előtt. Csak-olvasható, magukból a ChemIdConfig default-okból
        # számolva (nem egy harmadik, kézzel duplikált szám).
        from Prog.src.chemid_controller import ChemIdConfig
        _chemid_defaults = ChemIdConfig()
        self._chemid_conditioning_label = QLabel(
            f"{_chemid_defaults.conditioning_min_s / 3600:.0f} óra (fix)"
        )
        test_form.addRow("Kondicionálás (fix):", self._chemid_conditioning_label)
        self._chemid_relax_label = QLabel(
            f"{_chemid_defaults.relax_full_min_s / 3600:.0f} + "
            f"{_chemid_defaults.relax_empty_min_s / 3600:.0f} óra (fix, telített/üres)"
        )
        test_form.addRow("Relax idő (fix):", self._chemid_relax_label)

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

        self._relax_after_charge_spin = QDoubleSpinBox()
        self._relax_after_charge_spin.setRange(60.0, 7200.0)
        self._relax_after_charge_spin.setDecimals(0)
        self._relax_after_charge_spin.setValue(600.0)
        self._relax_after_charge_spin.setSuffix(" s")
        test_form.addRow("Relax töltés után:", self._relax_after_charge_spin)

        self._ocv_soc_step_spin = QDoubleSpinBox()
        self._ocv_soc_step_spin.setRange(1.0, 20.0)
        self._ocv_soc_step_spin.setDecimals(1)
        self._ocv_soc_step_spin.setValue(5.0)
        self._ocv_soc_step_spin.setSuffix(" %")
        test_form.addRow("OCV-SOC lépés:", self._ocv_soc_step_spin)

        # D9/F-04 (2026-09-13, Riportok/ui_policy_audit_20260913.md): a
        # "partial_discharge_rate" csoport láthatónak deklarálva, de a
        # tényleges OCV_SOC lépcsős kisütőáram-osztó (C/10) hardkódolt
        # `get_session_config()`-ban (lent), GUI-megjelenítés nélkül — az
        # operátor nem látta, milyen tényleges áramra fut a többnapos
        # lépcsőzetes kisütés. Csak-olvasható, a `derived_parameters.py`
        # kanonikus `compute_discharge_current_A()` formulájával (ugyanaz
        # a divisor, mint `get_session_config()`-ban), a F-02 mintáját
        # követve.
        self._ocv_soc_partial_discharge_rate_label = QLabel("-")
        test_form.addRow(
            "Lépcsős kisütőáram (C/10):", self._ocv_soc_partial_discharge_rate_label
        )

        # D9/F-07 (2026-09-13, Riportok/ui_policy_audit_20260913.md): a
        # "charge_safety_limits"/"discharge_safety_limits" csoportok
        # láthatónak deklarálva, de a tényleges safety-backstopok
        # (BatteryProfile.batt_absolute_max_V_per_cell,
        # SafetyManager.max_discharge_time_s/max_discharge_Ah_factor) a
        # kódban élnek, GUI-ból NEM állíthatók (ez HELYES, l.
        # rules/10-safety.md #7 -- szerkeszthető safety-limit GUI NEM
        # kerül bevezetésre). Csak-olvasható tájékoztató sor, a fix
        # konstansokból (BATT_ABSOLUTE_MAX_V_PER_CELL/
        # MAX_DISCHARGE_TIME_S/MAX_DISCHARGE_AH_FACTOR) számolva, nem egy
        # harmadik, kézzel duplikált számból.
        self._charge_safety_limit_label = QLabel("-")
        test_form.addRow("Max. cellafeszültség (fix):", self._charge_safety_limit_label)
        from Prog.src.safety import MAX_DISCHARGE_AH_FACTOR, MAX_DISCHARGE_TIME_S
        self._discharge_safety_limit_label = QLabel(
            f"{MAX_DISCHARGE_TIME_S / 3600:.0f} óra / "
            f"{MAX_DISCHARGE_AH_FACTOR:.2f}×kapacitás (fix)"
        )
        test_form.addRow("Max. kisütési idő/Ah (fix):", self._discharge_safety_limit_label)

        root.addWidget(test_box)
        self._test_form = test_form

        # --- Hőkompenzáció ---
        temp_box = QGroupBox("Hőkompenzáció")
        temp_form = QFormLayout(temp_box)
        self._temp_comp_combo = QComboBox()
        self._temp_comp_combo.addItems(["OFF", "MONITOR_ONLY", "ENABLED"])
        self._temp_comp_combo.setCurrentText("MONITOR_ONLY")
        temp_form.addRow("Mód:", self._temp_comp_combo)

        self._temp_sensor_combo = QComboBox()
        self._temp_sensor_combo.addItems(
            ["RTD_4W_PT100", "THERMISTOR_2W_NTC_10K_B3950"]
        )
        self._temp_sensor_combo.setCurrentText("RTD_4W_PT100")
        self._temp_sensor_combo.setToolTip(
            "RTD_4W_PT100: gyári 4-eres PT100.\n"
            "THERMISTOR_2W_NTC_10K_B3950: pótszonda sérült RTD esetén "
            "(2-eres, 10kΩ@25°C, B=3950K, szoftveres átszámítás)."
        )
        temp_form.addRow("Szenzor típus:", self._temp_sensor_combo)
        root.addWidget(temp_box)
        self._temp_box = temp_box

        # --- Töltési paraméterek ---
        charge_box = QGroupBox("Töltési paraméterek")
        charge_form = QFormLayout(charge_box)

        self._charge_current_spin = QDoubleSpinBox()
        self._charge_current_spin.setRange(0.0, 3.0)
        self._charge_current_spin.setDecimals(2)
        self._charge_current_spin.setSingleStep(0.01)
        self._charge_current_spin.setSuffix(" A")
        self._charge_current_spin.setSpecialValueText("auto (számított)")

        self._charge_current_label = QLabel("számított: — A")
        charge_form.addRow("Töltőáram:", self._charge_current_spin)
        charge_form.addRow("", self._charge_current_label)

        root.addWidget(charge_box)
        self._charge_box = charge_box

        # --- Kisütési paraméterek ---
        discharge_box = QGroupBox("Kisütési paraméterek")
        discharge_layout = QVBoxLayout(discharge_box)

        # Preset gombok
        preset_widget = QWidget()
        preset_hbox = QHBoxLayout(preset_widget)
        preset_hbox.setContentsMargins(0, 0, 0, 0)

        btn_c5  = QPushButton("C/5")
        btn_c10 = QPushButton("C/10")
        btn_c20 = QPushButton("C/20")
        preset_hbox.addWidget(btn_c5)
        preset_hbox.addWidget(btn_c10)
        preset_hbox.addWidget(btn_c20)
        preset_hbox.addStretch()
        discharge_layout.addWidget(preset_widget)

        discharge_form = QFormLayout()
        self._discharge_current_spin = QDoubleSpinBox()
        self._discharge_current_spin.setRange(0.0, LOAD_HW_MAX_CURRENT_A)
        self._discharge_current_spin.setDecimals(2)
        self._discharge_current_spin.setSingleStep(0.01)
        self._discharge_current_spin.setSuffix(" A")
        self._discharge_current_spin.setSpecialValueText("auto (C/5)")
        discharge_form.addRow("Áram:", self._discharge_current_spin)

        self._discharge_current_label = QLabel("")
        self._discharge_current_label.setWordWrap(True)
        discharge_form.addRow("", self._discharge_current_label)

        self._discharge_term_v_spin = QDoubleSpinBox()
        self._discharge_term_v_spin.setRange(0.0, 60.0)
        self._discharge_term_v_spin.setDecimals(2)
        self._discharge_term_v_spin.setSingleStep(0.01)
        self._discharge_term_v_spin.setSuffix(" V")
        self._discharge_term_v_spin.setSpecialValueText("auto (profil default)")
        discharge_form.addRow("Végfeszültség:", self._discharge_term_v_spin)
        discharge_layout.addLayout(discharge_form)

        root.addWidget(discharge_box)
        self._discharge_box = discharge_box
        self._discharge_form = discharge_form
        self._discharge_preset_widget = preset_widget

        # --- Kapcsolatok ---
        # WP-AK-03: minden reaktív mező a DynamicSessionModel-en keresztül
        # frissül — l. az osztály docstringjét az egyirányú adatfolyamról.
        self._capacity_spin.valueChanged.connect(self._on_capacity_changed)
        self._charge_current_spin.valueChanged.connect(self._on_charge_current_spin_changed)
        self._discharge_current_spin.valueChanged.connect(self._on_discharge_current_spin_changed)
        self._discharge_term_v_spin.valueChanged.connect(self._on_discharge_term_v_spin_changed)
        self._wiring_confirmed_cb.toggled.connect(self._on_wiring_confirmed_toggled)
        self._test_type_combo.currentTextChanged.connect(self._on_test_type_changed)
        # _psu_mode_combo → _on_psu_mode_changed (nincs dupla kötés)

        btn_c5.clicked.connect(lambda: self._apply_discharge_preset(5, 1.80))
        btn_c10.clicked.connect(lambda: self._apply_discharge_preset(10, 1.80))
        btn_c20.clicked.connect(lambda: self._apply_discharge_preset(20, 1.75))

        root.addStretch()

    def _on_profile_changed(self, name: str) -> None:
        if not hasattr(self, "_model"):
            return  # _build_ui() alatti kezdeti addItems()-jelzés, model még nincs
        from Prog.gui.derived_parameters import PROFILE_META
        defaults = PROFILE_META.get(name, {})
        self._cell_count_label.setText(str(defaults.get("cell_count", "?")))
        self._refresh_i10_source_label(name)
        self._refresh_chemid_readonly_labels(name)
        self._refresh_charge_safety_limit_label(name)
        self._apply_profile_yaml_overrides(name)
        # SUP02-DERIVE-001/REACT-006: a model saját maga kezeli a pack-feszültség
        # frissítést, a wiring-confirmation resetet és a 24V→SERIES ajánlást —
        # a widget itt csak jelenti be a felhasználói szándékot.
        self._model.set_battery_profile(name)
        self._render_from_model()

    def _refresh_i10_source_label(self, profile_name: str) -> None:
        """BAT-001/SRC-003/CFG-001: a versioned battery profile YAML (nem a
        PROFILE_META fallback) az egyetlen hiteles forrás -- ugyanazt a
        fájlt olvassa, mint main_window.py::_build_runner(), hogy a kezelő
        indítás előtt lássa, PONTOSAN mi kerülne ténylegesen a Ia=4×I10/
        Ib=20×I10 számításba."""
        from Prog import app_paths
        profile_path = (
            app_paths.bundle_dir() / "Prog" / "config" / "battery_profiles"
            / f"{profile_name}.yaml"
        )
        i10: float | None = None
        if profile_path.exists():
            with profile_path.open(encoding="utf-8") as f:
                profile_yaml = yaml.safe_load(f) or {}
            i10 = profile_yaml.get("i10_discharge_current_A")
        if i10 is None:
            self._i10_source_label.setText("HIÁNYZIK a profilból -- IEC Rb méréshez kötelező")
        else:
            self._i10_source_label.setText(f"{i10:.2f} A (profil)")

    def _refresh_chemid_readonly_labels(self, profile_name: str) -> None:
        """ChemID C10/20 áram- és cutoff-kijelzés a kapacitás-spinboxból,
        illetve a profil YAML-ból, a közös derived_parameters formulákkal.
        R16-B7: a BQ-design/learning explicit C10-bázisa ettől külön adat;
        a forrásolt FG20721 GUI-profilban kitöltött C10 nem azonosítja
        automatikusan a spinbox értékét C10-ként."""
        from Prog import app_paths
        from Prog.gui.derived_parameters import (
            DEFAULT_TERMINATE_V_PER_CELL,
            PROFILE_META,
            compute_discharge_current_A,
            compute_terminate_voltage_pack_V,
        )
        profile_path = (
            app_paths.bundle_dir() / "Prog" / "config" / "battery_profiles"
            / f"{profile_name}.yaml"
        )
        fallback = PROFILE_META.get(profile_name, PROFILE_META["FIAMM_12V"])
        cell_count = fallback.get("cell_count", 6)
        # Kör 20 Batch E5 (SW20-2) MAX verifier lelete (R-2): korábban itt
        # egy 4. kézzel karbantartott 1.80 literál élt -- pont az a minta,
        # amit a fix meg akart szüntetni. Most a közös konstansból jön.
        terminate_v_per_cell = DEFAULT_TERMINATE_V_PER_CELL
        if profile_path.exists():
            with profile_path.open(encoding="utf-8") as f:
                profile_yaml = yaml.safe_load(f) or {}
            cell_count = int(profile_yaml.get("cell_count", cell_count))
            terminate_v_per_cell = profile_yaml.get(
                "terminate_voltage_per_cell_V", terminate_v_per_cell
            )
        c20_A = compute_discharge_current_A(self._capacity_spin.value(), 20)
        cutoff_V = compute_terminate_voltage_pack_V(cell_count, terminate_v_per_cell)
        self._chemid_c20_current_label.setText(f"{c20_A:.2f} A (profil, C10/20)")
        self._chemid_cutoff_label.setText(f"{cutoff_V:.2f} V (profil)")
        # R1 B8c, F-24: ugyanaz a profil-cutoff, mint a ChemID-soré.
        self._ocv_soc_profile_cutoff_label.setText(f"{cutoff_V:.2f} V (profil)")

    def _refresh_charge_safety_limit_label(self, profile_name: str) -> None:
        """D9/F-07 (2026-09-13): `BATT_ABSOLUTE_MAX_V_PER_CELL` fix
        konstans (`battery_profile.py`), a cell_count a profil YAML-ból
        -- ugyanaz a forrás, mint `_refresh_i10_source_label()`-nél."""
        from Prog import app_paths
        from Prog.gui.derived_parameters import PROFILE_META
        from Prog.src.battery_profile import BATT_ABSOLUTE_MAX_V_PER_CELL
        profile_path = (
            app_paths.bundle_dir() / "Prog" / "config" / "battery_profiles"
            / f"{profile_name}.yaml"
        )
        fallback = PROFILE_META.get(profile_name, PROFILE_META["FIAMM_12V"])
        cell_count = fallback.get("cell_count", 6)
        if profile_path.exists():
            with profile_path.open(encoding="utf-8") as f:
                profile_yaml = yaml.safe_load(f) or {}
            cell_count = int(profile_yaml.get("cell_count", cell_count))
        max_pack_V = BATT_ABSOLUTE_MAX_V_PER_CELL * cell_count
        self._charge_safety_limit_label.setText(
            f"{BATT_ABSOLUTE_MAX_V_PER_CELL:.3f} V/cella "
            f"({max_pack_V:.2f} V pack, fix)"
        )

    def _refresh_ocv_soc_partial_discharge_rate_label(self) -> None:
        """D9/F-04 (2026-09-13): ugyanaz a C/10-osztó és C10-kapacitás
        forrás, mint `get_session_config()`-ban (`discharge_rate_
        divisor=10`) -- a `derived_parameters.py` kanonikus
        `compute_discharge_current_A()`-val számolva, nem egy harmadik,
        duplikált osztással."""
        from Prog.gui.derived_parameters import compute_discharge_current_A
        current_A = compute_discharge_current_A(self._capacity_spin.value(), 10)
        self._ocv_soc_partial_discharge_rate_label.setText(
            f"{current_A:.2f} A (kapacitásalapból)"
        )

    def _apply_profile_yaml_overrides(self, profile_name: str) -> None:
        """Review-kör 4/5 GUI F6 (P2, BAT-001/SRC-003/CFG-001 sibling -- l.
        _refresh_i10_source_label() pontos mintája ugyanerről a
        hibaosztályról) + Kör 20 Batch E5 (SW20-2, P2, a GUI F6 minta 1:1
        kiterjesztése a két testvér-mezőre): a profil-YAML azon mezői,
        amiket a TÉNYLEGESEN alkalmazott érték forrása (BatteryProfile /
        charge_controller.py / discharge_controller.py) ELSŐBBSÉGGEL
        használ, de a PROFILE_META fallback dict NEM tartalmaz --
        max_charge_current_A (PROFILE_MAX policy korábban MINDIG a
        generikus 0,25×C10 képlettel számolt), terminate_voltage_per_cell_V
        (PROFILE_DEFAULT policy korábban HARDKÓDOLT 1.80-at adott) és
        taper_current_A (a modell korábban MINDIG a generikus 0,03×C10
        képlettel számolt). EGYETLEN YAML-olvasás mind a háromhoz --
        ugyanazt a fájlt olvassa, mint main_window.py::_build_runner(), és
        ugyanazokkal a fallback-szemantikákkal, hogy a Start-összegző
        PONTOSAN a ténylegesen alkalmazott értéket mutassa."""
        from Prog import app_paths
        profile_path = (
            app_paths.bundle_dir() / "Prog" / "config" / "battery_profiles"
            / f"{profile_name}.yaml"
        )
        profile_yaml: dict = {}
        if profile_path.exists():
            with profile_path.open(encoding="utf-8") as f:
                profile_yaml = yaml.safe_load(f) or {}
        with self._model.batch_update():
            self._model.set_max_charge_current_A_profile_override(
                profile_yaml.get("max_charge_current_A")
            )
            self._model.set_terminate_voltage_per_cell_default_V(
                profile_yaml.get("terminate_voltage_per_cell_V")
            )
            self._model.set_taper_current_A_profile_override(
                profile_yaml.get("taper_current_A")
            )

    def _on_psu_mode_changed(self, mode: str) -> None:
        if not hasattr(self, "_model"):
            return
        self._model.set_psu_mode(mode, user_override=True)
        self._render_from_model()

    def _on_battery_model_changed(self, value: str) -> None:
        if not hasattr(self, "_model"):
            return
        self._model.set_battery_model(value)
        self._render_from_model()

    def _on_capacity_changed(self, value: float) -> None:
        if not hasattr(self, "_model"):
            return
        self._model.set_capacity(value)
        self._render_from_model()
        # D9/F-02: a C/20 kisütőáram a C10-kapacitásból (ez a mező) jön --
        # kapacitásváltásnál is frissíteni kell, nem csak profilváltásnál.
        self._refresh_chemid_readonly_labels(self._profile_combo.currentText())
        # D9/F-04: ugyanez igaz az OCV_SOC lépcsős (C/10) kisütőáramára.
        self._refresh_ocv_soc_partial_discharge_rate_label()

    def _on_charge_current_spin_changed(self, value: float) -> None:
        if not hasattr(self, "_model"):
            return
        from Prog.gui.derived_parameters import ChargeCurrentPolicy
        # A spinbox "auto (számított)" speciális értéke (minimum, 0.0) →
        # PROFILE_MAX policy (a profil saját, adatlap-specifikus max_charge_
        # current_A-ja dönt, l. get_session_config() — SOSEM a generikus
        # 0.25×C10 képlet írja felül csendben). Bármi más → explicit CUSTOM.
        if value <= self._charge_current_spin.minimum() + 1e-9:
            self._model.set_charge_current_policy(ChargeCurrentPolicy.PROFILE_MAX)
        else:
            self._model.set_charge_current_policy(ChargeCurrentPolicy.CUSTOM, custom_A=value)
        self._render_from_model()

    def _on_discharge_current_spin_changed(self, value: float) -> None:
        if not hasattr(self, "_model"):
            return
        from Prog.gui.derived_parameters import DischargeRatePolicy
        if value <= self._discharge_current_spin.minimum() + 1e-9:
            self._model.set_discharge_rate_policy(DischargeRatePolicy.C5)
        else:
            self._model.set_discharge_rate_policy(DischargeRatePolicy.CUSTOM, custom_A=value)
        self._render_from_model()

    def _on_discharge_term_v_spin_changed(self, value: float) -> None:
        if not hasattr(self, "_model"):
            return
        from Prog.gui.derived_parameters import TerminateVoltagePolicy
        if value <= self._discharge_term_v_spin.minimum() + 1e-9:
            # Kör 20 Batch E5 (SW20-2): l. __init__ azonos komment-hivatkozott
            # indoklása -- az "auto (profil default)" felirat mögött a
            # profil-YAML-ból feltöltött alapértéknek kell állnia, nem egy
            # hardkódolt 1.80-nak.
            self._model.set_terminate_voltage_policy(TerminateVoltagePolicy.PROFILE_DEFAULT)
        else:
            self._model.set_terminate_voltage_policy(TerminateVoltagePolicy.CUSTOM, custom_V=value)
        self._render_from_model()

    def _on_wiring_confirmed_toggled(self, checked: bool) -> None:
        if not hasattr(self, "_model"):
            return
        self._model.wiring_confirmed = checked

    def _on_test_type_changed(self, new_type: str) -> None:
        if not hasattr(self, "_model"):
            return
        # SUP02-CONFIG-001: mód-specifikus draft megőrzés — a model saját
        # maga menti/visszaállítja a policy-ket módváltáskor.
        self._model.set_test_type(new_type)
        self._render_from_model()
        self._update_visibility()

    def _update_visibility(self) -> None:
        """WP-AK-03 (SUP02-ACCEPT-001/1/8): tesztmódváltás után csak a
        releváns csoportok látszanak — `ui_policy.is_group_visible()` a
        központi, egyetlen forrás (nincs szétszórt `if test_type == ...`
        elágazás a widgetekben, l. SUP02-ARCH-003). Csoport-granularitáson
        (egész QGroupBox, ill. QFormLayout-sor) dolgozik — a jelenlegi
        widget-szervezés ennél finomabb bontást nem indokol."""
        from Prog.gui.ui_policy import is_group_visible

        test_type = self._model.test_type

        self._temp_box.setVisible(is_group_visible(test_type, "temperature_compensation"))
        self._charge_box.setVisible(
            is_group_visible(test_type, "charge_current")
            or is_group_visible(test_type, "charge_target")
        )
        self._discharge_box.setVisible(
            is_group_visible(test_type, "discharge_rate")
            or is_group_visible(test_type, "terminate_voltage")
        )
        # D9/F-03 (2026-09-13, Riportok/ui_policy_audit_20260913.md): a box
        # feletti OR korábban a BENNE lévő "Áram"/"Végfeszültség" SOROKAT is
        # egységesen megjelenítette, akkor is, ha csak EGYIK csoport volt
        # policy-látható (pl. OCV_SOC_CHARACTERIZATION: discharge_rate
        # rejtett, terminate_voltage látható) -- a rejtett csoporthoz tartozó
        # mező szerkeszthető maradt, miközben az érték a controller által
        # NÉMÁN figyelmen kívül hagyva (OCV_SOC saját, hardkódolt C/10
        # divisor-t használ). A preset-gombsor (C/5-C/10-C/20) a "discharge_
        # rate" sorhoz tartozik (mindkét spinboxot állítja, de elsődlegesen
        # a rátát).
        self._discharge_preset_widget.setVisible(
            is_group_visible(test_type, "discharge_rate")
        )
        self._discharge_form.setRowVisible(
            self._discharge_current_spin, is_group_visible(test_type, "discharge_rate")
        )
        self._discharge_form.setRowVisible(
            self._discharge_current_label, is_group_visible(test_type, "discharge_rate")
        )
        self._discharge_form.setRowVisible(
            self._discharge_term_v_spin, is_group_visible(test_type, "terminate_voltage")
        )
        self._test_form.setRowVisible(self._taper_spin, is_group_visible(test_type, "taper"))
        self._test_form.setRowVisible(
            self._relax_after_charge_spin, is_group_visible(test_type, "relax_after_charge")
        )
        self._test_form.setRowVisible(
            self._ocv_soc_step_spin, is_group_visible(test_type, "uniform_step")
        )
        self._test_form.setRowVisible(
            self._ocv_soc_partial_discharge_rate_label,
            is_group_visible(test_type, "partial_discharge_rate"),
        )
        self._test_form.setRowVisible(
            self._charge_safety_limit_label,
            is_group_visible(test_type, "charge_safety_limits"),
        )
        self._test_form.setRowVisible(
            self._discharge_safety_limit_label,
            is_group_visible(test_type, "discharge_safety_limits"),
        )
        self._test_form.setRowVisible(
            self._bq_port_edit, is_group_visible(test_type, "bq_port")
        )
        self._test_form.setRowVisible(
            self._golden_gg_current_spin,
            is_group_visible(test_type, "golden_gg_system_rate_current"),
        )
        self._test_form.setRowVisible(
            self._i10_source_label, is_group_visible(test_type, "i10_source_display")
        )
        self._test_form.setRowVisible(
            self._chemid_c20_current_label,
            is_group_visible(test_type, "chemid_c20_current"),
        )
        self._test_form.setRowVisible(
            self._chemid_cutoff_label,
            is_group_visible(test_type, "chemid_manufacturer_cutoff"),
        )
        # R1 B8c, F-24: a tesztformában (NEM a kisütési dobozban), így a
        # _discharge_box fenti láthatósági OR-ja nem rejtheti el.
        self._test_form.setRowVisible(
            self._ocv_soc_profile_cutoff_label,
            is_group_visible(test_type, "ocv_soc_profile_cutoff"),
        )
        self._test_form.setRowVisible(
            self._chemid_conditioning_label,
            is_group_visible(test_type, "chemid_conditioning"),
        )
        self._test_form.setRowVisible(
            self._chemid_relax_label, is_group_visible(test_type, "chemid_relax_5h")
        )

    def _psu_hw_max_A(self) -> float:
        from Prog.gui.derived_parameters import PSU_HW_MAX_CURRENT_A
        return PSU_HW_MAX_CURRENT_A.get(self._psu_mode_combo.currentText(), 1.5)

    def _update_charge_current_info(self) -> None:
        """Megtartva a korábbi metódusnév alatt (meglévő tesztek hívják
        közvetlenül) — a tényleges számítás mostantól a modellből jön."""
        self._render_from_model()

    def _render_from_model(self) -> None:
        """WP-AK-03: egyirányú visszaírás — a model aktuális állapotát írja
        a widgetekbe, jelblokkolással (SUP02-REACT-003: nincs signal recursion —
        a programozott setValue()/setChecked() NEM válthat ki újabb
        _on_X_changed()-et, ami újra a modellt hívná)."""
        from Prog.gui.derived_parameters import (
            LOAD_HW_MAX_CURRENT_A,
            PSU_HW_MAX_CURRENT_A,
            PSU_HW_MAX_VOLTAGE_V,
        )

        m = self._model

        widgets = (
            self._charge_current_spin, self._discharge_current_spin,
            self._discharge_term_v_spin, self._wiring_confirmed_cb,
            self._psu_mode_combo,
        )
        for w in widgets:
            w.blockSignals(True)
        try:
            requested = m.requested_charge_current_A
            if m.charge_current_policy.value == "PROFILE_MAX":
                self._charge_current_spin.setValue(self._charge_current_spin.minimum())
            elif requested is not None:
                self._charge_current_spin.setValue(requested)

            # SUP02-ACCEPT-001/2/6: a spinbox a modellbol frissul.
            # R8-B3: a tartomanyon kivuli kerest a kulon cimke mutatja.
            discharge_val = m.discharge_current_A
            if discharge_val is not None:
                self._discharge_current_spin.setValue(discharge_val)

            term_v = m.terminate_voltage_pack_V
            if term_v is not None:
                self._discharge_term_v_spin.setValue(term_v)

            self._wiring_confirmed_cb.setChecked(m.wiring_confirmed)
            needs_wiring = m.psu_mode in ("PARALLEL", "SERIES")
            self._wiring_confirmed_cb.setEnabled(needs_wiring)

            if self._psu_mode_combo.currentText() != m.psu_mode:
                self._psu_mode_combo.setCurrentText(m.psu_mode)
        finally:
            for w in widgets:
                w.blockSignals(False)

        # Governance-v2 review-kör 1 (CF-15/DFAM-017 3. előfordulás, batch
        # 4.5 MAX verifier P2-1 lelete): ez a hardkódolt "info" dict a MÁR
        # egyesített PSU_HW_MAX_VOLTAGE_V/PSU_HW_MAX_CURRENT_A (l. fentebb
        # a modul-import) 6. testvér-másolata volt, UGYANEBBEN a fájlban/
        # osztályban — a verifier a batch 4.5 fő fixje UTÁN, önálló
        # sibling-keresés során találta. Ma az értékek egyeznek (látens
        # kockázat), de ha a kanonikus dict-ek valaha módosulnának, ez a
        # kijelző maradt volna az EGYETLEN hely, ami elavult limitet
        # mutatna az operátornak, miközben validate() már az új limitet
        # kényszerítené ki.
        _PSU_MODE_INFO_SUFFIX = {"INDEPENDENT": " per csatorna", "PARALLEL": "", "SERIES": ""}
        voltage = PSU_HW_MAX_VOLTAGE_V.get(m.psu_mode)
        current = PSU_HW_MAX_CURRENT_A.get(m.psu_mode)
        if voltage is not None and current is not None:
            suffix = _PSU_MODE_INFO_SUFFIX.get(m.psu_mode, "")
            self._psu_mode_info_label.setText(f"Max: {voltage:.0f}V / {current:.1f}A{suffix}")
        else:
            self._psu_mode_info_label.setText("")

        if m.is_discharge_current_incompatible:
            self._discharge_current_label.setText(
                f"kért: {m.discharge_current_A:.2f} A — CONFIG_INCOMPATIBLE "
                f"(Load limit: {LOAD_HW_MAX_CURRENT_A:.2f} A)"
            )
            self._discharge_current_label.setStyleSheet("color: #b71c1c; font-weight: bold;")
        else:
            self._discharge_current_label.setText("")
            self._discharge_current_label.setStyleSheet("")

        # SUP02-VALID-002/POLICY-003: nincs csendes clamp — a widget felső
        # határa NEM a hardver-limit, és a kezelő korábban megadott értékét
        # sem írjuk felül. Start előtt a SessionConfig.validate() és a
        # DynamicSessionModel ellenőriz; a profilmaximumot az utóbbi kapuzza.
        # Itt csak vizuálisan jelezzük az eltérést.
        self._charge_current_spin.setMaximum(999.0)
        if requested is not None and requested > m.charge_current_profile_limit_A:
            # R9-B1: a profilkorlat is lathato a kert ertek mellett.
            self._charge_current_label.setText(
                f"kért: {requested:.2f} A — CONFIG_INCOMPATIBLE "
                f"(profilmaximum: {m.charge_current_profile_limit_A} A)"
            )
            self._charge_current_label.setStyleSheet("color: #b71c1c; font-weight: bold;")
        elif requested is not None and m.is_charge_current_incompatible:
            self._charge_current_label.setText(
                f"számított: {requested:.2f} A — CONFIG_INCOMPATIBLE "
                f"(PSU {m.psu_mode} limit: {m.charge_current_hardware_limit_A:.2f} A)"
            )
            self._charge_current_label.setStyleSheet("color: #b71c1c; font-weight: bold;")
        elif requested is not None:
            self._charge_current_label.setText(f"számított: {requested:.2f} A")
            self._charge_current_label.setStyleSheet("")

    def _apply_discharge_preset(self, divisor: int, v_per_cell: float) -> None:
        from Prog.gui.derived_parameters import (
            DischargeRatePolicy,
            TerminateVoltagePolicy,
        )
        policy_by_divisor = {
            5: DischargeRatePolicy.C5,
            10: DischargeRatePolicy.C10,
            20: DischargeRatePolicy.C20,
        }
        voltage_policy = (
            TerminateVoltagePolicy.V_1_75_PER_CELL if v_per_cell == 1.75
            else TerminateVoltagePolicy.V_1_80_PER_CELL
        )
        if divisor not in policy_by_divisor:
            # Kör 19 D8b (SW19-06, DFAM-004 silent_catchall_dispatch): a
            # korábbi `.get(divisor, DischargeRatePolicy.CUSTOM)` egy
            # ismeretlen divisor-t csendben CUSTOM-ra váltott volna --
            # MA nem elérhető (mind a 3 hívóhely, l. lent a gombok
            # bekötését, literál 5/10/20-at ad át), de latens csapda egy
            # jövőbeli negyedik preset-gombhoz. A test_runner.py::
            # _controller_for_step() `raise ValueError(f"Unknown
            # controller state type: {type(state)!r}")` bevált mintáját
            # követve, explicit hibát dobunk, nem hallgatunk el egy
            # ismeretlen divisor-t.
            raise ValueError(f"Unknown discharge preset divisor: {divisor!r}")
        with self._model.batch_update():
            self._model.set_discharge_rate_policy(policy_by_divisor[divisor])
            self._model.set_terminate_voltage_policy(voltage_policy)
        self._render_from_model()

    def _load_yaml(self) -> None:
        from Prog import app_paths
        # Kör 21 F8 (ARC21-4(b)): a korábbi `cfg.update(loaded)` sekély
        # merge volt -- `local_config.yaml` `instruments`-blokkja EGÉSZBEN
        # felülírta a `default_config.yaml`-ét, akkor is, ha nem
        # tartalmazta MINDEN almezőt (pl. `combination_mode`). L.
        # `_deep_merge_dicts()` docstringje a teljes indoklásért.
        cfg: dict[str, Any] = {}
        for path in (app_paths.default_config_path(), app_paths.local_config_path()):
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                cfg = _deep_merge_dicts(cfg, loaded)

        instr = cfg.get("instruments", {})
        for key, edit in (
            ("psu", self._psu_res_edit),
            ("load", self._load_res_edit),
            ("dmm_voltage", self._dmm_v_res_edit),
            ("dmm_temperature", self._dmm_t_res_edit),
        ):
            resource = instr.get(key, {}).get("resource", "")
            if resource and resource not in (
                "PLACEHOLDER",
                "USB0::PLACEHOLDER::INSTR",
                "TCPIP0::192.168.X.X::inst0::INSTR",
            ):
                edit.setText(resource)

        psu_mode = instr.get("psu", {}).get("combination_mode", "INDEPENDENT")
        idx = self._psu_mode_combo.findText(psu_mode)
        if idx >= 0:
            self._psu_mode_combo.setCurrentIndex(idx)

        temp_mode = cfg.get("temperature_compensation_mode", "MONITOR_ONLY")
        idx = self._temp_comp_combo.findText(temp_mode)
        if idx >= 0:
            self._temp_comp_combo.setCurrentIndex(idx)

        temp_sensor_mode = cfg.get("temperature_sensor_mode", "RTD_4W_PT100")
        idx = self._temp_sensor_combo.findText(temp_sensor_mode)
        if idx >= 0:
            self._temp_sensor_combo.setCurrentIndex(idx)

    def _save_local_config(self) -> None:
        """Kör 21 F8 (ARC21-1, DFAM-017): korábban ez a metódus a 4
        resource-stringet tartalmazó dict-et `yaml.dump()`-olta a TELJES
        `local_config.yaml` fölé -- minden MÁS, a fájlban élő kulcs (pl.
        `temperature_sensor_mode`, `instruments.psu.combination_mode`)
        egy routin "Resource stringek mentése" kattintásra CSENDBEN
        MEGSEMMISÜLT. Empirikusan igazolt e repo saját `local_config.
        yaml`-jával: a `temperature_sensor_mode`-ot a `CONFIG_HASH_
        FIELDS` (session_recovery.py) is figyeli, tehát a mező elvesztése
        egy legitim resume-ot is `ResumeDecision.BLOCKED_CONFIG_MISMATCH`-tel
        utasítana el.
        Javítva: read-modify-write -- a MEGLÉVŐ fájlt (ha van) beolvassa,
        csak a 4 resource-stringet frissíti benne (`_deep_merge_dicts()`),
        és a TELJES, összefésült eredményt írja vissza -- minden más
        kulcs sértetlen marad."""
        from Prog import app_paths
        path = app_paths.local_config_path()
        existing: dict[str, Any] = {}
        if path.exists():
            with path.open(encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        data = _deep_merge_dicts(existing, {
            "instruments": {
                "psu":             {"resource": self._psu_res_edit.text()},
                "load":            {"resource": self._load_res_edit.text()},
                "dmm_voltage":     {"resource": self._dmm_v_res_edit.text()},
                "dmm_temperature": {"resource": self._dmm_t_res_edit.text()},
            }
        })
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True)

    def _open_device_search(self) -> None:
        from Prog.gui.panels.device_search_dialog import DeviceSearchDialog
        dlg = DeviceSearchDialog(self)
        dlg.exec()

    def set_session_active(self, active: bool) -> None:
        """Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet: az
        "Eszközök keresése…" gomb korábban futó session/worker alatt is
        elérhető maradt — megnyomáskor a DeviceSearchDialog egy KÜLÖN VISA
        ResourceManager-en keresztül *IDN?-t küld MINDEN elérhető
        resource-ra, beleértve azokat is, amiket éppen a futó session
        InstrumentManager-je kizárólagosan használ (CLAUDE.md §9: "egy
        fizikai DMM-et egy tickben egy kijelölt komponens olvas") — VISA
        erőforrás-versengést (resource busy/timeout/SCPI-válasz-deszinkron)
        okozhatott volna egy aktív mérés kritikus DMM-visszacsatolású
        CC/CV szabályozási hurokjában.

        Governance-v2 review-kör 1 (CF-30, GUI-R1-12): korábban EZ VOLT
        AZ EGYETLEN mező, amit ez a metódus letiltott — a Konfiguráció fül
        MINDEN MÁS mezője (profil, kapacitás, minta-ID, teszttípus, PSU-
        mód, töltő-/kisütőáram, relax-idő, DMM/PSU/Load resource stringek,
        stb.) TOVÁBBRA IS szabadon szerkeszthető maradt egy futó session
        alatt. A riport-snapshot maga helyes (a session INDULÁSAKOR
        rögzített konfigurációt használja, nem az élő widget-értékeket),
        DE a felület félrevezető: az operátor azt hihetné, hogy egy futó
        session paramétereit módosítja, miközben valójában csak a
        KÖVETKEZŐ (még el sem indult) session beállításait írja felül.
        A teljes panel letiltása (self.setEnabled) minden mezőre kiterjed
        -- beleértve a jövőben hozzáadandó mezőket is, tehát ez a fix a
        DFAM-001 (sibling_safety_gate_omission) ismétlődését is
        megelőzi ezen a felületen, nem csak a jelenlegi mezőket fedi le.
        A _search_btn külön explicit letiltása emiatt feleslegessé vált
        (a panel már letiltja), de megmarad a korábbi, önmagában is
        helyes indoklás dokumentálásaként."""
        self.setEnabled(not active)
        self._search_btn.setEnabled(not active)

    # ------------------------------------------------------------------ #
    # WP-AK-03 (SUP02-ACCEPT-001/11/12): revision-gate + effective summary #
    # ------------------------------------------------------------------ #

    def refresh_validation(self):
        """Frissíti a modell validációs állapotát a JELENLEGI revisionra —
        hívandó közvetlenül `is_start_allowed()` ellenőrzése előtt, mert egy
        stale (korábbi revisionhoz tartozó) validáció sosem engedi a Startot."""
        return self._model.validate_effective_config()

    def is_start_allowed(self) -> bool:
        """SUP02-REACT-004/TEST-011: csak friss és validált revisionnel
        engedélyezett a Start — a hívónak előbb `refresh_validation()`-t kell
        hívnia ugyanabban a körben, különben ez mindig stale-nek látja magát."""
        return self._model.is_start_allowed()

    def build_start_summary(self) -> dict:
        """SUP02-VALID-005/UI-001: Start előtti effective summary — minden,
        amit a kezelőnek látnia kell, mielőtt elindítja a mérést."""
        return self._model.build_summary()

    def get_session_config(self) -> SessionConfig:
        from Prog.gui.ui_policy import is_group_visible

        test_type = self._test_type_combo.currentText()
        # A hiányzó CUSTOM érték nem alakítható automatikus 0,0 szentinellé.
        if (
            is_group_visible(test_type, "discharge_rate")
            and self._model.discharge_rate_policy.value == "CUSTOM"
            and self._model.discharge_current_A is None
        ):
            raise ValueError("GOV-029: hiányzó CUSTOM kisütőáram")
        if is_group_visible(test_type, "terminate_voltage") and self._model.terminate_voltage_pack_V is None:
            raise ValueError("GOV-029: hiányzó CUSTOM végfeszültség")
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
            test_type=test_type,
            runner_tick_s=self._tick_spin.value(),
            taper_hold_s=self._taper_spin.value(),
            discharge_rate_divisor=10,  # OCV-SOC: C/10 (OcvSocConfig default); normál kisütésnél irreleváns
            ocv_soc_step_percent=self._ocv_soc_step_spin.value(),
            temperature_compensation_mode=self._temp_comp_combo.currentText(),
            temperature_sensor_mode=self._temp_sensor_combo.currentText(),
            relax_after_charge_s=self._relax_after_charge_spin.value(),
            # WP-AK-03/SUP02-MODEL-003: a modell adja a tényleges, felbontott
            # értéket — a "0.0 = auto" sentinel a WIDGET-en (minimum spin-érték)
            # UI-affordance marad, de a SessionConfig-ba már nem egy kétértelmű
            # 0.0 kerül, hanem a modell explicit policy-döntése alapján:
            #   - töltőáram: PROFILE_MAX policy esetén VÁLTOZATLANUL 0.0 megy
            #     tovább — ez szándékos, NEM regresszió: a ChargeController saját,
            #     profil-specifikus (pl. YAML `max_charge_current_A` felülbírálás)
            #     auto-logikáját kell futni hagyni, amit a modell generikus
            #     0.25×C10 formulája csendben felülírna, ha mindig konkrét
            #     számot küldenénk. Látható CUSTOM mezőnél a megadott
            #     tényleges értéket küldjük.
            #   - kisütőáram/végfeszültség: nincs hasonló profil-specifikus
            #     override a controller oldalán, ezért itt MINDIG a modell
            #     felbontott értékét küldjük (sosem a régi hardcodolt divisor-
            #     alapú main_window.py fallback-ot).
            # R10-B1: rejtett toltoaram nem kerul a SessionConfig-ba; a draft megmarad.
            charge_current_A_override=(
                0.0 if (
                    self._model.charge_current_policy.value == "PROFILE_MAX"
                    or not is_group_visible(test_type, "charge_current")
                )
                else (self._model.requested_charge_current_A or 0.0)
            ),
            discharge_current_A=self._model.discharge_current_A or 0.0,
            discharge_rate_origin=discharge_rate_origin_for_mode(
                test_type, self._model.discharge_rate_origin, self._capacity_spin.value(), 10,
            ),
            # R1 B8c, F-24 (P2, DFAM-026, D-B8-3 a): a cfg-cutoffot KIZÁRÓLAG
            # a DischargeController fogyasztja (main_window.py::_make_
            # discharge_ctrl()), és csak a "terminate_voltage" csoportot
            # látó módokban fut DISCHARGE lépés. Más módban a (pl. CUSTOM-
            # öröklésből maradt) hatástalan érték nem kerülhet a
            # SessionConfig-ba/config-hashbe -- 0.0 a meglévő auto/profil
            # szentinel (DischargeConfig.terminate_voltage_V_override).
            discharge_terminate_voltage_V=(
                (self._model.terminate_voltage_pack_V or 0.0)
                if is_group_visible(test_type, "terminate_voltage")
                else 0.0
            ),
            bq_port=self._bq_port_edit.text().strip(),
            golden_gg_system_rate_current_A=self._golden_gg_current_spin.value(),
        )
