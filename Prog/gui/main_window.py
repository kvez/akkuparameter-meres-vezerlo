"""
MainWindow — QTabWidget koordinátor + QThread életciklus + objektumgráf factory.
A ConfigPanel-tól SessionConfig-ot kap, abból építi a teljes TestRunner stack-et.
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from dataclasses import replace as dataclasses_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import serial  # type: ignore[import-untyped]

    from Prog.src.instrument_manager import InstrumentManager
    from Prog.src.ocv_soc_controller import OcvSocConfig
    from Prog.src.test_runner import TestResult

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from Prog import app_paths
from Prog.gui.bq_diagnostics_worker import BqDiagnosticsWorker
from Prog.gui.panels.bq_diagnostics_panel import BqDiagnosticsPanel
from Prog.gui.panels.checkpoint_panel import CheckpointPanel
from Prog.gui.panels.config_panel import ConfigPanel, SessionConfig
from Prog.gui.panels.device_error_panel import DeviceErrorPanel
from Prog.gui.panels.live_panel import LivePanel
from Prog.gui.worker import TestRunnerWorker

# Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: report_generator.py
# korábban feltétel nélkül "DMM2_surface_PT100"-ot jelentett — ez a mapping
# a ténylegesen aktív config_panel.py::temperature_sensor_mode-ot fordítja
# a riportba kerülő forrás-azonosítóra.
_TEMPERATURE_SOURCE_LABELS = {
    "RTD_4W_PT100": "DMM2_surface_PT100",
    "THERMISTOR_2W_NTC_10K_B3950": "DMM2_surface_NTC_10K_B3950",
}


# Kor 18, Batch C7 R17-15 (DFAM-003 + DFAM-017): a Start-megerosito
# dialogus mezolistaja kezzel tartott, es MAS lista, mint a resume-kapu
# CONFIG_HASH_FIELDS/PROFILE_HASH_FIELDS -- semmi nem kototte ossze a
# kettot, igy hashelt, OPERATOR ALTAL ALLITHATO mezok ugy mentek at a
# megerositesen, hogy a kezelo nem latta. A d10cee0 (R2-04-F7) TESZT-ONLY
# strukturalis prevencio HARMADIK alkalmazasa: nincs uj futasideju kodut,
# nincs uj konfiguracios szabadsag (CLAUDE.md 5./8./10. szakasz).
_START_DIALOG_DISPLAYED_CONFIG_FIELDS: tuple[str, ...] = (
    # A MEGLEVO summary-alapu sorok mar megjelenitik (l. lent a
    # "Kisütőáram" sort) -- ugyanaz a nev a hash-fieldben es a summary
    # dict-ben, tehat nem uj sor kell, csak a klasszifikacio. FUGGETLEN
    # VERIFIER P3-4 lelete (Kor 18 SB4): a "Kisütőáram" sor CSAK akkor
    # jelenik meg, ha `"discharge_current_A" in summary` -- ami
    # derived_parameters.py::effective_config()-ben `is_group_visible(
    # test_type, "discharge_rate")`-tol fugg. A tesztelt 9 test_type
    # kozul 6-ban (CHARGE_ONLY, CHEMID/OCV_SOC/TRADITIONAL_LEARNING/
    # GOLDEN_GG/IEC_DC_INTERNAL_RESISTANCE) ez a kulcs HIANYZIK a
    # summary-bol, MIKOZBEN a mezo hashelve van es a nem-nulla
    # DischargeRatePolicy.C5 alapertek miatt tenylegesen ervenyes
    # setpontot hordoz -- ezekben a modokban a mezo LATHATATLAN marad
    # a Start-dialogusban, amit ez a statikus whitelist-meta-teszt
    # szerkezetileg nem tud elkapni. Regisztralt, jovobeli tetel, NEM
    # ebben a korben javitva (a klasszifikacio maga helyes, csak a
    # feltetelesseg korlatozott a lefedettseget).
    "test_type", "battery_profile_name", "psu_mode", "discharge_current_A",
    # Az UJ, cfg-alapu csoportositott sorok (l. lent, cfg is not None ag):
    "battery_model", "nominal_capacity_ah", "sample_id",
    "temperature_compensation_mode", "temperature_sensor_mode",
    "discharge_rate_divisor", "golden_gg_system_rate_current_A",
    "ocv_soc_step_percent", "relax_after_charge_s",
)
_START_DIALOG_FIELDS_INTENTIONALLY_NOT_DISPLAYED: tuple[str, ...] = (
    # A DERIVALT, EFFEKTIV ertek lathato helyettuk (a kezelo szamara ez
    # az ellenorizheto adat): requested_charge_current_A ill.
    # terminate_voltage_pack_V. R1 B8c, F-24: a "terminate_voltage" csoportot
    # NEM lato (nem fogyaszto) modokban a cfg-cutoff mindig a 0.0
    # szentinel (config_panel.py::get_session_config()), tehat ott sincs
    # rejtett, kezelo altal allithato hashelt ertek.
    "charge_current_A_override", "discharge_terminate_voltage_V",
    "discharge_rate_origin",  # Belső snapshot; a ráta/kapacitásalap már látható.
)


# R16-B5b: a sikertelen zárást a sessionreferencia elengedése után is őrizzük.
_PENDING_DISCONNECTS: list[Any] = []


def _cleanup_action(
    label: str, action: Callable[[], Any],
    on_event: Callable[[dict], None] | None = None,
) -> Any:
    try:
        return action()
    except Exception as exc:  # A független cleanupok folytatódnak.
        event = {
            "event_code": "SESSION_CLEANUP_FAILED",
            "event_message": f"{label}: {exc}", "severity": "CRITICAL",
        }
        logging.getLogger(__name__).critical("%s", event["event_message"])
        if on_event is not None:
            try:
                on_event(event)
            except Exception:  # A Python-napló már őrzi a hibát.
                logging.getLogger(__name__).exception("Cleanup-esemény továbbítása sikertelen")
        return None


def _disconnect_for_cleanup(instruments, on_event=None) -> None:
    def disconnect() -> None:
        try:
            instruments.disconnect_all()
        except Exception:
            if not any(item is instruments for item in _PENDING_DISCONNECTS):
                _PENDING_DISCONNECTS.append(instruments)
            raise
        else:
            _PENDING_DISCONNECTS[:] = [
                item for item in _PENDING_DISCONNECTS if item is not instruments
            ]

    _cleanup_action("Műszerzárás; újrapróbálás szükséges", disconnect, on_event)


def _retry_pending_disconnects(on_event=None) -> bool:
    for instruments in tuple(_PENDING_DISCONNECTS):
        _disconnect_for_cleanup(instruments, on_event)
    return not _PENDING_DISCONNECTS


def _format_start_summary(summary: dict, cfg: SessionConfig | None = None) -> str:
    """WP-AK-03 (SUP02-ACCEPT-001/12): a DynamicSessionModel.build_summary()
    dict-jét emberi olvasható szöveggé alakítja a Start-megerősítő dialógushoz.

    Kor 18, R17-15: a `cfg` (SessionConfig) opcionalis -- ha megadott,
    a hash-kritikus, de a `summary`-ben nem szereplo mezok is
    megjelennek, csoportositva (l. _START_DIALOG_DISPLAYED_CONFIG_
    FIELDS). A `None` alapertek megtartja a MEGLEVO hivohelyek/tesztek
    viselkedeset cfg atadasa nelkul."""
    lines = [
        f"Teszttípus: {summary.get('test_type')}",
        f"Akkumulátor: {summary.get('battery_profile_name')} "
        f"({summary.get('cell_count')} cella)",
        f"PSU mód: {summary.get('psu_mode')} "
        f"(bekötés megerősítve: {'igen' if summary.get('wiring_confirmed') else 'nem'})",
    ]
    if "requested_charge_current_A" in summary:
        lines.append(f"Töltőáram: {summary.get('requested_charge_current_A')} A")
    if "discharge_current_A" in summary:
        lines.append(f"Kisütőáram: {summary.get('discharge_current_A')} A")
    if "terminate_voltage_pack_V" in summary:
        lines.append(f"Végfeszültség: {summary.get('terminate_voltage_pack_V')} V")
    if "pulse_current_A" in summary:
        lines.append(f"Pulse áram: {summary.get('pulse_current_A')} A")
    if "capacity_basis_Ah" in summary:
        lines.append(
            f"Kapacitásalap: {summary.get('capacity_basis_Ah')} Ah "
            f"({summary.get('capacity_basis_quality')})"
        )
    custom_overrides = summary.get("custom_overrides") or []
    if custom_overrides:
        lines.append(f"Egyedi (custom) értékek: {', '.join(custom_overrides)}")
    warnings = summary.get("warnings") or []
    if warnings:
        lines.append("Figyelmeztetések: " + "; ".join(warnings))
    # Kor 18, R17-15 -- l. _START_DIALOG_DISPLAYED_CONFIG_FIELDS kommentjet.
    # A resume-kapu EZEKRE a mezokre hashel; ha a kezelo itt nem latja
    # oket, a legkisebb koltsegu pillanatban nem tudja elkapni az
    # eltérest. CSOPORTOSITVA, nem 9 kulon sorban (dialogus-bloat: a
    # figyelem higulasa onmagaban is defekt).
    if cfg is not None:
        lines.append(
            f"Minta/akku: sample_id={cfg.sample_id!r}, "
            f"modell={cfg.battery_model!r}, "
            f"nominál={cfg.nominal_capacity_ah} Ah"
        )
        lines.append(
            f"Mérőlánc/rács: hőkompenzáció={cfg.temperature_compensation_mode}, "
            f"hőszenzor={cfg.temperature_sensor_mode}, "
            f"kisütés-osztó={cfg.discharge_rate_divisor}, "
            f"GG rate-áram={cfg.golden_gg_system_rate_current_A} A, "
            f"OCV/SOC lépés={cfg.ocv_soc_step_percent}%, "
            f"relax töltés után={cfg.relax_after_charge_s} s"
        )
    lines.append("\nIndítod a mérést?")
    return "\n".join(lines)


# Review 2026-09-15 (C-05/DFAM-017+DFAM-010, teljes kódbázis review): ez a
# halmaz KORÁBBAN két helyen élt szét-szinkronizálva --
# _construct_and_connect_instruments() (a DMM max_voltage_jump_V limithez,
# a hardveres bring-up idején) 5 típust sorolt fel, _build_runner()
# (a SafetyManager is_bq_mode-jához) 6-ot, IEC_DC_INTERNAL_RESISTANCE-szel --
# holott egy komment mindkét helyen "ugyanaz" -ként hivatkozott rá. Hatás:
# IEC_DC_INTERNAL_RESISTANCE + INDEPENDENT/PARALLEL PSU-módban a feszültség-
# DMM max_voltage_jump_V=2.0-t kapott a helyes 4.0 helyett -- egy legitim
# soros-esés (a PRECHARGE fázis ugyanazon TF1/soros-dióda útvonalon tölt,
# mint OCV_SOC/CHEMID) hamis DMM_FEEDBACK_LOST-ot válthatott volna ki.
# Mostantól EGYETLEN forrás -- mindkét hívóhely ebből olvas.
_IS_BQ_MODE_TEST_TYPES: frozenset[str] = frozenset({
    "BQ_LEARNING_PHYSICAL", "OCV_SOC_CHARACTERIZATION", "CHEMID_CHARACTERIZATION_PBA",
    "TRADITIONAL_LEARNING", "GOLDEN_GG",  # WP-AK-16
    # Governance-v2 kör 2: az IEC_DC_INTERNAL_RESISTANCE PRECHARGE fázisa
    # ugyanazon a fizikai (TF1/soros dióda) útvonalon tölt, mint OCV_SOC/
    # CHEMID -- l. _make_iec_dc_ctrl() shared_charge_cfg.
    "IEC_DC_INTERNAL_RESISTANCE",
})


# C14/SW-P3-3: a "TRADITIONAL_LEARNING"/"GOLDEN_GG" needs_bq_transport-halmaz
# korábban két helyen (_start_test(), _build_runner()) szó szerint,
# duplikáltan volt beírva -- egyetlen forrás, mint _IS_BQ_MODE_TEST_TYPES.
_NEEDS_BQ_TRANSPORT_TEST_TYPES: frozenset[str] = frozenset({
    "TRADITIONAL_LEARNING", "GOLDEN_GG",
})


@dataclass
class _PreConnectedInstruments:
    """Release-audit 2026-08-23 (2.6): a blokkoló hardveres bring-up
    (VISA connect_all + BQ soros negotiate + identify_all) eredménye —
    a _construct_and_connect_instruments() adja vissza, worker-threadről
    (_InstrumentConnectWorker) vagy szinkron hívásból (tesztek,
    _build_runner() saját fallback-ága) egyaránt."""
    instruments: Any
    psu: Any
    load: Any
    dmm_v: Any
    dmm_t: Any
    bq_transport: Any
    session_bq_serial: Any
    instrument_identities: dict


def _ocv_soc_config_for(cfg: SessionConfig) -> OcvSocConfig:
    """R1 B5 (F-19): az OCV/SOC controller configjanak EGYETLEN epitesi
    helye -- a bring-up preflight (_construct_and_connect_instruments()) es
    a _build_runner() ugyanebbol olvassa a pulse_sample_interval_s-t, igy a
    preflight pontosan azt az erteket ellenorzi, amivel az impulzus futna."""
    from Prog.src.ocv_soc_controller import OcvSocConfig

    config = OcvSocConfig(
        discharge_rate_divisor=cfg.discharge_rate_divisor,
        step_percent=cfg.ocv_soc_step_percent,
    )
    # Ugyanaz a HIL-kapu a GUI preflight és a közvetlen controller számára.
    from Prog.src.pulse_measurement_service import require_battery_pulse_mode
    require_battery_pulse_mode(config.pulse_trigger_mode)
    return config


def _preflight_ocv_soc_sample_timer(dmm_v: Any, requested_interval_s: float) -> None:
    """R1 B5 (F-19): OCV_SOC inditaskori SAMP:TIM? MIN preflight -- CSAK
    olvasas, a configure_dcv() UTAN (a MIN az aktualis konfiguraciora
    ervenyes, 34460-70-Manual.txt 9043-9055. sor), minden ON elott.

    A dontes azonos a futasideju PulseMeasurementService.
    _verify_sample_timer_capability() kapuval (fail-closed: lekerdezesi hiba,
    nem veges / <= 0 valasz, vagy MIN > kert koz -> elutasitas, ugyanazokkal
    az okkodokkal). Elutasitaskor ConfigurationError; a hivo meglevo except-
    aga (safe_all_off -> disconnect_all) zar. Nincs fallback, nincs uj
    konfiguracio: a mintaveteli koz modositasa a Q-6 felhasznaloi dontes."""
    from Prog.src.exceptions import ConfigurationError
    from Prog.src.ocv_soc_controller import STANDARD_PULSE_NPLC
    from Prog.src.pulse_measurement_service import (
        TIMING_INFEASIBLE_CAPABILITY_INVALID,
        TIMING_INFEASIBLE_CAPABILITY_QUERY_FAILED,
        TIMING_INFEASIBLE_SAMPLE_TIMER_MIN,
    )

    prefix = "OCV/SOC indítás ELUTASÍTVA az energizálás előtt (OCV_SOC_PREFLIGHT "
    not_energized = (
        " A session nem adott ki PSU OUTPUT ON / Load INPUT ON parancsot."
    )
    try:
        raw = dmm_v.query_sample_timer_capability()
    except Exception as exc:
        raise ConfigurationError(
            f"{prefix}{TIMING_INFEASIBLE_CAPABILITY_QUERY_FAILED}): a feszültség-DMM "
            f"SAMP:TIM? MIN lekérdezése sikertelen: {exc}.{not_energized}"
        ) from exc
    try:
        min_interval_s = float(str(raw).strip())
    except (TypeError, ValueError):
        min_interval_s = math.nan
    if not math.isfinite(min_interval_s) or min_interval_s <= 0.0:
        raise ConfigurationError(
            f"{prefix}{TIMING_INFEASIBLE_CAPABILITY_INVALID}): a feszültség-DMM "
            f"SAMP:TIM? MIN válasza értelmezhetetlen: {raw!r}.{not_energized}"
        )
    if min_interval_s > requested_interval_s:
        raise ConfigurationError(
            f"{prefix}{TIMING_INFEASIBLE_SAMPLE_TIMER_MIN}): a feszültség-DMM "
            f"SAMP:TIM? MIN = {min_interval_s:.6g} s az impulzusmérés beállításán "
            f"(NPLC={STANDARD_PULSE_NPLC:g}, AZ ON), "
            f"a konfigurált impulzus-mintavételi köz {requested_interval_s:.6g} s "
            f"— a műszer ezt nem tudja teljesíteni, a session az első impulzusnál "
            f"(az órás előtöltés és relaxáció után) FAULT-tal állna meg."
            f"{not_energized} A mintavételi köz módosítása külön felhasználói "
            f"döntés (Q-6, CLAIM_LEDGER INSTR-DMM-SAMPTIM-001)."
        )


def _construct_and_connect_instruments(
    cfg: SessionConfig, needs_bq_transport: bool
) -> _PreConnectedInstruments:
    """A teljes hardveres bring-up: driver-objektumok létrehozása, VISA
    connect_all, (BQ módban) soros PicoBQTransport negotiate, majd
    identify_all — ez a Start-folyamat EGYETLEN blokkoló I/O szakasza.

    Release-audit 2026-08-23 (2.6): korábban ez _build_runner() belsejében,
    szinkron, a GUI-szálon futott — a vészleállítás/ablakbezárás a kapcsolódás
    idejére elérhetetlenné vált (CLAUDE.md §15 megsértése). Mostantól
    _start_test() ezt a függvényt egy külön QThread-en (_InstrumentConnectWorker)
    hívja, és az eredményt adja át _build_runner(cfg, pre_connected=...)-nak;
    a meglévő tesztek (l. tests/gui/test_build_runner.py) és a szinkron hívási
    mód közvetlenül, a GUI-szálon hívják — a viselkedés ott változatlan."""
    from Prog.drivers.device_dmm import Keysight34465ADMM
    from Prog.drivers.device_load import Keithley2380Load
    from Prog.drivers.device_psu import Keithley2220PSU
    from Prog.src.instrument_manager import InstrumentConfig, InstrumentManager
    from Prog.src.safety import PsuMode

    psu_mode = PsuMode(cfg.psu_mode)
    # l. _build_runner() azonos, dokumentált számítása — determinisztikus
    # függvénye cfg-nek, itt (a hardveres bring-up-hoz, a dmm_v jump-limithez)
    # szükséges, mielőtt a _build_runner()-beli profile/safety elkészülne.
    # Review 2026-09-15 (C-05): mindkét hívóhely a modul-szintű
    # _IS_BQ_MODE_TEST_TYPES-ból olvas -- l. ott a teljes indoklást.
    is_bq_mode = cfg.test_type in _IS_BQ_MODE_TEST_TYPES
    psu = Keithley2220PSU()
    load = Keithley2380Load()
    dmm_v = Keysight34465ADMM(
        max_voltage_jump_V=4.0 if (psu_mode == PsuMode.SERIES or is_bq_mode) else 2.0
    )
    dmm_t = Keysight34465ADMM()
    instruments = InstrumentManager(psu, load, dmm_v, dmm_t)

    instr_cfg = InstrumentConfig(
        psu_resource=cfg.psu_resource,
        load_resource=cfg.load_resource,
        dmm_voltage_resource=cfg.dmm_voltage_resource,
        dmm_temperature_resource=cfg.dmm_temperature_resource,
    )
    instruments.connect_all(instr_cfg)

    # Release-audit 2026-08-23 (2.11): connect_all() saját maga korrekt
    # rollbackot végez részleges hiba esetén (l. InstrumentManager.connect_all()),
    # de az AZUTÁN következő lépések (BQ soros negotiate, identify_all(),
    # majd — teljes kódbázis review 3. kör, 2026-08-29, GUI/UX P0 lelet — a
    # DMM/PSU konfiguráció is) korábban semmilyen hiba esetén nem zárták le
    # a már megnyitott VISA/soros kapcsolatokat — a következő Start-kísérlet
    # "resource busy"-szerű hibába futhatott ugyanazokon az USBTMC-
    # erőforrásokon.
    bq_transport = None
    session_bq_serial = None
    # Kor 18, Batch C11, HW-P2-1 (DFAM-018+DFAM-016): a lenti except-ag
    # safe_all_off()-ja SZEREP-referenciakat (self._psu/self._load) hiv --
    # ha az azonositas MEG NEM igazolta a szerep->fizikai eszkoz
    # leképezest, a per-eszkoz eredmeny SEMMIT nem mond az adott fizikai
    # muszer valodi kimeneti allapotarol (pl. felcserelt VISA resource-
    # stringek eseten a "PSU".safe_off() egy fizikailag Load-ra kuld
    # PSU-specifikus parancsot).
    identity_confirmed = False
    try:
        if needs_bq_transport:
            from Prog.drivers.pico_bq_transport import (
                LEARNING_SESSION_WRITE_METHODS,
                PicoBQTransport,
            )
            from Prog.src.exceptions import InstrumentError

            # Review 2026-09-15 (C-02/DFAM-040+DFAM-001, teljes kódbázis
            # review): korábban `PicoBQTransport(serial=bq_serial)` (a
            # dataclass mezőt közvetlenül átadva) MEGKERÜLTE a transport
            # SAJÁT `connect()`-jét, ezért `self._port` SOHA nem állt be —
            # a HIL-D5 reconnect-mechanizmus (`_reopen_serial()`,
            # CLAIM_LEDGER.yaml PICO-RECONNECT-001) emiatt a leghosszabb
            # (session-owned TRADITIONAL_LEARNING/GOLDEN_GG) produkciós
            # úton NÉMA NO-OP volt: egy soros I/O-hiba a fizikai ciklus
            # közben SOHA nem állt volna helyre, mert `_reopen_serial()`
            # `self._port is None`-nál azonnal visszatér. `PicoBQTransport()`
            # + explicit `connect()` a transport SAJÁT `serial.Serial()`-jét
            # hozza létre, és `self._port`-ot helyesen beállítja.
            #
            # HIL-STÁTUSZ (MAX verifier F2 lelete, 2026-09-15): a DEFEKT
            # (a beragadt pyserial-handle sosem áll helyre) valós HIL-en
            # igazolt volt (PICO-RECONNECT-001), DE maga a `_reopen_serial()`
            # JAVÍTÁSA a CLAIM_LEDGER.yaml szerint TOVÁBBRA IS HIL_PENDING —
            # a tényleges PermissionError/reopen-út még nem futott le fizikai
            # hardveren AZ ÓTA, hogy ez a session-oldali fix a mechanizmust
            # itt először aktiválja. Ez a diff tehát egy HIL_PENDING
            # helyreállítási utat kapcsol be a leghosszabb (15-30 órás),
            # felügyelet nélküli produkciós útra -- célzott HIL-újramérés
            # (valós USB-kábel-kihúzás/power-cycle egy éles TRADITIONAL_
            # LEARNING/GOLDEN_GG session közben) még ezután esedékes.
            #
            # ELFOGADOTT KOCKÁZAT (MAX verifier F3 lelete, 2026-09-15,
            # CLAIM_LEDGER.yaml PICO-RECONNECT-001 V7 lelete óta
            # dokumentálatlan volt): a `_reopen_serial()` ezen az úton
            # ELSŐ ÍZBEN valóban sikeres újranyitást is eredményezhet,
            # ami `PicoBQTransport._call()` saját retry-logikáját (itt a
            # session-transport a DEFAULT max_retries=3-mal épül, a
            # legszélesebb a 3 hívóhely közül: kalibráció max_retries=2,
            # diagnosztika max_retries=1) egy ÁLLAPOTVÁLTOZTATÓ BQ RPC
            # (unseal/it_enable/seal/Cell BL Data Flash write, l.
            # traditional_learning_controller.py) VAK ÚJRAKÜLDÉSÉHEZ
            # vezetheti, ha a soros I/O pont egy ilyen parancs UTÁN,
            # de a válasz beérkezése ELŐTT szakadt meg (CLAUDE.md §10.5:
            # bizonytalan előző eredményű parancs vak ismétlése tilos).
            # Ez a kockázat TUDATOSAN ELFOGADOTT ebben a körben, NEM
            # csökkentve: az `unseal()`/`it_enable()`/`seal()` hívások
            # a BQ34Z110 firmware szintjén idempotensek (ismételt hívás
            # ugyanarra az állapotra nem változtat semmin), a Cell BL
            # Data Flash write pedig readback-verified (l. traditional_
            # learning_controller.py cell_bl restore-tranzakció) -- egy
            # esetleges duplikált írás a readback-ellenőrzésen elakadna,
            # nem maradna néma. Ha ez a feltevés valaha megdől (pl. egy
            # jövőbeli, NEM idempotens BQ-parancs bekerül ugyanerre az
            # útra), a session-transport max_retries-ét explicit,
            # indokolt értékre kell szorítani a default öröklése helyett.
            bq_transport = PicoBQTransport()
            try:
                bq_transport.connect(cfg.bq_port, baudrate=115200, timeout=1.0)
                # Kör 21 F1/ARC21-3(b): ez az út KIZÁRÓLAG TRADITIONAL_LEARNING/
                # GOLDEN_GG-nél fut (l. _NEEDS_BQ_TRANSPORT_TEST_TYPES). A tényleges
                # write-hívások eltérnek: GOLDEN_GG csak unseal()/seal()-t hív;
                # TRADITIONAL_LEARNING unseal()/seal()/it_enable()-t hívja
                # közvetlenül, plusz write_data_flash_block()-ot a
                # Bq34z110DataFlashService/bq_controlled_write.py rétegen
                # keresztül. full_access() jelenleg NEM a learning session
                # hívja, hanem a különálló kalibrációs út (bq_gauge_
                # calibration.py/production_programming_controller.py, l.
                # CALIBRATION_WRITE_METHODS). restore_df_snapshot()-nak
                # jelenleg EGYÁLTALÁN NINCS PC-oldali hívója (a production_
                # programming_controller.py:30-32 docstringje explicit
                # szándékosan KERÜLI ezt az alacsonyabb szintű, opakabb
                # primitívet, a blokkonkénti bq_controlled_write.py-t
                # használja helyette) -- a gate ezt kizárólag forward-compat
                # okból tartalmazza. A gate SZÁNDÉKOSAN a teljes WP-AK-08
                # write-metódus-készletet kéri (LEARNING_SESSION_WRITE_
                # METHODS), nem csak az aktuálisan hívott részhalmazt: ez a
                # session induló kapuja, nem az egyes controllerek külön
                # kapuja -- ha egy jövőbeli learning-session módosítás
                # bevezeti full_access()/restore_df_snapshot() hívását is
                # (pl. egy közös helyreállítási út), a gate már ma lefedi,
                # nem kell külön firmware-kompatibilitási hibát diagnosztizálni
                # utólag. Ha a csatlakoztatott firmware ezeket nem exponálja,
                # itt bukjon el, MIELŐTT a session elindulna, ne az első
                # valódi write-nál, energizált/bekötött akku mellett.
                bq_transport.negotiate(required_write_methods=LEARNING_SESSION_WRITE_METHODS)
            except InstrumentError:
                # Teljes kódbázis review 13. kör (2026-08-31), Hardver/
                # Műszervezérlés P1 lelet: negotiate() a testvér
                # PicoBQTransportError/InstrumentTimeoutError kivételek
                # BÁRMELYIKÉT dobhatja (soros I/O timeout esetén az
                # utóbbit) — a korábbi szűk `except PicoBQTransportError`
                # az InstrumentTimeoutError-t nem kapta el itt, emiatt a
                # lenti zárás kimaradt (a soros port fájlleíró szivárgott),
                # és session_bq_serial sem lett beállítva, mire a külső
                # except Exception-ig felfutott a kivétel.
                #
                # C-02 KIEGÉSZÍTÉS: `bq_transport.disconnect()` (nem a nyers
                # serial.close()) -- ha `connect()` sikerült, de `negotiate()`
                # hibázott, ez `self._port`-ot IS nullázza, nem csak a
                # serial-t zárja (a transport objektum itt úgyis eldobásra
                # kerül, de a konzisztens takarítási mintát követi).
                bq_transport.disconnect()
                raise
            session_bq_serial = bq_transport.serial

        # Governance-v2 review-kör 1 (CF-38/HW-R1-06): a modellcsalád itt
        # kötött ahhoz, hogy a fentiekben melyik driverosztályt hoztuk létre
        # az adott szerepre (Keithley2220PSU/Keithley2380Load/
        # Keysight34465ADMM) — nem konfigurálható egyedi sorozatszám,
        # hanem a hívó (ez a függvény) tudja, milyen eszközt várt.
        # R3-B2 (R3-HW-01, DFAM-003): a Load-minta a pontos modell -- a
        # safety.py 250 W-ja és a device_load.py 6/60 A tartománya a
        # 2380-120-60-hoz tartozik (2380-120-60-900-01 Rev. A §4); a
        # 2380-500-15 (200 W, 3/15 A) itt bukjon el, minden ON előtt.
        instrument_identities = instruments.identify_all(
            expected_models={
                "PSU": "2220", "Load": "2380-120-60", "DMM_V": "34465A", "DMM_T": "34465A",
            }
        )
        identity_confirmed = True

        # Teljes kódbázis review 3. kör (2026-08-29), GUI/UX P0 lelet: a DMM/
        # PSU konfiguráció (mérési mód beállítás + PSU kombinációs mód
        # visszaigazolás) korábban _build_runner()-ben, a GUI-szálon,
        # SZINKRON futott — közvetlenül azután, hogy _on_connect_ready()
        # letiltotta az Emergency Stop gombot (l. ott). Ezek is blokkoló SCPI
        # write/query hívások (kb. 10-13 round-trip), ugyanúgy ide tartoznak,
        # mint a fenti connect_all()/negotiate()/identify_all() — a teljes
        # blokkoló bring-up EGYETLEN helyen, ezen a (worker-thread vagy
        # szinkron-fallback) függvényen belül fusson.
        from Prog.src.exceptions import ConfigurationError
        from Prog.src.ocv_soc_controller import STANDARD_PULSE_NPLC

        is_ocv_soc = cfg.test_type == "OCV_SOC_CHARACTERIZATION"
        dmm_v.configure_dcv(
            range_V=100, nplc=STANDARD_PULSE_NPLC if is_ocv_soc else 10,
        )
        # R16-B15aF1: a MIN az impulzus 1 NPLC/AZ ON állapotára érvényes.
        # A csak olvasó preflight után a lassú DCV-alapot visszaállítjuk.
        if is_ocv_soc:
            try:
                dmm_v.enable_autozero()
                actual_nplc = dmm_v.get_nplc()
                if not math.isclose(actual_nplc, STANDARD_PULSE_NPLC, rel_tol=1e-9):
                    raise ConfigurationError(
                        f"OCV_SOC_PREFLIGHT NPLC readback: {actual_nplc!r}"
                    )
                errors = dmm_v.check_error()
                if errors:
                    raise ConfigurationError(f"OCV_SOC_PREFLIGHT: {'; '.join(errors)}")
                _preflight_ocv_soc_sample_timer(
                    dmm_v, _ocv_soc_config_for(cfg).pulse_sample_interval_s
                )
            finally:
                dmm_v.configure_dcv(range_V=100, nplc=10)
        if cfg.temperature_sensor_mode == "RTD_4W_PT100":
            dmm_t.configure_temp_4wire_pt100(nplc=10)
        elif cfg.temperature_sensor_mode == "THERMISTOR_2W_NTC_10K_B3950":
            dmm_t.configure_temp_2wire_ntc_thermistor(
                r25_ohm=10000.0, beta_K=3950.0, nplc=10
            )
        else:
            raise ValueError(
                f"Ismeretlen temperature_sensor_mode: {cfg.temperature_sensor_mode!r}"
            )

        if psu_mode == PsuMode.INDEPENDENT:
            psu.set_mode_independent()
            expected_combination_mode = "OFF"
        elif psu_mode == PsuMode.PARALLEL:
            psu.set_mode_parallel()
            expected_combination_mode = "PAR"
        elif psu_mode == PsuMode.SERIES:
            psu.set_mode_series()
            expected_combination_mode = "SER"
        else:
            expected_combination_mode = None
        # Teljes kódbázis review 1. kör (2026-08-26), P1 lelet (HW-003): a
        # set_mode_*() hívás UTÁN korábban sosem történt visszaigazolás — l.
        # eredeti helyén (itt: _build_runner()) a részletes indoklás.
        if expected_combination_mode is not None:
            actual_combination_mode = psu.query_combination_mode()
            if actual_combination_mode != expected_combination_mode:
                raise ConfigurationError(
                    f"PSU kombinációs mód visszaigazolás sikertelen: kért "
                    f"{expected_combination_mode!r}, a PSU ténylegesen "
                    f"{actual_combination_mode!r}-t jelentett vissza — a "
                    f"session nem indítható, amíg ez nincs tisztázva "
                    f"(HW-003)."
                )
    except Exception as exc:
        # Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-04 lelet:
        # a disconnect_all() csak a VISA/soros erőforrásokat zárja, NEM
        # ad ki OUTPUT/INPUT OFF parancsot — a fizikai PSU/Load kimenet
        # egy korábbi, nem tisztán lezárt session állapotából itt még
        # aktív lehet. CLAUDE.md §6 explicit "connect/config-hiba" esetére
        # is Load OFF + PSU ALL OFF-ot ír elő, nem csak lezárt kapcsolatot.
        #
        # Kor 18, Batch C11, HW-P2-1: a safe_all_off() eredmenye eddig
        # ELDOBODOTT -- az operator a bring-up hibadialogusban SEMMIT nem
        # tudott meg a PSU/Load kimeneti allapotarol. A kivetelen (mint az
        # EGYETLEN, ezen az uton letezo eredmeny-hordozon) a TestResult-
        # tal AZONOS szokinccsel adjuk tovabb; a szoveget a megjelenitesi
        # hatar (_InstrumentConnectWorker.run()) allitja ossze, hogy a
        # kivetel TIPUSA es UZENETE valtozatlan maradjon minden meglevo
        # hivora es tesztre nezve.
        safe_off_result = _cleanup_action("Bring-up safe-off", instruments.safe_all_off)
        _disconnect_for_cleanup(instruments)
        if session_bq_serial is not None:
            _cleanup_action("BQ-portzárás", session_bq_serial.close)
        # Direkt attributum-hozzarendeles (nem setattr()): Exception-nek
        # van __dict__-je, tehat ez mukodik, es kerulI a ruff B010
        # ("setattr constant attribute value") lelet.
        exc.safe_off_attempted = True  # type: ignore[attr-defined]
        exc.safe_off_result = safe_off_result or {}  # type: ignore[attr-defined]
        exc.safe_off_identity_unconfirmed = not identity_confirmed  # type: ignore[attr-defined]
        raise

    return _PreConnectedInstruments(
        instruments=instruments,
        psu=psu, load=load, dmm_v=dmm_v, dmm_t=dmm_t,
        bq_transport=bq_transport,
        session_bq_serial=session_bq_serial,
        instrument_identities=instrument_identities,
    )


def _safe_off_not_confirmed_text(safe_off_result: object) -> str:
    """R1 B6, F-06 (DFAM-016/018): a bring-up hibautak (worker-ag (3),
    _on_connect_ready() abort- es _build_runner()-hiba aga) KOZOS "NEM
    igazolt" szovege egy safe_all_off() eredmenybol. Fail-closed: a nem
    dict eredmeny (pl. None) mindket eszkozre bizonytalannak szamit. Ures
    string, ha a PSU es a Load OFF parancsa is igazoltan sikeres volt."""
    result = safe_off_result if isinstance(safe_off_result, dict) else {}
    uncertain_devices = [n for n in ("PSU", "Load") if not result.get(n, False)]
    if not uncertain_devices:
        return ""
    return (
        "\n\nFIGYELEM — a biztonságos lekapcsolás NEM igazolt: "
        + ", ".join(uncertain_devices)
        + ". A kimenet ENERGIZÁLT maradhatott; ellenőrizd "
        "fizikailag a műszereket. (Parancs-szintű eredmény, "
        "nem hardveres readback.)"
    )


class _InstrumentConnectWorker(QObject):
    """Release-audit 2026-08-23 (2.6): _construct_and_connect_instruments()-t
    futtatja egy külön QThread-en, hogy a GUI-szál (és vele a vészleállítás/
    ablakbezárás) a hardveres kapcsolódás alatt is válaszképes maradjon —
    a device_search_dialog.py::_SearchWorker mintáját követve."""
    connect_ready: Signal = Signal(object)   # _PreConnectedInstruments
    connect_failed: Signal = Signal(str)

    def __init__(self, cfg: SessionConfig, needs_bq_transport: bool) -> None:
        super().__init__()
        self._cfg = cfg
        self._needs_bq_transport = needs_bq_transport

    @property
    def needs_bq_transport(self) -> bool:
        """Teljes kódbázis review 11. kör (2026-08-31), GUI/UX/Állapot P2
        lelet: a MainWindow-nak a bring-up ALATT (nem csak utána) is
        tudnia kell, hogy ez a folyamatban lévő connect-worker igényli-e
        a Pico BQ soros portját — l. _on_bq_connect_requested()/
        _on_bq_calibration_requested() kölcsönös kizárás kiegészítése."""
        return self._needs_bq_transport

    @Slot()
    def run(self) -> None:
        try:
            result = _construct_and_connect_instruments(self._cfg, self._needs_bq_transport)
        except Exception as exc:
            # Kor 18, Batch C11, HW-P2-1 (DFAM-018): ugyanaz a megjelenitesi
            # minta, mint _on_finished()-ben -- strukturalt flag az
            # eredmeny-hordozorol (l. _construct_and_connect_instruments()
            # attributum-hozzarendelesei), szoveg ITT osszeallitva. HAROM
            # SZANDEKOSAN KULON ag (DFAM-018 semantic-collapse-avoidance):
            # (1) az azonositas hianya mast jelent (a per-eszkoz eredmeny
            # ERTELMEZHETETLEN), mint (3) egy igazolt lekepezes melletti
            # sikertelen OFF (BIZONYTALAN). (2) a KOZBENSO ag -- fuggetlen
            # MAX verifier F1 lelete -- a connect_all() SAJAT (instrument_
            # manager.py-beli) hibaja, ami a `try:`-en KIVUL fut, tehat a
            # lenti attributumok EGYIKE sem kerul ra a kivetelre: enelkul
            # a fenti (1) es a lenti (3) egyike sem tuzelt volna, CSENDBEN
            # elhagyva a leggyakoribb bring-up hibat (musszer ki van
            # kapcsolva/rossz VISA cim/USB-kihuzas) -- fail-open pontosan
            # ott, ahol a legtobbszor bekovetkezik. Fail-closed: ha meg az
            # sem bizonyithato, hogy egyaltalan megkiserlodott a safe-off,
            # a legrosszabb feltetelezest jelezzuk (l. checkpoint_panel.py
            # /_on_finished() azonos "SAFE-OFF BIZONYÍTÉK HIÁNYZIK" mintaja).
            message = str(exc)
            if getattr(exc, "safe_off_identity_unconfirmed", False):
                message += (
                    "\n\nFIGYELEM — SAFE-OFF ÉRTELMEZHETETLEN (AZONOSÍTÁS "
                    "NÉLKÜL): a műszer-azonosítás nem igazolta, melyik fizikai "
                    "eszköz melyik szerepen van, ezért a most kiadott Load "
                    "INPUT OFF / PSU ALL OUTPUTS OFF parancsok nem biztos, "
                    "hogy a szánt eszközre mentek. A kimenet ENERGIZÁLT "
                    "maradhatott; ellenőrizd fizikailag a műszereket és a VISA "
                    "resource-stringeket, MIELŐTT újraindítanád."
                )
            elif not getattr(exc, "safe_off_attempted", False):
                message += (
                    "\n\nFIGYELEM — SAFE-OFF BIZONYÍTÉK HIÁNYZIK: a kapcsolódási "
                    "hiba korán következett be (pl. egy műszer nem válaszol), "
                    "ezért nincs nyoma annak, hogy a biztonságos lekapcsolás "
                    "egyáltalán megkísérlődött. A kimenet ENERGIZÁLT maradhatott; "
                    "ellenőrizd fizikailag a műszereket, MIELŐTT újraindítanád."
                )
            else:
                message += _safe_off_not_confirmed_text(
                    getattr(exc, "safe_off_result", None) or {}
                )
            self.connect_failed.emit(message)
            return
        self.connect_ready.emit(result)


@dataclass
class _BqConnectResult:
    """Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P0 lelet: a BQ
    Diagnosztika "Kapcsolódás" hardveres bring-up-jának (soros port nyitás +
    negotiate) eredménye — _BqConnectWorker adja vissza, hogy a GUI-szál
    ne végezze szinkron a blokkoló I/O-t."""
    bq_serial: Any
    driver: Any
    port: str


class _BqConnectWorker(QObject):
    """Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P0 lelet
    (F1): a BQ Diagnosztika "Kapcsolódás" gombja korábban a GUI-szálon,
    szinkron végezte a soros port megnyitását és a PicoBQTransport.negotiate()
    hívást (~1s worst-case) — ez alatt egy futó fizikai teszt Emergency Stop
    gombja késleltetve dolgozódott volna fel (CLAUDE.md §15). Ugyanaz a minta,
    mint _InstrumentConnectWorker a fő Start-folyamathoz."""
    connect_ready: Signal = Signal(object)   # _BqConnectResult
    connect_failed: Signal = Signal(str)

    def __init__(self, port: str) -> None:
        super().__init__()
        self._port = port

    @Slot()
    def run(self) -> None:
        from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
        from Prog.drivers.pico_bq_transport import PicoBQTransport
        from Prog.src.exceptions import InstrumentError

        # Teljes kódbázis review 2. kör (2026-08-28), Hardver/Műszervezérlés
        # P2 lelet: a soros port nyitása korábban itt, közvetlenül
        # pyserial-lel történt — most a PicoBQTransport.connect()-en
        # keresztül, CLAUDE.md §9 driver-interfész szerint.
        transport = PicoBQTransport(max_retries=1)
        try:
            # [WP-AK-07 Critical fix, független review C-2] rövidebb soros
            # timeout (2.0 -> 1.0s) — l. eredeti _on_bq_connect_requested()
            # kommentje, ugyanaz az indoklás.
            transport.connect(self._port, timeout=1.0)
        except ImportError as exc:
            self.connect_failed.emit(f"A pyserial csomag nincs telepítve: {exc}")
            return
        except Exception as exc:
            self.connect_failed.emit(str(exc))
            return

        try:
            transport.negotiate()
        except InstrumentError as exc:
            # Teljes kódbázis review 13. kör (2026-08-31), Hardver/
            # Műszervezérlés P1 lelet: negotiate() a testvér
            # PicoBQTransportError/InstrumentTimeoutError kivételek
            # BÁRMELYIKÉT dobhatja — a korábbi szűk `except
            # PicoBQTransportError` az InstrumentTimeoutError-t (pl. a
            # Pico nem válaszol timeout-on belül) NEM kapta el, ami ebben
            # a @Slot()-ban elkapatlan maradt volna: sem connect_ready,
            # sem connect_failed nem tüzelt volna, a hívó dialógus
            # véglegesen "Csatlakozás…" állapotban ragadt volna (CLAUDE.md
            # §15).
            transport.disconnect()
            self.connect_failed.emit(str(exc))
            return
        bq_serial = transport.serial

        driver = Bq34z110ReadOnlyDriver(transport=transport)
        self.connect_ready.emit(_BqConnectResult(bq_serial=bq_serial, driver=driver, port=self._port))


class MainWindow(QMainWindow):
    # Feature (2026-08-23, felhasználói kérés): a TRADITIONAL_LEARNING/
    # GOLDEN_GG session saját, periodikus BQ gauge-lekérdezése (controller
    # on_gauge_sample callback) a worker-szálról érkezik — ezen a Qt-signalon
    # keresztül jut biztonságosan a GUI-szálra (l. _make_traditional_learning_
    # ctrl()/_make_golden_gg_ctrl(), ahol ctrl.on_gauge_sample = ennek az
    # emit()-je, és __init__, ahol a BqDiagnosticsPanel.update_snapshot()-hoz
    # kötve van).
    gauge_snapshot_ready = Signal(object)
    # Block 6 (2026-08-25): a Block 2 PROFILE<->SESSION<->BQ Data Flash
    # konzisztencia-gate eredménye (TraditionalLearningController.
    # on_provisioning_consistency, PRECOND-nál egyszer) — ugyanaz a
    # thread-biztos Qt-signal minta, mint gauge_snapshot_ready.
    provisioning_consistency_ready = Signal(object)
    # Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P0 lelet: a
    # checkpointból folytatás korábban KÖZVETLEN Python-hívással
    # (self._worker.request_continue_from_checkpoint()) indult, NEM
    # signalon keresztül — a @Slot() dekorátor önmagában nem ad cross-
    # thread queued dispatch-ot, csak egy .connect()-tel párosítva. Emiatt
    # a folytatott (akár több órás) fizikai ciklus a GUI-szálon futott
    # szinkron módon, lefagyasztva az ablakot, és a Stop/Emergency Stop
    # gomb a teljes folytatás alatt nem dolgozódott fel (CLAUDE.md §15).
    _continue_from_checkpoint_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Akkuteszter — Labor műszerfal")
        self.resize(1280, 900)

        self._thread: QThread | None = None
        self._worker: TestRunnerWorker | None = None
        # Release-audit 2026-08-23 (2.18): a korábbi "FIX-02: closeEvent
        # safe_all_off" kommentár félrevezető volt — a safe-off ténylegesen
        # a TestRunner belső safe_all_off() hívásain keresztül történik
        # (test_runner.py), NEM közvetlenül closeEvent()-ben; a műszer-
        # kapcsolatok lezárása (disconnect_all()) pedig _on_finished()/
        # _on_connect_ready() hibaágakon történik (l. 2.10), nem itt.
        self._instruments: InstrumentManager | None = None
        self._session_dir: Path | None = None  # a session mappája (report.json helye)
        # CF-34 (governance-v2 review-kör 1): a session test_type-ja,
        # _build_runner()-ben frissítve -- l. _on_finished() recharge-
        # figyelmeztetése, ami ezt _worker megszűnése UTÁN is olvassa.
        self._active_test_type: str = ""
        # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P1 lelet (F3):
        # l. _on_finished()/_on_checkpoint_close() docstringje.
        # Governance-v2 review-kör (MAX verifier V4-2, P3): a típus korábban
        # `Any | None` volt -- az _on_checkpoint_close() feltétel nélküli
        # `dataclasses_replace()` hívása így semmilyen statikus ellenőrzést
        # nem kapott (egy jövőbeli, nem-dataclass érték csak futásidőben,
        # TestResult-vesztéssel derült volna ki). A TYPE_CHECKING-import
        # elkerüli a modul-szintű test_runner importot (a GUI lazy import
        # mintája, l. _build_runner()).
        self._pending_checkpoint_result: TestResult | None = None
        # Governance-v2 review-kör (MAX verifier V4-1, P2): a checkpoint
        # ELÉRÉSEKOR kiadott safe-off eszközönkénti eredménye -- l.
        # _on_checkpoint_close()/_write_report().
        self._checkpoint_safe_off_attempted: bool = False
        self._checkpoint_safe_off_result: dict = {}

        # WP-AK-07 (BQ Diagnostics GUI): a fő teszt-worker szálától FÜGGETLEN
        # életciklus — a diagnosztika akkor is csatlakoztatható/pollolható,
        # ha éppen nem fut battery test session.
        self._bq_thread: QThread | None = None
        self._bq_worker: BqDiagnosticsWorker | None = None
        self._bq_serial: object | None = None  # pyserial Serial, lazy import miatt object
        # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P0 lelet (F1):
        # a "Kapcsolódás" hardveres bring-up-ja (soros port + negotiate)
        # _BqConnectWorker-en fut, külön QThread-en — l. az osztály docstringjét.
        self._bq_connect_thread: QThread | None = None
        self._bq_connect_worker: _BqConnectWorker | None = None

        # WP-AK-16 (BQ-TRANSPORT-SESSION-GAP-01): a TestRunner-session saját
        # PicoBQTransport-ja (TRADITIONAL_LEARNING/GOLDEN_GG esetén) — kölcsönös
        # kizárásban a fenti BQ Diagnosztika/Kalibráció kapcsolatokkal, mert egy
        # fizikai Pico USB-CDC portja egyszerre csak egy klienst szolgálhat ki.
        self._session_owns_bq_transport: bool = False
        # MAX verifier F5 lelete (2026-09-15, C-02 után): ez a mező a C-02
        # fix óta WRITE-ONLY a produkciós kódban -- a tényleges zárási
        # logika (_close_session_bq_transport()) a lenti
        # self._session_bq_transport-ot olvassa, ennek a mezőnek
        # KIZÁRÓLAG diagnosztikai/introspekciós szerepe maradt (pl. tesztek
        # jelenlét-ellenőrzésre). Szándékosan MEGTARTVA (nem törölve) --
        # a törlése minden hívóhely átvizsgálását igényelné egy már
        # elfogadható méretű diffben (CLAUDE.md §10).
        self._session_bq_serial: serial.Serial | None = None
        # Review 2026-09-15 (C-02/DFAM-040+DFAM-001): a session-owned
        # PicoBQTransport OBJEKTUM referenciája (nem csak a nyers serial-é)
        # -- l. _close_session_bq_transport() kommentje, miért kell ennek
        # .disconnect()-et hívnia a puszta serial.close() helyett.
        self._session_bq_transport: Any | None = None

        # Release-audit 2026-08-23 (2.2): a TRADITIONAL_LEARNING/GOLDEN_GG
        # session controllerének referenciája — _write_report() ebből olvassa
        # ki a BQ learning eredmény-klasszifikációt/export-státuszt a session
        # végén (l. _build_runner() ctrl-factory-jai). Minden új
        # _build_runner()-hívás elején nullázva, hogy egy korábbi, MÁS
        # teszttípusú session ne szivárogjon át a riportba.
        self._last_traditional_learning_ctrl: Any | None = None
        self._last_golden_gg_ctrl: Any | None = None
        # Teljes kódbázis review 10. kör (2026-08-31), Metrológia/
        # Adatintegritás P2 lelet: a _last_golden_gg_ctrl mintáját követve —
        # a ChemIdCharacterizationController relax_dmm_fault_total mezőjét
        # _write_report() enélkül nem tudta kiolvasni (a controller-factory
        # sosem mentette a referenciát).
        self._last_chemid_ctrl: Any | None = None
        # Teljes kódbázis review 14. kör (2026-09-04), Szoftver/Architektúra
        # + Session/Workflow/Állapotgép P2 lelet: a _last_chemid_ctrl
        # mintáját követve — az OcvSocController relax_dmm_fault_total
        # mezőjét _write_report() enélkül nem tudta kiolvasni (a
        # controller-factory sosem mentette a referenciát), holott az
        # OCV_SOC session futtatja a leghosszabb (2-5 órás) relax-fázisokat,
        # ahol egy DMM-kiesés a legvalószínűbb és a legkritikusabb.
        self._last_ocv_soc_ctrl: Any | None = None
        # Governance-v2 kör 2: az IecDcResistanceController referenciája —
        # az _last_ocv_soc_ctrl/_last_chemid_ctrl mintáját követve.
        self._last_iec_dc_resistance_ctrl: Any | None = None
        # Release-audit 2026-08-23 (2.3): a session összes DischargeController
        # példánya (egy sessionben több DISCHARGE lépés is lehet) — innen
        # olvassa ki _write_report() a rb_timing_degraded jelzőt.
        self._discharge_ctrls_this_session: list = []

        # Release-audit 2026-08-23 (2.6): a Start-folyamat blokkoló hardveres
        # bring-up-ja (_construct_and_connect_instruments) ezen a külön
        # QThread-en fut, hogy a GUI-szál (és vele a vészleállítás/
        # ablakbezárás) a kapcsolódás alatt is válaszképes maradjon.
        self._connect_thread: QThread | None = None
        self._connect_worker: _InstrumentConnectWorker | None = None
        # R2-B6 (R2-GUI-02, DFAM-018): az _on_finished() altal kiirt
        # minosito utotag; a checkpoint Close ujrairja.
        self._terminal_status_suffix: str = ""
        self._pending_start_cfg: SessionConfig | None = None
        # Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet: l.
        # LivePanel.set_emergency_stop_enabled() docstringje.
        self._connect_abort_requested: bool = False
        # R2-B3 (R2-SES-01, DFAM-001): explicit bring-up fazis.
        self._bring_up_in_progress: bool = False

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
        # Review-kör 4/5 GUI F5 (P2, DFAM-012 rokona): a "Törlés" gomb
        # (DeviceErrorPanel) kizárólag a saját listáját/számlálóját
        # nullázta -- a LivePanel kompakt "Eszközhiba" számlálója
        # (_device_err_count) semmilyen módon nem értesült erről, tehát
        # egy MID-SESSION Törlés-kattintás után a két kijelzés (háttérfül:
        # "0 hiba", élő panel: a törlés ELŐTTI kumulatív érték) tartósan
        # ELTÉRT egymástól, félrevezetve az operátort a tényleges friss
        # hibaszámról. Ugyanaz a widget-életciklus (mindkét panel a
        # MainWindow konstruktorában, egyszer jön létre), tehát a kapcsolat
        # itt, session-független helyen köthető be.
        self._device_error_panel.cleared.connect(
            self._live_panel.reset_device_error_count
        )

        self._bq_diagnostics_panel = BqDiagnosticsPanel()
        self._tabs.addTab(self._bq_diagnostics_panel, "BQ Diagnosztika")
        self._bq_diagnostics_panel.connect_requested.connect(self._on_bq_connect_requested)
        self._bq_diagnostics_panel.disconnect_requested.connect(self._on_bq_disconnect_requested)
        self._bq_diagnostics_panel.calibration_requested.connect(self._on_bq_calibration_requested)
        # Feature (2026-08-23): session-vezérelt passzív élő nézet — l.
        # gauge_snapshot_ready docstring.
        self.gauge_snapshot_ready.connect(self._bq_diagnostics_panel.update_snapshot)
        # Block 6 (2026-08-25): l. provisioning_consistency_ready docstring.
        self.provisioning_consistency_ready.connect(
            self._bq_diagnostics_panel.set_provisioning_consistency
        )
        # DATA-006/WP-AK-07 rés (2026-08-24, session_fg1_20260823_200337
        # elemzés): a _on_bq_snapshot_ready() eddig KIZÁRÓLAG a Diagnosztika
        # fül ÖNÁLLÓ (standalone) worker-kapcsolatának snapshot_ready
        # szignálján volt bekötve a lemezre íráshoz — a gauge_snapshot_ready
        # (a TRADITIONAL_LEARNING/GOLDEN_GG session SAJÁT, belső
        # poll_bq_diagnostics() hívásaiból táplált szignál) sosem jutott el
        # a loggerhez, ezért egy teljes, sokórás session alatt a
        # bq_diagnostics.csv üres maradt, holott a session belül ténylegesen
        # rendszeresen (sample_interval_s kadenciával) lekérdezte a gauge-ot.
        self.gauge_snapshot_ready.connect(self._on_bq_snapshot_ready)

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

        self._build_menus()
        # Kör 20 Batch E9 (F20-G4): a Pico-előkészítő menüakció widget-
        # állapotát is beállítja a _refresh_calibration_blocked_state() --
        # ez a kezdeti hívás biztosítja, hogy az állapot induláskor is
        # explicit be legyen állítva (nem csak a QAction alapértelmezett
        # enabled=True értékére hagyatkozva, ami induláskor véletlenül
        # egyezik a helyes állapottal, de nem bizonyítottan garantált).
        self._refresh_calibration_blocked_state()

        self._review_incomplete_sessions_on_startup()

    # ------------------------------------------------------------------ #
    # Menüsor                                                             #
    # ------------------------------------------------------------------ #
    def _build_menus(self) -> None:
        """Eszközök menü — olyan, a mérési munkamenettől FÜGGETLEN eszköz-
        előkészítési műveletek helye, amelyek nem tartoznak egyetlen
        mérési fülhöz sem (a fülek a mérési workflow-t követik)."""
        tools_menu = self.menuBar().addMenu("&Eszközök")
        self._pico_provisioning_action = QAction(
            "Pico előkészítése BQ34Z110-hez…", self
        )
        self._pico_provisioning_action.setStatusTip(
            "Egy üres (friss MicroPython) Raspberry Pi Pico feltöltése és "
            "elindítása BQ34Z110-kommunikációhoz, bekötési útmutatóval."
        )
        self._pico_provisioning_action.triggered.connect(
            self._on_pico_provisioning_requested
        )
        tools_menu.addAction(self._pico_provisioning_action)

    def _on_pico_provisioning_requested(self) -> None:
        """A Pico-előkészítő dialógus megnyitása.

        KÖLCSÖNÖS KIZÁRÁS: a dialógus ÖNÁLLÓ soros kapcsolatot nyit (raw-REPL,
        majd egy záró negotiate()), és MODÁLIS — pontosan ugyanaz a két
        kockázat, mint `_on_bq_calibration_requested()`-nél:
        (1) port-versengés bármely másik BQ-kapcsolattal,
        (2) egy modális ablak alatt a LivePanel Vészleállítás gombja
            elérhetetlen (CLAUDE.md §15).
        Ezért ugyanaz a NÉGY kizárási feltétel érvényes, ugyanabban a
        sorrendben — l. az ottani, részletes indoklásokat."""
        if self._worker is not None:
            QMessageBox.warning(
                self, "Pico előkészítés",
                "Fizikai teszt van folyamatban - a dialógus modálisan blokkolná "
                "a Vészleállítás gombot. Állítsd le a tesztet, mielőtt Pico-t "
                "készítenél elő.",
            )
            return
        if self._bq_worker is not None:
            QMessageBox.warning(
                self, "Pico előkészítés",
                "Előbb bontsd a BQ Diagnosztika kapcsolatot - az előkészítés "
                "önálló soros kapcsolatot igényel ugyanahhoz a porthoz.",
            )
            return
        if self._session_owns_bq_transport:
            QMessageBox.warning(
                self, "Pico előkészítés",
                "Egy fizikai BQ tanítási session (Traditional Learning / Golden GG) "
                "jelenleg használja a Pico soros portját - az előkészítés csak a "
                "session befejezése után indítható.",
            )
            return
        if self._bq_connect_worker is not None or self._connect_worker is not None:
            QMessageBox.warning(
                self, "Pico előkészítés",
                "Egy hardveres kapcsolódás jelenleg folyamatban van - az "
                "előkészítés csak a kapcsolódás befejezése (vagy megszakítása) "
                "után indítható.",
            )
            return
        from Prog.gui.panels.pico_provisioning_dialog import PicoProvisioningDialog

        dlg = PicoProvisioningDialog(self)
        dlg.exec()

    # ------------------------------------------------------------------ #
    # WP-AK-02 (REC-001..004): power-loss / process-crash recovery       #
    # ------------------------------------------------------------------ #

    def _sessions_root(self) -> Path:
        return app_paths.exe_dir() / "Mérések" / "Sessions"

    def _review_incomplete_sessions_on_startup(self) -> None:
        """App-indításkori "operator review" (REC-001..004 Restart workflow) —
        tisztán informatív: még nincs aktuális SessionConfig, tehát nem
        dönthető el, melyik megszakadt session lenne ténylegesen folytatható
        (ehhez konfiguráció/profil/műszer-egyezés kell, l. `_start_test()`).
        Itt csak felsoroljuk, mit talált a kezelő, hogy tudjon róla."""
        from Prog.src.session_recovery import discover_incomplete_sessions

        incomplete = discover_incomplete_sessions(self._sessions_root())
        if not incomplete:
            return

        lines = []
        # Kör 19 D8e (GUI-R19-04, DFAM-018 3. előfordulása "severity-bearing
        # text through a neutral constructor" alakzatra): a lenti ciklus
        # ⚠-jelölt sorai KRITIKUS safety-tartalmat hordozhatnak (energizált
        # maradhatott kimenet, UNSEALED chip, olvashatatlan checkpoint), de
        # a dialógus korábban FELTÉTEL NÉLKÜL QMessageBox.information()-t
        # használt -- ugyanaz a minta, mint _on_finished() is_degraded
        # felosztása (l. ott), 1:1 követve.
        has_warning = False
        for entry in incomplete:
            cp = entry["checkpoint"]
            safe = "biztonságos határon" if cp.get("safe_boundary") else "AKTÍV lépés közben"
            step = cp.get("step", "?")
            lines.append(f"• {entry['session_dir'].name} — {step} ({safe})")
            completion = cp.get("tlc_completion")
            if (
                completion is not None or step == "traditional_learning"
            ) and not self._tlc_recharge_completed(completion):
                evidence_text = (
                    "a BQ-bizonyíték megmaradt" if isinstance(completion, dict)
                    and completion.get("learning_evidence") else "a BQ-bizonyítékot ellenőrizni kell"
                )
                lines.append(f"    ⚠ TLC: a záró újratöltés nincs igazolva; {evidence_text}, aktív töltés nem folytatható.")
                has_warning = True
            # R17-06 (Kor 18, Batch C7 SB4, DFAM-018): l. session_recovery.
            # py::discover_incomplete_sessions() "unreadable" kulcsanak
            # kommentjet. Ha a checkpoint.json nem olvashato, az ALATTA
            # kovetkezo harom maradvany-sor kozul KETTO (BQ DF / BQ seal)
            # `{}`-bol nem tud tuzelni (nema false-negative), a HARMADIK
            # (safe-off) VISZONT tuzelne, mert `cp.get("last_safe_off_
            # result")` `{}`-bol `None`-t ad, ami a "SOHA nem lett
            # megkiserelve" agat valtja ki -- ez a szoveg NEM hamis (a
            # kimenet valoban lehet energizalt), csak REDUNDANS az itt
            # kiirt, szigorubb "kezeld ugy, mintha MINDENT hordozna"
            # figyelmeztetessel szemben. A CONTINUE ezert a duplikacio
            # elkerulesehez load-bearing (fuggetlen verifier lelete,
            # Kor 18 SB4 P3-1), nem azert, mert a harmadik sor egyebkent
            # nema maradna. Fail-closed: a legrosszabb feltetelezest
            # jelezzuk ki. NEM blokkol (a resume-tiltast az evaluate_
            # resume() safe_boundary kapuja vegzi).
            if entry.get("unreadable"):
                lines.append(
                    "    ⚠ CHECKPOINT NEM OLVASHATÓ (csonka/sérült fájl): "
                    "a session állapota NEM rekonstruálható — kezeld úgy, "
                    "mintha BQ Data Flash maradványt, UNSEALED chipet ÉS "
                    "energizált PSU/Load kimenetet hordozna. Kézi "
                    "ellenőrzés KÖTELEZŐ, MIELŐTT bármihez hozzáérnél."
                )
                has_warning = True
                continue
            # Review-kör 7 (2026-09-15), C-12: l. TraditionalLearningController
            # .pending_bq_df_restore docstringje -- ha a checkpoint.json ezt
            # a mezőt hordozza, a fizikai BQ34Z110 chip valószínűleg még
            # ideiglenes Cell BL Set Volt Threshold-on áll ÉS unsealed
            # állapotban van. Ez explicit, kézi figyelmeztetést igényel --
            # egy "biztonságos" resume ELINDULÁSA (vagy a session egyszerű
            # figyelmen kívül hagyása) NEM állítja vissza a chipet.
            pending = cp.get("pending_bq_df_restore")
            if pending:
                lines.append(
                    f"    ⚠ BQ Data Flash MARADVÁNY: {pending.get('field')} "
                    f"ideiglenes learning-értéken maradhatott, eredeti = "
                    f"{pending.get('original_mV')} mV; a chip UNSEALED "
                    "lehet. Kézi ellenőrzés/visszaállítás szükséges "
                    "(Prog/tools/dump_bq_data_flash_table.py a jelenlegi "
                    "érték ellenőrzéséhez)."
                )
                has_warning = True
            # R2-11b (2026-09-15, DFAM-016 + DFAM-026): a fenti maradvány a
            # TraditionalLearning útra jellemző, ideiglenes Data Flash
            # ÉRTÉKRŐL szól; ez a MÁSODIK, SZÁNDÉKOSAN ELKÜLÖNÍTETT sor a
            # GoldenGgController tisztán HOZZÁFÉRÉS-állapotbeli maradványa
            # (a chip UNSEALED maradhatott a PRECOND Qmax/Ra forrásolvasás
            # után, BQ-DF-SEALED-001) -- l. GoldenGgController.
            # pending_bq_unsealed docstringje, miért nem szabad a kettőt
            # egy mezőbe/sorba összeolvasztani (DFAM-018).
            pending_unsealed = cp.get("pending_bq_unsealed")
            if pending_unsealed:
                lines.append(
                    "    ⚠ BQ SEAL MARADVÁNY: a BQ34Z110 chip UNSEALED "
                    "állapotban maradhatott "
                    f"({pending_unsealed.get('controller')}, "
                    f"{pending_unsealed.get('reason')}) — a session-végi, "
                    "visszaellenőrzött seal() nem futott le igazoltan. "
                    "Nyitott (vissza nem állított) Data Flash-érték-maradvány "
                    "ezen az úton nincs, de a chip "
                    "újrahasználata ELŐTT kézi visszazárás/ellenőrzés "
                    "szükséges (Prog/tools/dump_bq_data_flash_table.py "
                    "CONTROL_STATUS SS bit táblázata)."
                )
                has_warning = True
            # R17-03+R17-17+V4-1 (2026-09-16, Kör 17 MAX-effort review,
            # DFAM-016+018+026): a fenti KÉT sor a BQ34Z110 chip
            # állapotáról szól -- ez a HARMADIK, SZÁNDÉKOSAN ELKÜLÖNÍTETT
            # sor a PSU/Load KIMENETI állapotáról (energizált maradhat-e),
            # amit eddig SEMMILYEN startup-csatorna nem jelzett. Három
            # állapot: kulcs hiányzik/None = nincs bizonyíték a session
            # végi lekapcsolásra (a checkpoint.json-t megelőző crash miatt
            # a runner sosem jutott a garantált safe-off-ig); dict, ahol
            # bármely eszköz False = bizonytalan (a parancs kiadva, de
            # nem igazoltan sikeres); dict, ahol minden eszköz True =
            # nincs mit jelezni (nem kerül sorra).
            last_safe_off = cp.get("last_safe_off_result")
            if last_safe_off is None:
                lines.append(
                    "    ⚠ SAFE-OFF BIZONYÍTÉK HIÁNYZIK: nincs nyoma annak, "
                    "hogy a PSU/Load kikapcsolása megtörtént ebben a "
                    "sessionben. A kimenet ENERGIZÁLT maradhatott; "
                    "ellenőrizd fizikailag a műszereket, MIELŐTT bármihez "
                    "hozzáérnél."
                )
                has_warning = True
            else:
                uncertain_devices = [
                    n for n in ("PSU", "Load") if not last_safe_off.get(n, False)
                ]
                if uncertain_devices:
                    lines.append(
                        "    ⚠ SAFE-OFF BIZONYTALAN: a kikapcsolás NEM "
                        "igazolt a következő eszközön: "
                        + ", ".join(uncertain_devices)
                        + ". A kimenet ENERGIZÁLT maradhatott; ellenőrizd "
                        "fizikailag a műszereket. (A kikapcsolás a "
                        "parancseredmény vagy a safe-off utáni "
                        "állapotellenőrzés alapján nem igazolt.)"
                    )
                    has_warning = True

        message = (
            "Az alábbi session(ök) nem futottak le rendesen a végéig (nincs "
            "report.json — feltehetően összeomlás vagy áramkimaradás):\n\n"
            + "\n".join(lines)
            + "\n\nHa a fizikailag ugyanazon akkumulátorral/felállással "
              "ugyanazt a konfigurációt indítod újra, az app automatikusan "
              "felajánlja a biztonságos határról való folytatást."
        )
        if has_warning:
            QMessageBox.warning(self, "Megszakadt session(ök) — SAFE-OFF ELLENŐRZÉS SZÜKSÉGES", message)
        else:
            QMessageBox.information(self, "Megszakadt session(ök) találhatók", message)
        # Teljes kódbázis review 9. kör (2026-08-30), Session/Workflow P2
        # lelet: korábban a fenti dialógus után a régi session mappája
        # semmilyen nyomot nem kapott arról, hogy az operátor ténylegesen
        # megtekintette — ha az operátor NEM folytatja (vagy nincs is
        # egyező konfiguráció a folytatáshoz), a mappa örökre "megszakadt,
        # sorsa dokumentálatlan" marad, minden induláskor újra megjelenve
        # ugyanebben a listában (CLAUDE.md §13: auditálható log és riport
        # — a session sorsa utólag nem volt rekonstruálható). Ez NEM egy
        # teljes report.json (az hamis terminális-minősítést sugallna egy
        # olyan session-re, aminek nincs valós terminál-állapota, CLAUDE.md
        # §23 tiltja a bizonyíték nélküli minősítést) — csak egy kis,
        # additív jelzőfájl, ami a discover_incomplete_sessions() logikáját
        # nem befolyásolja (az kizárólag checkpoint.json/report.json
        # jelenlétét nézi).
        self._mark_incomplete_sessions_reviewed(incomplete)

    def _mark_incomplete_sessions_reviewed(self, incomplete: list[dict]) -> None:
        reviewed_at_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        for entry in incomplete:
            marker_path = entry["session_dir"] / "startup_review_note.json"
            if marker_path.exists():
                continue
            try:
                marker_path.write_text(
                    json.dumps(
                        {
                            "reviewed_at_iso": reviewed_at_iso,
                            "note": (
                                "Az operátor a startup review-n megtekintette "
                                "ezt a megszakadt session-t. Ha egy másik "
                                "session később sikeresen folytatta, annak "
                                "report.json-ja tartalmazza a parent_session_id "
                                "hivatkozást erre a mappára."
                            ),
                        },
                        indent=2, ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Ablak lezárás (FIX-02)                                              #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:
        if self._connect_worker is not None:
            # Release-audit 2026-08-23 (2.6): a hardveres bring-up (connect/
            # identify/BQ negotiate) még fut egy külön QThread-en — az ablak
            # itteni erőszakos bezárása egy éppen futó worker-szálat hagyna
            # egy megsemmisített MainWindow-ra hivatkozva. A GUI-szál maga
            # válaszképes marad (ez pont a 2.6 javítás lényege), az operátor
            # néhány másodpercet várhat, vagy megszakíthatja a kapcsolódást.
            QMessageBox.information(
                self, "Csatlakozás folyamatban",
                "A műszerekhez való csatlakozás még folyamatban van. "
                "Várj, amíg befejeződik (vagy hibára fut), majd zárd be az "
                "ablakot.",
            )
            event.ignore()
            return
        if self._bq_connect_worker is not None:
            # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P0 lelet:
            # ugyanaz a minta, mint a fenti _connect_worker ágnál — a BQ
            # Diagnosztika kapcsolódás-worker még fut egy külön QThread-en.
            QMessageBox.information(
                self, "BQ csatlakozás folyamatban",
                "A BQ Diagnosztika kapcsolódása még folyamatban van. Várj, "
                "amíg befejeződik (vagy hibára fut), majd zárd be az ablakot.",
            )
            event.ignore()
            return
        if self._worker is not None and self._pending_checkpoint_result is not None:
            # Governance-v2 review-kör (MAX verifier V4-4, P2, DFAM-016):
            # egy resumable BQ checkpointnál PARKOLÓ session (a worker
            # `run()`-ja MÁR visszatért, a szál csak él, hogy a "Folytatás"
            # lehetséges maradjon) korábban a LENTI "Teszt fut" ágra esett:
            # a dialógus HAMISAN azt ígérte, hogy "a safe_off automatikusan
            # megtörténik", majd a Yes-ág `_emergency_stop()`-ot hívott --
            # ami itt CSAK egy flaget állít a dormant runneren, amit SOHA
            # semmi nem olvas ki (a run()-hurok nem fut, a folytatás pedig
            # már nem következik be). Eredmény: NEM ment ki OFF parancs,
            # NEM történt disconnect_all(), és NEM készült report.json --
            # a session-mappa checkpoint.json-nal, riport nélkül maradt
            # (session_recovery.discover_incomplete_sessions() emiatt
            # megszakadtként kezelte volna), miközben az operátor egy
            # kifejezett biztonsági ígéretet kapott. Ez az ág mostantól a
            # "Teszt lezárása" gombbal AZONOS, teljes terminális lezárást
            # végzi (friss safe-off -> disconnect -> report.json), és a
            # dialógus szövege ezt pontosan írja le.
            #
            # FONTOS PONTOSÍTÁS (független MAX verifier, 2026-09-12): a
            # `MANUAL_CHECKPOINT` a teljes kódbázisban KIZÁRÓLAG a
            # `bq_learning_physical()` terv UTOLSÓ lépéseként szerepel
            # (test_runner.py ~171. sor), ahol `checkpoint_is_terminal`
            # mindig igaz -> `resume_possible` mindig False ->
            # `_pending_checkpoint_result` SOHA nem íródik (l. main_window.py
            # `:1328` körüli egyetlen értékadás). Ez az ág ma tehát
            # NOT_REACHABLE -- ugyanaz a minősítés, mint a projekt más,
            # tudatosan megtartott látens védelmi rétegeinél (l.
            # test_runner.py S5-03 megjegyzése, worker.py `:127-134`).
            # KEEP_AS_IS: a védelem akkor válik éllé, ha valaha egy MÁSIK
            # terv is `resume_possible=True` checkpointot generál.
            #
            # ISMERT, ELFOGADOTT, ALACSONY KOCKÁZATÚ RESIDUÁLIS RÉS (P3,
            # független verifier): ha a `_cleanup_thread()` timeoutol (lent,
            # `:802`) és az ablak emiatt NYITVA marad, egy MÁSODIK zárási
            # kísérlet a `_on_checkpoint_close()` belsejében már `None`-ra
            # állított `_pending_checkpoint_result` miatt a LENTI, "Teszt
            # fut" ágra esne (a hamis "automatikusan megtörténik" szöveggel).
            # Fizikai kockázat nincs (a friss safe-off + report.json már az
            # ELSŐ kísérletnél megtörtént, a második kísérlet `_emergency_
            # stop()`-ja csak egy már dormant runner flagjét állítaná) -- a
            # javítás egy külön, a `_pending_checkpoint_result` resetjétől
            # FÜGGETLEN állapotjelzőt igényelne, amit ez a kör szándékosan
            # NEM vezetett be (kockázat/haszon arány, minimal-diff elv).
            reply = QMessageBox.question(
                self, "BQ checkpoint — session lezárása",
                "A session egy BQ checkpointnál parkol (a fizikai ciklus "
                "áll).\n\n"
                "Az ablak bezárása a \"Teszt lezárása\" gombbal AZONOS módon, "
                "VÉGLEGESEN lezárja a sessiont:\n"
                "• friss safe-off (Load INPUT OFF + PSU ALL OUTPUTS OFF),\n"
                "• műszer-kapcsolatok lezárása,\n"
                "• report.json megírása.\n\n"
                "A session ezután NEM folytatható. Biztosan bezárod?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                # Release-audit 2026-08-23 (2.14): explicit defaultButton=No.
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # Egyetlen, MEGLÉVŐ terminális lezáró útvonal -- nem külön,
            # könnyen elsodródó másolat (DFAM-016: kézzel felsorolt
            # terminális ágak). Tartalmazza a checklist-naplózást, a friss
            # safe-offot, a riportot és a szál-takarítást is.
            self._on_checkpoint_close()
            if self._worker is not None:
                # _on_checkpoint_close() -> _cleanup_thread() timeoutolt (a
                # szál MÉG FUT) -- ugyanaz a kapu, mint a lenti "Teszt fut"
                # ágban (S4-01): ilyenkor az ablak NEM zárható be.
                event.ignore()
                QMessageBox.warning(
                    self, "Bezárás megszakítva",
                    "A session lezárása elindult, de a teszt-szál még nem "
                    "állt le. Az ablak biztonsági okból NEM zárható be, amíg "
                    "ez be nem fejeződik -- várj, majd próbáld újra.\n\n"
                    "NE zárd be a programot a Feladatkezelőből.",
                )
                return
            if not self._cleanup_bq_thread():
                event.ignore()
                QMessageBox.warning(
                    self, "Bezárás megszakítva",
                    "A BQ Diagnosztika szál még nem állt le. Az ablak "
                    "biztonsági okból NEM zárható be, amíg ez be nem "
                    "fejeződik -- várj, majd próbáld újra.",
                )
                return
            # _finalize_terminal_session() ezt már elvégezte; a hívás
            # idempotens (l. ott a guard), és a "sosem épült fel a session"
            # eset miatt marad meg.
            self._close_session_bq_transport()
            event.accept()
            return
        if self._worker is not None:
            reply = QMessageBox.question(
                self, "Teszt fut",
                "Teszt folyamatban van. Biztosan bezárod az ablakot?\n"
                "(A safe_off automatikusan megtörténik.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                # Release-audit 2026-08-23 (2.14): explicit defaultButton=No —
                # anélkül egy véletlen Enter-lenyomás a Qt-alapértelmezés
                # szerint a Yes-t (proceed) választaná, elolvasás nélkül.
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._emergency_stop()
                # Review-kör 4 (2026-09-09), Session/Workflow S4-01 (P1,
                # MAX verifier kötelező): a _cleanup_thread() visszatérési
                # értékét ELLENŐRIZNI kell -- self._emergency_stop() csak
                # egy flaget állít, a tényleges safe-off/BQ Cell BL restore
                # a worker-szálon fut tovább, és ez akár 30s-es
                # runner_tick_s mellett is jóval túllépheti az 5s-es
                # wait()-et. Ha a szál még fut, az ablakot NEM szabad
                # bezárni -- a felhasználó a _connect_worker ág már
                # meglévő mintáját kapja (event.ignore() + magyarázat).
                # Review-kör 4 (2026-09-09), MAX verifier (a153925b3b9fbce5c)
                # P3-1: a dialógus szövege ne ígérjen konkrét (a valóságban
                # akár tíz másodperces nagyságrendű: 30s-es runner_tick_s +
                # ~15s BQ Cell BL restore + VISA safe-off) várakozási időt,
                # és explicit tiltsa a Feladatkezelős kilövést -- az a
                # legrosszabb kimenet (egyetlen finally sem fut, PSU
                # bekapcsolva maradhat, BQ chip az ideiglenes learning-
                # küszöbön ragadhat, pontosan amit ez a kapu megelőz).
                if not self._cleanup_thread():
                    event.ignore()
                    QMessageBox.warning(
                        self, "Bezárás megszakítva",
                        "A vészleállítás elindult, de a teszt-szál még nem "
                        "állt le (a safe-off/BQ-tranzakció folyamatban "
                        "lehet, ez akár fél percnél is tovább tarthat). "
                        "Az ablak biztonsági okból NEM zárható be, amíg ez "
                        "be nem fejeződik -- várj, majd próbáld újra.\n\n"
                        "NE zárd be a programot a Feladatkezelőből -- az a "
                        "PSU-t bekapcsolva és a BQ chipet ideiglenes "
                        "tanulási küszöbön hagyhatja.",
                    )
                    return
                # Review-kör 4 (2026-09-09), MAX verifier (a153925b3b9fbce5c)
                # P3-2, DFAM-016 sibling -- l. _cleanup_thread() fenti
                # kommentje ugyanerről a mintáról a BQ diagnosztika-szálon.
                if not self._cleanup_bq_thread():
                    event.ignore()
                    QMessageBox.warning(
                        self, "Bezárás megszakítva",
                        "A BQ Diagnosztika szál még nem állt le. Az ablak "
                        "biztonsági okból NEM zárható be, amíg ez be nem "
                        "fejeződik -- várj, majd próbáld újra.",
                    )
                    return
                self._close_session_bq_transport()
                event.accept()
            else:
                event.ignore()
        else:
            if not self._cleanup_bq_thread():
                event.ignore()
                QMessageBox.warning(
                    self, "Bezárás megszakítva",
                    "A BQ Diagnosztika szál még nem állt le. Az ablak "
                    "biztonsági okból NEM zárható be, amíg ez be nem "
                    "fejeződik -- várj, majd próbáld újra.",
                )
                return
            self._close_session_bq_transport()
            event.accept()

    def _close_session_bq_transport(self) -> None:
        """Release-audit 2026-08-23 (2.13): a WP-AK-16 session-szintű
        PicoBQTransport soros portja (TRADITIONAL_LEARNING/GOLDEN_GG)
        szándékosan nyitva marad CHECKPOINT_STOPPED+resume_possible állapotban
        (l. _on_finished()), amíg a session folytatható — de az ablak
        bezárásakor a folytatás lehetősége is megszűnik, a portot ekkor
        mindenképp le kell zárni. Korábban ez csak a terminális _on_finished()
        ágban történt meg, closeEvent()-ben soha.

        Review 2026-09-15 (C-02/DFAM-040+DFAM-001, G-08): korábban ez a
        metódus a nyers `self._session_bq_serial.close()`-t hívta közvetlenül
        -- ez zárta a portot, DE a PicoBQTransport OBJEKTUM `self._port`
        mezőjét NEM nullázta (mert sosem a transport SAJÁT `disconnect()`-je
        futott). Ha bármi (pl. egy stale referencia) ezután mégis hívna egy
        `_call()`-t ugyanazon a transport-objektumon, a `_reopen_serial()`
        `self._port` alapján CSENDBEN újranyitná a portot, visszavonva ennek
        a metódusnak a szándékát (ugyanaz a hibaosztály, amit a
        `PicoBQTransport.disconnect()` saját docstringje -- MAX verifier
        2. kör NEW-1 lelete -- már kezel). Mostantól a transport SAJÁT
        `disconnect()`-jét hívjuk, ami mindkettőt (serial.close() ÉS
        self._port=None) elvégzi."""
        if self._session_owns_bq_transport and self._session_bq_transport is not None:
            try:
                self._session_bq_transport.disconnect()
            except Exception:
                pass
            self._session_bq_serial = None
            self._session_bq_transport = None
            self._session_owns_bq_transport = False
            # Feature (2026-08-23): a Diagnosztika fül visszakapja a normál,
            # önálló kapcsolódási UI-t.
            self._bq_diagnostics_panel.set_session_owned(False)
            # Teljes kódbázis review 3. kör (2026-08-29), GUI/UX P2 lelet: a
            # session-vezérelt módban beállított 30s-os poll-intervallum
            # (l. _build_runner() set_poll_interval_ms(30000) hívása) itt
            # visszaáll az önálló diagnosztika-fül 2s-es alapértelmezésére —
            # enélkül egy KÖVETKEZŐ, önálló "Kapcsolódás" a 30s-os staleness-
            # küszöböt örökölné, feleslegesen lassítva egy valódi kiesés
            # felismerését.
            self._bq_diagnostics_panel.set_poll_interval_ms(2000)

    # ------------------------------------------------------------------ #
    # Start / Stop                                                        #
    # ------------------------------------------------------------------ #

    def _start_test(self) -> None:
        if self._worker is not None or self._connect_worker is not None:
            return
        if not _retry_pending_disconnects(self._live_panel.append_event):
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
                QMessageBox.StandardButton.No,  # release-audit 2026-08-23 (2.14)
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # WP-AK-03 (SUP02-ACCEPT-001/11): Start csak friss és validált
        # DynamicSessionModel-revisionnel engedélyezett — a validációt itt,
        # közvetlenül ellenőrzés előtt frissítjük, mert bármelyik korábbi
        # widget-interakció óta stale-lé válhatott volna.
        self._config_panel.refresh_validation()
        if not self._config_panel.is_start_allowed():
            # Review-kör 2 (2026-09-09), friss GUI/UX F7 (DFAM-026): a
            # DynamicSessionModel.build_summary()["blocking_errors"] már
            # korábban is felépítette a konkrét, human-readable okot (pl.
            # a kért töltőáram > PSU-mód limitje), de ez a hívóhely eddig
            # csak egy generikus üzenetet mutatott -- a kezelő nem tudta
            # meg a konkrét okot a mezők manuális átvizsgálása nélkül. A
            # generikus szöveg MARAD (a stale-revision esetre, amit a
            # blocking_errors nem fed), a konkrét okok ADDITÍV kiegészítésként.
            message = (
                "A dinamikus konfiguráció-modell szerint a jelenlegi beállítás "
                "nem indítható (pl. CONFIG_INCOMPATIBLE töltőáram vagy stale "
                "revision). Ellenőrizd a mezőket."
            )
            blocking_errors = self._config_panel.build_start_summary().get("blocking_errors") or []
            if blocking_errors:
                message += "\n\n" + "\n".join(blocking_errors)
            QMessageBox.warning(self, "Konfiguráció nem indítható", message)
            return

        # WP-AK-03 (SUP02-ACCEPT-001/12): Start előtti teljes effective summary.
        summary = self._config_panel.build_start_summary()
        reply = QMessageBox.question(
            self, "Összefoglaló — Start megerősítése",
            _format_start_summary(summary, cfg),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # release-audit 2026-08-23 (2.14)
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # WP-AK-16 kölcsönös kizárás — a _build_runner() belsejében is
        # ellenőrizve marad (defense in depth), de itt, a hardveres connect
        # ELŐTT szűrjük ki, hogy ne kössünk le feleslegesen egy soros/VISA
        # erőforrást olyan konfigurációhoz, ami úgyis elutasítódna.
        needs_bq_transport = cfg.test_type in _NEEDS_BQ_TRANSPORT_TEST_TYPES
        if needs_bq_transport and self._bq_worker is not None:
            QMessageBox.critical(
                self, "Inicializálási hiba",
                "Előbb bontsd a BQ Diagnosztika kapcsolatot - a Traditional "
                "Learning/Golden GG session önálló soros kapcsolatot igényel "
                "ugyanahhoz a porthoz.",
            )
            return
        # Teljes kódbázis review 11. kör (2026-08-31), GUI/UX/Állapot P2
        # lelet: a fenti self._bq_worker csak a MÁR MEGKÖTÖTT BQ
        # Diagnosztika kapcsolatot védi — egy folyamatban lévő "Kapcsolódás"
        # (_bq_connect_worker, ~1-4s worst-case) alatt ez még None, mégis
        # ugyanahhoz a fizikai Pico soros porthoz próbálna kapcsolódni egy
        # MÁSODIK, önálló klienssel.
        if needs_bq_transport and self._bq_connect_worker is not None:
            QMessageBox.critical(
                self, "Inicializálási hiba",
                "A BQ Diagnosztika kapcsolódása jelenleg folyamatban van - a "
                "Traditional Learning/Golden GG session csak a kapcsolódás "
                "befejezése (vagy megszakítása) után indítható.",
            )
            return

        # Release-audit 2026-08-23 (2.6): a blokkoló hardveres bring-up (VISA
        # connect_all/identify_all, BQ soros negotiate) worker-threadre kerül,
        # hogy a GUI-szál — és vele a vészleállítás/ablakbezárás — a
        # kapcsolódás alatt is válaszképes maradjon (CLAUDE.md §15). A
        # fennmaradó (gyors, nem-blokkoló) konstrukció _on_connect_ready()-ben
        # fut, a _build_runner() korábbi, teljes logikáját változatlanul híva.
        self._pending_start_cfg = cfg
        # Review-kör 2 (2026-09-09), friss GUI/UX F8 (P3, DFAM-001): korábban
        # a reset_plots() csak _on_connect_ready()-ben (a hardveres bring-up
        # SIKERES befejezése UTÁN) futott le -- egy előző FAULT/DONE session
        # utolsó allapota a teljes bring-up idejére (VISA connect + identify +
        # BQ negotiate + DMM/PSU konfiguráció, akár tíz másodpercekig)
        # látszólag aktívan megmaradt, és _on_connect_failed() esetén
        # egyáltalán nem is futott volna le. A resetet ide, a connect
        # INDÍTÁSA elé húzva mindkét hiányt lezárja egyetlen hívással.
        self._live_panel.reset_plots()
        self._live_panel.set_start_enabled(False)
        # R2-B6 (R2-GUI-04/02): az elfogadott Start a session-hatar.
        self._tabs.setTabEnabled(self._checkpoint_tab_index, False)
        self._terminal_status_suffix = ""
        # Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet: l.
        # LivePanel.set_emergency_stop_enabled() docstringje.
        self._connect_abort_requested = False
        self._live_panel.set_emergency_stop_enabled(True)
        # Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet: l.
        # ConfigPanel.set_session_active() docstringje.
        self._config_panel.set_session_active(True)
        self._status_bar.showMessage("Csatlakozás a műszerekhez…")

        self._connect_thread = QThread()
        self._connect_worker = _InstrumentConnectWorker(cfg, needs_bq_transport)
        self._connect_worker.moveToThread(self._connect_thread)
        self._connect_thread.started.connect(self._connect_worker.run)
        self._connect_worker.connect_ready.connect(self._on_connect_ready)
        self._connect_worker.connect_failed.connect(self._on_connect_failed)
        self._connect_thread.start()
        # R2-B3: a connect_ready/connect_failed queued jel csak e metodus
        # visszaterese utan erkezhet, es mindketto nullazza a jelzot.
        self._bring_up_in_progress = True
        self._refresh_calibration_blocked_state()

    def _on_connect_ready(self, pre_connected: _PreConnectedInstruments) -> None:
        """R2-B3 (R2-SES-01, DFAM-001/032): a bring-up fazisjelzo MINDEN
        kilepesen (abort, build-hiba, worker-inditas, varatlan kivetel)
        nullazodik -- l. _on_connect_ready_impl()."""
        try:
            self._on_connect_ready_impl(pre_connected)
        finally:
            self._bring_up_in_progress = False
            self._connect_abort_requested = False

    def _on_connect_ready_impl(self, pre_connected: _PreConnectedInstruments) -> None:
        """A _connect_worker.connect_ready jele (keresztszálas, queued
        connection) hívja a GUI-szálon — ide csak akkor jutunk el, amikor a
        connect worker run()-ja már lefutott és emittált, tehát a
        _cleanup_connect_thread()-beli connect_thread.wait() itt biztonságos
        (nem blokkol érdemben, nem holtpont — szemben azzal, ha a GUI-szál
        SAJÁT MAGA várna szinkron .wait()-tel a connect_thread-re, MIELŐTT az
        eseményhurok kézbesíthetné ezt a jelet)."""
        cfg = self._pending_start_cfg
        self._cleanup_connect_thread()
        self._live_panel.set_emergency_stop_enabled(False)
        if self._connect_abort_requested:
            # Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet: a
            # kapcsolódás alatt kért vészleállítás itt válik ténylegessé —
            # a MÁR sikeresen megnyitott kapcsolatokat azonnal lezárjuk, a
            # session NEM indul el.
            self._connect_abort_requested = False
            # Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-04
            # lelet: l. _construct_and_connect_instruments() except-ágának
            # azonos indoklása — disconnect_all() önmagában nem ad ki
            # OUTPUT/INPUT OFF parancsot, safe_all_off() kell előbb.
            # R1 B6, F-06: az eredmény korábban eldobódott -- l. lent.
            abort_safe_off_result = pre_connected.instruments.safe_all_off()
            _disconnect_for_cleanup(pre_connected.instruments, self._live_panel.append_event)
            # Teljes kódbázis review 4. kör (2026-08-29), GUI/UX P2 lelet: a
            # fenti disconnect_all() csak a PSU/Load/DMM_V/DMM_T VISA-
            # erőforrásokat zárja — a pre_connected.session_bq_serial (ha a
            # test_type BQ-transportot is igényelt) itt korábban SOSEM
            # záródott le, ellentétben a _construct_and_connect_instruments()
            # except-ágával (fent, l. session_bq_serial.close()), ami
            # ugyanezt a kapcsolatot kivétel esetén helyesen lezárja. Nyitva
            # maradt pyserial.Serial-t hagyott a GC-re, ami a következő
            # BQ-kapcsolódási kísérletet (Diagnosztika fül/új Start) "port
            # busy"-hibába futtathatta.
            if pre_connected.session_bq_serial is not None:
                _cleanup_action(
                    "BQ-portzárás", pre_connected.session_bq_serial.close,
                    self._live_panel.append_event,
                )
            self._config_panel.set_session_active(False)
            self._live_panel.set_start_enabled(True)
            self._status_bar.showMessage(
                "Kapcsolódás megszakítva vészleállítás miatt — a session nem indult el."
            )
            # R1 B6, F-06 (DFAM-016/018): egy nem igazolt OFF nem maradhat
            # csendben -- a dialógus a safe-off, a disconnect és a portzárás
            # UTÁN jön, a worker-ág (3) azonos szövegével.
            abort_warning = _safe_off_not_confirmed_text(abort_safe_off_result)
            if abort_warning:
                QMessageBox.critical(
                    self, "VÉSZLEÁLLÍTÁS — bizonytalan safe-off",
                    "Kapcsolódás megszakítva vészleállítás miatt — a session "
                    "nem indult el." + abort_warning,
                )
            return
        if cfg is None:
            return
        try:
            # R2-B3 (Codex B3-PLAN-02, DFAM-001): a szinkron Qt-jelet/Python-
            # callbacket kivalto GUI-elokeszites a _build_runner() abort-kapuja
            # ELOTT fut (a meglevo build-hiba ag alatt): egy beagyazott slot-
            # kivetelbol jovo abortot a kapu meg a Logger/szulomarker elott
            # ervenyesiti.
            self._live_panel.reset_plots()
            # CF-27 (governance-v2, DFAM-001): a LivePanel staleness-küszöbe
            # (l. LivePanel._refresh_staleness()) a session TÉNYLEGES
            # runner_tick_s-éhez igazodik, a bq_diagnostics_panel.py::
            # set_poll_interval_ms() mintáját követve.
            self._live_panel.set_expected_tick_interval_ms(int(cfg.runner_tick_s * 1000))
            # Teljes kódbázis review 16. kör (2026-09-04), GUI/UX/Állapot P3
            # lelet: a LivePanel.reset_plots() mintáját követve — a
            # DeviceErrorPanel korábban semmilyen Start-eseménynél nem
            # törlődött, egy korábbi session SCPI-hibái láthatók maradtak egy
            # KÉSŐBBI, hibamentes session alatt is.
            self._device_error_panel.clear()
            self._tabs.setCurrentIndex(1)
            runner, test_plan, resume_start_step_index = self._build_runner(
                cfg, pre_connected=pre_connected
            )
        except Exception as exc:
            # Release-audit 2026-08-23 (2.10): a hardveres connect már
            # sikeres volt — ha a controller-építés hibázik, a kapcsolatokat
            # itt kell lezárni, különben a következő Start-kísérlet a régi,
            # sosem lezárt VISA-erőforrásokba ütközhet.
            # Review-kör 2 (2026-09-08), Szoftver/Architektúra F1 lelet: a
            # fenti `self._instruments` EBBEN a kivétel-ágban még a
            # KORÁBBI (előző sikeres session, ha volt) instrument-managerre
            # mutathat -- a `_build_runner()` csak a controller-építés
            # KÖZBEN, a profil/SafetyManager konstrukció UTÁN írja felül
            # `self._instruments`-t a frissen csatlakoztatott
            # `pre_connected.instruments`-szel (l. lent a metódus törzsében).
            # Egy KORÁBBI kivétel (pl. profil-validáció) esetén tehát ez a
            # régi, jellemzően már lezárt/idegen objektumot zárta volna le,
            # miközben a MOST, ebben a próbálkozásban ténylegesen megnyitott
            # `pre_connected.instruments` (és a `pre_connected.session_bq_
            # serial`, ha a test_type BQ-transportot igényelt) LEZÁRATLANUL
            # szivárgott — a fenti abort-ág (l. `_connect_abort_requested`)
            # már ugyanezt a `pre_connected`-alapú mintát követi.
            # Review-kör 2 (2026-09-08), Hardver/Műszervezérlés HW2-04
            # lelet: l. _construct_and_connect_instruments() except-ágának
            # azonos indoklása — disconnect_all() önmagában nem ad ki
            # OUTPUT/INPUT OFF parancsot, safe_all_off() kell előbb.
            # R1 B6, F-06: az eredmény a lenti hibadialógusba kerül.
            build_fail_safe_off_result = pre_connected.instruments.safe_all_off()
            _disconnect_for_cleanup(pre_connected.instruments, self._live_panel.append_event)
            # R1 B6, NS-B6-1 (F-09 testvére, DFAM-018): ha a _build_runner()
            # a `self._instruments = pre_connected.instruments` értékadás
            # UTÁN bukott (pl. Logger), a referencia a most lezárt managerre
            # mutatna. Identitásfeltételes: egy korábbi hibánál a MÁSIK
            # (előző) manager referenciája érintetlen marad.
            if self._instruments is pre_connected.instruments:
                self._instruments = None
            # KORREKCIÓ (Kör 20 Batch E9, F20-G2, 2026-09-19) a MAX verifier F6
            # leletéhez (2026-09-15, C-02 után): F6 kizárólag a TRANSPORT-
            # HANDLE biztonságát vizsgálta (a self._session_bq_transport-ot
            # KIZÁRÓLAG a _close_session_bq_transport() olvassa, tehát egy
            # itt elkapott kivétel utáni el-nem-nullázott mező semmilyen
            # _call()/_reopen_serial()-t nem tesz elérhetővé) -- ez a
            # megállapítás VÁLTOZATLANUL IGAZ és NEM módosult. F6 azonban NEM
            # vizsgálta a GUI-ÁLLAPOT következményét: ha `_build_runner()`
            # ez a hívás UTÁN hibázik, hogy a fenti self._session_bq_
            # transport/self._session_owns_bq_transport mezőket beállította
            # (l. `_build_runner()` WP-AK-16 blokkja), a
            # self._session_owns_bq_transport=True ÉS a
            # self._bq_diagnostics_panel.set_session_owned(True)-mód
            # LATCH-elve marad -- három menü/panel belépési pontot zár le
            # (:865 Pico előkészítése, :3041, :3207) egy HAMIS "Egy fizikai
            # BQ tanítási session … jelenleg használja a Pico soros portját"
            # üzenettel, amit KIZÁRÓLAG egy alkalmazás-újraindítás oldott
            # volna fel. Javítva: a _close_session_bq_transport() hívása a
            # nyers `pre_connected.session_bq_serial.close()` ELÉ került --
            # a guard-ja (`if self._session_owns_bq_transport and
            # self._session_bq_transport is not None:`) helyesen no-op, ha
            # `_build_runner()` a WP-AK-16 blokk ELŐTT hibázott (a mezők
            # sosem lettek beállítva), és helyesen leállítja+nullázza a
            # transportot ÉS a panel-módot, ha UTÁNA. A lenti nyers
            # `session_bq_serial.close()` VÁLTOZATLANUL megmarad (az egyetlen
            # út, ami a "_build_runner() a WP-AK-16 blokk ELŐTT hibázott, de a
            # pre_connected.session_bq_serial már megnyílt" esetet zárja le) --
            # a két hívás EGYMÁST NEM ZÁRJA KI, mindkettő idempotens no-op a
            # másik által már lezárt handle-ön (pyserial.Serial.close()
            # ismételt hívása nem hibázik).
            _cleanup_action(
                "BQ-transport lezárása", self._close_session_bq_transport,
                self._live_panel.append_event,
            )
            if pre_connected.session_bq_serial is not None:
                _cleanup_action(
                    "BQ-portzárás", pre_connected.session_bq_serial.close,
                    self._live_panel.append_event,
                )
            self._config_panel.set_session_active(False)
            self._live_panel.set_start_enabled(True)
            self._status_bar.showMessage("Inicializálási hiba.")
            QMessageBox.critical(
                self, "Inicializálási hiba",
                str(exc) + _safe_off_not_confirmed_text(build_fail_safe_off_result),
            )
            return

        self._live_panel.set_start_enabled(True)

        self._thread = QThread()
        self._worker = TestRunnerWorker(
            runner, test_plan, start_step_index=resume_start_step_index
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.sample_ready.connect(self._live_panel.update_sample)
        self._worker.status_changed.connect(self._live_panel.set_status)
        self._worker.status_changed.connect(self._update_status_bar_from_worker_status)
        self._worker.fault.connect(self._live_panel.set_fault)
        self._worker.fault.connect(self._on_fault)
        self._worker.finished.connect(self._on_finished)

        self._tabs.setTabEnabled(self._checkpoint_tab_index, False)   # reset
        self._worker.event_ready.connect(self._live_panel.append_event)
        self._worker.device_error_ready.connect(
            self._device_error_panel.append_device_error
        )
        # CF-29 (governance-v2, DFAM-012 rokona): korábban a
        # device_error_ready CSAK a háttérfülre volt kötve — élő nézeten
        # semmi nem jelezte SCPI hibát. Qt signal több független slotot is
        # fogad, ezért ez KIEGÉSZÍTŐ kapcsolat, a fenti változatlan.
        self._worker.device_error_ready.connect(
            self._live_panel.append_device_error
        )
        self._worker.step_changed.connect(self._on_step_changed)
        self._worker.step_changed.connect(self._live_panel.set_step)
        # Review-kör 5, Hardver/Műszervezérlés HW-R5-B (P2, felhasználói
        # döntés 2026-09-10): PSU CV/CC regulációs állapot -- KÜLÖN
        # szignál, NEM a device_error_ready-n keresztül (l. worker.py::
        # psu_regulation_ready docstringje, miért nem escaláló csatorna).
        self._worker.psu_regulation_ready.connect(
            self._live_panel.update_psu_regulation
        )
        self._worker.checkpoint_reached.connect(
            self._checkpoint_panel.show_checkpoint
        )
        self._worker.checkpoint_reached.connect(self._on_checkpoint_reached)

        self._worker.status_changed.connect(self._on_status_changed)

        # P0-javítás (2026-08-26): a folytatás a worker SAJÁT szálán fusson,
        # queued Qt-connection-nel — l. _continue_from_checkpoint_requested
        # docstringje.
        self._continue_from_checkpoint_requested.connect(
            self._worker.request_continue_from_checkpoint
        )

        # R2-B3 (Codex B3-PLAN-02): fail-closed hatarvedelem -- ha a kapu es a
        # worker letrejotte kozott (ma Qt-callback-mentes szakasz) megis abort
        # erkezett (a hook ott csak a flaget allitja, muszer-I/O nelkul), a
        # runner SAJAT, szerializalt veszleallitasi utja kapja meg: a run() az
        # ELSO VEGREHAJTANDO lepes elott (resume eseten az index nem feltetlenul
        # 0) ellenorzi, es a veszaga (_emergency_stop) ekkor safe-offot (Load/
        # PSU OFF) es hibapollt vegezhet -- aktiv lepes nem indul.
        if self._connect_abort_requested:
            self._worker.request_emergency_stop(
                "UNHANDLED_EXCEPTION: VÉSZLEÁLLÍTÁS kérve a session-indítás közben"
            )
        self._thread.start()
        self._status_bar.showMessage("Teszt elindítva…")
        self._refresh_calibration_blocked_state()

    def _on_connect_failed(self, message: str) -> None:
        # R2-B3 (Codex B3-PLAN-01, DFAM-032): a fazisjelzo es az abort-flag
        # kivetel eseten is nullazodik.
        try:
            self._cleanup_connect_thread()
            self._live_panel.set_emergency_stop_enabled(False)
            self._connect_abort_requested = False
            self._config_panel.set_session_active(False)
            self._live_panel.set_start_enabled(True)
            self._status_bar.showMessage("Csatlakozási hiba.")
            QMessageBox.critical(self, "Inicializálási hiba", message)
        finally:
            self._bring_up_in_progress = False
            self._connect_abort_requested = False

    def _cleanup_connect_thread(self) -> None:
        self._pending_start_cfg = None
        if self._connect_thread:
            self._connect_thread.quit()
            self._connect_thread.wait(5000)
            self._connect_thread.deleteLater()
            self._connect_thread = None
        if self._connect_worker:
            self._connect_worker.deleteLater()
            self._connect_worker = None
        self._refresh_calibration_blocked_state()

    def _stop_test(self) -> None:
        if self._worker:
            self._worker.request_stop()
            self._status_bar.showMessage("Leállítás folyamatban…")

    def _emergency_stop(self) -> None:
        if self._worker:
            self._worker.request_emergency_stop("USER_EMERGENCY_STOP")
            self._status_bar.showMessage("VÉSZLEÁLLÍTÁS kérve…")
        elif self._connect_worker is not None:
            # Teljes kódbázis review 1. kör (2026-08-26), GUI/UX P2 lelet: a
            # kapcsolódás (VISA connect/identify, BQ negotiate) egy blokkoló
            # hívássorozat, amit itt nem lehet biztonságosan félbeszakítani
            # — de amint befejeződik (siker vagy hiba), a rendszernek NE
            # engedje tovább a session-indítást, hanem azonnal zárja le a
            # már megnyitott kapcsolatokat. l. _on_connect_ready().
            self._connect_abort_requested = True
            self._status_bar.showMessage(
                "VÉSZLEÁLLÍTÁS kérve — a kapcsolódás befejezése után a "
                "műszerek azonnal lezáródnak, a session NEM indul el."
            )
        else:
            # Governance-v2 review-kör 1 (GUI reviewer, CF-28/GUI-R1-09,
            # DFAM-018): ha sem worker, sem connect_worker nincs (a session
            # már véget ért, vagy a checkpoint-panel "Safe Off" gombja egy
            # már eltakarított worker mellett aktív maradt), a metódus
            # korábban NÉMÁN tért vissza — a kezelő egy vészleállító gombot
            # nyomott meg, és semmilyen visszajelzést nem kapott arról,
            # hogy az nem hajtott végre semmit.
            self._status_bar.showMessage(
                "Nincs aktív session — a vészleállítás nem alkalmazható."
            )

    # ------------------------------------------------------------------ #
    # Worker events                                                       #
    # ------------------------------------------------------------------ #

    def _update_status_bar_from_worker_status(self, status: str) -> None:
        """Teljes kódbázis review 7. kör (2026-08-30), GUI/UX/Állapot P2
        lelet #2: korábban a worker.py::status_changed jel közvetlenül a
        self._status_bar.showMessage-re volt kötve — ez a nyers
        "CHECKPOINT_STOPPED" szöveget írta a státuszsorba, MIELŐTT
        _on_finished() (5. kör 839eb60 fixje) egyáltalán lefutna
        (worker.py::_do_run() előbb status_changed.emit("CHECKPOINT_
        STOPPED")-et ad, majd csak utána finished.emit(result)-et).
        Terminális (nem-resumable) esetben _on_finished() utólag "Kész"-re
        felülírja, de RESUMABLE checkpointnál SEMMI nem írja felül újra —
        a nyers, nem-informatív szöveg véglegesen a státuszsorban maradt,
        épp amikor az operátornak a legkonkrétabb (BQ-checkpoint) teendő-
        utasításra van szüksége. CHECKPOINT_STOPPED esetén ezért itt nem
        írunk semmit — a _on_checkpoint_reached() MÁR beállította a
        helyes, informatív üzenetet; minden más értéknél (RUNNING/FAULT/
        STOPPED/DONE) a régi, feltétel nélküli viselkedés marad.

        FIGYELEM: ne nevezd át _on_status_changed()-re — az a MÁSIK,
        MEGLÉVŐ, TAB-váltó handler neve (l. lentebb, RUNNING esetén a
        checkpoint fülről az élő nézetre vált), amit a status_changed jel
        egy KÜLÖN connect()-hívása is fogad ugyanebben a metódusban."""
        if status == "CHECKPOINT_STOPPED":
            return
        self._status_bar.showMessage(status)

    @staticmethod
    def _tlc_recharge_completed(completion) -> bool:
        if not isinstance(completion, dict):
            return False
        final = completion.get("final_recharge")
        if not isinstance(final, dict) or final.get("status") != "COMPLETED":
            return False
        off = final.get("off_readback")
        if not isinstance(off, dict) or off.get("Load") is not False or off.get("PSU") is not False:
            return False
        try:
            completed = datetime.fromisoformat(final["completed_at_utc"])
            next_campaign = datetime.fromisoformat(final["next_campaign_not_before_utc"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            completed.tzinfo is not None and next_campaign.tzinfo is not None
            and completed.utcoffset() == timedelta(0)
            and next_campaign.utcoffset() == timedelta(0)
            and next_campaign - completed == timedelta(hours=48)
        )

    def _on_finished(self, result) -> None:
        # DATA-003: keep_thread==False pontosan a "terminális állapot" jelentése
        # (DONE/FAULT/STOPPED, vagy CHECKPOINT_STOPPED ami NEM folytatható) —
        # minden ilyen esetben kötelező a riport, nem csak DONE-nál.
        keep_thread = (result.status == "CHECKPOINT_STOPPED"
                       and result.resume_possible)
        is_terminal = not keep_thread
        # Review-kör 5, Session/Workflow S5-04 (P2, S4-01 sibling -- l.
        # _cleanup_thread() docstringje ugyanerről a hibaosztályról): a
        # visszatérési érték korábban itt is el lett dobva -- a Start
        # gombot ténylegesen a KORÁBBAN (a `finished` előtt) tüzelő
        # `status_changed` jel nyomán a LivePanel.set_status() engedélyezi
        # vissza, FÜGGETLENÜL attól, hogy a szál ténylegesen leállt-e. Ha a
        # `wait(5000)` timeoutol, a Start gombot explicit vissza kell
        # tiltani a metódus VÉGÉN (felülírva a set_status() korábbi
        # hatását), különben egy új Start a MÉG ÉLŐ régi worker-szál
        # mellett egy MÁSODIK InstrumentManager/TestRunner-t hozhatna
        # létre -- egyidejű hardver-hozzáférés kockázata.
        cleanup_ok = True
        if not keep_thread:
            cleanup_ok = self._cleanup_thread()
        # Review-kör 2 (2026-09-09), friss GUI/UX F1: a _finalize_terminal_
        # session() visszaadott session_meta-ja (l. ott a docstringet) a
        # DONE/terminális-CHECKPOINT_STOPPED popup minősítő mezőinek
        # forrása lentebb.
        session_meta: dict | None = None
        if is_terminal:
            session_meta = self._finalize_terminal_session(result)
        else:
            # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P1 lelet (F3):
            # resumable checkpointnál (keep_thread=True) a worker/session
            # SZÁNDÉKOSAN életben marad — de ha a kezelő a checkpoint fülön
            # "Teszt lezárása"-val véglegesen bezárja anélkül, hogy folytatná
            # (a dokumentált, elsődleges kilépési út, l. CheckpointPanel info-
            # szövege), a session onnantól TÉNYLEGESEN véget ér, ugyanúgy,
            # mint egy terminális állapot. A takarításhoz (műszer-
            # lecsatlakozás, BQ transport zárás, report.json) szükséges
            # `result`-ot itt mentjük el — l. _on_checkpoint_close().
            self._pending_checkpoint_result = result
            # Teljes kódbázis review 18. kör (2026-09-05), GUI/UX/Állapot
            # P2 lelet: a worker.py::_do_run() a fenti keep_thread=True
            # ágban IS kiadja a status_changed.emit("CHECKPOINT_STOPPED")-
            # et, amit a LivePanel.set_status() fogad — mivel a status
            # != "RUNNING", a Start gomb ott visszaengedélyeződik, HOLOTT
            # a worker/szál szándékosan életben marad. Ha az operátor
            # ekkor visszavált az Élő mérés fülre, egy engedélyezettnek
            # látszó, de a _start_test() guardja miatt csendben no-op-oló
            # Start gombot lát. Explicit letiltás, amíg a worker létezik —
            # visszaengedélyezés _on_checkpoint_close()-ban, a tényleges
            # takarítás után.
            self._live_panel.set_start_enabled(False)
        # Teljes kódbázis review 5. kör (2026-08-29), GUI/UX P2 lelet: a
        # feltétel korábban csak result.status != "FAULT" volt — egy
        # RESUMABLE checkpointnál (keep_thread=True) ez feltétel nélkül
        # felülírta az _on_checkpoint_reached() által MÁR beállított,
        # valós állapotot leíró üzenetet ("BQ checkpoint elérve — végezd
        # el a BQ műveletet, majd folytathatod.") egy hamis "Kész"-re —
        # ellentmondva a closeEvent()-nél ugyanekkor megjelenő "Teszt fut.
        # Biztosan bezárod?" kérdésnek is. A lenti "elif result.status ==
        # 'CHECKPOINT_STOPPED': pass" komment ("tab és status bar már
        # frissítve") hamis előfeltevésen alapult. Egy TERMINÁLIS (nem-
        # resumable) CHECKPOINT_STOPPED esetén (keep_thread=False) viszont
        # a "Kész" továbbra is helyes — a session ténylegesen véget ért.
        # R1 B6, F-31: a lent kiírt státuszszöveg (a `message` később
        # popupszöveggé alakul), a tartós minősítő utótaghoz.
        status_bar_message: str | None = None
        if result.status != "FAULT" and not keep_thread:
            # Teljes kódbázis review 6. kör (2026-08-29), GUI/UX/Állapot P2
            # lelet #2: ez a feltétel korábban minden nem-FAULT terminális
            # állapotra (DONE ÉS a kézi Stop gombbal megszakított STOPPED
            # is) egységesen "Kész"-t írt a státuszsorba — DONE-nál van
            # kiegészítő "Teszt befejezve" popup, FAULT-nál kritikus
            # dialógus, STOPPED-nél viszont SEMMILYEN megkülönböztető
            # jelzés (a report.json helyesen DEGRADED-et ír, ez tisztán
            # GUI-kommunikációs rés volt). "Kész" csak valódi DONE-nál
            # (és a terminális, nem-resumable CHECKPOINT_STOPPED-nél,
            # ahol a session ténylegesen sikeresen véget ért) maradjon.
            status_label = (
                "Leállítva (kézi Stop)" if result.status == "STOPPED" else "Kész"
            )
            message = (
                f"{status_label} | Töltve: {result.total_charge_ah:.4f} Ah | "
                f"Kisütve: {result.total_discharge_ah:.4f} Ah"
            )
            if result.status == "CHECKPOINT_STOPPED":
                # Kör 21 F11 (F21-G4, DFAM-018): a fenti "Kész" a TERMINÁLIS
                # (nem-resumable) CHECKPOINT_STOPPED-nél HELYES marad -- a
                # session tényleg véget ért, l. fenti komment és a round-5
                # döntés, EZ NEM annak a döntésnek a felülbírálása. De MAGA
                # a "Kész" szó törölte az _on_checkpoint_reached() által
                # MÁR beállított, fennálló BQ-teendő-emlékeztetőt, holott a
                # kezelőnek MÉG el kell végeznie a BQ-műveletet.
                #
                # `result.resume_possible` itt LOGIKAILAG garantáltan False
                # -- ha True lenne, `keep_thread` (fent) is True lenne, és
                # az `if ... not keep_thread:` fenti kapu ELSŐ ellenőrzése
                # miatt ide sosem futnánk be. Ezért csak a nem-resumable
                # ("zárd le a sessiont") szöveget épitjük, NEM egy holt
                # `if result.resume_possible: ...` ágat.
                message = f"{message} — végezd el a BQ műveletet, majd zárd le a sessiont."
            status_bar_message = message
            self._status_bar.showMessage(status_bar_message)
        if not keep_thread and result.status in ("DONE", "CHECKPOINT_STOPPED", "STOPPED", "FAULT"):
            # CF-34 (governance-v2 review-kör 1, DFAM-026 declared_policy_
            # without_consumer): ui_policy.py "recharge_required_warning"
            # csoportja korábban SENKI által nem volt lekérdezve -- a
            # DISCHARGE_ONLY/CHARACTERIZATION/OCV_SOC_CHARACTERIZATION/
            # CHEMID_CHARACTERIZATION_PBA módok mind lemerített AGM/VRLA
            # akkumulátorral zárultak, de a "Kész" popup erről semmit nem
            # jelzett -- az akku sulfatáció-kockázatnak volt kitéve, ha a
            # kezelő nem tudta/nem emlékezett rá, hogy azonnal vissza kell
            # tölteni. Most a MEGLÉVŐ, deklarált policy-t olvassuk ki.
            # Review-kör 5 (2026-09-10), GUI/UX G5-04 (P3, DFAM-026 sibling):
            # a fenti gate korábban csak DONE/CHECKPOINT_STOPPED-re nézett --
            # egy kézi Stop vagy FAULT ugyanúgy mélykisütött állapotban
            # hagyhatja az akkut (pl. a DISCHARGE fázis közepén), csak épp
            # nem a tervezett utolsó lépésnél állt meg. A recharge/physical-
            # cycle-only figyelmeztetés a FIZIKAI akku-állapotot jelzi, nem
            # a session befejezési módját, ezért STOPPED/FAULT-nál is
            # ugyanúgy meg kell jelennie.
            from Prog.gui.ui_policy import is_group_visible
            # Kör 18 (2026-09-16), GUI/UX reviewer P3-1: a fejsor korábban
            # feltétel nélkül "Teszt kész — {status}"-t írt, tehát egy
            # FAULT/STOPPED session dialógusa "Test done" szöveggel
            # nyílt -- pontosan az a hamis-befejezés szöveg, amit a
            # :1581 status-bar testvér (6. kör, 2026-08-29) már kifejezetten
            # kiküszöbölt. Ugyanaz a fejsor-szabály itt is.
            if result.status == "STOPPED":
                header = "Teszt befejezve — Leállítva (kézi Stop)"
            elif result.status == "FAULT":
                header = "Teszt befejezve — HIBA (FAULT)"
            else:
                header = "Teszt kész"
            message = (
                f"{header}\n"
                f"Töltve: {result.total_charge_ah:.4f} Ah\n"
                f"Kisütve: {result.total_discharge_ah:.4f} Ah"
            )
            # Review-kör 2 (2026-09-09), friss GUI/UX F1 (P1): a session
            # MÁR kiszámolt minősítő mezői (capacity_result_quality,
            # BQ-learning eredmény, GOLDEN_GG csomag-készültség,
            # warning_code_counts) korábban SOHA nem jutottak el ide -- egy
            # DEGRADED vagy BQ_LEARNING_FAILED/AWAITING minősítésű, akár
            # 20-30 órás fizikai ciklus is minősítés nélküli "Kész"-ként
            # jelent meg (CLAUDE.md §7 "Fizikai ciklus ≠ sikeres BQ
            # learning", .claude/rules/10-safety.md #10 "Hamis PASS/DONE
            # magasabb prioritású"). session_meta a MÁR elkészült
            # report.json forrása (_write_report() adja vissza
            # _finalize_terminal_session()-en át) -- nincs új számítás,
            # csak megjelenítés.
            is_degraded = False
            if session_meta is not None:
                quality = session_meta.get("capacity_result_quality")
                if quality and quality != "OK":
                    message += f"\n\nMinőség: {quality}"
                    is_degraded = True
                bq_result = session_meta.get("bq_learning_result")
                if bq_result and bq_result != "BQ_LEARNING_VERIFIED":
                    message += f"\nBQ tanulás eredménye: {bq_result}"
                    is_degraded = True
                # Teljes kódbázis review 2. kör (2026-09-15, R2-10, DFAM-026
                # 5. érintése, DFAM-016 rokona): a fenti bq_result sor közli,
                # hogy a tanulás nem VERIFIED, de NEM mondja meg, hogy a
                # FIZIKAI chipen ideiglenes Cell BL Set Volt Threshold
                # maradhatott ÉS a chip UNSEALED lehet. A `pending_bq_df_
                # restore` checkpoint-csatorna (l. TraditionalLearning
                # Controller.pending_bq_df_restore, ill. main_window.py::
                # _review_incomplete_sessions_on_startup(),
                # session_recovery.py 312-331) KIZÁRÓLAG a
                # report.json NÉLKÜLI (crash) sessionöket fedi -- a
                # normálisan lezárt, de sikertelen restore-ú sessiont senki
                # nem jelezte a kezelőnek. A prefix-szűrés SZÁNDÉKOSAN szűk:
                # csak a Cell BL visszaírás és a SEALED-állapot flagjei --
                # egy jövőbeli, tisztán metrológiai learning-flag NE váltsa ki.
                bq_flags = session_meta.get("bq_learning_quality_flags") or []
                bq_chip_flags = [
                    f for f in bq_flags
                    if f.startswith(("BL_THRESHOLD_RESTORE_FAILED", "BQ_SEAL_"))
                ]
                if bq_chip_flags:
                    message += (
                        "\n\nFIGYELEM — a BQ34Z110 chip fizikai állapota nem "
                        "igazoltan állt vissza: " + ", ".join(bq_chip_flags)
                        + ". A Cell BL Set Volt Threshold ideiglenes "
                        "learning-értéken maradhatott és a chip UNSEALED "
                        "lehet; a chip újrahasználása ELŐTT kézi ellenőrzés/"
                        "visszaállítás szükséges (Prog/tools/"
                        "dump_bq_data_flash_table.py)."
                    )
                    is_degraded = True
                if "golden_gg_package_ready_for_ti_submission" in session_meta and not (
                    session_meta["golden_gg_package_ready_for_ti_submission"]
                ):
                    message += "\nGOLDEN_GG csomag NEM kész TI beküldésre."
                    is_degraded = True
                # Teljes kódbázis review 2. kör (2026-09-15, R2-11a, 14.
                # writer-kör, DFAM-026 4. előfordulása): a golden_gg_quality_
                # flags korábban SOHA nem jutott el a kezelőig -- egy
                # sikertelen, visszaellenőrzött reseal (BQ_SEAL_RESTORE_
                # FAILED_AT_EXPORT_PACKAGE) egy egyébként sikeres sessionön
                # csak a tranziens live event-feedben jelent meg, a chip
                # fizikailag UNSEALED maradhatott jelzés nélkül. A prefix-
                # szűrés SZÁNDÉKOSAN szűk: csak a chip biztonsági állapotára
                # (SEALED/UNSEALED) és a BQ-transzport elérhetőségére
                # vonatkozó flagek váltanak ki figyelmeztetést -- egy
                # jövőbeli, tisztán metrológiai GG-flag NE tegye ezt. A
                # golden_gg_controller.py-ban nincs `_classify_result()`-
                # szerű eredmény-klasszifikáló függvény (l. _check_bq_
                # transport_escalation() docstringje, 1261-1268. sor): a
                # self._quality_flags-et SEHOL nem olvassa vissza a
                # controller egyetlen döntéshez sem, és a
                # package_ready_for_ti_submission mezőt is a hívó `_ensure_
                # bq_sealed_or_flag()` visszatérési értékétől függetlenül
                # állítja -- ez a fix KIZÁRÓLAG a megjelenítést pótolja.
                gg_flags = session_meta.get("golden_gg_quality_flags") or []
                gg_seal_flags = [
                    f for f in gg_flags if f.startswith(("BQ_SEAL_", "BQ_TRANSPORT_"))
                ]
                if gg_seal_flags:
                    message += (
                        "\n\nFIGYELEM — a BQ34Z110 chip SEALED állapotának "
                        "visszaállítása nem igazolt ezen a sessionön: "
                        + ", ".join(gg_seal_flags)
                        + ". A chip UNSEALED lehet; kézi ellenőrzés szükséges."
                    )
                    is_degraded = True
                # Kör 17 MAX-effort review, R17-12 (DFAM-021+DFAM-018): egy
                # érvénytelen IEC DC belső ellenállás mérés (Ri_ohm/Isc_A
                # None-nal) korábban "Kész"-ként jelent meg minősítés
                # nélkül -- l. a session_meta["Ri_DC_IEC_quality"] fenti
                # kommentje a forrás-ellenőrzött indoklásért, miért CSAK a
                # quality (NEM a timing_degraded) váltja ki ezt a sort.
                iec_quality = session_meta.get("Ri_DC_IEC_quality")
                if iec_quality and iec_quality != "OK":
                    message += f"\nIEC DC belső ellenállás mérés minősége: {iec_quality}"
                    is_degraded = True
                # Kor 18, Batch C12 (METRO-P2-1, DFAM-021+DFAM-018): l. a
                # fenti ocv_soc_point_quality_counts kommentje. KORREKCIO
                # (fuggetlen MAX verifier P1 lelete, ELOZO allitas HAMIS
                # volt): a DEGRADED_TIMING_TOLERANCE MINOSITES az IEC
                # timing_degraded-jevel PONTOSAN AZONOS alakzat -- a
                # verifier vegponttol-vegpontig szimulacioval (valos
                # OcvSocController + PulseMeasurementService, realisztikus
                # ora-lepesekkel, nem a teszt-fixture idealizalt monoton
                # oraval) igazolta, hogy alapertelmezett runner_tick_s=2.0s
                # mellett a t1_actual_s STRUKTURALISAN kb. 1.0-2.1s-mal
                # kesik a celidohoz kepest (5/5 realisztikus eset, 89/112
                # egy szeles parameter-sweepben), tehat a legtobb/minden
                # alapertelmezett-configu OCV/SOC session MINDEN pontja
                # DEGRADED_TIMING_TOLERANCE-t kapna -- pontosan az az
                # alarm-fatigue kockazat, amit az IEC timing_degraded
                # kizarasa eppen elkerult. A TOBBI 8 minositesi kategoria
                # (CUTOFF_DURING_PULSE/SKIPPED_LOW_VOLTAGE/DEGRADED_DMM_
                # LOSS_DURING_RELAX/DEGRADED_SETPOINT_CURRENT/DEGRADED_
                # BASELINE_CURRENT_ASSUMED/INVALID_PULSE_RESISTANCE/
                # CURRENT_TRACE_INCOMPLETE/VOLTAGE_TRACE_INCOMPLETE) VALODI,
                # eseti anomaliat jeleznek (a verifier ezt is fuggetlenul
                # megerositette) -- CSAK a DEGRADED_TIMING_TOLERANCE kerul
                # kizarasra a popup-kapubol, 1:1 tukrozve az IEC precedenst.
                # A mezo VALTOZATLANUL bekerul a report.json/ocv_soc_table.
                # csv-be (post-hoc elemzeshez), csak a popup-gate-bol
                # marad ki -- l. WORK_STATE.json "IEC preflight-kapu
                # runner_tick_s x sample_delay_2_s" DECISION_REQUIRED
                # tetelet, amely most az OCV/SOC testver-elofordulasat is
                # tartalmazza.
                _OCV_SOC_POPUP_GATE_EXCLUDED_QUALITY = "DEGRADED_TIMING_TOLERANCE"
                ocv_soc_quality_counts = session_meta.get("ocv_soc_point_quality_counts") or {}
                degraded_ocv_soc_points = {
                    q: c for q, c in ocv_soc_quality_counts.items()
                    if q not in ("OK", _OCV_SOC_POPUP_GATE_EXCLUDED_QUALITY) and c
                }
                if degraded_ocv_soc_points:
                    message += "\nOCV/SOC pont-minőség: " + ", ".join(
                        f"{q}×{c}" for q, c in degraded_ocv_soc_points.items()
                    )
                    is_degraded = True
                # D9/F-06 (2026-09-13): a sikeres GPCCHEM-export útvonala
                # korábban sehol nem jelent meg az operátor számára -- ez
                # informatív sor, NEM degraded-jelző (a CHEMID export-hiba
                # már a FAULT-úton jelenik meg, külön).
                chemid_zip_path = session_meta.get("chemid_export_zip_path")
                if chemid_zip_path:
                    message += f"\nGPCCHEM csomag: {chemid_zip_path}"
                warning_counts = session_meta.get("warning_code_counts") or {}
                if warning_counts:
                    message += "\nFigyelmeztetések: " + ", ".join(
                        f"{code}×{count}" for code, count in warning_counts.items()
                    )
            else:
                # Review-kör 4/5 GUI F4 (P2, DFAM-026 sibling): session_meta
                # None azt jelenti, hogy _write_report() kivétellel bukott --
                # a hívó (_write_report() except ága) ekkor MÁR mutat egy
                # külön, explicit "Riport generálás hiba" QMessageBox.warning-
                # ot, DE ez az itteni, KÖZVETLENÜL utána megjelenő "Teszt
                # befejezve" popup korábban feltétel nélkül .information()-t
                # használt, minősítés/említés nélkül -- az operátor egy
                # "minden rendben" jellegű üzenetet látott KÖZVETLENÜL az
                # előző hibaüzenet után, ami félrevezető (a session
                # report.json-ja ténylegesen NEM készült el).
                message += (
                    "\n\nFIGYELEM: a session report.json fájlja NEM készült el "
                    "— csak a nyers mérési adatok (samples.csv) érhetők el."
                )
                is_degraded = True
            # R17-03+R17-17+V4-1 (2026-09-16, Kör 17 MAX-effort review,
            # DFAM-016+018+026, Kör 18 P2 korrekcio -- l. lent): a
            # session-végi safe-off bizonytalan eredménye korábban
            # SEMMILYEN élő-GUI vagy dialógus-csatornán nem jelent meg --
            # az operátor egy feltétel nélküli "Kész" popupot látott,
            # akkor is, ha a PSU/Load OFF parancs NEM igazoltan sikeres.
            # SZÁNDÉKOSAN KÜLÖN figyelmeztetés a fenti BQ-sorok mellett
            # (DFAM-018 semantic-collapse-avoidance: a chip biztonsági
            # állapota és a PSU/Load kimeneti állapota KÉT KÜLÖN
            # kockázat). Kör 18 (2026-09-16, GUI/UX + session/workflow
            # reviewer, KONVERGENS lelet -- két független reviewer
            # egymástól függetlenül találta meg): ez a blokk KORÁBBAN a
            # fenti `if session_meta is not None:` ágon BELÜL állt, holott
            # `result.safe_off_attempted`/`result.safe_off_result` a
            # `TestResult`-ból jön, NEM a `session_meta`-ból -- ha
            # `_write_report()` kivétellel bukott (session_meta is None),
            # ÉPP a legrosszabb esetben (nincs report.json SEM) tűnt el a
            # figyelmeztetés. Most feltétel nélkül fut, mindkét ágon.
            # Emellett a P3-2 lelet szerint `safe_off_attempted is False`
            # (safe-off SOHA nem lett megkísérelve -- a startup-dialógus
            # testvér-ága ezt MÁR külön "SAFE-OFF BIZONYÍTÉK HIÁNYZIK"
            # sorral kezeli) korábban néma volt itt -- most külön,
            # megkülönböztetett sort kap, nem összeolvasztva az
            # "uncertain" esettel (ugyanaz a DFAM-018 elv).
            if not result.safe_off_attempted:
                message += (
                    "\n\nFIGYELEM — a session végi biztonságos "
                    "lekapcsolás SOHA nem lett megkísérelve (nincs "
                    "safe-off bizonyíték). A kimenet ENERGIZÁLT "
                    "maradhatott; ellenőrizd fizikailag a műszereket."
                )
                is_degraded = True
            else:
                uncertain_devices = [
                    n for n in ("PSU", "Load")
                    if not (result.safe_off_result or {}).get(n, False)
                ]
                if uncertain_devices:
                    message += (
                        "\n\nFIGYELEM — a session végi biztonságos "
                        "lekapcsolás NEM igazolt: "
                        + ", ".join(uncertain_devices)
                        + ". A kimenet ENERGIZÁLT maradhatott; ellenőrizd "
                        "fizikailag a műszereket. (A kikapcsolás a "
                        "parancseredmény vagy a safe-off utáni "
                        "állapotellenőrzés alapján nem igazolt.)"
                    )
                    is_degraded = True
            # D9/F-08 (2026-09-13, Riportok/ui_policy_audit_20260913.md):
            # is_group_visible() ismeretlen test_type-ra (pl. a `_active_
            # test_type` __init__-beli "" kezdőértékére) fail-open módon
            # MINDEN csoportra True-t ad -- ma nem reprodukálható élesen
            # (_build_runner() a cfg.test_type-ot egy ValueError-ral védett
            # dispatch-ágon MÁR leellenőrizte, mielőtt self._active_test_
            # type=cfg.test_type futna, tehát idáig csak sikeresen
            # felismert test_type jut el), de egy jövőbeli, a dispatch-
            # ágból kimaradó test_type-nál csendben hibás lenne -- a
            # kezelő egy szándékosan csak BQ_LEARNING_PHYSICAL-ra szóló
            # figyelmeztetést kapna olyan módra is, ahol az irreleváns. Az
            # explicit `self._active_test_type and` guard a fail-open
            # alapértéket nem módosítja (az is_group_visible() más
            # hívóhelyein, pl. config_panel.py, változatlan marad), csak
            # ezt a 2, safety-releváns figyelmeztetés-gate-et teszi
            # fail-closeddá ismeretlen/üres test_type-ra.
            if self._active_test_type and is_group_visible(
                self._active_test_type, "recharge_required_warning"
            ):
                if self._active_test_type == "TRADITIONAL_LEARNING":
                    completion = getattr(
                        getattr(self, "_last_traditional_learning_ctrl", None), "tlc_completion", None,
                    )
                    final_recharge = completion.get("final_recharge") if isinstance(completion, dict) else None
                    completed = self._tlc_recharge_completed(completion)
                    if completed and isinstance(final_recharge, dict):
                        message += (
                            f"\n\nIgazolt záró újratöltés vége (UTC): {final_recharge['completed_at_utc']}."
                            f"\n48 h megfigyelés; következő kampány legkorábban (UTC): "
                            f"{final_recharge.get('next_campaign_not_before_utc')}."
                        )
                    else:
                        message += (
                            "\n\nFIGYELEM: a záró újratöltés nincs igazolva — "
                            "ellenőrzött újratöltés szükséges; a 48 h kezdőpontja nem ismert."
                        )
                else:
                    message += (
                        "\n\nFIGYELEM: az akkumulátor kisütött állapotban maradt — "
                        "a szulfatáció elkerülése érdekében azonnal vissza kell tölteni."
                    )
            # Review-kör 2 (2026-09-08), GUI/UX F1/F2 lelet (DFAM-026
            # declared_policy_without_consumer, a CF-34/recharge_required_
            # warning PONTOS testvére): ui_policy.py "physical_cycle_only_
            # warning" csoportja (BQ_LEARNING_PHYSICAL módra deklarálva)
            # korábban SENKI által nem volt lekérdezve -- a kezelő a "Kész"
            # popupból nem tudta meg, hogy ez a mód CSAK a fizikai töltés/
            # kisütés ciklust futtatta le, BQ-oldali (Qmax/Ra/UpdateStatus)
            # tanulás-ellenőrzés NÉLKÜL (CLAUDE.md §7: "Fizikai ciklus ≠
            # sikeres BQ learning"). Review-kör 2 (2026-09-09), friss GUI/UX
            # F2: ez a fix korábban HOLT volt, mert BQ_LEARNING_PHYSICAL
            # szerkezetileg soha nem éri el a status=="DONE"-t (utolsó lépése
            # MANUAL_CHECKPOINT, tehát terminálisan CHECKPOINT_STOPPED-del
            # zár) -- a gate fenti bővítése (not keep_thread and status in
            # (DONE, CHECKPOINT_STOPPED)) teszi elérhetővé éles futásban.
            if self._active_test_type and is_group_visible(
                self._active_test_type, "physical_cycle_only_warning"
            ):
                message += (
                    "\n\nFIGYELEM: ez a mód KIZÁRÓLAG a fizikai töltés/kisütés "
                    "ciklust futtatta le — ez ÖNMAGÁBAN NEM jelenti a BQ34Z110 "
                    "tanulás sikerét. A tényleges learning-eredményhez BQ-oldali "
                    "(Qmax/Ra/UpdateStatus) ellenőrzés szükséges."
                )
            # R1 B6, F-31 (DFAM-018): a popup bezárása után a tartós
            # státuszsor egy minősített eredménynél is feltétel nélkül
            # "Kész | ..." maradt. A MÁR kiszámolt is_degraded alapján
            # utótag -- csak a "Kész"-címkéjű státuszokra (DONE, terminális
            # CHECKPOINT_STOPPED; D-B6-3). A BQ-teendő szöveg megmarad.
            status_suffix = ""
            if (
                is_degraded
                and status_bar_message is not None
                and result.status in ("DONE", "CHECKPOINT_STOPPED")
            ):
                # R2-B6 (R2-GUI-03): riport-kudarcnal nem hivatkozhat report.json-ra.
                if session_meta is None:
                    status_suffix = (
                        " — FIGYELEM: a report.json NEM készült el, "
                        "l. a záró üzenetet"
                    )
                else:
                    status_suffix = (
                        " — FIGYELEM: minősített eredmény, "
                        "l. a záró üzenetet és a report.json-t"
                    )
                self._status_bar.showMessage(f"{status_bar_message}{status_suffix}")
            self._terminal_status_suffix = status_suffix
            if is_degraded:
                QMessageBox.warning(self, "Teszt befejezve", message)
            else:
                QMessageBox.information(self, "Teszt befejezve", message)
        elif result.status == "CHECKPOINT_STOPPED":
            pass  # resumable ág (keep_thread=True): tab és status bar már frissítve az _on_checkpoint_reached-ben

        if not keep_thread:
            # S5-04 -- l. fenti komment: a set_status() már engedélyezte
            # (vagy engedélyezni fogja) a Start gombot, ezt itt, a metódus
            # legvégén EXPLICIT módon cleanup_ok-hoz igazítjuk -- MINDKÉT
            # irányban (nem csak False-nál), mert FAULT esetén a `fault`
            # jel (l. _on_fault()) ELŐBB fut le, és ha AZ a hívás
            # timeoutolt (Start letiltva), de ez a MÁSODIK, itteni hívás
            # már sikeres (a szál időközben leállt), a Start gombnak
            # helyesen újra engedélyezettnek kell lennie -- nem maradhat
            # örökre letiltva egy korábbi, azóta megoldódott timeout miatt.
            self._live_panel.set_start_enabled(cleanup_ok)
            if not cleanup_ok:
                self._live_panel.append_event({
                    "event_code": "WORKER_THREAD_CLEANUP_TIMEOUT",
                    "event_message": (
                        "A teszt-szál nem állt le 5 másodpercen belül a "
                        "session befejezése után -- a Start gomb "
                        "biztonsági okból tiltva marad, amíg ez be nem "
                        "fejeződik."
                    ),
                    "severity": "CRITICAL",
                })

    def _finalize_terminal_session(self, result) -> dict | None:
        """Közös takarítás minden VALÓS terminális állapothoz — DONE/FAULT/
        STOPPED/terminális CHECKPOINT_STOPPED (_on_finished()-ből), ÉS egy
        resumable checkpointból "Teszt lezárása"-val véglegesen bezárt
        session (_on_checkpoint_close()-ból, l. ott a docstringet). Korábban
        (teljes kódbázis review 2. kör, 2026-08-28, GUI/UX P1 lelet F3) az
        utóbbi út EGYIKÉT sem végezte el — a VISA/soros kapcsolatok nyitva
        maradtak, az "Eszközök keresése" gomb véglegesen letiltva maradt, és
        a session sosem kapott report.json-t (session_recovery.py::
        discover_incomplete_sessions() ezért crashelt/megszakadt sessionként
        mutatta volna, és find_resumable_session() akár resume-ra is
        felkínálhatta volna egy MÁR lezártnak szánt sessiont).

        Review-kör 2 (2026-09-09), friss GUI/UX F1: visszaadja a
        _write_report() session_meta dict-jét (vagy None-t), hogy az
        _on_finished() DONE/terminális-CHECKPOINT_STOPPED popupja a MÁR
        kiszámolt minősítő mezőket megjeleníthesse — l. _write_report()
        docstringje. Az _on_checkpoint_close()-beli hívás a visszatérési
        értéket szándékosan nem használja (ott nincs popup)."""
        if self._instruments is not None:
            # Release-audit 2026-08-23 (2.10): a session ténylegesen véget
            # ért — a műszer-kapcsolatokat itt kell lezárni. Korábban ez
            # sosem történt meg explicit módon a GUI-rétegben, minden Start
            # új InstrumentManager-t hozott létre, a régi VISA/soros
            # kapcsolatok GC-re bízva maradtak.
            _disconnect_for_cleanup(self._instruments, self._live_panel.append_event)
            # R1 B6, F-09 (DFAM-016): a lezárt managerre mutató referencia
            # miatt egy későbbi idle excepthook a bontott kapcsolatokon adott
            # ki safe-offot (hamis "NEM igazolt" CRITICAL). Az OFF-
            # bizonytalanság NEM vész el: a `result` (report.json, popup)
            # hordozza.
            self._instruments = None
        # R1 B6, F-08 (DFAM-001): a session lezárult -- a checkpoint panel
        # "Safe Off" gombja innen hatástalan lenne (mindkét hívóút:
        # _on_finished() és _on_checkpoint_close()).
        _cleanup_action(
            "Checkpoint-panel lezárása", self._checkpoint_panel.set_session_finalized,
            self._live_panel.append_event,
        )
        # WP-AK-16: a TRADITIONAL_LEARNING/GOLDEN_GG session PicoBQTransport-ja
        # csak terminális állapotban szabadul fel (nem CHECKPOINT_STOPPED+
        # resume_possible esetén) - amíg a session folytatható, a portot nem
        # engedjük át a BQ Diagnosztika/Kalibráció paneleknek.
        _cleanup_action(
            "BQ-transport lezárása", self._close_session_bq_transport,
            self._live_panel.append_event,
        )
        # A panel hibája sem akadályozhatja a végleges riportot.
        _cleanup_action(
            "Konfigurációs panel lezárása",
            lambda: self._config_panel.set_session_active(False), self._live_panel.append_event,
        )
        return self._write_report(result)

    def _write_report(self, result) -> dict | None:
        """Összefoglaló report.json írása a session mappába. Visszaadja a
        felépített session_meta dict-et (l. lent, a DONE/terminális-
        CHECKPOINT_STOPPED popup minősítő mezőinek forrása -- Review-kör 2,
        friss GUI/UX F1), vagy None-t, ha nem készült el (nincs
        session_dir, vagy a riport-generálás kivétellel bukott).
        DATA-003: minden terminális állapotra (DONE/FAULT/STOPPED/terminális
        CHECKPOINT_STOPPED) — nem csak sikeres befejezéskor."""
        if self._session_dir is None:
            return None
        try:
            from Prog.src.report_generator import ReportGenerator
            rg = ReportGenerator()
            # WP-AK-02 review-lelet (Important/P1, REC-003): ha ez a session
            # egy megszakadt session cross-process folytatása volt, a
            # riportnak jeleznie kell — a felhalmozott Ah span-elhet egy
            # process-crash-en, a metrológiai folytonosság nem bizonyított
            # olyan szigorúan, mint egy meg nem szakadt session esetén.
            parent_session_id = getattr(self, "_parent_session_id", "")
            is_resumed = bool(parent_session_id)
            # Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
            # Adatintegritás P2 lelet: a capacity_result_quality (lent)
            # korábban NEM vette figyelembe a golden_gg/chemid relax-fázisok
            # alatti DMM-hibaszámlálót (relax_dmm_fault_total, 10. kör
            # `25950b3` óta a session_meta-ban), mert azt a dict LÉTREHOZÁSA
            # UTÁN, egy KÉSŐBBI feltételes blokkban vette fel — a
            # capacity_result_quality string ekkor már véglegesítve volt.
            # A ggc/cic referenciákat ezért ide, a dict elé húzva a
            # minősítés is figyelembe tudja venni.
            ggc = self._last_golden_gg_ctrl
            cic = self._last_chemid_ctrl
            # Teljes kódbázis review 12. kör (2026-08-31), Szoftver/
            # Architektúra P2 lelet: a tlc referenciát is ide, a
            # session_meta dict elé kellett húzni (a ggc/cic mintáját
            # követve, l. fenti 11. körös komment) — a
            # TraditionalLearningController RELAX_1/RELAX_2H fázisai
            # ugyanazt a beágyazott RelaxController-t használják, mint
            # golden_gg/chemid, de a relax_dmm_fault_total property
            # korábban hiányzott róla, ezért a minősítés sosem látta.
            tlc = self._last_traditional_learning_ctrl
            # Teljes kódbázis review 14. kör (2026-09-04), Szoftver/
            # Architektúra + Session/Workflow/Állapotgép P2 lelet: az osc
            # referenciát is ide, a session_meta dict elé kellett húzni (a
            # ggc/cic/tlc mintáját követve) — az OcvSocController a
            # negyedik testvér, amelynek a relax_dmm_fault_total property-je
            # eddig hiányzott, holott az OCV_SOC session futtatja a
            # leghosszabb (2-5 órás) relax-fázisokat.
            osc = self._last_ocv_soc_ctrl
            # Governance-v2 kör 2: az IecDcResistanceController az ötödik
            # összetett controller — ugyanazt a phase_elapsed_s/charge_config
            # mintát követi, mint ggc/cic/tlc/osc (l. lentebbi tuple-bővítés).
            iec = self._last_iec_dc_resistance_ctrl
            # Teljes kódbázis review 11. kör (2026-08-31), Metrológia/
            # Adatintegritás P2 lelet: result.relax_dmm_fault_count (a
            # standalone StepKind.RELAX RelaxController session-szintű
            # összege, l. test_runner.py _flush_totals()) ugyanabba az
            # összegbe tartozik, mint a beágyazott golden_gg/chemid
            # relax-fázisok DMM-hibaszámlálója — mindegyik ugyanazt jelzi:
            # a relax alatti akkufeszültség-mérés megbízhatóságát.
            relax_dmm_fault_total = (
                getattr(ggc, "relax_dmm_fault_total", 0)
                + getattr(cic, "relax_dmm_fault_total", 0)
                + getattr(tlc, "relax_dmm_fault_total", 0)
                + getattr(osc, "relax_dmm_fault_total", 0)
                # MAX verifier lelet (governance-v2 kör 2, F5): az
                # IecDcResistanceController az ötödik testvér, ugyanazzal
                # a relax_dmm_fault_total property-vel.
                + getattr(iec, "relax_dmm_fault_total", 0)
                + result.relax_dmm_fault_count
            )
            # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás
            # NEW-2 (DFAM-024 2. előfordulás + DFAM-021): a chemid/
            # golden_gg/iec_dc_resistance controllerek egy sikertelen
            # hőmérséklet-DMM-olvasást a korábbi (vagy konstruktor-default
            # 20.0) értékkel helyettesítettek CSENDBEN, quality="OK"-ként
            # exportálva -- a capacity_result_quality gate ezt korábban
            # egyáltalán nem látta (a _relax_dmm_fault_total-mintát csak a
            # feszültség-DMM-kiesésre vezették be). L. lent a per-
            # controller *_temp_dmm_fault_ticks mezőket is.
            temp_dmm_fault_ticks_total = (
                getattr(ggc, "temp_dmm_fault_ticks", 0)
                + getattr(cic, "temp_dmm_fault_ticks", 0)
                + getattr(iec, "temp_dmm_fault_ticks", 0)
                # Review-kör 3 (2026-09-09), friss Metrológia M3-02 /
                # Szoftver/Architektúra F2 (P2): a tlc/osc testvérek
                # korábban hiányoztak erről a property-ről és emiatt
                # erről az összegről is -- l. a controllerek saját
                # temp_dmm_fault_ticks property docstringje.
                + getattr(tlc, "temp_dmm_fault_ticks", 0)
                + getattr(osc, "temp_dmm_fault_ticks", 0)
            )
            # Teljes kódbázis review 22. kör (2026-09-07), Metrológia/
            # Adatintegritás P3 lelet: a report_generator.py régóta
            # exponálja a psu_mode_max_voltage_V/current_A mezőket a
            # riport tetején (session_meta.get()-tel), de session_meta-ba
            # egyik production hívóhely sem tette be a hozzájuk tartozó
            # ÉRTÉKET — a riportban mindig null volt. Keithley 2220-30-1
            # hardver limitek (l. charge_controller.py psu_mode_max/
            # psu_hw_max_A dict-jei ugyanezekkel az értékekkel).
            # Governance-v2 review-kör 1 (CF-15/DFAM-017 3. előfordulás,
            # batch 4.5): a fenti 2 dict korábban itt, önálló function-
            # scoped másolatként élt — a kanonikus forrás mostantól
            # derived_parameters.PSU_HW_MAX_VOLTAGE_V/PSU_HW_MAX_CURRENT_A
            # (a charge_controller.py saját, biztonsági döntési célú
            # másolata SZÁNDÉKOSAN külön marad, l. SUP02-ARCH-004 komment
            # ott — ez a riport-metaadat út, nem aktív vezérlési döntés).
            from Prog.gui.derived_parameters import (
                PSU_HW_MAX_CURRENT_A,
                PSU_HW_MAX_VOLTAGE_V,
            )
            psu_mode_str = getattr(self, "_psu_mode_str", "UNKNOWN")
            session_meta = {
                "status": result.status,
                "reason": result.reason,
                "psu_mode": psu_mode_str,
                "psu_mode_max_voltage_V": PSU_HW_MAX_VOLTAGE_V.get(psu_mode_str),
                "psu_mode_max_current_A": PSU_HW_MAX_CURRENT_A.get(psu_mode_str),
                # Teljes kódbázis review 20. kör (2026-09-06), Metrológia/
                # Adatintegritás P1 lelet — l. self._temperature_compensation_
                # mode_str beállításának kommentje (_build_runner()).
                "temperature_compensation_mode": getattr(
                    self, "_temperature_compensation_mode_str", "MONITOR_ONLY"
                ),
                # R9-B4: a session OCV-controllerenek tenyleges kapacitasalapja.
                "SOC_estimate_method": {
                    "MEASURED_CAPACITY_BASED": "coulomb_counting_from_measured_capacity",
                    "NOMINAL_CAPACITY_BASED_SOC": "coulomb_counting_from_nominal_capacity",
                }.get(getattr(osc, "capacity_basis_quality", "")),
                "total_charge_ah": result.total_charge_ah,
                "total_discharge_ah": result.total_discharge_ah,
                # Teljes kódbázis review 17. kör (2026-09-05), Metrológia/
                # Adatintegritás P2 lelet: az Ah-testvérpár Wh-oldala —
                # korábban a TestResult sem exponálta, ezért egy teljes
                # session energiamérlege (Wh) sosem jutott el a report.json-ig.
                "total_charge_wh": result.total_charge_wh,
                "total_discharge_wh": result.total_discharge_wh,
                # Governance-v2 review-kör 1 (CF-18/DFAM-022, batch 4.3): a
                # TELJES, megszakítás(ok)on átívelő session időtartama —
                # l. TestRunner._session_start_time kommentje.
                "total_session_duration_s": result.total_session_duration_s,
                "active_phase": result.active_step_label,
                # CF-19 (governance-v2 kör 2, DFAM-023): "active_phase" fent
                # mindig az OPAK top-level TestStep címke (pl. "golden_gg"),
                # sosem a controller tényleges belső állapota -- ez a két új
                # mező a ggc/cic/tlc/osc referenciák valamelyikéből (amelyik
                # nem None -- egy session pontosan egy összetett controllert
                # használ) a TÉNYLEGES utolsó belső állapotot és az abban
                # eltöltött időt adja hozzá, az active_phase MELLÉ (nem
                # helyette -- visszafelé kompatibilis). Sima (nem összetett)
                # StepKindek esetén mindkét mező None marad.
                "active_internal_phase": next(
                    (
                        getattr(getattr(c, "state", None), "value", None)
                        for c in (ggc, cic, tlc, osc, iec)
                        if c is not None and getattr(c, "state", None) is not None
                    ),
                    None,
                ),
                "active_internal_phase_elapsed_s": next(
                    (
                        getattr(c, "phase_elapsed_s", None)
                        for c in (ggc, cic, tlc, osc, iec)
                        if c is not None and getattr(c, "phase_elapsed_s", None) is not None
                    ),
                    None,
                ),
                "safe_off_attempted": result.safe_off_attempted,
                "safe_off_result": result.safe_off_result,
                # Governance-v2 review-kör (MAX verifier V4-1, P2): a fenti
                # két mező a session LEZÁRÁSAKOR kiadott safe-off friss
                # eredménye. Egy BQ checkpointnál parkolt sessionnél a
                # checkpoint ELÉRÉSEKOR (test_runner.py::_run_manual_
                # checkpoint()) kiadott safe-off egy KÜLÖN,
                # önálló bizonyíték arról, hogy a fizikai ciklus
                # biztonságos állapotban állt le -- ez korábban semmilyen
                # perzisztens artefaktumban nem maradt meg, miután a
                # lezárás a saját, friss értékeire cserélte a mezőket.
                # SZÁNDÉKOSAN külön néven: a kettő NEM ugyanaz a mérés,
                # és nem cserélhető fel (CLAUDE.md §7 "setpoint,
                # commanded state és readback külön fogalom" szellemében).
                "checkpoint_safe_off_attempted": getattr(
                    self, "_checkpoint_safe_off_attempted", False
                ),
                "checkpoint_safe_off_result": getattr(
                    self, "_checkpoint_safe_off_result", {}
                ),
                "battery_temperature_source": _TEMPERATURE_SOURCE_LABELS.get(
                    getattr(self, "_temperature_sensor_mode", "RTD_4W_PT100"),
                    "DMM2_surface_PT100",
                ),
                "cell_count": getattr(self, "_cell_count", None),
                # Teljes kódbázis review 9. kör (2026-08-30), Metrológia/
                # Adatintegritás P2 lelet: korábban csak is_resumed/status
                # döntötte el a minősítést — egy DONE, nem resumed session
                # "OK"-ot kapott akkor is, ha közben nem-fatális SCPI
                # kommunikációs hiba történt vagy egy extrém (blokkoló VISA
                # I/O miatti) dt_s-tüske fordult elő (l. result.
                # extreme_dt_s_count/max_observed_dt_s, test_runner.py
                # _flush_totals() session-szintű összegzése) — ezek a
                # jelzők ugyanabban a session_meta-ban jelen voltak, de a
                # minősítés nem vette figyelembe őket (CLAUDE.md §12:
                # minden fontos eredmény kapjon quality/minősítést).
                "capacity_result_quality": (
                    "DEGRADED" if (
                        is_resumed
                        or result.status != "DONE"
                        or result.communication_faults_count > 0
                        or result.extreme_dt_s_count > 0
                        or relax_dmm_fault_total > 0
                        or result.temperature_out_of_range_count > 0
                        # Review-kör 2 (2026-09-09), friss Metrológia/
                        # Adatintegritás NEW-2 — l. temp_dmm_fault_ticks_total
                        # fenti komment.
                        or temp_dmm_fault_ticks_total > 0
                        # Review-kör 3 (2026-09-09), MAX verifier
                        # (a4d9e82debe0bdfcb) P2, DFAM-001 4. előfordulása: a
                        # fenti temp_dmm_fault_ticks_total csak az 5 összetett
                        # controllert fedi -- egy önálló StepKind.CHARGE/
                        # DISCHARGE lépés (bare Charge/DischargeController)
                        # hőmérséklet-DMM-kiesését csak a result.temp_dmm_
                        # fault_ticks (test_runner.py, minden StepKind-et
                        # lefedő delta-alapú aggregáció, l.
                        # temperature_out_of_range_count mintája) látja.
                        or result.temp_dmm_fault_ticks > 0
                        # Review-kör 2 (2026-09-09), friss Szoftver/
                        # Architektúra F2 (DFAM-018): egy önálló (nem
                        # beágyazott) StepKind.RELAX alatti tartós
                        # hőmérséklet-DMM-kiesés a RelaxController-ben
                        # TUDATOSAN csak warning-ra degradálódik (nincs
                        # aktív energiaátadás relax alatt, l. relax_
                        # controller.py::advance() docstringje) -- ez a
                        # downgrade helyes, de korábban SEHOL nem jutott el
                        # a capacity_result_quality-ig, mert az csak a
                        # (voltage-DMM-specifikus) relax_dmm_fault_total-t
                        # nézte. A warning_code_counts-ban MÁR jelen van
                        # (test_runner.py::_build_sample()
                        # getattr(controller, "last_warning_code")-on
                        # keresztül, RelaxController.last_warning_code
                        # property) -- ez csak azt olvassa ki.
                        or "TEMPERATURE_MONITOR_LOST_CRITICAL" in (
                            result.warning_code_counts or {}
                        )
                    ) else "OK"
                ),
                "emergency_stop_occurred": (result.status == "FAULT"),
                "error_queue_deferred_s": result.error_queue_deferred_s,
                "relax_missed_deadline_count": result.relax_missed_deadline_count,
                "communication_faults_count": result.communication_faults_count,
                "extreme_dt_s_count": result.extreme_dt_s_count,
                "max_observed_dt_s": result.max_observed_dt_s,
                "standalone_relax_dmm_fault_count": result.relax_dmm_fault_count,
                "temperature_out_of_range_count": result.temperature_out_of_range_count,
                # Review-kör 3 (2026-09-09), MAX verifier (a4d9e82debe0bdfcb)
                # P2, DFAM-001 4. előfordulása -- l. a DEGRADED-gate fenti
                # komment ugyanerről a mezőről. A per-controller attribúciós
                # kulcsok (golden_gg_temp_dmm_fault_ticks stb.) változatlanul
                # megmaradnak; ez a MINDEN StepKind-et lefedő session-szintű
                # összeg.
                "temp_dmm_fault_ticks": result.temp_dmm_fault_ticks,
                # Governance-v2 review-kör 1 (CF-20/DFAM-021, batch 4.6): a
                # 13 lehetséges WarningCode session-szintű előfordulás-
                # száma — korábban csak az UTOLSÓ aktív tick jelzése volt
                # elérhető (samples.csv warning_flags oszlopa), a
                # report.json-ból nem volt rekonstruálható, hányszor
                # fordult elő egy adott figyelmeztetés a session alatt.
                "warning_code_counts": result.warning_code_counts,
                "interrupted_session_recovered": is_resumed,
                "parent_session_id": parent_session_id,
                # DATA-006: a ténylegesen aktív profil-paraméterek (nem csak
                # egy hash) — l. self._last_profile_snapshot docstringje.
                "profile_snapshot": getattr(self, "_last_profile_snapshot", None),
            }
            # Release-audit 2026-08-23 (2.2): a BQ learning eredmény-
            # klasszifikáció (§11.6) és a GOLDEN_GG export-státusz (2.1)
            # felvétele a riportba — korábban egyik sem jutott el idáig.
            if tlc is not None and getattr(tlc, "result_summary", None) is not None:
                session_meta["bq_learning_result"] = tlc.result_summary.result
                session_meta["bq_learning_quality_flags"] = list(
                    tlc.result_summary.quality_flags
                )
            # DATA-006: a beágyazott ChargeController tényleges session-
            # specifikus konfigurációja (pl. max_charge_Ah_factor) — enélkül
            # a session log-ból nem volt visszakereshető, hogy egy adott
            # futáshoz milyen töltési/tanítási paraméterek voltak
            # ténylegesen érvényben. getattr védelemmel (nem minden tlc
            # test-double/jövőbeli variáns exponálja).
            tlc_charge_config = getattr(tlc, "charge_config", None) if tlc is not None else None
            # Teljes kódbázis review 15. kör (2026-09-04), Szoftver/
            # Architektúra P2 lelet: a GoldenGgController-nek a fenti tlc-
            # vel azonos okból (DATA-006) ugyanígy ki kell töltenie a
            # charge_config_snapshot-ot — a fenti tlc-ág és ez az ág
            # kölcsönösen kizáró (egy sessionben legfeljebb egyik fut).
            ggc_charge_config = getattr(ggc, "charge_config", None) if ggc is not None else None
            # Teljes kódbázis review 16. kör (2026-09-04), Session/Workflow
            # P2 lelet: a fenti tlc/ggc mintát a maradék két testvér
            # (ChemIdCharacterizationController/OcvSocController) is
            # megkapja — mind a 4 controller ugyanazt a DATA-006
            # traceability-igényt hordozza, és mind a 4 ág kölcsönösen
            # kizáró (egy sessionben legfeljebb egy fut).
            cic_charge_config = getattr(cic, "charge_config", None) if cic is not None else None
            osc_charge_config = getattr(osc, "charge_config", None) if osc is not None else None
            iec_charge_config = getattr(iec, "charge_config", None) if iec is not None else None
            if tlc_charge_config is not None:
                session_meta["charge_config_snapshot"] = asdict(tlc_charge_config)
            elif ggc_charge_config is not None:
                session_meta["charge_config_snapshot"] = asdict(ggc_charge_config)
            elif cic_charge_config is not None:
                session_meta["charge_config_snapshot"] = asdict(cic_charge_config)
            elif osc_charge_config is not None:
                session_meta["charge_config_snapshot"] = asdict(osc_charge_config)
            elif iec_charge_config is not None:
                session_meta["charge_config_snapshot"] = asdict(iec_charge_config)
            # Governance-v2 kör 2: az IEC 60896-21 §6.3 DC belső ellenállás
            # EGYETLEN, nem-sweep eredménye (Ri/Isc) a session-szintű
            # report.json-ba is bekerül, nem csak a dedikált
            # iec_dc_resistance_result.csv-be (l. logger.py::log_iec_dc_
            # resistance_result() ugyanerről az eredmény-dict-ről).
            iec_result = getattr(iec, "result", None) if iec is not None else None
            if iec_result is not None:
                session_meta["Ri_DC_IEC_ohm"] = iec_result.get("Ri_ohm")
                session_meta["Isc_A"] = iec_result.get("Isc_A")
                # Kör 17 MAX-effort review, R17-12 (DFAM-021+DFAM-018): a
                # mérés érvényessége (quality) és az időzítés-minősítés
                # (timing_degraded/synchronization_quality/t_sample_1_s/
                # t_sample_2_s) korábban KIZÁRÓLAG a dedikált
                # iec_dc_resistance_result.csv-be jutott el (l.
                # logger.py::IEC_DC_RESISTANCE_COLUMNS) -- a fő report.json
                # csak a nyers Ri/Isc értéket kapta meg, "quality" nélkül.
                # Egy érvénytelen mérés (INVALID_LOAD_CURRENT_PAIR/
                # INVALID_RESISTANCE/INVALID_MEASUREMENT_MISSING, Ri_ohm/
                # Isc_A=None) így "DONE"+"OK" session-ként jelent meg a
                # befejezés-popupon (l. lent a quality-gate).
                session_meta["Ri_DC_IEC_quality"] = iec_result.get("quality")
                # Az időzítés mezői a report.json-ba kerülnek, a popup
                # DEGRADED-kapujától továbbra is függetlenül. R16-B22 óta
                # a runner saját órájú IEC-blokkot használ: a 20/5 s-os
                # célminta időzítése független a külső ticktől, ezért a
                # timing_degraded nem garantált. A tényleges V-bélyeg célhoz
                # mért eltérése és a hiányos mérés minősítése a controller
                # feladata. A popup meglévő minősítési szabálya itt nem változik.
                session_meta["Ri_DC_IEC_timing_degraded"] = iec_result.get("timing_degraded")
                session_meta["Ri_DC_IEC_synchronization_quality"] = iec_result.get(
                    "synchronization_quality"
                )
                session_meta["Ri_DC_IEC_t_sample_1_s"] = iec_result.get("t_sample_1_s")
                session_meta["Ri_DC_IEC_t_sample_2_s"] = iec_result.get("t_sample_2_s")
                # Kör 21 F13 (METRO-R21-3, DFAM-021, R17-12 mintáját 1:1
                # követve): a DC belső ellenállás erősen hőmérséklet-
                # függő (rules/20-metrology.md), de a 4 mérési hőmérséklet-
                # mező (temperature_C/temperature_valid a Ua/Ia pontra,
                # temperature_2_C/temperature_2_valid a Ub/Ib pontra --
                # M4-06, review-kör-4) korábban KIZÁRÓLAG a dedikált
                # iec_dc_resistance_result.csv-be jutott el (l. logger.py
                # IEC_DC_RESISTANCE_COLUMNS), a fő report.json nem kapta
                # meg. Sem a popup-gate-et, sem a quality-számítást nem
                # érinti -- csak perzisztálás post-hoc elemzéshez.
                session_meta["Ri_DC_IEC_temperature_C"] = iec_result.get("temperature_C")
                session_meta["Ri_DC_IEC_temperature_valid"] = iec_result.get("temperature_valid")
                session_meta["Ri_DC_IEC_temperature_2_C"] = iec_result.get("temperature_2_C")
                session_meta["Ri_DC_IEC_temperature_2_valid"] = iec_result.get(
                    "temperature_2_valid"
                )
            # Kor 18, Batch C12 (METRO-P2-1, DFAM-021, R17-12 mintajat 1:1
            # koveteve): az OcvSocController minden SOC-pontra kiszamolja
            # a measurement_quality-t (CUTOFF_DURING_PULSE/SKIPPED_LOW_
            # VOLTAGE/DEGRADED_*/INVALID_PULSE_RESISTANCE/OK), es az
            # ocv_soc_table.csv-be el is jut soronkent -- de session-szintu
            # osszesites nelkul a report.json/befejezes-popup korabban
            # SOHA nem latta: egy tobb pontot is degradaltkent minositett
            # session "Kesz"-kent jelent meg minosites nelkul.
            point_quality_counts = getattr(osc, "point_quality_counts", None)
            if point_quality_counts:
                session_meta["ocv_soc_point_quality_counts"] = point_quality_counts
            tlc_learning_config = getattr(tlc, "learning_config", None) if tlc is not None else None
            if tlc_learning_config is not None:
                session_meta["learning_config_snapshot"] = asdict(tlc_learning_config)
            # Block 6 (2026-08-25) — Block 2 (PROFILE<->SESSION<->BQ Data
            # Flash konzisztencia-gate) és Block 3 (Cell BL restore
            # evidence, a result_summary MINDEN mezője, nem csak
            # .result/.quality_flags mint fent). isinstance() védelem: a
            # tlc egy test-double/jövőbeli variáns lehet (l. a fenti
            # charge_config/learning_config getattr-mintát), aminek a
            # result_summary/provisioning_consistency_report mezője nem
            # feltétlenül a valódi típus — asdict() erre TypeError-t
            # dobna. `decision` egy Enum — asdict() ezt NEM konvertálja
            # (ugyanaz a korlát, mint production_programming_controller.py
            # ProductionRecord.to_dict()-nél), ezért kézzel .value-ra cserélve.
            from Prog.src.bq_consistency_gate import ProvisioningConsistencyReport
            from Prog.src.traditional_learning_controller import LearningResultSummary

            tlc_gate_report = (
                getattr(tlc, "provisioning_consistency_report", None) if tlc is not None else None
            )
            if isinstance(tlc_gate_report, ProvisioningConsistencyReport):
                gate_dict = asdict(tlc_gate_report)
                gate_dict["decision"] = tlc_gate_report.decision.value
                session_meta["provisioning_consistency"] = gate_dict
            tlc_result_summary = getattr(tlc, "result_summary", None) if tlc is not None else None
            if isinstance(tlc_result_summary, LearningResultSummary):
                session_meta["learning_result_summary_full"] = asdict(tlc_result_summary)
            completion = getattr(tlc, "tlc_completion", None) if tlc is not None else None
            if isinstance(completion, dict):
                session_meta["tlc_completion"] = completion
                evidence = completion.get("learning_evidence")
                if isinstance(evidence, dict) and isinstance(evidence.get("result_summary"), dict):
                    frozen_summary = evidence["result_summary"]
                    session_meta["learning_result_summary_full"] = frozen_summary
                    session_meta["bq_learning_result"] = frozen_summary["result"]
                    session_meta["bq_learning_quality_flags"] = frozen_summary["quality_flags"]
            if ggc is not None:
                session_meta["golden_gg_package_ready_for_ti_submission"] = (
                    ggc.package_ready_for_ti_submission
                )
                session_meta["golden_gg_export_dir"] = ggc.export_dir
                session_meta["golden_gg_export_zip_path"] = ggc.export_zip_path
                # Teljes kódbázis review 10. kör (2026-08-31), Metrológia/
                # Adatintegritás P2 lelet: korábban ez a számláló csak az
                # export-zip belsejében jelent meg (csak sikeres exportnál) —
                # a fő report.json-ban FAULT-tal végződő session esetén is
                # látszania kell.
                session_meta["golden_gg_relax_dmm_fault_total"] = ggc.relax_dmm_fault_total
                # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás
                # NEW-2 -- l. temp_dmm_fault_ticks_total fenti komment.
                session_meta["golden_gg_temp_dmm_fault_ticks"] = ggc.temp_dmm_fault_ticks
                # Teljes kódbázis review 2. kör (2026-09-15, R2-11a, 14.
                # writer-kör, DFAM-026 4. előfordulása): a GoldenGgController.
                # quality_flags-nek korábban NULLA production fogyasztója volt
                # -- sem itt, sem a report_generator.py-ban nem jelent meg. A
                # controller saját, dokumentált tervezési döntése szerint
                # (l. _check_bq_transport_escalation() docstringje,
                # golden_gg_controller.py 1261-1268. sor -- nincs `_classify_
                # result()`-szerű függvény ebben a fájlban) a flag-ek SOSEM
                # blokkolnak egyetlen eredményt sem, csak attesztálnak -- ez
                # a fix KIZÁRÓLAG a megjelenítést/riportot pótolja, a
                # klasszifikációt NEM köti be.
                session_meta["golden_gg_quality_flags"] = list(ggc.quality_flags)
            if cic is not None:
                session_meta["chemid_relax_dmm_fault_total"] = cic.relax_dmm_fault_total
                session_meta["chemid_temp_dmm_fault_ticks"] = cic.temp_dmm_fault_ticks
                # Kör 17 MAX-effort review, R17-24a (DFAM-021+DFAM-026): a
                # RELAX_5H_FULL fázis végi feszültsége -- a teljes ChemID
                # karakterizáció legjelentősebb OCV-adata -- korábban
                # kiszámolódott, de sehova nem jutott el (l.
                # chemid_controller.py::relaxed_full_voltage_V docstringje).
                session_meta["chemid_relaxed_full_voltage_V"] = cic.relaxed_full_voltage_V
                session_meta["chemid_relaxed_full_voltage_valid"] = cic.relaxed_full_voltage_valid
                # D9/F-06 (2026-09-13, Riportok/ui_policy_audit_20260913.md):
                # a GOLDEN_GG testvér (fent, golden_gg_export_dir/
                # golden_gg_export_zip_path) már exponálja a sikeres export
                # útvonalát a riportban -- a CHEMID-nek ugyanez korábban
                # HIÁNYZOTT: a sikeres GPCCHEM-export útvonala sehol nem
                # jelent meg, csak a HIBA-út (GPCCHEM_EXPORT_FAILED ->
                # emergency_stop() -> FAULT) volt látható. A chemid_
                # controller.py-nak nincs package_ready_for_ti_submission
                # flagje (a golden_gg-vel ellentétben) -- nem is kell, mert
                # az export-hiba itt mindig hard FAULT-ra megy, nincs
                # "degraded, de lezárt" köztes állapot.
                session_meta["chemid_export_dir"] = cic.export_dir
                session_meta["chemid_export_zip_path"] = cic.export_zip_path
            if tlc is not None:
                # Teljes kódbázis review 12. kör (2026-08-31), Szoftver/
                # Architektúra P2 lelet — l. fenti komment és a
                # traditional_learning_controller.py relax_dmm_fault_total
                # property docstringje.
                session_meta["traditional_learning_relax_dmm_fault_total"] = (
                    tlc.relax_dmm_fault_total
                )
                # Review-kör 3 (2026-09-09), friss Metrológia M3-02 /
                # Szoftver/Architektúra F2 (P2) -- l.
                # temp_dmm_fault_ticks_total fenti komment és a
                # traditional_learning_controller.py temp_dmm_fault_ticks
                # property docstringje.
                session_meta["traditional_learning_temp_dmm_fault_ticks"] = (
                    tlc.temp_dmm_fault_ticks
                )
            if osc is not None:
                # Teljes kódbázis review 14. kör (2026-09-04), Szoftver/
                # Architektúra + Session/Workflow/Állapotgép P2 lelet — l.
                # fenti komment és az ocv_soc_controller.py
                # relax_dmm_fault_total property docstringje.
                session_meta["ocv_soc_relax_dmm_fault_total"] = (
                    osc.relax_dmm_fault_total
                )
                # Review-kör 3 (2026-09-09), friss Metrológia M3-02 /
                # Szoftver/Architektúra F2 (P2) -- l.
                # temp_dmm_fault_ticks_total fenti komment és az
                # ocv_soc_controller.py temp_dmm_fault_ticks property
                # docstringje.
                session_meta["ocv_soc_temp_dmm_fault_ticks"] = (
                    osc.temp_dmm_fault_ticks
                )
            if iec is not None:
                # Review-kör 2 (2026-09-09), friss Szoftver/Architektúra F7
                # (DFAM-001, 5. testvér): az IecDcResistanceController
                # relax_dmm_fault_total-ja MÁR benne van a fenti
                # relax_dmm_fault_total összegben (l. getattr(iec, ...) a
                # dict elé), de a per-controller attribúciós kulcsok közül
                # (ggc/cic/tlc/osc mind kapott sajátot) ez hiányzott -- a
                # "melyik réteg okozta a DEGRADED-et?" kérdés IEC_DC session
                # esetén a report.json-ból nem volt megválaszolható.
                session_meta["iec_dc_resistance_relax_dmm_fault_total"] = (
                    iec.relax_dmm_fault_total
                )
                # Review-kör 2 (2026-09-09), friss Metrológia/Adatintegritás
                # NEW-2 -- l. temp_dmm_fault_ticks_total fenti komment. Az
                # IEC eredmény saját "temperature_valid" mezője (l.
                # iec_dc_resistance_controller.py result dict) a MÉRÉSI
                # PILLANATRA vonatkozik; ez a session-szintű összeg a teljes
                # session alatti sikertelen olvasási ticket számolja.
                session_meta["iec_dc_resistance_temp_dmm_fault_ticks"] = (
                    iec.temp_dmm_fault_ticks
                )
            if self._discharge_ctrls_this_session:
                session_meta["rb_timing_degraded"] = any(
                    getattr(dc, "rb_timing_degraded", False)
                    for dc in self._discharge_ctrls_this_session
                )
                # R1 B8c, F-25 (P2, DFAM-021, D-B8-6): a DischargeController
                # feloldott/konfiguralt (effektiv) aram-SETPOINTja es
                # cutoffja (nem muszer-readback) -- a profile_snapshot nem alapertelmezett
                # policy (pl. 1,75 V/cella, CUSTOM) eseten a profil 1,80 V/
                # cellajat mutatja. Egy sessionben minden DISCHARGE lepes
                # ugyanabbol a cfg-bol epul (_make_discharge_ctrl()), ezert
                # az utolso controller snapshotja a sessione. getattr: a fenti
                # rb_timing_degraded mintajara (test-double/jovobeli variansnal
                # a kulcs None marad, nem hiba). Ha egyetlen DISCHARGE
                # controller sem jott letre (pl. korai FAULT), a kulcs a
                # report_generator-ban None.
                snapshot_fn = getattr(
                    self._discharge_ctrls_this_session[-1], "config_snapshot", None
                )
                session_meta["discharge_config_snapshot"] = (
                    snapshot_fn() if callable(snapshot_fn) else None
                )
            report = rg.generate(session_meta)
            report_path = self._session_dir / "report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            # Review-kör 2 (2026-09-09), friss GUI/UX F1 (P1): a hívó
            # (_finalize_terminal_session() -> _on_finished()) ebből olvassa
            # ki a DONE/terminális-CHECKPOINT_STOPPED popup minősítő
            # mezőit (capacity_result_quality/bq_learning_result/
            # golden_gg_package_ready_for_ti_submission/warning_code_counts)
            # -- korábban ezek KIZÁRÓLAG a lemezre írt report.json-ban
            # léteztek, a GUI-nak semmilyen hozzáférése nem volt hozzájuk.
            return session_meta
        except Exception as exc:
            # Teljes kódbázis review 8. kör (2026-08-30), GUI/UX/Állapot P2
            # lelet #1: korábban ez a hibaüzenet KIZÁRÓLAG a státuszsorba
            # került — ha a hívó _finalize_terminal_session()-en keresztül
            # _on_finished()-ből jött (DONE/STOPPED/terminális CHECKPOINT_
            # STOPPED), a KÖZVETLENÜL utána, MÉG ugyanabban a szinkron
            # hívásláncban (nincs közbenső Qt event-loop iteráció) lefutó,
            # feltétel nélküli "Kész | Töltve.../Kisütve..." blokk azonnal
            # felülírta ezt az üzenetet — DONE esetén ráadásul egy "Teszt
            # befejezve" popup is jött, ami szintén nem említette a hibát.
            # Az operátor SEMMILYEN látható jelzést nem kapott arról, hogy
            # a session report.json-ja nem készült el (CLAUDE.md §13
            # "logírási hiba látható kezelése"). A modális QMessageBox —
            # ellentétben a bármikor felülírható státuszsorral — garantáltan
            # látható marad, mielőtt a hívó folytatná a "Kész" üzenet
            # beállításával.
            self._status_bar.showMessage(f"Riport generálás hiba: {exc}")
            QMessageBox.warning(
                self, "Riport generálás hiba",
                f"A session report.json fájlja NEM készült el:\n{exc}\n\n"
                "A nyers mérési adatok (samples.csv) továbbra is elérhetők "
                "a session mappájában.",
            )
            # Review-kör 2 (2026-09-09), friss GUI/UX F1: explicit None --
            # a session_meta itt nem készült el, a hívó (_on_finished()) ezt
            # a "nincs minősítő adat, csak alap popup" jelzésként kezeli.
            return None

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

    def _perform_gui_safe_off(self, context: str) -> tuple[bool, dict]:
        """CLAUDE.md §6 ("befejezés/fault: Load INPUT OFF és PSU ALL OUTPUTS
        OFF") friss, GUI-rétegből kiadott safe-offja, két hívóhelyen, MINDKÉT
        HELYEN KIZÁRÓLAG arra a terminális útra, ahol a TestRunner MÁR nem
        fut (tehát a runner saját, garantált `_safe_all_off_with_uncertainty_
        check()`-je nem futhat le): (1) a resumable BQ checkpointnál PARKOLÓ
        session végleges lezárása ("Teszt lezárása" gomb, ill. ablakbezárás
        -- l. _on_checkpoint_close() és closeEvent()); (2) D6 (2026-09-13,
        DECISION_REGISTER.md GOV-023, MAX verifier 1. kör F1 javítás után):
        `Prog/main.py::_install_emergency_excepthook()` egy a GUI/main-thread
        szálon kezeletlenül maradt kivétel esetén hívja, DE KIZÁRÓLAG akkor,
        ha NINCS élő eszköz-birtokos szál: vagy `self._worker is None` ÉS
        `self._connect_worker is None` ÉS `self._bring_up_in_progress` False
        (nincs session, és bring-up sem fut -- R2-B3 idle-előfeltétel),
        vagy a worker egy resumable checkpointnál PARKOL
        (`self._pending_checkpoint_result is not None`). Ha AKTÍV session
        fut, a hook a worker SAJÁT, szerializált `request_emergency_stop()`-
        útját használja. Ha `self._worker is None` és a bring-up fut -- a
        connect-szál (R1 B6, F-07), VAGY a connect utáni build/resume
        szakasz (R2-B3: explicit `_bring_up_in_progress` jelző, a connect-
        szál indításától a ready/failed handler bármely kilépéséig) --, a
        hook csak a `_connect_abort_requested` flaget állítja (mint a "Safe
        Off" gomb, l. _emergency_stop()), műszer-I/O nélkül. Ez a csak-flag
        ág PONTOSAN `self._worker is None` mellett érvényes: a worker
        hozzárendelése után a hook már a worker `request_emergency_stop()`-
        útjára delegál, akkor is, ha a bring-up jelző még True. A lezárást
        (a MOST megnyitott managerrel) a _on_connect_ready() abort-ága,
        ill. a build/resume szakaszban az ELSŐ kapu -- a _build_runner()
        resume-modál utáni abort-kapuja a Logger/szülőmarker előtt, a
        meglévő build-hiba ágon -- végzi; a kapu és a worker létrejötte
        közti maradék ablakot a MÁSODIK, fail-closed kapu zárja a
        `self._thread.start()` előtt: a worker `request_emergency_stop()`-
        ja, amelyet a runner az első végrehajtandó lépés előtt a saját
        vészágán (safe-off + hibapoll) érvényesít. Egyik esetben sem
        ezt a metódust hívja (l. ott a részletes indoklást), pont azért,
        hogy ez a metódus precondition-je (nincs konkurens eszköz-birtokos)
        a második hívóhelyen is igaz maradjon.

        Szálbiztonság: mindkét hívóhelyen igaz, hogy amikor ez a metódus
        lefut, nincs aktív worker-szál, amely a drivereken dolgozna -- az
        (1) hívóhelynél ezt a `_pending_checkpoint_result is not None`
        állapot garantálja (pontosan azt jelenti, hogy a worker `run()`-ja
        MÁR visszatért -- worker.py::_do_run() emittálta a `finished`-et,
        keep_thread=True --, a worker-szál csak az eseményhurkában
        várakozik, és a folytatás, `_on_continue_requested()`, ELŐBB
        nullázza a `_pending_checkpoint_result`-ot), a (2) hívóhelynél a
        fenti hook-feltételek (parkoló checkpoint, ill. `self._worker is
        None`, `self._connect_worker is None` és nincs bring-up, azaz
        `self._bring_up_in_progress` False).

        Réteghatár (.claude/rules/40-instruments.md "GUI nem vezérel
        közvetlenül műszert"): ez a hívás ugyanazt a MEGLÉVŐ
        `InstrumentManager.safe_all_off()` API-t használja, mint a
        main_window.py MÁR meglévő három bring-up hibaútja (l.
        _construct_and_connect_instruments() except-ág, _on_connect_ready()
        abort- és _build_runner()-hiba ága) -- nem SCPI-t küld, és nem
        vezérlési (setpoint) művelet, hanem a §6 biztonsági alapállapot
        helyreállítása azon az úton, ahol más réteg nem teheti meg.

        Visszatérés: `(attempted, result)` -- pontosan a TestResult
        `safe_off_attempted`/`safe_off_result` mezőinek szemantikájával.
        """
        if self._instruments is None:
            # Nincs mit lekapcsolni (pl. a session sosem épült fel) -- a
            # riportban ez NEM lehet hamis pozitív attesztáció.
            self._live_panel.append_event({
                "event_code": "SAFE_OFF_SKIPPED_NO_INSTRUMENTS",
                "event_message": (
                    f"{context}: nincs élő műszer-kapcsolat, safe-off nem "
                    "hajtható végre."
                ),
                "severity": "CRITICAL",
            })
            return False, {}
        try:
            result = self._instruments.safe_all_off()
        except Exception as exc:
            # A safe_all_off() eszközönként már elnyeli a hibákat (l. ott),
            # de egy váratlan kivétel sem akadályozhatja meg a hívó
            # lecsatlakozás/riport lépéseit -- a tény viszont NEM tűnhet el.
            self._live_panel.append_event({
                "event_code": "SAFE_OFF_FAILED",
                "event_message": f"{context}: {exc}",
                "severity": "CRITICAL",
            })
            return True, {}
        uncertain = [name for name in ("Load", "PSU") if not result.get(name, False)]
        if uncertain:
            # CLAUDE.md §10.4 / test_runner.py::_safe_all_off_with_uncertainty_
            # check() azonos szabálya: egy nem igazoltan sikeres OFF sosem
            # maradhat csendben.
            self._live_panel.append_event({
                "event_code": "SAFE_OFF_RESULT_UNCERTAIN",
                "event_message": (
                    f"{context}: safe_off() nem igazoltan sikeres a következő "
                    f"eszköz(ök)ön: {', '.join(uncertain)}"
                ),
                "severity": "CRITICAL",
            })
        else:
            self._live_panel.append_event({
                "event_code": "SAFE_OFF_PERFORMED",
                "event_message": (
                    f"{context}: Load INPUT OFF + PSU ALL OUTPUTS OFF "
                    "parancs minden eszközön hibamentesen kiadva "
                    "(parancs-szintű eredmény, nem hardveres readback)."
                ),
            })
        return True, result

    def _on_checkpoint_close(self) -> None:
        # R2-B6 (R2-GUI-04): uj session bring-upja alatt a regi lezaras nem
        # irhat lezart statuszt es nem engedelyezheti a Startot.
        if self._bring_up_in_progress:
            return
        self._tabs.setTabEnabled(self._checkpoint_tab_index, False)
        self._tabs.setCurrentIndex(0)
        self._log_checkpoint_checklist()
        if self._pending_checkpoint_result is not None:
            # Teljes kódbázis review 23. kör (2026-09-07), Session/Workflow/
            # Állapotgép P1 lelet: ha az operátor a checkpointnál parkolva
            # előbb a "Safe Off" gombbal (_emergency_stop()) vészleállítást
            # kért, majd "Teszt lezárása"-t nyom, ez az ág korábban SOSEM
            # ellenőrizte a kérést — a report.json a régi, változatlan
            # CHECKPOINT_STOPPED eredményből íródott, az explicit
            # vészleállítás-kérés nyomtalanul eltűnt. Pontos testvérpárja a
            # 19. kör worker.py::request_continue_from_checkpoint()
            # fixjének (81741a4), csak a "Folytatás" ágra lett alkalmazva,
            # a "Close" ágra nem.
            # Governance-v2 review-kör 1 (MAX verifier F2, batch 4.1
            # follow-up, 3. kör): a `safe_off_attempted`/`safe_off_result`
            # MINDKÉT ágon (vészleállítással zárva ÉS normál "Teszt
            # lezárása") a `_on_checkpoint_close()` SAJÁT, ezen a ponton
            # meghozott döntését tükrözi, nem a checkpoint ELÉRÉSEKOR (akár
            # órákkal/napokkal korábban) számolt, itt újra NEM ellenőrzött
            # értéket.
            # Governance-v2 review-kör (MAX verifier V4-4/V4-1, batch 4.1b):
            # EZEN A GUI-oldali ("Teszt lezárása" gomb + closeEvent) ÚTVONALON
            # javítva. FONTOS, független verifier által azonosított rés: a
            # PONTOS TESTVÉR-ÚTVONAL a worker.py::request_continue_from_
            # checkpoint() vészleállítás-ágán (kb. :184-192, "Safe Off" gomb a
            # checkpoint-panelen) TOVÁBBRA IS `safe_off_attempted=False`-t ír
            # -- ez a batch 4.1b MÁSIK, MÉG NYITOTT fele, NEM ez a sor zárja
            # le a teljes hibaosztályt (l. worker.py saját, ott is nyitva
            # hagyott kommentje). Ez az ág korábban a `False`/`{}` értékpárt írta,
            # mert ténylegesen EGYETLEN friss OFF parancs sem ment ki innen
            # (`_finalize_terminal_session()` csak `disconnect_all()`-t hív,
            # ami NEM ad ki OUTPUT/INPUT OFF-ot). A CLAUDE.md §6
            # ("befejezés: Load INPUT OFF és PSU ALL OUTPUTS OFF") szerint
            # viszont a session TÉNYLEGES lezárása itt történik — a
            # checkpointnál akár napokig parkolt session alatt a fizikai
            # állapot bizonyítatlan (operátori BQ-munka, kézi beavatkozás),
            # ezért a `disconnect_all()` ELŐTT friss safe-offot adunk ki
            # (eszközönkénti sikerjelzéssel -- ez PARANCS-szintű eredmény,
            # NEM hardveres readback, l. device_psu.py::safe_off()). A mezők
            # mostantól ennek a TÉNYLEGES, ITT végrehajtott műveletnek az
            # eredményét tükrözik.
            checkpoint_result = self._pending_checkpoint_result
            safe_off_attempted, safe_off_result = self._perform_gui_safe_off(
                "CHECKPOINT_CLOSE"
            )
            # Governance-v2 review-kör (MAX verifier V4-1): a checkpoint
            # ELÉRÉSEKOR kiadott safe-off (test_runner.py::
            # _run_manual_checkpoint() -> _safe_all_off_with_uncertainty_
            # check()) az EGYETLEN bizonyíték arra, hogy a fizikai ciklus
            # biztonságos állapotban parkolt le — a fenti felülírás után
            # semmilyen perzisztens artefaktumban nem maradt volna meg.
            # Külön, NEM összekeverhető néven kerül a report.json-ba
            # (l. _write_report()).
            self._checkpoint_safe_off_attempted = bool(
                getattr(checkpoint_result, "safe_off_attempted", False)
            )
            self._checkpoint_safe_off_result = dict(
                getattr(checkpoint_result, "safe_off_result", None) or {}
            )
            result = dataclasses_replace(
                checkpoint_result,
                safe_off_attempted=safe_off_attempted,
                safe_off_result=safe_off_result,
            )
            runner = getattr(self._worker, "runner", None) if self._worker is not None else None
            if runner is not None and getattr(runner, "emergency_stop_requested", False):
                reason = runner.emergency_stop_reason
                # l. a fenti 23. körös komment (ugyanez a "Safe Off" gomb ->
                # "Teszt lezárása" út) — itt a státusz/indoklás/folytathatóság
                # mezőket állítjuk át a MÁR friss safe-off eredményt hordozó
                # `result`-on.
                result = dataclasses_replace(
                    result,
                    status="FAULT",
                    reason=f"EMERGENCY_STOP_AT_CHECKPOINT: {reason}",
                    resume_possible=False,
                )
                self._status_bar.showMessage(f"VÉSZLEÁLLÍTÁS a checkpointnál: {reason}")
            else:
                self._status_bar.showMessage("Teszt lezárva — konfigurálj új mérést.")
            # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P1 lelet
            # (F3) — l. _finalize_terminal_session()/_on_finished() docstringje.
            self._finalize_terminal_session(result)
            self._pending_checkpoint_result = None
        else:
            # R2-B6 (R2-GUI-02): az F-31 minosites nem veszhet el a lezaraskor.
            self._status_bar.showMessage(
                "Teszt lezárva — konfigurálj új mérést." + self._terminal_status_suffix
            )
        # Review-kör 5, Session/Workflow S5-04 (P2, S4-01 sibling) -- l.
        # _on_finished()/_on_fault() azonos hivatkozású kommentje: a
        # visszatérési érték korábban itt is el lett dobva, a Start gomb
        # FELTÉTEL NÉLKÜL engedélyezve lett -- ha a szál nem állt le
        # 5s-en belül, ez egy MÁSODIK, egyidejű InstrumentManager/
        # TestRunner létrejöttét tenné lehetővé Start-ra kattintva, amíg
        # a régi worker-szál esetleg még a safe-off/BQ-tranzakció
        # közepén jár.
        cleanup_ok = self._cleanup_thread()
        # Teljes kódbázis review 18. kör (2026-09-05), GUI/UX/Állapot P2
        # lelet: l. _on_finished() keep_thread ágának kommentje — a Start
        # gomb ott explicit letiltásra került, amíg a worker/szál életben
        # volt; a tényleges takarítás (fenti _cleanup_thread()) után itt
        # engedélyezzük vissza -- de CSAK ha az ténylegesen sikeres volt.
        self._live_panel.set_start_enabled(cleanup_ok)
        if not cleanup_ok:
            self._live_panel.append_event({
                "event_code": "WORKER_THREAD_CLEANUP_TIMEOUT",
                "event_message": (
                    "A teszt-szál nem állt le 5 másodpercen belül a "
                    "checkpoint lezárása után -- a Start gomb biztonsági "
                    "okból tiltva marad, amíg ez be nem fejeződik."
                ),
                "severity": "CRITICAL",
            })

    def _log_checkpoint_checklist(self) -> None:
        """Release-audit 2026-08-23 (2.17): a BQ checkpoint-checklist
        (CheckpointPanel) korábban dekoratív volt — a bejelölés semmit nem
        kapuzott és sehova nem naplózódott, így az operátor tévesen hihette,
        hogy a bepipálás számít valamit. A "Teszt lezárása" gombnál mostantól
        az állapot bekerül az eseménynaplóba és egy külön fájlba a session
        mappájában, auditálhatóság céljából — ez NEM kapu (a lezárás nem
        blokkolódik hiányos checklist esetén), csak látható nyom."""
        if self._session_dir is None:
            return
        state = self._checkpoint_panel.checklist_state()
        unchecked = [item for item, done in state.items() if not done]
        self._live_panel.append_event({
            "event_code": "BQ_CHECKPOINT_CHECKLIST_AT_CLOSE",
            "event_message": (
                f"{len(state) - len(unchecked)}/{len(state)} bejelölve"
                + (f" — hiányzó: {', '.join(unchecked)}" if unchecked else "")
            ),
        })
        try:
            checklist_path = self._session_dir / "bq_checkpoint_checklist.json"
            checklist_path.write_text(
                json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            # Teljes kódbázis review 18. kör (2026-09-05), GUI/UX/Állapot P3
            # lelet: az írási hiba korábban csendben elveszett (except:
            # pass) — CLAUDE.md §13 "logírási hiba látható kezelése", a
            # _write_report()-nál (8. kör) már javított hibaosztály
            # testvér-írási útja. Nem kapu (a lezárás nem blokkolódik),
            # csak látható diagnosztikai nyom.
            self._live_panel.append_event({
                "event_code": "BQ_CHECKPOINT_CHECKLIST_WRITE_FAILED",
                "event_message": str(exc),
            })

    def _on_continue_requested(self) -> None:
        if self._worker is None:
            return
        # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P1 lelet (F3):
        # a folytatás megkezdésekor a korábban elmentett checkpoint-result
        # elévül — a worker tovább fut, és vagy egy ÚJABB checkpointot ér el
        # (újra elmentődik _on_finished()-ben), vagy egy valódi terminális
        # állapotot (azt a normál is_terminal ág kezeli). Enélkül egy
        # elképzelhető (bár szűk) versenyhelyzetben egy elavult eredmény
        # íródna ki, ha valahogy mégis _on_checkpoint_close() futna le előbb.
        self._pending_checkpoint_result = None
        # P0-javítás (2026-08-26): signalon keresztül, NEM közvetlen
        # metódushívással — l. _continue_from_checkpoint_requested
        # docstringje (a worker a saját QThread-jén fusson tovább, ne a
        # GUI-szálon blokkolva).
        self._continue_from_checkpoint_requested.emit()
        self._live_panel.reset_plots()
        self._checkpoint_panel.set_continuing()
        self._status_bar.showMessage("Folytatás indítása...")

    def _on_status_changed(self, status: str) -> None:
        if (status == "RUNNING"
                and self._tabs.currentIndex() == self._checkpoint_tab_index):
            self._tabs.setCurrentIndex(1)

    def _on_fault(self, reason: str) -> None:
        # Review-kör 5, Session/Workflow S5-04 MAX verifier B1 lelet
        # (2026-09-10, PARTIAL -> javítva): az EREDETI S5-04 fix itt is
        # explicit set_start_enabled(False)-t hívott cleanup_ok=False
        # esetén, azzal az (empirikusan MEGCÁFOLT) feltevéssel, hogy
        # _on_finished() ELŐBB fut le és "övé az utolsó szó". A VALÓS
        # sorrend fordított: a lenti QMessageBox.critical() egy application-
        # modal dialógus, ami egy BEÁGYAZOTT Qt event loopot nyit -- a MÁR
        # SORBAN ÁLLÓ `finished` jel (worker.py::_do_run() a `fault` UTÁN,
        # de UGYANABBAN a hívásban emittálja) ez alatt kézbesítődik, tehát
        # _on_finished() (és annak MAGA a cleanup_ok-alapú set_start_
        # enabled()-hívása) TELJESEN lefut, MIELŐTT ez a metódus a modal
        # visszatérése után folytatódna. Az itteni (korábbi) explicit
        # tiltás emiatt UTOLJÁRA futott, és FELÜLÍRTA _on_finished() saját,
        # AZÓTA ESETLEG SIKERES (2. próbálkozás) cleanup-eredményét --
        # egy VALÓDI (bár ritka: ~10s ablak) forgatókönyvben a Start gomb
        # VÉGLEGESEN, alkalmazás-újraindításig letiltva ragadt volna, holott
        # a szál ténylegesen leállt. Javítás: ez a metódus TOVÁBBRA IS
        # meghívja _cleanup_thread()-et (a takarítás -- signal-disconnect,
        # esetleges korai leállás -- ettől függetlenül hasznos), de a Start
        # gomb állapotát NEM állítja be innen -- _on_finished() a
        # `finished` jel garantált (a Qt sor miatt akár ITT, a modal alatt,
        # akár a metódus visszatérése után) lefutása miatt EGYEDÜLI,
        # megbízható forrás, függetlenül a pontos időzítéstől.
        self._cleanup_thread()
        self._status_bar.showMessage(f"HIBA: {reason}")
        QMessageBox.critical(
            self, "Teszt hiba", f"A teszt leállt:\n{reason}")

    def _cleanup_thread(self) -> bool:
        """Visszaadja, hogy a worker-szál ténylegesen leállt-e (vagy sosem
        is futott) -- False, ha a wait(5000) timeoutolt és a szál MÉG FUT.
        Review-kör 4 (2026-09-09), Session/Workflow S4-01 (P1, MAX verifier
        kötelező): korábban a wait() visszatérési értéke el lett dobva, és
        a hívó (closeEvent()) feltétel nélkül event.accept()-elt egy még
        futó worker-szál mellett is -- ha a szál épp a safe-off/BQ Cell BL
        restore közepén járt, az ablak bezárása után a PSU fizikailag ON
        maradhatott, vagy a BQ chip véglegesen az ideiglenes learning-
        küszöbön ragadhatott. False esetén a self._thread/self._worker
        referenciát SZÁNDÉKOSAN nem nullázzuk és deleteLater()-t sem
        adunk ki a szálra (Qt: egy még futó QThread törlése nem
        biztonságos) -- a hívó dönthet úgy, hogy nem fejezi be a
        műveletet (l. closeEvent())."""
        # Teljes kódbázis review 3. kör (2026-08-29), GUI/UX P2 lelet: a
        # _continue_from_checkpoint_requested (MainWindow-szintű, az app
        # teljes élettartama alatt élő Signal) minden sikeres Start új
        # kapcsolatot ad hozzá az adott session workerére (l. _start_test()),
        # de korábban sosem lett disconnect-elve — mivel a Qt signal-slot
        # kapcsolat a küldő oldalon (MainWindow, sosem szűnik meg) tartja
        # életben a fogadót, minden lezárt session teljes worker/controller-
        # gráfja (TestRunner/SafetyManager/Logger/BatteryProfile) a folyamat
        # végéig memóriában maradt volna a self._worker.deleteLater()
        # ellenére is (CLAUDE.md §15 "Worker és QThread szabályosan
        # szabaduljon fel").
        if self._worker:
            try:
                self._continue_from_checkpoint_requested.disconnect(
                    self._worker.request_continue_from_checkpoint
                )
            except (TypeError, RuntimeError):
                # Nincs is bekötve (pl. ez az ág sosem futott le sikeres
                # Start után) — nem hiba, csendben átléphető.
                pass
        if self._thread:
            self._thread.quit()
            if not self._thread.wait(5000):
                return False
            self._thread.deleteLater()
            self._thread = None
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        self._refresh_calibration_blocked_state()
        return True

    # ------------------------------------------------------------------ #
    # BQ Diagnosztika (WP-AK-07) — a fő teszt-worker szálától FÜGGETLEN    #
    # életciklus, saját QThread-en.                                        #
    # ------------------------------------------------------------------ #

    def _on_bq_connect_requested(self, port: str) -> None:
        if self._bq_worker is not None or self._bq_connect_worker is not None:
            return

        # WP-AK-16: kölcsönös kizárás - egy aktív TRADITIONAL_LEARNING/GOLDEN_GG
        # session már birtokolja a Pico soros portját.
        if self._session_owns_bq_transport:
            QMessageBox.warning(
                self, "BQ Diagnosztika",
                "Egy fizikai BQ tanítási session (Traditional Learning / Golden GG) "
                "jelenleg használja a Pico soros portját - a Diagnosztika csak a "
                "session befejezése után csatlakozhat.",
            )
            return
        # Teljes kódbázis review 11. kör (2026-08-31), GUI/UX/Állapot P2
        # lelet: a fenti self._session_owns_bq_transport csak a MÁR
        # MEGKÖTÖTT session-transportot védi — egy folyamatban lévő
        # TRADITIONAL_LEARNING/GOLDEN_GG hardveres bring-up (_connect_worker,
        # ~1-4s worst-case) alatt ez az ellenőrzés még False, mégis
        # ugyanahhoz a fizikai Pico soros porthoz próbálna kapcsolódni egy
        # MÁSODIK, önálló klienssel (a "port egyszerre csak egy klienst
        # szolgál ki megbízhatóan" dokumentált előfeltevés megsértése).
        if (
            self._connect_worker is not None
            and getattr(self._connect_worker, "needs_bq_transport", False)
        ):
            QMessageBox.warning(
                self, "BQ Diagnosztika",
                "Egy fizikai BQ tanítási session (Traditional Learning / Golden GG) "
                "kapcsolódása jelenleg folyamatban van - a Diagnosztika csak a "
                "kapcsolódás befejezése után csatlakozhat.",
            )
            return

        # Teljes kódbázis review 2. kör (2026-08-28), GUI/UX P0 lelet (F1): a
        # soros port nyitás + negotiate() korábban itt, szinkron, a GUI-szálon
        # futott (~1s worst-case) — most _BqConnectWorker-en, külön QThread-en.
        self._bq_connect_thread = QThread()
        self._bq_connect_worker = _BqConnectWorker(port)
        self._bq_connect_worker.moveToThread(self._bq_connect_thread)
        self._bq_connect_thread.started.connect(self._bq_connect_worker.run)
        self._bq_connect_worker.connect_ready.connect(self._on_bq_connect_ready)
        self._bq_connect_worker.connect_failed.connect(self._on_bq_connect_failed)
        self._bq_connect_thread.start()
        self._refresh_calibration_blocked_state()

    def _on_bq_connect_ready(self, result: _BqConnectResult) -> None:
        self._cleanup_bq_connect_thread()
        self._bq_serial = result.bq_serial

        self._bq_thread = QThread()
        # [WP-AK-07 Critical fix, C-1/C-2] a soros port (bq_serial) átadva a
        # workernek - a bezárás a worker SAJÁT szálán történik
        # (_stop_and_close()), sosem a GUI-szálról, l. _cleanup_bq_thread().
        self._bq_worker = BqDiagnosticsWorker(result.driver, serial_conn=result.bq_serial)
        self._bq_worker.moveToThread(self._bq_thread)
        self._bq_thread.started.connect(self._bq_worker.start_polling)
        self._bq_worker.snapshot_ready.connect(self._bq_diagnostics_panel.update_snapshot)
        self._bq_worker.snapshot_ready.connect(self._on_bq_snapshot_ready)
        self._bq_worker.connection_error.connect(self._bq_diagnostics_panel.set_connection_error)
        self._bq_thread.start()

        self._bq_diagnostics_panel.set_connected(result.port)
        # Kör 20 Batch E9 (F20-G4 follow-up, MAX/NORMAL verifier lelete,
        # 2026-09-19): a fenti self._bq_worker beállítás UTÁN a Pico-
        # előkészítő menüakció widget-állapota (l.
        # _refresh_calibration_blocked_state() pico_provisioning_blocked
        # ága) STALE marad, ha ez a hívás hiányzik -- a _cleanup_bq_
        # connect_thread() (fent) MÁR hívta a refresh-t, de MÉG a
        # self._bq_worker beállítása ELŐTT, tehát azzal a régi (None)
        # értékkel. Ugyanaz a DFAM-001 minta, amit ez a batch a
        # menüakció click-time/widget-state szétcsúszására eredetileg
        # javított -- most a SAJÁT, újonnan bevezetett bq_worker-
        # függősége hozott létre egy újabb, ugyanilyen hívóhely-hiányt.
        self._refresh_calibration_blocked_state()

    def _on_bq_connect_failed(self, message: str) -> None:
        self._cleanup_bq_connect_thread()
        QMessageBox.critical(self, "BQ Diagnosztika — kapcsolódási hiba", message)

    def _cleanup_bq_connect_thread(self) -> None:
        if self._bq_connect_thread:
            self._bq_connect_thread.quit()
            self._bq_connect_thread.wait(5000)
            self._bq_connect_thread.deleteLater()
            self._bq_connect_thread = None
        if self._bq_connect_worker:
            self._bq_connect_worker.deleteLater()
            self._bq_connect_worker = None
        self._refresh_calibration_blocked_state()

    def _on_bq_snapshot_ready(self, snapshot) -> None:
        # WP-AK-07 "közös session logging": a BQ diagnosztika CSAK akkor
        # naplózódik lemezre, ha éppen fut egy battery test session (van
        # aktív TestRunner/Logger) — a session-könyvtárba, közös
        # timestamp-domainbe, l. Prog/src/logger.py::log_bq_diagnostics().
        # Kapcsolat nélküli, önálló diagnosztika-nézet (nincs futó teszt)
        # esetén a panel ÉLŐBEN mutatja az értékeket, de nem kerül lemezre
        # — ez egy tudatos, dokumentált WP-AK-07 hatókör-döntés (a "közös"
        # jelző pontosan ezt jelenti: MEGOSZTOTT session, nem egy önálló,
        # mindig-naplózó BQ-session-fogalom bevezetése).
        if self._worker is not None:
            try:
                self._worker.runner.logger.log_bq_diagnostics(snapshot)
            except ValueError:
                # Jóindulatú versenyhelyzet: a session épp most zárult le
                # (Logger.close() már lefutott), mire ez a lekérdezés
                # visszaért — a BQ-poller saját, FÜGGETLEN 2s-es ütemezésű,
                # nem szinkronizált a teszt-session befejezésével. Nem
                # adatvesztés (a KÖVETKEZŐ poll már megint élő session
                # esetén ír, vagy egyáltalán nem ír, ha nincs aktív session
                # — ugyanaz, mint amikor sosem volt session), csak egy
                # elkésett írási kísérlet egy már bezárt fájlba.
                pass

    def _on_bq_disconnect_requested(self) -> None:
        # Review-kör 5 (2026-09-09), GUI/UX/Állapot G5-02 (P2, DFAM-001,
        # a closeEvent()-ben már bevált bool-ellenőrző minta 3. hívóhelye):
        # a _cleanup_bq_thread() visszatérési értékét korábban eldobtuk --
        # timeoutnál (a szál még fut, pl. blokkoló soros olvasás közepén)
        # a set_disconnected() ekkor is lefutott volna, hamis "Nincs
        # kapcsolat"-ot mutatva egy MÉG ÉLŐ worker/soros port mellett, és a
        # "Kapcsolódás" gomb újra engedélyezve egy csendes no-opra futott
        # volna (_on_bq_connect_requested() early-return, ha self._bq_worker
        # not None).
        if not self._cleanup_bq_thread():
            QMessageBox.warning(
                self, "Bontás folyamatban",
                "A BQ Diagnosztika szál még nem állt le (folyamatban lévő "
                "soros olvasás). A kapcsolat NEM bontható még -- várj, "
                "majd próbáld újra.",
            )
            return
        self._bq_diagnostics_panel.set_disconnected()
        # Kör 20 Batch E9 (F20-G4 follow-up, l. _on_bq_connect_ready()
        # azonos indoklása): a fenti _cleanup_bq_thread() nullázza
        # self._bq_worker-t, de a Pico-előkészítő menüakció widget-
        # állapota enélkül a hívás nélkül TARTÓSAN letiltva marad egy
        # sikeres leválás UTÁN is, bármi más blokkoló ok nélkül.
        self._refresh_calibration_blocked_state()

    def _refresh_calibration_blocked_state(self) -> None:
        """Teljes kódbázis review 23. kör (2026-09-07), GUI/UX/Állapot P3
        lelet: a "Gauge kalibráció…" gomb widget-állapotát a 3 MARADÉK
        (session_owns_bq_transport/bq_worker MÁR bekötve a 22. körben,
        l. bq_diagnostics_panel.py) kölcsönös kizárási feltétellel is
        szinkronban kell tartani — l. _on_bq_calibration_requested() a
        pontos indoklásért mindhárom feltételre. Minden hívóhely, ahol a
        3 alábbi flag valamelyike változik, ezt a metódust hívja.

        Kör 20 Batch E9 (F20-G4, 2026-09-19, DFAM-001 RECURRING): a "Pico
        előkészítése…" menüakció (l. _on_pico_provisioning_requested())
        korábban SOHA nem kapott setEnabled()-et -- mind a NÉGY kizárási
        feltételét kizárólag kattintás-idejű QMessageBox.warning érvényesítette,
        tehát a kezelő a menüben mindig aktívnak látta, csak a kattintás UTÁN
        derült ki, hogy blokkolva van. A testvér-minta (ez a metódus, a
        kalibrációs gombra) már megoldotta ugyanezt egy MÁSIK, SZŰKEBB
        feltétel-halmazra. A Pico-akció SAJÁT, NÉGY feltétele (a fenti
        `blocked` HÁROM-elemű bool-jától SZÁNDÉKOSAN KÜLÖN számítva -- a
        `blocked` újrafelhasználása a Pico-akciót HIBÁS okokból tiltaná/
        engedné: a kalibráció nem érzékeny `_bq_worker`-re, a Pico-előkészítés
        viszont igen, l. _on_pico_provisioning_requested() négy ága)."""
        blocked = (
            self._worker is not None
            or self._bq_connect_worker is not None
            or self._connect_worker is not None
        )
        self._bq_diagnostics_panel.set_calibration_blocked_by_other_activity(blocked)

        pico_provisioning_blocked = (
            self._worker is not None
            or self._bq_worker is not None
            or self._session_owns_bq_transport
            or self._bq_connect_worker is not None
            or self._connect_worker is not None
        )
        self._pico_provisioning_action.setEnabled(not pico_provisioning_blocked)

    def _on_bq_calibration_requested(self, port: str) -> None:
        # Teljes kódbázis review 13. kör (2026-08-31), GUI/UX/Állapot P0
        # lelet: a `BqCalibrationDialog.exec()` (lent) alkalmazás-modális —
        # amíg nyitva van, a MainWindow (a LivePanel Stop/Emergency Stop
        # gombjait is beleértve) TELJESEN elérhetetlenné válik. A lenti
        # kölcsönös kizárás korábban csak a BQ-transport port-versengését
        # fedte le — egy nem-BQ-transportot használó, de aktívan PSU/Load-ot
        # vezérlő fizikai teszt (CHARGE_ONLY/DISCHARGE_ONLY/CHARACTERIZATION/
        # OCV_SOC/CHEMID) alatt a kalibrációs dialógus akadálytalanul
        # megnyitható volt, kikapcsolva a kezelő manuális vészleállítási
        # lehetőségét a dialógus teljes (akár perces) nyitva-tartása alatt
        # (CLAUDE.md §15 "Emergency stop mindig látható és egyértelmű
        # legyen").
        if self._worker is not None:
            QMessageBox.warning(
                self, "Gauge kalibráció",
                "Fizikai teszt van folyamatban - a kalibrációs dialógus "
                "modálisan blokkolná a Vészleállítás gombot. Állítsd le a "
                "tesztet, mielőtt kalibrációt indítanál.",
            )
            return
        # WP-AK-09: kölcsönös kizárás - a kalibrációs dialógus ÖNÁLLÓ soros
        # kapcsolatot nyit (l. bq_calibration_dialog.py modul docstring),
        # ezért a BQ Diagnosztika kapcsolatnak bontva kell lennie, mielőtt
        # a dialógus megpróbálná megnyitni ugyanazt a fizikai portot.
        if self._bq_worker is not None:
            QMessageBox.warning(
                self, "Gauge kalibráció",
                "Előbb bontsd a BQ Diagnosztika kapcsolatot - a kalibrációs "
                "dialógus önálló soros kapcsolatot igényel ugyanahhoz a "
                "porthoz.",
            )
            return
        # WP-AK-16: kölcsönös kizárás - l. _on_bq_connect_requested().
        if self._session_owns_bq_transport:
            QMessageBox.warning(
                self, "Gauge kalibráció",
                "Egy fizikai BQ tanítási session (Traditional Learning / Golden GG) "
                "jelenleg használja a Pico soros portját - a kalibráció csak a "
                "session befejezése után csatlakozhat.",
            )
            return
        # Teljes kódbázis review 11. kör (2026-08-31), GUI/UX/Állapot P2
        # lelet: l. _on_bq_connect_requested() ugyanerről a hiányzó,
        # ÁTMENETI (kapcsolódás folyamatban) kizárásról — mindkét
        # versengő folyamatra (BQ Diagnosztika "Kapcsolódás" ÉS
        # TRADITIONAL_LEARNING/GOLDEN_GG bring-up) vonatkozik.
        if self._bq_connect_worker is not None:
            QMessageBox.warning(
                self, "Gauge kalibráció",
                "A BQ Diagnosztika kapcsolódása jelenleg folyamatban van - a "
                "kalibráció csak a kapcsolódás befejezése (vagy megszakítása) "
                "után indítható.",
            )
            return
        # Teljes kódbázis review 15. kör (2026-09-04), GUI/UX/Állapot P1
        # lelet: a fenti feltétel korábban `needs_bq_transport`-hoz kötve
        # KIZÁRÓLAG a TRADITIONAL_LEARNING/GOLDEN_GG bring-upot fedte le
        # (port-versengés miatt) — egy NEM BQ-transportot igénylő teszttípus
        # (CHARGE_ONLY/DISCHARGE_ONLY/CHARACTERIZATION/OCV_SOC/CHEMID)
        # hardveres bring-upja alatt (`self._connect_worker` MÉG fut,
        # `self._worker` MÉG None) a kalibrációs dialógus akadálytalanul
        # megnyitható volt. Mivel a lenti `dlg.exec()` alkalmazás-modális
        # beágyazott eseményhurka pumpálja a Qt-eseménysort, a még
        # függőben lévő `connect_ready` szignál a dialógus NYITVA-tartása
        # KÖZBEN kézbesítődhetett — a fizikai teszt (PSU/Load) ténylegesen
        # elindult, miközben a Vészleállítás gomb a modális dialógus miatt
        # elérhetetlen maradt (CLAUDE.md §15). A feltétel ezért mostantól
        # `needs_bq_transport`-tól FÜGGETLENÜL, BÁRMILYEN folyamatban lévő
        # hardveres bring-up alatt blokkol.
        if self._connect_worker is not None:
            QMessageBox.warning(
                self, "Gauge kalibráció",
                "Egy fizikai teszt hardveres kapcsolódása jelenleg folyamatban "
                "van - a kalibráció csak a kapcsolódás befejezése (vagy "
                "megszakítása) után indítható, mert a dialógus modálisan "
                "blokkolná a Vészleállítás gombot, miközben a teszt közben "
                "elindulhatna.",
            )
            return
        from Prog.gui.panels.bq_calibration_dialog import BqCalibrationDialog
        dlg = BqCalibrationDialog(port, self)
        dlg.exec()

    def _cleanup_bq_thread(self) -> bool:
        """Visszaadja, hogy a BQ diagnosztika-szál ténylegesen leállt-e -- l.
        _cleanup_thread() docstringje ugyanerről a mintáról (review-kör 4,
        MAX verifier a153925b3b9fbce5c P3-2, DFAM-016 sibling).
        [WP-AK-07 Critical fix, független review C-1/C-2, 2 javítási kör]
        1. kör (review): KORÁBBAN itt közvetlen `self._bq_worker.stop_polling()`
        Python-hívás történt a GUI-szálról egy a worker SAJÁT szálához
        tartozó QTimer-re, PLUSZ a soros port lezárása is itt, a GUI-szálon —
        mindkettő szál-biztonsági szabálysértés volt. Javítva a
        `stop_requested` szignál emittálására váltással.
        2. kör (saját tesztem, test_bq_diagnostics_worker.py::
        TestBqDiagnosticsWorkerThreadSafety, buktatta el): az 1. kör
        ideiglenes verziója rögtön a `stop_requested.emit()` UTÁN,
        SZINKRON módon hívta a `thread.quit()`-ot innen — ez versenyzett a
        még postázott, de nem feldolgozott `stop_requested`->`_stop_and_close`
        meghívással, és a `close()` hívás TELJES KIMARADÁSÁHOZ vezetett (a
        `QThread.quit()` a kilépési jelzőt azonnal állítja, nem a postázott
        esemény-sorba fűzve). Egy köztes próbálkozás (`stopped` szignál a
        workertől -> `thread.quit()` slot a GUI-szálon) HOLTPONTHOZ vezetett
        volna: `self._bq_thread.wait(5000)` itt LENT blokkolja a GUI-szálat,
        ami alatt a GUI-szál eseményhurka nem pumpál, így egy onnan induló
        keresztszálas `quit()`-hívás sosem jutott volna célba. A végleges,
        helyes megoldás: `BqDiagnosticsWorker._stop_and_close()` a SAJÁT
        (worker) szálán fut le (a `stop_requested` queued connection miatt),
        és a takarítás VÉGÉN saját magát kéri leállni (`self.thread().quit()`
        — dokumentáltan thread-safe, de itt nincs is szükség keresztszálas
        hívásra). Itt, a GUI-szálon ezért MÁR csak `emit()` + `wait()`
        marad — sem közvetlen `stop_polling()`, sem közvetlen `quit()`."""
        if self._bq_thread:
            if self._bq_worker:
                self._bq_worker.stop_requested.emit()
            else:
                self._bq_thread.quit()
            # Review-kör 4 (2026-09-09), MAX verifier (a153925b3b9fbce5c)
            # P3-2 -- l. _cleanup_thread() docstringje ugyanerről a DFAM-016
            # mintáról (CF-10b). Timeoutnál NEM adunk ki deleteLater()-t egy
            # még futó szálra, és NEM nullázzuk a referenciát -- a hívó
            # (closeEvent()) eldöntheti, hogy megvárja-e.
            if not self._bq_thread.wait(5000):
                return False
            self._bq_thread.deleteLater()
            self._bq_thread = None
        if self._bq_worker:
            self._bq_worker.deleteLater()
            self._bq_worker = None
        # A bezárás a worker saját szálán már megtörtént (l. fent) - itt
        # csak a MainWindow-oldali referenciát ürítjük, NEM hívunk close()-t
        # ismét (dupla close() pyserial-lal ártalmatlan lenne, de a GUI-
        # szálról indított close() lenne pont az, amit ez a javítás kizár).
        self._bq_serial = None
        return True

    # ------------------------------------------------------------------ #
    # Objektumgráf factory                                               #
    # ------------------------------------------------------------------ #

    def _build_runner(
        self, cfg: SessionConfig, pre_connected: _PreConnectedInstruments | None = None
    ):
        import uuid

        import yaml  # type: ignore[import-untyped]

        from Prog.src.battery_profile import BatteryProfile
        from Prog.src.charge_controller import ChargeConfig, ChargeController
        from Prog.src.chemid_controller import (
            ChemIdCharacterizationController,
            ChemIdConfig,
        )
        from Prog.src.discharge_controller import DischargeConfig, DischargeController
        from Prog.src.golden_gg_controller import (
            GoldenGgController,
            GoldenGgPhysicalConfig,
        )
        from Prog.src.golden_gg_export import GoldenGgConfig
        from Prog.src.gpcchem_export import GpcChemConfig
        from Prog.src.iec_dc_resistance_controller import (
            IecDcResistanceConfig,
            IecDcResistanceController,
        )
        from Prog.src.logger import LogConfig, Logger
        from Prog.src.ocv_soc_controller import OcvSocController
        from Prog.src.relax_controller import RelaxConfig, RelaxController
        from Prog.src.safety import PsuMode, SafetyManager, TempCompMode
        from Prog.src.session_recovery import (
            compute_config_hash,
            compute_profile_hash,
            find_resumable_session,
        )
        from Prog.src.test_runner import (
            StepKind,
            TestPlan,
            TestRunner,
            TestRunnerConfig,
        )
        from Prog.src.traditional_learning_controller import (
            LearningConfig,
            TraditionalLearningController,
        )

        # WP-AK-16: kölcsönös kizárás - a BQ Diagnosztika/Kalibráció panelek egy
        # teljesen külön soros kapcsolatot tartanak ugyanahhoz a fizikai Pico
        # porthoz; egyszerre csak egy kliens szolgálható ki (l. _on_bq_connect_
        # requested()/_on_bq_calibration_requested() tükör-guardjai).
        needs_bq_transport = cfg.test_type in _NEEDS_BQ_TRANSPORT_TEST_TYPES
        if needs_bq_transport and self._bq_worker is not None:
            raise ValueError(
                "Előbb bontsd a BQ Diagnosztika kapcsolatot - a "
                "Traditional Learning/Golden GG session önálló soros "
                "kapcsolatot igényel ugyanahhoz a porthoz."
            )

        # BAT-001/SRC-003/CFG-001: a verziózott battery profile YAML (gyártói/
        # mérnöki paraméterek forrása) az egyetlen hiteles forrás legyen — nem
        # csak session-specifikus mezőket (kapacitás, modell) kap a GUI-tól.
        # Governance-v2 review-kör 1 (CF-15/DFAM-017 3. előfordulás, batch
        # 4.5): korábban egy itt élt, önálló _PROFILE_META_FALLBACK dict —
        # ugyanaz az érték, mint a kanonikus derived_parameters.PROFILE_META
        # (amit a config_panel.py és a DynamicSessionModel is használ),
        # önálló, testvér-inkonzisztens másolatként.
        from Prog.gui.derived_parameters import (
            DEFAULT_TERMINATE_V_PER_CELL,
            PROFILE_META,
        )
        fallback = PROFILE_META.get(
            cfg.battery_profile_name, PROFILE_META["FIAMM_12V"]
        )
        profile_yaml_path = (
            app_paths.bundle_dir() / "Prog" / "config" / "battery_profiles"
            / f"{cfg.battery_profile_name}.yaml"
        )
        profile_yaml: dict = {}
        if profile_yaml_path.exists():
            with profile_yaml_path.open(encoding="utf-8") as f:
                profile_yaml = yaml.safe_load(f) or {}

        profile = BatteryProfile(
            battery_name=cfg.battery_profile_name,
            manufacturer=profile_yaml.get("manufacturer", fallback["manufacturer"]),
            model=cfg.battery_model,
            nominal_voltage_V=profile_yaml.get("nominal_voltage_V", fallback["nominal_voltage_V"]),
            cell_count=int(profile_yaml.get("cell_count", fallback["cell_count"])),
            nominal_capacity_Ah=cfg.nominal_capacity_ah,
            chemistry=profile_yaml.get("chemistry", "AGM_VRLA"),
            profile_source=profile_yaml.get("profile_source", ""),
            charge_voltage_per_cell_V=profile_yaml.get("charge_voltage_per_cell_V", 2.40),
            # Kör 20 Batch E5 (SW20-2): a fallback most a
            # derived_parameters.py-ban élő közös konstansból jön, hogy ne
            # legyen kézzel szinkronban tartott 3. hardkódolt 1.80 literál
            # (l. DynamicSessionModel.set_terminate_voltage_per_cell_default_V()).
            terminate_voltage_per_cell_V=profile_yaml.get(
                "terminate_voltage_per_cell_V", DEFAULT_TERMINATE_V_PER_CELL
            ),
            float_voltage_per_cell_V=profile_yaml.get("float_voltage_per_cell_V", 2.27),
            max_charge_current_A=profile_yaml.get("max_charge_current_A"),
            taper_current_A=profile_yaml.get("taper_current_A"),
            temp_comp_mV_per_cell_per_degC=profile_yaml.get(
                "temp_comp_mV_per_cell_per_degC", -2.5
            ),
            test_temperature_min_C=profile_yaml.get("test_temperature_min_C", 15.0),
            test_temperature_max_C=profile_yaml.get("test_temperature_max_C", 30.0),
            # Governance-v2 kör 2: IEC_DC_INTERNAL_RESISTANCE módhoz kötelező,
            # a konkrét akkumodell saját C10 kisütési táblázatából (10h,
            # 1,80V/cella) -- l. battery_profile.py mező-docstringje.
            i10_discharge_current_A=profile_yaml.get("i10_discharge_current_A"),
        )

        if needs_bq_transport:
            from decimal import Decimal

            from Prog.src.bq_consistency_gate import build_initial_df_values
            from Prog.src.exceptions import ProfileValidationError

            # A GUI mező C10; FG20721 10H=6,7 Ah, 20H=7,2 Ah
            # (FG_FOLDER_EN Rev.11.2025 p2). Csak egész párhuzamos szám igazolt.
            count = Decimal(str(cfg.nominal_capacity_ah)) / Decimal("6.7")
            if not count.is_finite() or count < 1 or count != count.to_integral_value():
                raise ProfileValidationError(
                    "SOURCE_REQUIRED: FG20721 GUI C10 = 6,7 Ah × párhuzamos szám"
                )
            profile.nominal_capacity_Ah = float(Decimal("7.2") * count)
            profile.C10_capacity_Ah = profile.fg20721_c10_capacity_Ah
            # Q-22: tartományhiba már az objektumgráf építésekor blokkol.
            build_initial_df_values(profile)

        psu_mode  = PsuMode(cfg.psu_mode)
        temp_mode = TempCompMode(cfg.temperature_compensation_mode)
        # OCV_SOC_CHARACTERIZATION és CHEMID_CHARACTERIZATION_PBA is teljes
        # hardveres felállással (pl. TF1 panel, soros dióda) fut, ugyanazok a
        # magasabb soros esés értékek jellemzőek rájuk mint BQ módban — l.
        # dmm_feedback_lost_hibaelemzes_20260628.txt (WP-AK-05: ChemID a
        # BQ_LEARNING/OCV_SOC-hoz hasonló teljes töltés/relax/kisütés ciklust
        # fut ugyanazon a hardveren, ugyanaz a soros-esés kockázat vonatkozik rá).
        # Review 2026-09-15 (C-05): mindkét hívóhely a modul-szintű
        # _IS_BQ_MODE_TEST_TYPES-ból olvas -- l. ott a teljes indoklást.
        is_bq_mode = cfg.test_type in _IS_BQ_MODE_TEST_TYPES
        safety = SafetyManager(
            profile=profile,
            psu_mode=psu_mode,
            temp_comp_mode=temp_mode,
            is_bq_mode=is_bq_mode,
        )

        # Release-audit 2026-08-23 (2.6): a teljes hardveres bring-up (driver-
        # objektumok, VISA connect_all, BQ soros negotiate, identify_all)
        # _construct_and_connect_instruments()-be lett kiszervezve — ha a
        # hívó (_start_test()) ezt már elvégezte egy külön QThread-en, az
        # eredményt itt csak átvesszük (pre_connected); ha nem (tesztek,
        # szinkron hívási mód), most, szinkron, a GUI-szálon végezzük el,
        # BYTE-azonos viselkedéssel a korábbi inline blokkal.
        if pre_connected is None:
            pre_connected = _construct_and_connect_instruments(cfg, needs_bq_transport)
        psu = pre_connected.psu
        load = pre_connected.load
        dmm_v = pre_connected.dmm_v
        dmm_t = pre_connected.dmm_t
        self._instruments = pre_connected.instruments
        self._psu_mode_str: str = cfg.psu_mode  # H-02: riporthoz
        # Teljes kódbázis review 20. kör (2026-09-06), Metrológia/
        # Adatintegritás P1 lelet: a `temp_mode` fent (1772. sor) csak a
        # SafetyManager konstruktorába jutott el — a _write_report()
        # session_meta dict-je sosem kapott "temperature_compensation_mode"
        # kulcsot, ezért a ReportGenerator mindig a saját "MONITOR_ONLY"
        # alapértékére esett vissza, FÜGGETLENÜL a ténylegesen kiválasztott
        # (OFF/MONITOR_ONLY/ENABLED) módtól — CLAUDE.md §11.2/§12 "a
        # ténylegesen alkalmazott konfigurációt logold" megsértése.
        self._temperature_compensation_mode_str: str = cfg.temperature_compensation_mode
        self._cell_count: int = profile.cell_count  # DIS-003: riporthoz (24V blokk-jelzés)
        # Teljes kódbázis review 1. kör (2026-08-26), P2 lelet: report_generator.py
        # korábban mindig "DMM2_surface_PT100"-ot jelentett hőmérséklet-forrásként,
        # függetlenül attól, hogy ténylegesen a PT100 (4-eres RTD) vagy az NTC
        # termisztor pótszonda módban futott-e a session (CLAUDE.md §12: a
        # ténylegesen alkalmazott konfigurációt logold).
        self._temperature_sensor_mode: str = cfg.temperature_sensor_mode
        # DATA-006 (2026-08-24, felhasználói kérés): korábban a profil
        # (nominal_capacity_Ah, C10_capacity_Ah, charge_voltage_per_cell_V
        # stb.) TÉNYLEGES értékei sehol nem kerültek a session logba/
        # report.json-ba — csak egy visszafejthetetlen hash (profile_hash,
        # resume-egyezés ellenőrzésére). Emiatt utólag, a session mappából,
        # nem volt rekonstruálható, milyen kapacitás/feszültség-paraméterek
        # voltak ténylegesen aktívak egy adott futáshoz.
        self._last_profile_snapshot: dict = asdict(profile)
        # Release-audit 2026-08-23 (2.2): nullázás minden új build-nél, hogy
        # egy korábbi (más teszttípusú) session controllere ne szivárogjon
        # át a jelenlegi session _write_report()-jébe.
        self._last_traditional_learning_ctrl = None
        self._last_golden_gg_ctrl = None
        self._last_chemid_ctrl = None
        self._last_ocv_soc_ctrl = None
        self._last_iec_dc_resistance_ctrl = None
        self._discharge_ctrls_this_session = []
        # Governance-v2 review-kör (MAX verifier V4-1): ugyanaz a "ne
        # szivárogjon át az előző session" nullázás, mint fent -- a
        # checkpoint-kori safe-off bizonyíték session-specifikus.
        self._checkpoint_safe_off_attempted = False
        self._checkpoint_safe_off_result = {}

        # WP-AK-16: session-szintű PicoBQTransport - csak TRADITIONAL_LEARNING/
        # GOLDEN_GG esetén, a WP-AK-07 _on_bq_connect_requested() mintáját
        # követve.
        bq_transport = pre_connected.bq_transport
        if pre_connected.session_bq_serial is not None:
            self._session_bq_serial = pre_connected.session_bq_serial
            # Review 2026-09-15 (C-02): a transport OBJEKTUM referenciája is
            # el kell tárolódjon, hogy _close_session_bq_transport() a
            # .disconnect()-jét (nem csak a nyers serial.close()-t) hívhassa
            # -- l. ott a teljes indoklást.
            self._session_bq_transport = bq_transport
            self._session_owns_bq_transport = True
            # Feature (2026-08-23): a Diagnosztika fül passzív élő nézetre
            # vált — nincs önálló Kapcsolódás/Bontás, csak a session saját
            # gauge-lekérdezéséből táplált megjelenítés (l.
            # gauge_snapshot_ready docstring).
            self._bq_diagnostics_panel.set_session_owned(
                True, session_label=cfg.test_type
            )
            # Teljes kódbázis review 3. kör (2026-08-29), GUI/UX P2 lelet: a
            # panel staleness-küszöbe (set_poll_interval_ms() alapértelmezés:
            # 2000ms, "önálló" diagnosztika-fül módhoz) korábban sosem lett
            # átállítva session-vezérelt módban — a TRADITIONAL_LEARNING/
            # GOLDEN_GG session ezt a panelt saját, LearningConfig/
            # GoldenGgPhysicalConfig.sample_interval_s (mindkettő 30.0s
            # alapértelmezéssel) kadenciájú gauge-mintavétellel táplálja, NEM
            # 2s-enként — a hardcode-olt 4.0s küszöb miatt a panel a session
            # idejének nagy részében hamisan "ELAVULT ADAT"-ot mutatott
            # (alarm fatigue, egy valódi BQ-kiesés ugyanígy nézett volna ki).
            self._bq_diagnostics_panel.set_poll_interval_ms(30000)

        # Governance-v2 review-kör (MAX verifier O-3/F5, DFAM-001): a terv
        # felépítése SZÁNDÉKOSAN ITT, a resume-kapu ELŐTT történik (korábban
        # a metódus végén, a TestRunnerConfig felépítése UTÁN volt) — a
        # session_recovery.evaluate_resume() BLOCKED_INVALID_STEP_INDEX
        # kapujához a JELENLEGI terv lépésszáma kell, MIELŐTT a checkpointból
        # átvett resume_start_step_index/Ah/Wh értékek bekerülnének a
        # TestRunnerConfig-ba. Tiszta kód-mozgatás: a blokk kizárólag
        # cfg.test_type-tól függ, viselkedése változatlan (az ismeretlen
        # test_type ValueError-ja ugyanúgy a hívó _start_test()/_on_connect_ready()
        # hibakezelésébe fut, csak még a Logger/session-mappa létrehozása
        # ELŐTT, tehát üres session-mappát sem hagy hátra).
        if cfg.test_type == "CHARGE_ONLY":
            test_plan = TestPlan.charge_only()
        elif cfg.test_type == "DISCHARGE_ONLY":
            test_plan = TestPlan.discharge_only()
        elif cfg.test_type == "CHARACTERIZATION":
            test_plan = TestPlan.characterization()
        elif cfg.test_type == "OCV_SOC_CHARACTERIZATION":
            test_plan = TestPlan.ocv_soc_characterization()
        elif cfg.test_type == "CHEMID_CHARACTERIZATION_PBA":
            # WP-AK-05 (Mapping): explicit ág — korábban minden fel nem
            # ismert test_type (ide értve ezt is) csendben a
            # bq_learning_physical() tervre esett vissza.
            test_plan = TestPlan.chemid_characterization_pba()
        elif cfg.test_type == "BQ_LEARNING_PHYSICAL":
            test_plan = TestPlan.bq_learning_physical()
        elif cfg.test_type == "TRADITIONAL_LEARNING":
            test_plan = TestPlan.traditional_learning()
        elif cfg.test_type == "GOLDEN_GG":
            test_plan = TestPlan.golden_gg()
        elif cfg.test_type == "IEC_DC_INTERNAL_RESISTANCE":
            test_plan = TestPlan.iec_dc_internal_resistance()
        else:
            raise ValueError(f"Ismeretlen test_type: {cfg.test_type!r}")

        # WP-AK-02 (REC-001..004): a checkpoint traceability-mezőihez szükséges
        # azonosítás — CSAK connect_all() UTÁN lehetséges (élő *IDN? kell).
        instrument_identities = pre_connected.instrument_identities
        config_hash = compute_config_hash(cfg)
        profile_hash = compute_profile_hash(profile)

        # WP-AK-02 Restart workflow: "incomplete session detect → ... →
        # battery/config/profile check → operator review → resume only safe
        # boundary". A biztonságos-egyezés ellenőrzést a session_recovery
        # végzi (evaluate_resume) — itt csak az operátor explicit
        # jóváhagyását kérjük, mielőtt bármit folytatnánk.
        parent_session_id = ""
        resume_start_step_index = 0
        resume_initial_charge_ah = 0.0
        resume_initial_discharge_ah = 0.0
        # Governance-v2 review-kör 1 (CF-17/MET-R1-08, batch 4.3): az Ah-
        # testvérpár Wh-oldala, ugyanaz a resume-átvételi minta, mint fent.
        resume_initial_charge_wh = 0.0
        resume_initial_discharge_wh = 0.0
        # Governance-v2 review-kör 1 (CF-18 + V-4, batch 4.3, SPR-04
        # hátralévő fele): idő-origó + 5 diagnosztikai számláló, ugyanaz a
        # resume-átvételi minta, mint fent.
        resume_initial_session_start_time_iso = ""
        resume_initial_communication_faults_count = 0
        resume_initial_extreme_dt_s_count = 0
        resume_initial_max_observed_dt_s = 0.0
        resume_initial_relax_dmm_fault_count = 0
        resume_initial_temperature_out_of_range_count = 0
        # MAX verifier (review-kör 3, adbbda9f6120dfd33) P3, DFAM-001 5.
        # előfordulása: a temp_dmm_fault_ticks (l. test_runner.py
        # TestRunnerConfig.initial_temp_dmm_fault_ticks) korábban csak
        # ÍRÓDOTT a checkpoint.json-ba, de sosem OLVASÓDOTT vissza
        # resume-nál -- ellentétben a fenti temperature_out_of_range_count
        # 4-lépéses (checkpoint-write/GUI-lokál-init/GUI-olvasás/
        # TestRunnerConfig-átadás) teljes körforgásával, ez a mező csak az
        # első és utolsó lépést kapta meg (l. test_runner.py::run() seed).
        resume_initial_temp_dmm_fault_ticks = 0
        # Review-kör 4 (2026-09-09), S4-05/M4-05 (P2/P3, DFAM-022) -- l.
        # test_runner.py::run() self._warning_code_counts docstringje
        # ugyanerről a resume-korforgásról, 1:1 a fenti temp_dmm_fault_
        # ticks mintáját követve.
        resume_initial_warning_code_counts: dict = {}
        resume_initial_error_queue_deferred_s: dict[str, float] = {}
        resume_initial_relax_missed_deadline_count = 0
        match = find_resumable_session(
            self._sessions_root(), config_hash, profile_hash,
            instrument_identities, cfg.sample_id,
            # Governance-v2 review-kör (MAX verifier O-3/F5): a JELENLEGI terv
            # lépésszáma — enélkül egy tartományon kívüli next_safe_step_index
            # egy garantáltan FAULT-ra futó "folytatást" indítana el, közben a
            # szülő session checkpointját véglegesen RESUMED_INTO_NEW_SESSION-
            # ként megjelölve (l. session_recovery.py azonos hivatkozású
            # kommentje).
            total_step_count=len(test_plan.steps),
        )
        if match is not None:
            # WP-AK-02 review-lelet (Important/P2): üres sample_id mellett
            # egyező config/profil/műszer-felállás ELMÉLETBEN más fizikai
            # akkumulátorra is felkínálhatja a folytatást (az operátor
            # egyszerűen sosem tölti ki a mezőt) — ezt nem rejtjük el, hanem
            # explicit figyelmeztetjük az operátort a döntés előtt.
            sample_warning = ""
            if not cfg.sample_id:
                sample_warning = (
                    "\n\n⚠ FIGYELEM: a minta-azonosító (sample_id) mindkét "
                    "session-nél üres — a rendszer emiatt NEM tudja kizárni, "
                    "hogy fizikailag MÁS akkumulátor van most a padon. Csak "
                    "akkor folytasd, ha biztos vagy benne, hogy ugyanazt az "
                    "akkumulátort teszteled tovább."
                )
            reply = QMessageBox.question(
                self, "Megszakadt session folytatható",
                f"A(z) '{match['session_dir'].name}' session megszakadt, de a "
                f"jelenlegi konfiguráció/profil/műszer-felállás/minta pontosan "
                f"egyezik vele, és biztonságos határon (nem aktív CC/CV/"
                f"DISCHARGE/PULSE közben) állt meg."
                + sample_warning +
                "\n\nFolytatod ezt a sessiont a megszakadási pontról, vagy új, "
                "független sessiont indítasz?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                # Release-audit 2026-08-23 (2.14): explicit defaultButton=No —
                # ez a konzervatívabb választás (új, független session), és
                # pont ez a dialógus figyelmeztet legélesebben (üres
                # sample_id esetén), hogy csak megfontoltan folytassuk.
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                parent_session_id = match["session_dir"].name
                resume_start_step_index = match["checkpoint"].get("next_safe_step_index", 0)
                # WP-AK-02 review-lelet (Important/P1): a megszakadás előtti
                # Ah-összegek átvétele — enélkül a report.json a resume utáni
                # session Ah-t 0-ról számolná, elveszítve a korábbi haladást.
                resume_initial_charge_ah = match["checkpoint"].get("charge_ah", 0.0) or 0.0
                resume_initial_discharge_ah = match["checkpoint"].get("discharge_ah", 0.0) or 0.0
                # CF-17/MET-R1-08 (batch 4.3): Wh-testvérpár, l. fenti
                # resume_initial_charge_ah azonos indoklása.
                resume_initial_charge_wh = match["checkpoint"].get("charge_wh", 0.0) or 0.0
                resume_initial_discharge_wh = match["checkpoint"].get("discharge_wh", 0.0) or 0.0
                # CF-18 + V-4 (batch 4.3): idő-origó + 5 diagnosztikai
                # számláló, l. fenti resume_initial_charge_ah azonos
                # indoklása. Az üres string alapérték a TestRunner.run()
                # oldalon jelöli, hogy nincs átvehető idő-origó (l. ott).
                resume_initial_session_start_time_iso = (
                    match["checkpoint"].get("session_start_time_iso", "") or ""
                )
                resume_initial_communication_faults_count = (
                    match["checkpoint"].get("communication_faults_count", 0) or 0
                )
                resume_initial_extreme_dt_s_count = (
                    match["checkpoint"].get("extreme_dt_s_count", 0) or 0
                )
                resume_initial_max_observed_dt_s = (
                    match["checkpoint"].get("max_observed_dt_s", 0.0) or 0.0
                )
                resume_initial_relax_dmm_fault_count = (
                    match["checkpoint"].get("relax_dmm_fault_count", 0) or 0
                )
                resume_initial_temperature_out_of_range_count = (
                    match["checkpoint"].get("temperature_out_of_range_count", 0) or 0
                )
                resume_initial_temp_dmm_fault_ticks = (
                    match["checkpoint"].get("temp_dmm_fault_ticks", 0) or 0
                )
                resume_initial_warning_code_counts = (
                    match["checkpoint"].get("warning_code_counts") or {}
                )
                resume_initial_error_queue_deferred_s = dict(
                    match["checkpoint"].get("error_queue_deferred_s") or {}
                )
                resume_initial_relax_missed_deadline_count = (
                    match["checkpoint"].get("relax_missed_deadline_count", 0) or 0
                )
        # R2-B3 (R2-SES-01, DFAM-001): a resume-modal alatti veszhook csak a
        # flaget allitja; itt, a Logger/session-mappa es a szulo
        # RESUMED_INTO_NEW_SESSION markere ELOTT ervenyesul (a hivo meglevo
        # build-hiba aga: safe_all_off -> disconnect_all -> BQ-port -> dialogus).
        if self._connect_abort_requested:
            raise RuntimeError(
                "VÉSZLEÁLLÍTÁS kérve a session-építés közben (kezeletlen "
                "kivétel a GUI-szálon) — a műszerek lezárulnak, a session "
                "NEM indul el."
            )
        self._parent_session_id: str = parent_session_id  # DATA-003/REC-003: riporthoz

        # Teljes kódbázis review 3. kör (2026-08-29), GUI/UX P0 lelet: a DMM
        # mérési mód + PSU kombinációs mód beállítás/visszaigazolás (korábban
        # itt, a GUI-szálon, szinkron futott) most _construct_and_connect_
        # instruments()-be lett kiszervezve — l. ott a részletes indoklást.
        # Ez a hívás mindkét ágon (worker-thread pre_connected VAGY a fenti
        # szinkron _construct_and_connect_instruments() fallback) MÁR
        # lefutott, mielőtt idáig értünk.

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        # CF-34 (governance-v2 review-kör 1, DFAM-026 declared_policy_
        # without_consumer): a session TÉNYLEGES test_type-ja kell
        # _on_finished()-ben (a "Kész" popup recharge-figyelmeztetéséhez),
        # de az ott elérhető self._worker ekkorra már None lehet
        # (_cleanup_thread() korábban lefut) -- a session_dir mintáját
        # követve itt, a session-építés idején mentjük el.
        self._active_test_type = cfg.test_type
        self._session_dir = self._sessions_root() / f"session_{cfg.battery_model}_{stamp}"
        logger = Logger(self._session_dir, LogConfig())
        process_run_id = str(uuid.uuid4())

        # Kivétel esetén (pl. vezérlő-konstruálás hibája) a Logger nem maradhat
        # nyitva — üres session mappát hagyna hátra (l. dmm_feedback_lost_hibaelemzes_20260628.txt)
        try:
            # Teljes kódbázis review 1. kör (2026-08-26), P2 lelet (CLAUDE.md
            # §12: "A ténylegesen alkalmazott DMM/PSU/load konfigurációt
            # logold") — a fenti DMM range/NPLC/hőmérséklet-mód beállítás
            # korábban sehol nem került session-szintű naplózásra; utólag
            # (pl. egy per-review vizsgálatnál) nem volt visszamenőleg
            # rekonstruálható, milyen konfigurációval készült egy adott
            # session mérése.
            logger.log_event(
                "DMM_CONFIGURATION",
                # R1 B8b, F-26: a nplc=10 a BRING-UP érték; a runner
                # lépésenként (és relax-alállapotonként) átállítja -- azt a
                # NPLC_SET események naplózzák (test_runner.py::_log_nplc_set()).
                "DMM_V: range=100V nplc=10 (bring-up; lépésenkénti NPLC: "
                f"NPLC_SET események); DMM_T: mode={cfg.temperature_sensor_mode} nplc=10",
            )
            # Governance-v2 review-kör 1 (batch 4.2, CF-07 fix mellék-
            # hatásaként felfedezve; MAX verifier F-4 pontosítás: a hívás a
            # KÖRÜLVEVŐ try/except-en BELÜLRE kerül, hogy egy itteni I/O
            # hiba ne hagyjon nyitott Loggert/üres session-mappát maga
            # mögött): a `log_event()` alapból CSAK a kritikus eseményeknél
            # (`is_critical`/`_CRITICAL_EVENTS`) flush-el azonnal — ez a
            # session-nyitó audit-bejegyzés enélkül a memóriában maradt
            # volna egészen a következő flush-ig (pl. az első minta-tickig,
            # vagy a sessiont eldobó objektumgráf garbage collectionjéig,
            # ami NEM determinisztikus időzítésű). Egy korai crash/áram-
            # kimaradás a session elindulása UTÁN, de az első flush ELŐTT
            # nyomtalanul elveszítette volna EZT a CLAUDE.md §12 által
            # kifejezetten megkövetelt, visszamenőleg rekonstruálandó
            # konfigurációs bejegyzést.
            logger.flush_all()

            # Governance-v2 review-kör 1 (CF-16/MET-R1-04+16+18, DFAM-001 6.
            # előfordulás + DFAM-017, batch 4.4): a testvér
            # ocv_soc_controller.py::_discharge_current_A (azonos minta:
            # kapacitás/discharge_rate_divisor) már a kanonikus
            # `_c10_base_Ah`-t használja — ez a hívóhely a `_c10_base_Ah`
            # property-t MEGKERÜLVE, nyers `nominal_capacity_Ah`-t
            # használt. R16-B7 óta explicit C10-kapacitást csak a TL/GG
            # BQ-ág (needs_bq_transport) tölt ki a forrásolt FG20721-profilban;
            # az általános kisütési úton a C10 nincs kitöltve, ezért itt a
            # _c10_base_Ah névleges fallbackje érvényes (pl. 7,2 Ah).
            discharge_A = (
                cfg.discharge_current_A
                if cfg.discharge_current_A > 0
                else profile._c10_base_Ah / cfg.discharge_rate_divisor
            )

            # 2026-08-23 (felhasználói review, 2. kör — traditional_learning
            # OV incidens): a session/GUI töltési konfigurációja (taper,
            # töltőáram-felülírás) mostantól KÖZÖS a CHARGE_ONLY, a
            # TRADITIONAL_LEARNING és a GOLDEN_GG között — korábban a
            # Learning/Golden GG egy bare ChargeConfig()-ot használt, ami
            # figyelmen kívül hagyta a GUI-ban beállított töltőáramot,
            # miközben ugyanazon a fizikai áramúton (TF1 panel) tölt, mint
            # a CHARGE_ONLY. `fast_loop_enabled=True`: élő session-ben a
            # CC->CV határ közelében/CV-ben gyorsabb, ténylegesen mért idejű
            # bel-ső résztick-ekkel szabályoz (l. charge_controller.py
            # ChargeConfig.fast_loop_* docstring) — teszt-alapértelmezésben
            # (ChargeConfig()) ez KIKAPCSOLVA marad.
            shared_charge_cfg = ChargeConfig(
                taper_hold_s=cfg.taper_hold_s,
                charge_current_A_override=cfg.charge_current_A_override,
                fast_loop_enabled=True,
            )
            # ST1 (G1-UD-7): a LivePanel elévültküszöbe a fast loop mintaköz-
            # nyújtó keretét is tartalmazza (l. LivePanel.set_fast_loop_
            # budget_s()); 0.0, ha a ténylegesen futó (resume esetén a
            # hátralévő) lépések között nincs töltő lépés.
            runs_charge_step = any(
                step.kind not in (StepKind.DISCHARGE, StepKind.RELAX, StepKind.MANUAL_CHECKPOINT)
                for step in test_plan.steps[resume_start_step_index:]
            )
            self._live_panel.set_fast_loop_budget_s(
                shared_charge_cfg.fast_loop_max_substeps * shared_charge_cfg.fast_loop_period_s
                if shared_charge_cfg.fast_loop_enabled and runs_charge_step else 0.0
            )

            def _make_charge_ctrl():
                return ChargeController(
                    psu, load, dmm_v, dmm_t, profile, safety, shared_charge_cfg,
                )

            def _make_discharge_ctrl():
                ctrl = DischargeController(
                    psu, load, dmm_v, dmm_t, profile, safety,
                    DischargeConfig(
                        discharge_current_A=discharge_A,
                        terminate_voltage_V_override=cfg.discharge_terminate_voltage_V,
                        rate_origin=cfg.discharge_rate_origin,
                    ),
                )
                # Release-audit 2026-08-23 (2.3): _write_report() ebből
                # olvassa ki a rb_timing_degraded jelzőt a session végén —
                # egy session-ben több DISCHARGE lépés is lehet (pl.
                # CHARACTERIZATION plan discharge_1/discharge_2), a lista
                # minden létrehozott controllert megőriz, hogy egyik fázis
                # degraded-státusza se veszhessen el a következő fázis
                # felülírásában.
                ctrl.on_event = lambda ev: _forward_controller_event(ev, "DISCHARGE_EVENT")
                self._discharge_ctrls_this_session.append(ctrl)
                return ctrl

            def _forward_controller_event(ev: dict, event_code_fallback: str) -> None:
                # Governance-v2 review-kör 1 (CF-07, DFAM-011+012, batch
                # 4.2): a kontrollerek SAJÁT on_event-je (Relax/ChemID/
                # TraditionalLearning/GoldenGG/OcvSoc) korábban KIZÁRÓLAG
                # a loggerbe írt, a `severity` mezőt eldobva — a `runner.
                # on_event` lánc (worker._handle_event -> event_ready ->
                # LivePanel.append_event) sosem kapta meg. Emiatt pl. a
                # batch 4.1 CF-01 fix LEARNING_BL_THRESHOLD_RESTORE_FAILED
                # eseménye élőben LÁTHATATLAN maradt volna az operátor
                # számára — pont azt a hibát rejtve el, amit a CF-01 fix
                # orvosolt (l. MASTER_CONSOLIDATED.md "CF-01 és CF-07
                # kölcsönhatása"). A `runner` változóra a lezárás a lenti
                # `runner = TestRunner(...)` értékadás UTÁN, a tényleges
                # factory-hívásokkor (run() közben) hivatkozik — ez már a
                # worker által bekötött on_event-et tartalmazza.
                #
                # MAX verifier F-3 (batch 4.2 CF-07 utóellenőrzés): a
                # log_event() I/O-hibája enélkül megakadályozta volna a
                # GUI-forwardot is — a batch 4.1 V-7 leletének azonos
                # mintáját követve (l. test_runner.py::
                # _abort_active_controller()) itt is best-effort védjük.
                try:
                    # Review-kör 2 (2026-09-09), friss Szoftver/Architektúra
                    # F5 (DFAM-001): a fenti ev['severity']='CRITICAL' eddig
                    # eldobódott a logger.log_event() hívásnál -- az
                    # events.csv is_critical=False-t rögzített egy elvben
                    # kritikus eseményre (pl. RELAX_TEMPERATURE_DMM_FAULT),
                    # miközben az élő panel (event_log_widget.py) a
                    # severity-t helyesen pirosra színezte. Az élő oldal
                    # NEM változik, csak a logger-oldali is_critical jelző
                    # igazodik hozzá.
                    logger.log_event(
                        ev.get("event_code", event_code_fallback), ev.get("event_message", ""),
                        is_critical=(ev.get("severity") == "CRITICAL"),
                    )
                except Exception:
                    pass
                if runner.on_event is not None:
                    runner.on_event(ev)

            _DEFAULT_RELAX_S = 7200.0  # RelaxConfig.min_relax_s default

            _POST_DISCHARGE_RELAX_S = 18000.0   # 5h: FIAMM spec / BQ Learning

            def _make_relax_ctrl(step):
                # Governance-v2 review-kör 1 (CF-33/DFAM-025
                # implicit_dispatch_on_unstructured_label, batch 4.5): a
                # relax-időtartam korábban a step.label SZÖVEGÉBEN keresett
                # "discharge" substringgel dőlt el — a label szabadon
                # átnevezhető (TestPlan.characterization()/
                # bq_learning_physical() konstrukciója semmi mást nem
                # kényszerít ki), egy jövőbeli átnevezés csendben rossz
                # (2h a szükséges 5h helyett) relax-időt választott volna,
                # teszt nem védte. A step.relax_purpose (test_runner.py::
                # TestStep) az EXPLICIT, plan-konstrukciókor beállított
                # strukturált jelző — nem a label szövegétől függ.
                #
                # Kör 19 Batch D4 (SW19-01, DFAM-017): a `cfg.test_type ==
                # "CHARGE_ONLY"` ág maga is egy MÁSODIK, konvertálatlan
                # forrás maradt ugyanebben a feltételben -- a "relax_after_
                # charge" mezőcsoport CHARACTERIZATION módban is látható/
                # szerkeszthető és a Start-dialógus ott is megjeleníti
                # (l. ui_policy.py TEST_MODE_POLICIES), de a fenti `elif`
                # csak CHARGE_ONLY-ra ismerte fel -- CHARACTERIZATION
                # módban az operátor által beállított érték CSENDBEN
                # figyelmen kívül maradt, mindig a 7200s alapértékre esve
                # (12x eltérés a 600s GUI-alapértéknél), miközben a Start-
                # dialógus a figyelmen kívül hagyott értéket erősítette
                # meg. JAVÍTVA: a ui_policy registry (EGYETLEN forrás a
                # mezőcsoport-láthatóságra) dönti el, mely test_type-ok
                # kapják a konfigurált értéket -- `visible_groups_for()`-t
                # használva (NEM `is_group_visible()`-t): az utóbbi
                # SZÁNDÉKOSAN fail-open ismeretlen test_type-ra (l.
                # ui_policy.py docstring), ami itt a RÖVIDEBB, operátor-
                # konfigurált relaxidőt választaná default-ként egy
                # ismeretlen módra -- `visible_groups_for()` fail-closed
                # (üres halmazt ad ismeretlen kulcsra), tehát egy jövőbeli,
                # itt nem kezelt test_type biztonságosan a KONZERVATÍVABB
                # _DEFAULT_RELAX_S-re esik, nem egy esetlegesen rövidebb
                # értékre.
                from Prog.gui.ui_policy import visible_groups_for
                if step.relax_purpose == "post_discharge":
                    relax_s = _POST_DISCHARGE_RELAX_S
                elif "relax_after_charge" in visible_groups_for(cfg.test_type):
                    relax_s = cfg.relax_after_charge_s
                else:
                    relax_s = _DEFAULT_RELAX_S   # 2h post-charge
                # Teljes kódbázis review 22. kör (2026-09-07), Szoftver/
                # Architektúra P2 lelet: az önálló StepKind.RELAX útnak
                # (ez a closure) nincs külső controllere, ami a hőmérséklet-
                # DMM kiesését/tartományát figyelné — l. relax_controller.py
                # konstruktor-kommentje.
                rc = RelaxController(
                    dmm_v, RelaxConfig(min_relax_s=relax_s),
                    dmm_temperature=dmm_t, safety=safety,
                )
                rc.on_event = lambda ev: _forward_controller_event(ev, "RELAX_EVENT")
                return rc

            ocv_soc_config = _ocv_soc_config_for(cfg)

            def _make_ocv_soc_ctrl():
                # Teljes kódbázis review 4. kör (2026-08-29), Session/Workflow
                # P1 lelet: l. shared_charge_cfg fenti kommentje — OCV_SOC
                # PRECHARGE ugyanazon a fizikai (TF1/soros dióda) útvonalon
                # tölt, mint TRADITIONAL_LEARNING/GOLDEN_GG, ezért ugyanazt a
                # session-szintű charge_config-ot kell kapnia.
                ctrl = OcvSocController(
                    psu, load, dmm_v, dmm_t, profile, safety, ocv_soc_config,
                    charge_config=shared_charge_cfg,
                )
                ctrl.on_soc_point = lambda data: logger.log_ocv_soc_point(data)
                # Governance-v2 review-kör 1 (CF-07, DFAM-011, batch 4.2):
                # korábban ennek a controllernek EGYÁLTALÁN NEM volt
                # bekötött on_event-je (l. ocv_soc_controller.py::__init__
                # kommentje) — a relax-fázis DMM-kiesése élőben
                # láthatatlan maradt.
                ctrl.on_event = lambda ev: _forward_controller_event(ev, "OCV_SOC_EVENT")
                # Teljes kódbázis review 14. kör (2026-09-04), Szoftver/
                # Architektúra + Session/Workflow/Állapotgép P2 lelet: a
                # testvér _make_chemid_ctrl()/_make_golden_gg_ctrl() mintáját
                # követve — enélkül _write_report() nem tudta kiolvasni az
                # OcvSocController relax_dmm_fault_total mezőjét.
                self._last_ocv_soc_ctrl = ctrl
                return ctrl

            def _make_chemid_ctrl():
                # WP-AK-05/SUP03-CHEMID-019: a NumCellSeries a hiteles profilból
                # származik (nincs külön, manuálisan is elrontható GUI-mező) —
                # a gpcchem_export.validate_num_cell_series() emiatt itt sosem
                # ütközne, csak jövőbeli/eltérő hívóutakra marad védelemként.
                chemid_config = ChemIdConfig()
                export_cfg = GpcChemConfig(
                    num_cell_series=profile.cell_count,
                    battery_model=cfg.battery_model,
                    battery_sample_id=cfg.sample_id,
                    manufacturer_cutoff_voltage_V=profile.terminate_voltage_pack_V,
                    # Review-lelet (WP-AK-05): a ChemIdConfig SAJÁT
                    # sample_interval_s mezőjével explicit szinkronban tartva —
                    # ha nem ide íródna, a két, egymástól független 30.0
                    # alapérték a jövőben csendben szétcsúszhatna.
                    sample_interval_s=chemid_config.sample_interval_s,
                )
                # SUP03-DATA-001/CHEMID-019: app_version/git_commit/operator
                # jelenleg sehonnan nem elérhető forrásadat ebben a projektben
                # (nincs verziókövetés/build-metaadat modul) — a mezők üresen
                # maradnak (nincs kitalált érték), DE ezt explicit, látható
                # limitációként rögzítjük a quality riportban, nem hallgatjuk el.
                missing_identifier_fields = [
                    name for name, value in (
                        ("AppVersion", export_cfg.app_version),
                        ("GitCommit", export_cfg.git_commit),
                        ("Operator", export_cfg.operator),
                    ) if not value
                ]
                source_ref = (
                    f"session_id={self._session_dir.name}\n"
                    f"config_hash={config_hash}\n"
                    f"profile_hash={profile_hash}\n"
                    f"instrument_identities={instrument_identities}\n"
                )
                ctrl = ChemIdCharacterizationController(
                    psu, load, dmm_v, dmm_t, profile, safety, chemid_config,
                    export_output_dir=self._session_dir / "gpcchem_export",
                    export_config=export_cfg,
                    source_session_reference=source_ref,
                    missing_identifier_fields=missing_identifier_fields,
                    # Teljes kódbázis review 4. kör (2026-08-29), Session/
                    # Workflow P1 lelet: l. shared_charge_cfg fenti
                    # kommentje/_make_ocv_soc_ctrl — CHEMID FULL_CHARGE_
                    # MANUFACTURER_METHOD ugyanazon a fizikai útvonalon tölt.
                    charge_config=shared_charge_cfg,
                )
                ctrl.on_event = lambda ev: _forward_controller_event(ev, "CHEMID_EVENT")
                self._last_chemid_ctrl = ctrl
                return ctrl

            def _make_traditional_learning_ctrl():
                # WP-AK-16: a controller saját, önálló állapotgép - a LearningConfig
                # defaultjai (relax_after_charge_s=7200/relax_after_discharge_s=18000
                # "master" minőségi ciklus, l. traditional_learning_controller.py)
                # elegendők, nincs GUI-specifikus felülbírálás ebben a körben.
                ctrl = TraditionalLearningController(
                    psu, load, dmm_v, dmm_t, bq_transport, profile, safety,
                    LearningConfig(),
                    charge_config=shared_charge_cfg,
                )
                # Release-audit 2026-08-23 (2.2): korábban nem volt bekötve —
                # a LEARNING_DONE (és minden más) esemény sosem jutott el az
                # eseménynaplóba. A result_summary-t (BQ learning eredmény-
                # klasszifikáció, CLAUDE.md §11.6) a session-szintű
                # _last_traditional_learning_ctrl referencián keresztül
                # _write_report() olvassa ki a session végén.
                ctrl.on_event = lambda ev: _forward_controller_event(
                    ev, "TRADITIONAL_LEARNING_EVENT"
                )
                # Feature (2026-08-23): a BQ Diagnosztika fül élő, passzív
                # nézete — l. gauge_snapshot_ready docstring.
                ctrl.on_gauge_sample = self.gauge_snapshot_ready.emit
                # Block 6 (2026-08-25): l. provisioning_consistency_ready docstring.
                ctrl.on_provisioning_consistency = self.provisioning_consistency_ready.emit
                self._last_traditional_learning_ctrl = ctrl
                return ctrl

            def _make_golden_gg_ctrl():
                # Release-audit 2026-08-23 (2.1): korábban export_config sosem
                # lett átadva — a GoldenGgConfig hiányában _run_export() az
                # EXPORT_PACKAGE lépésben rögtön DONE-ba lépett anélkül, hogy
                # bármilyen TI submission csomag ténylegesen elkészült volna,
                # miközben a "GOLDEN_GG_PACKAGE_READY" eseményt mégis kiadta.
                # A ChemID export (_make_chemid_ctrl) mintáját követve.
                golden_gg_export_cfg = GoldenGgConfig(
                    num_cell_series=profile.cell_count,
                    battery_model=cfg.battery_model,
                    battery_sample_id=cfg.sample_id,
                    system_rate_current_A=cfg.golden_gg_system_rate_current_A,
                    manufacturer_cutoff_voltage_V=profile.terminate_voltage_pack_V,
                )
                missing_metadata_fields = [
                    name for name, value in (
                        ("AppVersion", golden_gg_export_cfg.app_version),
                        ("GitCommit", golden_gg_export_cfg.git_commit),
                        ("Operator", golden_gg_export_cfg.operator),
                        # Governance-v2 review-kör 1 (CF-31, DFAM-003):
                        # golden_gg_export.py::GoldenGgConfig.to_text()
                        # SUP03-GG-003/004 szerint e két szöveges mező
                        # KÖTELEZŐ ("a rendszer NEM tudja automatikusan
                        # eldönteni, hogy a leírt felállás megfelelő-e"),
                        # de idáig sosem lett kitöltve ÉS sosem szerepelt
                        # a missing_metadata_fields-ben -- a config.txt
                        # csendben, jelöletlenül üresen ment ki a TI
                        # submissionba.
                        (
                            "ThermalEnvironmentDescription",
                            golden_gg_export_cfg.thermal_environment_description,
                        ),
                        (
                            "ThermistorPlacementDescription",
                            golden_gg_export_cfg.thermistor_placement_description,
                        ),
                    ) if not value
                ]
                source_ref = (
                    f"session_id={self._session_dir.name}\n"
                    f"config_hash={config_hash}\n"
                    f"profile_hash={profile_hash}\n"
                    f"instrument_identities={instrument_identities}\n"
                )
                ctrl = GoldenGgController(
                    psu, load, dmm_v, dmm_t, bq_transport, profile, safety,
                    GoldenGgPhysicalConfig(
                        system_rate_current_A=cfg.golden_gg_system_rate_current_A,
                    ),
                    export_output_dir=self._session_dir / "golden_gg_export",
                    export_config=golden_gg_export_cfg,
                    source_session_reference=source_ref,
                    missing_metadata_fields=missing_metadata_fields,
                    charge_config=shared_charge_cfg,
                )
                ctrl.on_event = lambda ev: _forward_controller_event(ev, "GOLDEN_GG_EVENT")
                # Feature (2026-08-23): a BQ Diagnosztika fül élő, passzív
                # nézete — l. gauge_snapshot_ready docstring.
                ctrl.on_gauge_sample = self.gauge_snapshot_ready.emit
                self._last_golden_gg_ctrl = ctrl
                return ctrl

            def _make_iec_dc_ctrl():
                # Governance-v2 kör 2: IEC 60896-21 §6.3 kétpontos DC belső
                # ellenállás mérés -- a PRECHARGE fázis ugyanazt a session-
                # szintű charge_config-ot kapja, mint OCV_SOC/CHEMID (l.
                # shared_charge_cfg kommentje fent/_make_ocv_soc_ctrl).
                # profile.i10_discharge_current_A hiánya esetén a
                # controller konstruktora explicit ConfigurationError-t dob
                # (l. iec_dc_resistance_controller.py) -- itt nem
                # duplikáljuk az ellenőrzést.
                ctrl = IecDcResistanceController(
                    psu, load, dmm_v, dmm_t, profile, safety,
                    IecDcResistanceConfig(),
                    charge_config=shared_charge_cfg,
                )
                ctrl.on_result = lambda data: logger.log_iec_dc_resistance_result(data)
                ctrl.on_event = lambda ev: _forward_controller_event(ev, "IEC_DC_RESISTANCE_EVENT")
                self._last_iec_dc_resistance_ctrl = ctrl
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
                    session_id=self._session_dir.name,
                    process_run_id=process_run_id,
                    parent_session_id=parent_session_id,
                    config_hash=config_hash,
                    profile_hash=profile_hash,
                    sample_id=cfg.sample_id,
                    instrument_identities=instrument_identities,
                    initial_charge_ah=resume_initial_charge_ah,
                    initial_discharge_ah=resume_initial_discharge_ah,
                    # CF-17/MET-R1-08 (batch 4.3): Wh-testvérpár.
                    initial_charge_wh=resume_initial_charge_wh,
                    initial_discharge_wh=resume_initial_discharge_wh,
                    # CF-18 + V-4 (batch 4.3, SPR-04 hátralévő fele):
                    # idő-origó + 5 diagnosztikai számláló.
                    initial_session_start_time_iso=resume_initial_session_start_time_iso,
                    initial_communication_faults_count=resume_initial_communication_faults_count,
                    initial_extreme_dt_s_count=resume_initial_extreme_dt_s_count,
                    initial_max_observed_dt_s=resume_initial_max_observed_dt_s,
                    initial_relax_dmm_fault_count=resume_initial_relax_dmm_fault_count,
                    initial_temperature_out_of_range_count=(
                        resume_initial_temperature_out_of_range_count
                    ),
                    initial_temp_dmm_fault_ticks=resume_initial_temp_dmm_fault_ticks,
                    initial_warning_code_counts=resume_initial_warning_code_counts,
                    initial_error_queue_deferred_s=resume_initial_error_queue_deferred_s,
                    initial_relax_missed_deadline_count=resume_initial_relax_missed_deadline_count,
                ),
                charge_ctrl_factory=_make_charge_ctrl,
                discharge_ctrl_factory=_make_discharge_ctrl,
                relax_ctrl_factory=_make_relax_ctrl,
                ocv_soc_ctrl_factory=_make_ocv_soc_ctrl,
                chemid_ctrl_factory=_make_chemid_ctrl,
                traditional_learning_ctrl_factory=_make_traditional_learning_ctrl,
                golden_gg_ctrl_factory=_make_golden_gg_ctrl,
                iec_dc_resistance_ctrl_factory=_make_iec_dc_ctrl,
            )

            # Review-kör 2 (2026-09-08), Session/Workflow/Állapotgép S-2
            # lelet + normal-effort independent verifier follow-up
            # (2026-09-09, P2 lelet): eddig semmi nem jelölte meg a SZÜLŐ
            # session könyvtárát, ha a folytatást elfogadták --
            # discover_incomplete_sessions() kizárólag azt nézi, hogy van-e
            # checkpoint.json ÉS nincs report.json; egy sikeresen
            # folytatott szülő session könyvtárában ez a feltétel
            # VÁLTOZATLANUL fennállt (a report.json az ÚJ, folytatott
            # session saját könyvtárába íródik, nem ide), tehát a szülő
            # MINDEN jövőbeli induláskor újra felkínálta magát folytatásra.
            # A meglévő "van report.json -> nem incomplete" szabályt
            # használjuk ki (nem új kizárási logikát vezetünk be).
            #
            # A jelölés SZÁNDÉKOSAN ITT, a metódus SIKERES visszatérése
            # ELŐTT, KÖZVETLENÜL történik (nem a fenti "Igen" válasz
            # pillanatában) — a verifier rámutatott, hogy a korábbi, korai
            # jelölés egy közbeeső hibát (Logger/TestPlan/controller-
            # factory építés a lenti except Exception ágig) esetén a szülőt
            # "resumed"-nek jelölte volna, miközben az ÚJ session sosem ért
            # el checkpointolható állapotot — az operátor haladása
            # (a szülő checkpoint.json-ja) emiatt LÁTHATATLANNÁ vált volna
            # minden GUI-oldali helyreállítási/informatív folyamat előtt,
            # jóllehet az adat fizikailag megmaradt volna a lemezen. Itt,
            # a sikeres return előtt jelölve ez a kockázat megszűnik: ha
            # bármi hibázik a construction alatt, a kivétel a jelölés
            # ELŐTT dob, a szülő továbbra is resumable marad (ugyanaz a
            # konzervatív irány, mint a fix előtti viselkedés volt egy
            # sikertelen resume-nál).
            if parent_session_id and match is not None:
                # Kör 21 F5 (SES21-02, DFAM-032+DFAM-004): ez az ÍRÁS az
                # EGYETLEN rekord arról, hogy a szülő resume-ját MÁR
                # felhasználtuk -- egy korábbi `except OSError: pass` itt
                # csendben elnyelte a hibát, ha a lemez-írás sikertelen
                # volt, és a szülő TOVÁBBRA IS resumable-nek látszott
                # volna a discover_incomplete_sessions()-nek. Egy
                # KÉSŐBBI indítás így ismét felkínálhatta volna a szülőt,
                # és két FÜGGETLEN gyerek-session indulhatott volna,
                # UGYANAZZAL a felhalmozott Ah/Wh-seeddel, UGYANAZON a
                # fizikai akkun -- napokig tartó mérés adatintegritását
                # sértve. SZÁNDÉKOSAN NEM fogjuk el itt: a hiba a metódust
                # KÖRÜLVEVŐ, MÁR meglévő `except Exception:` ágra fut
                # (lent), ami a hívó (`_on_connect_ready()`) `QMessageBox.
                # critical()`-ján keresztül BLOKKOLÓ operátori
                # figyelmeztetést ad, ÉS a hívó SAJÁT `return`-je (nem itt)
                # a worker/thread felépítése ELŐTT visszautasítja a
                # gyerek-session indítását -- ez a konzervatívabb irány
                # (CLAUDE.md §6.2: "bizonytalan állapot is hiba"), mint egy
                # néma, sikeres indítás egy MEGDUPLÁZOTT resume-mal.
                #
                # MAX verifier lelete (2026-09-19): a sima `write_text()`
                # NEM atomikus -- egy `open()` UTÁNI, de a teljes tartalom
                # kiírása ELŐTTI hiba (pl. ENOSPC) egy CSONKA report.json-t
                # hagyna a szülő könyvtárban, ami a `discover_incomplete_
                # sessions()`-t (letezés-alapú, nem parse-alapú) örökre
                # kizárná a resume-kínálatból, miközben a checkpoint.json
                # szerint a mérés folytatható lenne. Javítva: 1:1 a MÁR
                # LÉTEZŐ `logger.py::write_checkpoint()` tmp+rename mintáját
                # követve -- a `.write_text()` egy `.json.tmp` fájlra megy,
                # a végső, egy-lépéses `Path.replace()` csak a MÁR TELJES
                # tartalmat mozgatja a végleges névre (a legtöbb fájlrendszer
                # egy könyvtáron belüli rename-je LÁTHATÓSÁGRA atomikus).
                #
                # Kör 21 F15 (SES21-04) UTÓLAGOS KORREKCIÓJA (MAX verifier
                # F15-ös köre, 2026-09-19): `Path.replace()` UGYANAZ, mint
                # `os.replace()` (`Path.replace()` a CPython forrásában
                # szó szerint `os.replace(self, target)`-et hív) -- ez a
                # hely tehát az az EGYETLEN valódi sibling, amit a Kör 21
                # F15/SES21-04 archívum-sweepje ("grep os.replace") a
                # pathlib-hívási forma miatt NEM talált meg, holott ÉPP
                # EZ a hely a fixnek szánt logger.py-mintát vette át 1:1
                # (l. fenti komment). fsync a rename ELŐTT pótolva, mint
                # ott. FONTOS ASZIMMETRIA logger.py-hoz képest: EZ a marker
                # egy LÉTEZÉS-alapú (nem parse-alapú) fogyasztóval bír
                # (`session_recovery.py::discover_incomplete_sessions()`
                # a pontos fájlnevet nézi) -- egy csonka/0-hosszú fájl a
                # rename UTÁN is a FAIL-SAFE irányba esik (a szülő session
                # kimarad a resume-kínálatból, nem hamisan kínálja fel).
                # A fsync ETTŐL FÜGGETLENÜL helyes és szükséges (a tartalom
                # tényleges kiírását garantálja a rename előtt), de a
                # FIZIKAI (power-loss) tartósság-állítás -- ugyanúgy, mint
                # logger.py-nál -- HIL_PENDING marad, könyvtár-fsync nélkül
                # (Windows-on nem elérhető azonos módon).
                marker = match["session_dir"] / "report.json"
                marker_tmp = marker.with_suffix(".json.tmp")
                with marker_tmp.open("w", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "status": "RESUMED_INTO_NEW_SESSION",
                        "resumed_at_iso": datetime.now(timezone.utc).isoformat(),
                    }, indent=2, ensure_ascii=False))
                    f.flush()
                    os.fsync(f.fileno())
                marker_tmp.replace(marker)
            return runner, test_plan, resume_start_step_index
        except Exception:
            logger.close()
            # WP-AK-16: ha a hiba a BQ-transport megnyitása UTÁN, de a session
            # tényleges elindítása előtt történt, a session soha nem indult el
            # ténylegesen - a portot itt kell felszabadítani, különben a
            # kölcsönös kizárás flag-je hamisan "foglaltként" ragadna.
            # (a _close_session_bq_transport() a Diagnosztika fület is
            # visszaállítja normál, önálló-kapcsolódási módba)
            if needs_bq_transport:
                self._close_session_bq_transport()
            raise
