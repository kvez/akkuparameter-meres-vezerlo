"""
BqDiagnosticsPanel widget-szintű tesztek — WP-AK-07 kapu: érvénytelen
BqFieldReading SOHA nem jelenhet meg "0"-ként/hihető értékként a UI-n.
"""
from __future__ import annotations

from dataclasses import fields, replace

import pytest

from Prog.drivers.device_bq34z110 import Bq34z110ReadOnlyDriver
from Prog.gui.panels.bq_diagnostics_panel import BqDiagnosticsPanel
from Prog.src.bq_consistency_gate import (
    DfFieldComparison,
    ProvisioningConsistencyReport,
    ProvisioningGateDecision,
)
from Prog.src.bq_diagnostics import BqFieldReading, poll_bq_diagnostics
from Prog.tests.mock_drivers.mock_bq34z110_transport import MockBq34z110Transport


def make_driver() -> tuple[Bq34z110ReadOnlyDriver, MockBq34z110Transport]:
    transport = MockBq34z110Transport()
    driver = Bq34z110ReadOnlyDriver(transport=transport)
    return driver, transport


def _set_qmax_and_update_status(
    driver: Bq34z110ReadOnlyDriver, transport: MockBq34z110Transport,
    qmax: int, update_status: int,
) -> None:
    block = bytearray(32)
    block[0:2] = qmax.to_bytes(2, "big")
    block[4] = update_status
    transport.set_block(driver.DF_SC_STATE, driver.DF_BLOCK_STATE, bytes(block))


class TestConnectionUi:
    def test_connect_requested_emits_port_text(self, qapp):
        panel = BqDiagnosticsPanel()
        panel._port_edit.setText("COM7")
        ports = []
        panel.connect_requested.connect(ports.append)
        panel._connect_btn.click()
        assert ports == ["COM7"]

    def test_empty_port_does_not_emit(self, qapp):
        panel = BqDiagnosticsPanel()
        panel._port_edit.setText("")
        ports = []
        panel.connect_requested.connect(ports.append)
        panel._connect_btn.click()
        assert ports == []

    def test_set_connected_updates_status_and_button_states(self, qapp):
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        assert "COM7" in panel._status_lbl.text()
        assert panel._connect_btn.isEnabled() is False
        assert panel._disconnect_btn.isEnabled() is True

    def test_set_disconnected_resets_button_states(self, qapp):
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel.set_disconnected()
        assert panel._connect_btn.isEnabled() is True
        assert panel._disconnect_btn.isEnabled() is False

    def test_set_connected_disables_calibration_button(self, qapp):
        """[regresszió, Teljes kódbázis review 22. kör (2026-09-07), GUI/UX/
        Állapot P3 lelet] a "Gauge kalibráció…" gomb saját tooltipje
        előírja, hogy a BQ Diagnosztika kapcsolatnak bontva kell lennie —
        korábban ezt kizárólag a main_window.py kattintás-idejű
        QMessageBox.warning()-ja kényszerítette ki, a testvér-gombokkal
        (connect/disconnect/port) ellentétben a widget-állapot nem
        tükrözte, hogy a gomb elérhetetlen legyen élő kapcsolat alatt."""
        panel = BqDiagnosticsPanel()
        assert panel._calibration_btn.isEnabled() is True
        panel.set_connected("COM7")
        assert panel._calibration_btn.isEnabled() is False

    def test_set_disconnected_reenables_calibration_button(self, qapp):
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel.set_disconnected()
        assert panel._calibration_btn.isEnabled() is True

    def test_calibration_blocked_by_other_activity_disables_button(self, qapp):
        """[regresszió, Teljes kódbázis review 23. kör (2026-09-07), GUI/UX/
        Állapot P3 lelet] a 22. kör (deddebf) csak a panel SAJÁT bq_worker-
        alapú kapcsolat-állapotát kötötte be — a MainWindow-oldali 3
        további kölcsönös kizárási feltételt (fizikai teszt fut/BQ-
        kapcsolódás folyamatban/hardveres bring-up folyamatban) ez az
        additív blokkoló-jelző fedi le."""
        panel = BqDiagnosticsPanel()
        assert panel._calibration_btn.isEnabled() is True
        panel.set_calibration_blocked_by_other_activity(True)
        assert panel._calibration_btn.isEnabled() is False
        panel.set_calibration_blocked_by_other_activity(False)
        assert panel._calibration_btn.isEnabled() is True

    def test_calibration_blocked_by_other_activity_combines_with_own_connection(self, qapp):
        """A 2 forrás (saját bq-kapcsolat ÉS a MainWindow-jelző) egyike sem
        írhatja felül csendben a másikat — csak akkor engedélyezett a
        gomb, ha EGYIK forrás sem blokkolja."""
        panel = BqDiagnosticsPanel()
        panel.set_calibration_blocked_by_other_activity(True)
        panel.set_connected("COM7")  # saját kapcsolat is blokkol
        panel.set_disconnected()  # saját kapcsolat feloldva, DE a MainWindow-jelző még blokkol
        assert panel._calibration_btn.isEnabled() is False
        panel.set_calibration_blocked_by_other_activity(False)
        assert panel._calibration_btn.isEnabled() is True

    def test_session_owned_still_blocks_calibration_button(self, qapp):
        """A set_session_owned() útvonal a refaktor után is helyesen
        blokkol — nem sérülhetett a _refresh_calibration_button_enabled()
        bevezetésével."""
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, "test session")
        assert panel._calibration_btn.isEnabled() is False
        panel.set_session_owned(False)
        assert panel._calibration_btn.isEnabled() is True


class TestSnapshotRenderNeverSilentZero:
    """WP-AK-07 kapu: 'BQ disconnect / stale data / malformed reply nem
    válhat csendesen 0 értékké' — a UI-n is."""

    def test_valid_snapshot_shows_actual_values(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        transport.set_control_register(driver.MAC_CHEM_ID, 0x0800)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._value_labels["voltage_mV"].text() == "12500"
        assert panel._value_labels["chem_id"].text() == "0x0800"

    def test_invalid_qmax_shows_dash_not_zero(self, qapp):
        """Data Flash blokk sosem lett beállítva -> read_qmax() malformed-reply
        kivételt dob (WP-AK-07 driver-oldali kapu-javítás) -> a mezőnek "—"-t
        kell mutatnia, SOHA nem "0"-t."""
        driver, transport = make_driver()
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._value_labels["qmax"].text() == "—"
        assert panel._value_labels["qmax"].text() != "0"

    def test_invalid_field_gets_invalid_style_and_tooltip(self, qapp):
        driver, transport = make_driver()
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        label = panel._value_labels["qmax"]
        assert "c62828" in label.styleSheet()  # _INVALID_STYLE piros
        assert label.toolTip() != ""

    def test_ra_table_invalid_all_dashes_not_zero(self, qapp):
        driver, transport = make_driver()
        transport.simulate_checksum_error = True
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        for lbl in panel._ra_labels:
            assert lbl.text() == "—"

    def test_ra_table_valid_shows_15_values(self, qapp):
        driver, transport = make_driver()
        ra_block = bytes([0xFF, 0x55]) + b"".join(
            v.to_bytes(2, "big", signed=True) for v in range(1, 16)
        )
        transport.set_block(driver.DF_SC_RA_TABLE_CELL0, driver.DF_BLOCK_RA_TABLE, ra_block)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._ra_labels[0].text() == "1.0"
        assert panel._ra_labels[14].text() == "15.0"

    def test_ocvtaken_qen_vok_decoded_from_correct_registers(self, qapp):
        """WP-AK-06/07 tanulság analógiája: OCVTAKEN a Flags()-ből, QEN/VOK
        a CONTROL_STATUS()-ból jön — két külön regiszter, nem szabad
        összekeveredniük a GUI-n."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_FLAGS, 0x0080)  # csak OCVTAKEN
        transport.set_control_register(driver.MAC_CONTROL_STATUS, 0x0003)  # QEN+VOK
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._value_labels["ocvtaken"].text() == "IGEN"
        assert panel._value_labels["qen"].text() == "IGEN"
        assert panel._value_labels["vok"].text() == "IGEN"
        assert panel._value_labels["rup_dis"].text() == "nem"


class TestFreshness:
    def test_disconnect_marks_all_fields_stale(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert "c62828" not in panel._value_labels["voltage_mV"].styleSheet()
        panel.set_disconnected()
        assert "c62828" in panel._value_labels["voltage_mV"].styleSheet()

    def test_disconnect_clears_stale_field_text_to_dash(self, qapp):
        """[regresszió, Teljes kódbázis review 17. kör (2026-09-05),
        GUI/UX/Állapot P1 lelet] _mark_all_stale() korábban csak a
        stílust váltotta pirosra, a szöveget nem törölte — az utolsó
        érvényes eszközérték piros színnel, de valós számként maradt
        látható szétkapcsolás után."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._value_labels["voltage_mV"].text() != "—"
        panel.set_disconnected()
        assert panel._value_labels["voltage_mV"].text() == "—"
        for lbl in panel._ra_labels:
            assert lbl.text() == "—"

    def test_disconnect_clears_stale_field_tooltip(self, qapp):
        """[regresszió, Teljes kódbázis review 18. kör (2026-09-05), GUI/UX/
        Állapot P3 lelet] a 17. kör _mark_all_stale()-fixe a szöveget/
        stílust törölte, de a tooltipet nem — ha az utolsó sikeres
        kapcsolat alatti utolsó olvasás egy mezőn érvénytelen volt, a
        kapcsolat bontása után a "—" felirat felett egy RÉGI, már
        irreleváns hibaüzenet maradt látható."""
        driver, _transport = make_driver()
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        # A qmax mező kezdettől érvénytelen (nincs beállítva Data Flash
        # blokk) -> konkrét hibaüzenetes tooltipet kap.
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._value_labels["qmax"].toolTip() != ""
        panel.set_disconnected()
        assert panel._value_labels["qmax"].toolTip() == ""

    def test_disconnect_clears_provisioning_consistency_table(self, qapp):
        """[regresszió, Teljes kódbázis review 20. kör (2026-09-06), GUI/UX/
        Állapot P2 lelet] a Provisioning konzisztencia (Block 2) táblázat
        korábban csak set_session_owned(active=True, ...)-nál törlődött —
        önálló Bontás után egy korábbi, más session-hez tartozó
        gate-eredmény VÉGLEGESEN megmaradt. (Review-kör 5, GUI G5-01 P2:
        a set_session_owned(active=False) SESSION-VÉG útja azóta NEM
        delegál ide — l. TestSessionOwnedMode.test_session_owned_false_
        preserves_evidence_for_operator_review lent.)"""
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        report = ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.CONSISTENT,
            reason="minden vizsgált DF-mező a profil-elvárt tolerancián belül",
            comparisons=(
                DfFieldComparison(
                    field_name="design_capacity_mAh", profile_expected=7000,
                    bq_readback=7000, unit="mAh", tolerance=50, within_tolerance=True,
                ),
            ),
        )
        panel.set_provisioning_consistency(report)
        assert panel._provisioning_status_lbl.text() != "— (nincs adat)"

        panel.set_disconnected()

        assert panel._provisioning_status_lbl.text() == "— (nincs adat)"
        for expected_lbl, readback_lbl, tolerance_lbl, status_lbl in (
            panel._provisioning_rows.values()
        ):
            for lbl in (expected_lbl, readback_lbl, tolerance_lbl, status_lbl):
                assert lbl.text() == "—"

    def test_freshness_label_updates_after_snapshot(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        assert panel._freshness_lbl.text() == ""
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert "frissítés" in panel._freshness_lbl.text()

    def test_all_invalid_snapshot_does_not_count_as_successful_refresh(self, qapp):
        """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F6 (P3,
        DFAM-018)] poll_bq_diagnostics() minden mezőt külön try/except-be
        csomagol, ezért egy fizikai Pico-lecsatlakozásnál NEM dob kivételt
        — egy teljesen érvénytelen (minden valid=False) snapshotot ad
        vissza. Ezt a panel korábban feltétel nélkül "sikeres frissítésnek"
        könyvelte el (self._last_success_perf mindig frissült), így az
        "ELAVULT ADAT" jelzés soha nem állt volna be egy valódi kiesésnél."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._last_success_perf is not None

        panel._last_success_perf = None  # a "sosem volt sikeres olvasás" allapot szimulalasa
        transport.simulate_timeout = True
        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._last_success_perf is None, (
            "egy teljesen ervenytelen snapshot NEM szamithat sikeres frissitesnek"
        )

    def test_poll_timeout_marks_all_value_labels_stale(self, qapp):
        """[regresszió, Kör 17 MAX-effort review, R17-23, DFAM-001+018]
        a poll-timeout ág korábban KIZÁRÓLAG a _freshness_lbl fejléc-
        cimkét jelölte "ELAVULT ADAT"-nak -- a 19+15 (Ra-tábla) érték-
        cimke az UTOLSÓ JÓ értéken maradt, jelöletlenül, holott a panel
        MÁR rendelkezik a _mark_all_stale() mechanizmussal (amit a
        testvér live_panel.py is átvett, CF-27)."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert "c62828" not in panel._value_labels["voltage_mV"].styleSheet()

        panel._last_success_perf -= 10.0  # jocskan a stale_threshold_s fole
        panel._refresh_freshness_label()

        assert "ELAVULT ADAT" in panel._freshness_lbl.text()
        assert panel._value_labels["voltage_mV"].text() == "—"
        assert "c62828" in panel._value_labels["voltage_mV"].styleSheet()
        for lbl in panel._ra_labels:
            assert lbl.text() == "—"

    def test_successful_poll_after_timeout_clears_all_markers(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        panel._last_success_perf -= 10.0
        panel._refresh_freshness_label()
        assert panel._value_labels["voltage_mV"].text() == "—"

        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._value_labels["voltage_mV"].text() != "—"
        assert "c62828" not in panel._value_labels["voltage_mV"].styleSheet()

    def test_values_not_marked_before_stale_threshold(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        panel._refresh_freshness_label()  # frissen frissult, meg nem stale

        assert panel._value_labels["voltage_mV"].text() != "—"
        assert "c62828" not in panel._value_labels["voltage_mV"].styleSheet()

    def test_no_marking_before_first_successful_snapshot(self, qapp):
        panel = BqDiagnosticsPanel()
        panel._refresh_freshness_label()  # _last_success_perf is None -> korai return
        assert panel._freshness_lbl.text() == ""

    def test_timeout_marking_is_repeatable_after_recovery(self, qapp):
        """A _all_stale_marked teljesitmeny-optimalizalo flag NE
        akadalyozza meg egy MASODIK stale-atmenet jeloleset egy
        sikeres helyreallitas utan."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        panel._last_success_perf -= 10.0
        panel._refresh_freshness_label()
        assert panel._value_labels["voltage_mV"].text() == "—"

        panel.update_snapshot(poll_bq_diagnostics(driver))  # helyreallitas
        assert panel._value_labels["voltage_mV"].text() != "—"

        panel._last_success_perf -= 10.0  # ismet stale
        panel._refresh_freshness_label()

        assert panel._value_labels["voltage_mV"].text() == "—"


class TestKor20BatchE16NoDataYetIndicatorAndConnectionErrorRecovery:
    """Kör 20 Batch E16 (F20-G5 + F20-G6, DFAM-018+DFAM-040):
    F20-G5: a _refresh_freshness_label() korábban CSENDBEN maradt üres, ha
    a chip a kapcsolódás óta SOHA nem adott vissza egyetlen érvényes mezőt
    sem. F20-G6: set_connection_error() piros "Kapcsolódási hiba"-ra
    állítja a _status_lbl-t, de korábban SEMMI nem állította vissza egy
    KÉSŐBBI, sikeres frissítésen."""

    # -- F20-G5: "MÉG NEM ÉRKEZETT ÉRVÉNYES BQ-ADAT" ------------------- #

    def test_freshness_label_shows_no_data_yet_after_connect_without_valid_snapshot(self, qapp):
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel._refresh_freshness_label()
        assert panel._freshness_lbl.text() == "MÉG NEM ÉRKEZETT ÉRVÉNYES BQ-ADAT"
        assert "c62828" in panel._freshness_lbl.styleSheet()

    def test_freshness_label_no_data_yet_message_clears_on_first_valid_snapshot(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel._refresh_freshness_label()
        assert panel._freshness_lbl.text() == "MÉG NEM ÉRKEZETT ÉRVÉNYES BQ-ADAT"

        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert "frissítés" in panel._freshness_lbl.text()

    def test_freshness_label_stays_blank_when_never_connected_at_all(self, qapp):
        """Counter-teszt: a fel-nem-csatlakozott (kezdeti) állapot NEM
        kaphatja meg a "MÉG NEM ÉRKEZETT" üzenetet -- az egy KAPCSOLÓDOTT,
        de adat nélküli állapotot jelez, nem az "even connect sem történt"
        alapállapotot."""
        panel = BqDiagnosticsPanel()
        panel._refresh_freshness_label()
        assert panel._freshness_lbl.text() == ""

    def test_disconnect_clears_no_data_yet_state_for_next_connection(self, qapp):
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel._refresh_freshness_label()
        assert panel._freshness_lbl.text() == "MÉG NEM ÉRKEZETT ÉRVÉNYES BQ-ADAT"

        panel.set_disconnected()

        assert panel._freshness_lbl.text() == ""
        assert panel._connected_since_perf is None

    def test_session_owned_mode_also_shows_no_data_yet_message(self, qapp):
        """A session-vezérelt belépési pont (set_session_owned(True, ...))
        ugyanazt a "belépési pillanat" mintát kapja, mint az önálló
        set_connected() -- ha a session saját gauge-lekérdezése SOHA nem
        ad érvényes mezőt, a jelzésnek itt is meg kell jelennie."""
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, session_label="Golden GG")
        panel._refresh_freshness_label()
        assert panel._freshness_lbl.text() == "MÉG NEM ÉRKEZETT ÉRVÉNYES BQ-ADAT"

    def test_session_owned_entry_resets_stale_freshness_from_prior_own_connection(self, qapp):
        """Widget-szintű API-önkonzisztencia teszt (NEM egy ma élő
        MainWindow-race reprodukciója -- NORMAL verifier önállóan
        igazolta, hogy a main_window.py:3565 hívóhely elé a _start_test()/
        _build_runner() már meglévő kölcsönös-kizárási kapui miatt egy
        önálló kapcsolat MINDIG le van bontva, mielőtt egy session átveheti
        a portot). A panel API-ja mégis ugyanazt az "belépési pillanat
        nullázza a régi frissesség-időbélyeget" mintát követi minden
        be- és kilépési pontján (set_connected()/_reset_connection_ui()),
        ezért set_session_owned(True, ...)-nak ÖNMAGÁBAN, a hívó
        garanciáitól függetlenül is helyesen kell viselkednie, ha valaha
        MÁS hívó (pl. egy jövőbeli teszt/eszköz) közvetlenül, set_
        disconnected() közbeiktatása nélkül hívná."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._last_success_perf is not None

        panel.set_session_owned(True, session_label="Golden GG")

        assert panel._last_success_perf is None
        panel._refresh_freshness_label()
        assert panel._freshness_lbl.text() == "MÉG NEM ÉRKEZETT ÉRVÉNYES BQ-ADAT"

    # -- F20-G6: a kapcsolódási hiba-állapot ne legyen sticky ---------- #

    def test_connection_error_status_is_restored_by_next_successful_snapshot(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel.set_connection_error("simulated I/O hiba")
        assert "Kapcsolódási hiba" in panel._status_lbl.text()

        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert "Kapcsolódva: COM7" in panel._status_lbl.text()
        assert panel._connect_btn.isEnabled() is False
        assert panel._disconnect_btn.isEnabled() is True

    def test_connection_error_not_restored_by_all_invalid_snapshot(self, qapp):
        """Counter-teszt: egy TOVÁBBRA IS érvénytelen (minden mező
        valid=False) snapshot NEM állíthatja vissza a kapcsolódott
        státuszt -- csak egy VALÓDI, legalább egy érvényes mezőt hozó
        frissítés."""
        driver, transport = make_driver()
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel.set_connection_error("simulated I/O hiba")
        transport.simulate_timeout = True

        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert "Kapcsolódási hiba" in panel._status_lbl.text()

    def test_connection_error_recovery_does_not_fire_in_session_owned_mode(self, qapp):
        """A session-vezérelt módban (set_session_owned(True, ...)) egy
        sikeres snapshot NEM hívhatja set_connected()-et -- felülírná a
        "Session vezérli (…) — csak olvasás" állapotot és a session alatt
        szándékosan letiltott connect/disconnect gombokat."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, session_label="Golden GG")

        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert "Session vezérli" in panel._status_lbl.text()
        assert panel._connect_btn.isEnabled() is False
        assert panel._disconnect_btn.isEnabled() is False


class TestKor21BatchF3LateSnapshotAfterTeardownDoesNotFakeConnection:
    """Kör 21 Batch F3 (F21-G1, DFAM-032, a Kör 20 E16/F20-G6 fix saját
    regressziója): a `not self._session_owned` guard önmagában hamis-
    negatív egy KÉSVE kézbesített snapshot esetén (session-vég UTÁN a Qt
    event-queue-ban ragadt `snapshot_ready`/`gauge_snapshot_ready`) --
    `set_session_owned(False)` már törölte `_session_owned`-et ÉS
    `_reset_connection_ui()`-n át `_connected_port`-ot is, tehát a régi
    guard átengedte a hívást egy `set_connected("")`-ra: a panel HAMISAN
    "Kapcsolódva: " (üres portnév) állapotba került, letiltva Connect/
    port/kalibráció gombokat, amikor NINCS kapcsolat."""

    def test_late_snapshot_after_session_teardown_does_not_fake_connected_state(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, session_label="Golden GG")
        panel.set_session_owned(False)
        assert panel._connected_port == ""
        assert panel._session_owned is False

        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._status_lbl.text() == "Nincs kapcsolat"
        assert panel._connect_btn.isEnabled() is True
        assert panel._own_bq_connection_blocks_calibration is False

    def test_late_snapshot_after_standalone_disconnect_does_not_fake_connected_state(self, qapp):
        """Ugyanaz az alakzat az önálló (nem session-vezérelt) Kapcsolódás/
        Bontás úton -- `set_disconnected()` is `_reset_connection_ui()`-n
        megy át, tehát `_connected_port` itt is üresre törlődik, DE
        `_session_owned` már eleve False volt (sosem session-vezérelt)."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel.set_disconnected()
        assert panel._connected_port == ""

        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._status_lbl.text() == "Nincs kapcsolat"
        assert panel._connect_btn.isEnabled() is True

    def test_f20_g6_sticky_connection_error_recovery_still_works_while_connected(self, qapp):
        """Counter-teszt (§2.4 kikötés): a F21-G1 fix NEM szabad, hogy
        visszanyissa a F20-G6-ot -- egy sikeres frissítés egy VALÓS, aktív
        kapcsolat mellett (`_connected_port` nem üres) TOVÁBBRA IS
        visszaállítja a korábbi `set_connection_error()`-t."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel.set_connection_error("simulated I/O hiba")
        assert "Kapcsolódási hiba" in panel._status_lbl.text()

        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert "Kapcsolódva: COM7" in panel._status_lbl.text()
        assert panel._connect_btn.isEnabled() is False
        assert panel._disconnect_btn.isEnabled() is True


class TestCheckpointStyleEventLog:
    """Feature (2026-08-23, felhasználói kérés): "checkpoint jelleggel"
    figyelt esemény-lista — UpdateStatus/Qmax/QEN/VOK/OCVTAKEN/RUP_DIS
    változásakor, időbélyeggel, a meglévő EventLogWidget-et újrahasznosítva."""

    def test_first_valid_reading_does_not_emit_event(self, qapp):
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x02)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._session_event_log._list.count() == 0

    def test_update_status_change_emits_event(self, qapp):
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x02)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x05)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._session_event_log._list.count() == 1
        text = panel._session_event_log._list.item(0).text()
        assert "UpdateStatus" in text
        assert "0x0002" in text and "0x0005" in text

    def test_qmax_change_emits_event(self, qapp):
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x02)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        _set_qmax_and_update_status(driver, transport, qmax=150, update_status=0x02)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._session_event_log._list.count() == 1
        assert "Qmax" in panel._session_event_log._list.item(0).text()

    def test_unchanged_value_does_not_emit_event(self, qapp):
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x02)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._session_event_log._list.count() == 0

    def test_ocvtaken_bit_change_emits_event(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_FLAGS, 0x0000)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        transport.set_register(driver.STD_CMD_FLAGS, 0x0080)  # OCVTAKEN bit
        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._session_event_log._list.count() == 1
        assert "OCVTAKEN" in panel._session_event_log._list.item(0).text()

    def test_set_disconnected_clears_session_event_log(self, qapp):
        """[regresszió, Teljes kódbázis review 16. kör (2026-09-04), GUI/UX/
        Állapot P3 lelet] a 7. kör fixje (953679e) csak a _last_field_values
        (változás-detektáló) dict-et törölte set_disconnected()-ben — a
        TÉNYLEGESEN MEGJELENÍTETT _session_event_log-ot nem, így egy korábbi
        (más fizikai kapcsolathoz tartozó) bejegyzés a jelenlegi session
        bizonyítékaként tűnhetett fel egy újracsatlakozás után."""
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x02)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x05)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._session_event_log._list.count() == 1

        panel.set_disconnected()

        assert panel._session_event_log._list.count() == 0
        panel.set_connected("COM7")  # R1 F-29: új kapcsolat (előfeltétel)

        # Új kapcsolat, új esemény — a régi bejegyzés nem térhet vissza.
        driver2, transport2 = make_driver()
        _set_qmax_and_update_status(driver2, transport2, qmax=200, update_status=0x02)
        panel.update_snapshot(poll_bq_diagnostics(driver2))
        _set_qmax_and_update_status(driver2, transport2, qmax=200, update_status=0x06)
        panel.update_snapshot(poll_bq_diagnostics(driver2))

        assert panel._session_event_log._list.count() == 1

    def test_set_session_owned_active_clears_session_event_log(self, qapp):
        """L. a fenti teszt docstringje — ugyanaz a védelem
        set_session_owned(active=True)-ra is vonatkozik."""
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x02)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x05)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._session_event_log._list.count() == 1

        panel.set_session_owned(True, "TRADITIONAL_LEARNING")

        assert panel._session_event_log._list.count() == 0

    def test_qen_vok_bit_change_emits_events(self, qapp):
        driver, transport = make_driver()
        transport.set_control_register(driver.MAC_CONTROL_STATUS, 0x0000)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        transport.set_control_register(driver.MAC_CONTROL_STATUS, 0x0003)  # QEN+VOK
        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._session_event_log._list.count() == 2

    def test_invalid_reading_does_not_emit_or_move_baseline(self, qapp):
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x02)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        transport.simulate_checksum_error = True  # qmax/update_status most invalid
        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._session_event_log._list.count() == 0

    def test_disconnect_reconnect_does_not_falsely_report_change(self, qapp):
        """[regresszió, Teljes kódbázis review 7. kör (2026-08-30), GUI/UX/
        Állapot P3 lelet] set_disconnected() korábban nem törölte
        _last_field_values-t — ha az operátor egy önálló BQ Diagnosztika
        kapcsolatot bont, majd (akár egy MÁSIK eszközzel/akkuval) újra
        csatlakozik, az ELSŐ új snapshot a korábbi kapcsolat utolsó
        értékéhez lett hasonlítva, két időben/fizikailag össze nem függő
        kapcsolat adatait "változásként" összemosva."""
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x02)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")  # R1 F-29: élő kapcsolat (előfeltétel)
        panel.update_snapshot(poll_bq_diagnostics(driver))  # baseline: qmax=100

        panel.set_disconnected()

        # "Újracsatlakozás" egy eltérő értékű eszközzel/akkuval — ez az ÚJ
        # kapcsolat ELSŐ olvasása, nem szabad "változásnak" számítania.
        panel.set_connected("COM7")  # R1 F-29: új kapcsolat (előfeltétel)
        _set_qmax_and_update_status(driver, transport, qmax=150, update_status=0x02)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._session_event_log._list.count() == 0


class TestSessionOwnedMode:
    """Feature (2026-08-23, felhasználói kérés): session-vezérelt, passzív
    élő nézet — nincs Kapcsolódás/Bontás/Kalibráció gomb, a session saját
    gauge-lekérdezéséből táplálva (update_snapshot() ugyanaz a metódus)."""

    def test_session_owned_disables_connection_controls(self, qapp):
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, session_label="Golden GG")

        assert panel._connect_btn.isEnabled() is False
        assert panel._disconnect_btn.isEnabled() is False
        assert panel._calibration_btn.isEnabled() is False
        assert panel._port_edit.isEnabled() is False
        assert "Golden GG" in panel._status_lbl.text()

    def test_session_owned_false_restores_normal_state(self, qapp):
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, session_label="Golden GG")
        panel.set_session_owned(False)

        assert panel._connect_btn.isEnabled() is True
        assert panel._port_edit.isEnabled() is True

    def test_session_owned_false_preserves_evidence_for_operator_review(self, qapp):
        """Review-kör 5, GUI G5-01 (P2): session-vég (set_session_owned
        (False)) korábban a set_disconnected()-et hívta, ami a bizonyíték-
        felületet (event-log, provisioning-táblázat) IS törölte — pedig az
        operátor jellemzően PONT a session vége UTÁN akarja átnézni a
        lezárult session eredményét. A kapcsolat-UI-nak VISSZA kell állnia
        "Nincs kapcsolat"-ra, de az evidence-nek MEG kell maradnia."""
        driver, transport = make_driver()
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, session_label="Traditional Learning")
        panel.update_snapshot(poll_bq_diagnostics(driver))
        # Egy esemény a naplóba, ugyanazzal a dict-alapú API-val, mint amit
        # a panel saját _check_bit_change()/_check_field_change() használ.
        panel._session_event_log.append_event({
            "event_code": "TEST_EVENT", "event_message": "VOK törölve (esemény teszt)",
        })
        report = ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.CONSISTENT,
            reason="minden vizsgált DF-mező a profil-elvárt tolerancián belül",
            comparisons=(
                DfFieldComparison(
                    field_name="design_capacity_mAh", profile_expected=7000,
                    bq_readback=7000, unit="mAh", tolerance=50, within_tolerance=True,
                ),
            ),
        )
        panel.set_provisioning_consistency(report)
        assert panel._session_event_log._list.count() == 1
        assert panel._provisioning_status_lbl.text() != "— (nincs adat)"

        panel.set_session_owned(False)

        assert panel._status_lbl.text() == "Nincs kapcsolat"
        assert panel._connect_btn.isEnabled() is True
        assert panel._session_event_log._list.count() == 1, (
            "a lezárult session event-naplója NEM veszhet el a session vége után"
        )
        assert panel._provisioning_status_lbl.text() != "— (nincs adat)", (
            "a lezárult session provisioning-gate eredménye NEM veszhet el "
            "a session vége után"
        )

    def test_session_owned_accepts_live_snapshots(self, qapp):
        """A session-vezérelt módban is a SZOKÁSOS update_snapshot()
        metódus tölti fel a mezőket — nincs külön render-útvonal."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12800)
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, session_label="Traditional Learning")

        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert panel._value_labels["voltage_mV"].text() == "12800"


class TestProvisioningConsistencyRender:
    """Block 6 (2026-08-25) — set_provisioning_consistency() render."""

    def test_none_shows_placeholder(self, qapp):
        panel = BqDiagnosticsPanel()
        panel.set_provisioning_consistency(None)
        assert "nincs adat" in panel._provisioning_status_lbl.text()
        for expected_lbl, readback_lbl, tolerance_lbl, status_lbl in panel._provisioning_rows.values():
            assert expected_lbl.text() == "—"
            assert readback_lbl.text() == "—"
            assert status_lbl.text() == "—"

    def test_consistent_report_shows_ok_status(self, qapp):
        panel = BqDiagnosticsPanel()
        report = ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.CONSISTENT,
            reason="minden vizsgált DF-mező a profil-elvárt tolerancián belül",
            comparisons=(
                DfFieldComparison(
                    field_name="design_capacity_mAh", profile_expected=7000,
                    bq_readback=7000, unit="mAh", tolerance=50, within_tolerance=True,
                ),
                DfFieldComparison(
                    field_name="number_of_series_cells", profile_expected=6,
                    bq_readback=6, unit="db", tolerance=0, within_tolerance=True,
                ),
            ),
        )
        panel.set_provisioning_consistency(report)
        assert "CONSISTENT" in panel._provisioning_status_lbl.text()
        expected_lbl, readback_lbl, _tolerance_lbl, status_lbl = panel._provisioning_rows["design_capacity_mAh"]
        assert expected_lbl.text() == "7000 mAh"
        assert readback_lbl.text() == "7000 mAh"
        assert status_lbl.text() == "OK"

    def test_mismatched_field_shows_elter_status_others_unaffected(self, qapp):
        panel = BqDiagnosticsPanel()
        report = ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH,
            reason="eltérő mező(k) a toleranciasávon kívül: design_capacity_mAh",
            comparisons=(
                DfFieldComparison(
                    field_name="design_capacity_mAh", profile_expected=7000,
                    bq_readback=4000, unit="mAh", tolerance=50, within_tolerance=False,
                ),
                DfFieldComparison(
                    field_name="number_of_series_cells", profile_expected=6,
                    bq_readback=6, unit="db", tolerance=0, within_tolerance=True,
                ),
            ),
        )
        panel.set_provisioning_consistency(report)
        assert "BLOCKED_CONFIGURATION_MISMATCH" in panel._provisioning_status_lbl.text()
        mismatch_row = panel._provisioning_rows["design_capacity_mAh"]
        assert mismatch_row[1].text() == "4000 mAh"
        assert mismatch_row[3].text() == "ELTÉR"
        ok_row = panel._provisioning_rows["number_of_series_cells"]
        assert ok_row[3].text() == "OK"

    def test_readback_failure_report_has_no_comparisons(self, qapp):
        panel = BqDiagnosticsPanel()
        report = ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.BLOCKED_BQ_DF_READBACK_FAILED,
            reason="BQ Data Flash olvasás sikertelen: simulated",
            comparisons=(),
        )
        panel.set_provisioning_consistency(report)
        assert "BLOCKED_BQ_DF_READBACK_FAILED" in panel._provisioning_status_lbl.text()
        for expected_lbl, *_rest in panel._provisioning_rows.values():
            assert expected_lbl.text() == "—"

    def test_set_session_owned_true_resets_previous_report(self, qapp):
        """Egy ÚJ session ne örökölje az előző session gate-eredményét."""
        panel = BqDiagnosticsPanel()
        panel.set_provisioning_consistency(ProvisioningConsistencyReport(
            decision=ProvisioningGateDecision.CONSISTENT, reason="ok", comparisons=(),
        ))
        panel.set_session_owned(True, session_label="Traditional Learning")
        assert "nincs adat" in panel._provisioning_status_lbl.text()


def _event_log_texts(panel: BqDiagnosticsPanel) -> list[str]:
    lst = panel._session_event_log._list
    return [lst.item(i).text() for i in range(lst.count())]


class TestR1F29LateSignalsAfterTeardownAndConnectionBoundBaseline:
    """[regresszió, R1 F-29, DFAM-018+DFAM-001] Bontáskor a GUI-szál a
    `_cleanup_bq_thread()` `wait()`-jében blokkol, ezért a worker szálán már
    kibocsátott, a GUI-sorban várakozó `snapshot_ready`/`connection_error`
    (ill. session-vég után a `gauge_snapshot_ready`) CSAK a
    `set_disconnected()`/`set_session_owned(False)` UTÁN kézbesül. A panel
    korábban ezt kapcsolat nélkül is renderelte (érték, "utolsó frissítés",
    alap-mozgatás, esemény-napló), ill. a hibajelet "Kapcsolódási hiba"-ként
    mutatta egy "Nincs kapcsolat" állapotban. Az összehasonlítási alap
    (_last_field_values) session-vég után sem ürült, így egy következő önálló
    kapcsolat első olvasása a session utolsó értékeihez hasonlított."""

    # -- (a) késő snapshot önálló bontás után ------------------------- #

    def test_late_snapshot_after_standalone_disconnect_is_not_rendered(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        snapshot = poll_bq_diagnostics(driver)
        panel.update_snapshot(snapshot)
        assert panel._value_labels["voltage_mV"].text() == "12500"

        panel.set_disconnected()
        panel.update_snapshot(snapshot)  # a GUI-sorban ragadt, késve kézbesített

        assert panel._value_labels["voltage_mV"].text() == "—"
        assert panel._last_success_perf is None
        assert panel._last_field_values == {}
        assert "frissítés" not in panel._freshness_lbl.text()
        assert panel._status_lbl.text() == "Nincs kapcsolat"

    # -- (b) késő gauge-snapshot session-vég után; evidence megőrzése --- #

    def test_late_gauge_snapshot_after_session_end_not_rendered_and_evidence_kept(self, qapp):
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        _set_qmax_and_update_status(driver, transport, qmax=7000, update_status=0x04)
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, "TRADITIONAL_LEARNING")
        panel.update_snapshot(poll_bq_diagnostics(driver))
        _set_qmax_and_update_status(driver, transport, qmax=7000, update_status=0x05)
        panel.update_snapshot(poll_bq_diagnostics(driver))
        evidence_before = _event_log_texts(panel)
        assert len(evidence_before) == 1  # előre feltöltött, NEM üres napló
        assert "BQ_UPDATE_STATUS_CHANGED" in evidence_before[0]

        panel.set_session_owned(False)
        _set_qmax_and_update_status(driver, transport, qmax=6500, update_status=0x06)
        panel.update_snapshot(poll_bq_diagnostics(driver))  # késve kézbesített

        assert panel._value_labels["voltage_mV"].text() == "—"
        assert panel._value_labels["update_status"].text() == "—"
        assert panel._last_success_perf is None
        assert panel._status_lbl.text() == "Nincs kapcsolat"
        assert _event_log_texts(panel) == evidence_before  # nem nő, nem vész el

    # -- (c) session-vég -> új önálló kapcsolat: friss alap ------------ #

    def test_new_standalone_connection_after_session_end_has_fresh_baseline(self, qapp):
        driver1, transport1 = make_driver()
        _set_qmax_and_update_status(driver1, transport1, qmax=7000, update_status=0x04)
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, "TRADITIONAL_LEARNING")
        panel.update_snapshot(poll_bq_diagnostics(driver1))
        panel.set_session_owned(False)

        driver2, transport2 = make_driver()  # egy MÁSIK chip
        _set_qmax_and_update_status(driver2, transport2, qmax=5000, update_status=0x05)
        panel.set_connected("COM7")
        panel.update_snapshot(poll_bq_diagnostics(driver2))

        texts = _event_log_texts(panel)
        assert not any("BQ_UPDATE_STATUS_CHANGED" in t for t in texts), texts
        assert not any("BQ_QMAX_CHANGED" in t for t in texts), texts
        assert panel._value_labels["qmax"].text() == "5000"

    # -- pozitív kontroll: ugyanazon kapcsolaton a valódi változás esemény #

    def test_same_connection_real_change_after_error_and_recovery_still_emits_event(self, qapp):
        """Az alap KIZÁRÓLAG valódi kapcsolat-KEZDÉSKOR ürülhet: a sikeres
        poll miatti ismételt set_connected() (F20-G6 helyreállítás) NEM
        nyelheti el az ugyanazon kapcsolat valódi UpdateStatus-változását."""
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x04)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel.update_snapshot(poll_bq_diagnostics(driver))
        panel.set_connection_error("simulated I/O hiba")
        assert "Kapcsolódási hiba" in panel._status_lbl.text()

        _set_qmax_and_update_status(driver, transport, qmax=100, update_status=0x05)
        panel.update_snapshot(poll_bq_diagnostics(driver))

        assert "Kapcsolódva: COM7" in panel._status_lbl.text()
        texts = _event_log_texts(panel)
        assert len(texts) == 1, texts
        assert "BQ_UPDATE_STATUS_CHANGED" in texts[0]
        assert "0x0004" in texts[0] and "0x0005" in texts[0]

    # -- késő connection_error önálló bontás után ------------------------ #

    def test_late_connection_error_after_standalone_disconnect_keeps_no_connection(self, qapp):
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")
        panel.set_disconnected()

        panel.set_connection_error("simulated I/O hiba")  # késve kézbesített

        assert panel._status_lbl.text() == "Nincs kapcsolat"
        assert panel._status_lbl.styleSheet() == ""

    def test_connection_error_without_standalone_connection_ignored_in_session_mode(self, qapp):
        """A set_connection_error() KIZÁRÓLAG az önálló BqDiagnosticsWorker
        callbackje: a session-tulajdonlás önmagában NEM engedheti át (a
        session-vezérelt "csak olvasás" felirat nem írható felül)."""
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, session_label="Golden GG")

        panel.set_connection_error("simulated I/O hiba")

        assert "Session vezérli" in panel._status_lbl.text()

    def test_connection_error_on_active_connection_then_successful_poll_recovers(self, qapp):
        """Pozitív kontroll a fenti őrhöz: aktív önálló kapcsolaton a hibajel
        TOVÁBBRA IS megjelenik, és egy sikeres poll helyreállítja."""
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_VOLTAGE, 12500)
        panel = BqDiagnosticsPanel()
        panel.set_connected("COM7")

        panel.set_connection_error("simulated I/O hiba")
        assert panel._status_lbl.text() == "Kapcsolódási hiba: simulated I/O hiba"

        panel.update_snapshot(poll_bq_diagnostics(driver))
        assert panel._status_lbl.text() == "Kapcsolódva: COM7"
        assert panel._value_labels["voltage_mV"].text() == "12500"

    # -- FIFO-végső session-snapshot: bekerül, majd megmarad ------------ #

    def test_final_session_snapshot_before_session_end_is_logged_and_kept(self, qapp):
        """A session-runner a végső gauge-snapshotot a `finished` ELŐTT,
        ugyanarról a szálról emittálja (queued FIFO), ezért az a
        set_session_owned(False) ELŐTT kézbesül -- az őr ezt a végső
        session-evidence-et NEM dobhatja el (a teljes Qt-lánc változata:
        test_build_runner.py, TestR1F29BqDiagnosticsLateSignalFullChain)."""
        driver, transport = make_driver()
        _set_qmax_and_update_status(driver, transport, qmax=7000, update_status=0x04)
        panel = BqDiagnosticsPanel()
        panel.set_session_owned(True, "TRADITIONAL_LEARNING")
        panel.update_snapshot(poll_bq_diagnostics(driver))
        _set_qmax_and_update_status(driver, transport, qmax=7100, update_status=0x05)
        panel.update_snapshot(poll_bq_diagnostics(driver))  # a session UTOLSÓ snapshotja
        texts = _event_log_texts(panel)
        assert any("BQ_UPDATE_STATUS_CHANGED" in t for t in texts), texts
        assert any("BQ_QMAX_CHANGED" in t for t in texts), texts

        panel.set_session_owned(False)

        assert _event_log_texts(panel) == texts


class TestR10B2TooltipRecovery:
    """R10-B2: a helyreallt bit- es Ra-kijelzes nem oriz regi hibat."""

    @pytest.fixture
    def snapshots(self, qapp, monkeypatch):
        monkeypatch.setattr(
            "Prog.gui.panels.bq_diagnostics_panel.time.monotonic", lambda: 100.0,
        )
        driver, transport = make_driver()
        transport.set_register(driver.STD_CMD_FLAGS, 0x0080)
        transport.set_control_register(driver.MAC_CONTROL_STATUS, 0x0003)
        _set_qmax_and_update_status(driver, transport, qmax=7000, update_status=0x04)
        ra_block = bytes([0xFF, 0x55]) + b"".join(
            v.to_bytes(2, "big", signed=True) for v in range(1, 16)
        )
        transport.set_block(driver.DF_SC_RA_TABLE_CELL0, driver.DF_BLOCK_RA_TABLE, ra_block)
        valid = poll_bq_diagnostics(driver)
        invalid = replace(valid, **{
            f.name: BqFieldReading(
                value=None, valid=False, error="I2C NACK TEST", timestamp_monotonic=100.0,
            )
            for f in fields(valid)
        })
        panel = BqDiagnosticsPanel()
        panel.set_connected("MOCK")
        try:
            yield panel, valid, invalid
        finally:
            panel._stale_timer.stop()
            panel.close()

    @pytest.mark.parametrize(("key", "text"), [
        ("ocvtaken", "IGEN"), ("qen", "IGEN"), ("vok", "IGEN"),
        ("rup_dis", "nem"), ("ss", "nem"), ("fas", "nem"),
        ("calmode", "nem"), ("cca", "nem"), ("bca", "nem"), ("csv", "nem"),
    ])
    def test_bit_tooltip_clears_after_recovery(self, snapshots, key, text):
        panel, valid, invalid = snapshots
        label = panel._value_labels[key]
        panel.update_snapshot(valid)
        assert label.text() == text
        valid_style = label.styleSheet()
        assert label.toolTip() == ""

        panel.update_snapshot(invalid)
        assert panel._last_success_perf == 100.0
        assert not panel._all_stale_marked
        assert label.text() == "—"
        assert "c62828" in label.styleSheet()
        assert label.toolTip() == "I2C NACK TEST"

        panel.update_snapshot(valid)
        assert label.text() == text
        assert label.styleSheet() == valid_style
        assert label.toolTip() == ""

    @pytest.mark.parametrize("count", [15, 3])
    def test_all_ra_tooltips_clear_after_recovery(self, snapshots, count):
        panel, valid, invalid = snapshots
        panel.update_snapshot(valid)
        assert len(panel._ra_labels) == 15
        assert [lbl.text() for lbl in panel._ra_labels] == [
            f"{i:.1f}" for i in range(1, 16)
        ]
        assert all(lbl.toolTip() == "" for lbl in panel._ra_labels)

        panel.update_snapshot(invalid)
        assert panel._last_success_perf == 100.0
        assert not panel._all_stale_marked
        for lbl in panel._ra_labels:
            assert lbl.text() == "—"
            assert "c62828" in lbl.styleSheet()
            assert lbl.toolTip() == "I2C NACK TEST"

        recovered = replace(valid, ra_table_mohm=replace(
            valid.ra_table_mohm, value=list(range(1, count + 1)),
        ))
        panel.update_snapshot(recovered)
        for i, lbl in enumerate(panel._ra_labels):
            assert lbl.text() == (f"{i + 1:.1f}" if i < count else "—")
            assert ("c62828" in lbl.styleSheet()) == (i >= count)
            assert lbl.toolTip() == ""


@pytest.mark.parametrize("pack_word,x10,status,decision", [
    (0x0961, "0 bit", "OK", ProvisioningGateDecision.CONSISTENT),
    (0x09E1, "1 bit", "ELTÉR", ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH),
])
def test_r16b20_panel_displays_x10_bit(qapp, pack_word, x10, status, decision):
    from Prog.src.bq_consistency_gate import (
        build_expected_df_values,
        evaluate_provisioning_consistency,
    )
    from Prog.tests.test_bq_consistency_gate import _profile, _service_for

    profile = _profile()
    values = build_expected_df_values(profile)
    values["pack_configuration"] = pack_word
    report = evaluate_provisioning_consistency(profile, _service_for(values))
    assert report.decision == decision
    panel = BqDiagnosticsPanel()
    panel.set_provisioning_consistency(report)
    expected_lbl, readback_lbl, tolerance_lbl, status_lbl = panel._provisioning_rows["pack_configuration"]
    assert expected_lbl.text() == "0 bit"
    assert readback_lbl.text() == x10
    assert tolerance_lbl.text() == "±0 bit"
    assert status_lbl.text() == status
