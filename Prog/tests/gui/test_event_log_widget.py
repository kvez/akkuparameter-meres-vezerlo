"""EventLogWidget._infer_severity() — regressziós tesztek.

Governance-v2 review-kör 1 (CF-07/GUI-R1-13, DFAM-011, batch 4.2):
korábban egy explicit, de a `_SEVERITY_BG` mapben nem szereplő severity
(pl. "CRITICAL") CSENDBEN eldobódott, és a widget az event_code-alapú
találgatásra esett vissza — ami nem garantáltan ad helyes eredményt.
"""
from __future__ import annotations

from Prog.gui.panels.event_log_widget import (
    _MAX_ROWS,
    _SEVERITY_BG,
    EventLogWidget,
    _format_ts,
    _infer_severity,
)


def _evt(i: int) -> dict:
    return {"event_code": f"E{i}", "event_message": "x"}


class TestF4TruncationVisibility:
    """[regresszió, review-kör 2 (2026-09-09), friss GUI/UX F4 (P2,
    DFAM-001/012)] a testvér device_error_panel.py-t a CF-29 fix már
    ellátta ezzel a jelzővel -- ez a widget (LivePanel élő eseménynaplója
    + BqDiagnosticsPanel._session_event_log) korábban NÉMÁN dobta el a
    legrégebbi 500 feletti sort. 1:1 a device_error_panel.py
    TestCF29TruncationVisibility mintája."""

    def test_count_label_plain_below_max_rows(self, qapp):
        widget = EventLogWidget()
        widget.append_event(_evt(0))
        widget.append_event(_evt(1))
        assert widget._count_lbl.text() == "2 esemény"

    def test_no_truncation_note_while_under_limit(self, qapp):
        widget = EventLogWidget()
        for i in range(_MAX_ROWS):
            widget.append_event(_evt(i))
        assert widget._truncated_count == 0
        assert widget._count_lbl.text() == f"{_MAX_ROWS} esemény"

    def test_truncation_note_appears_once_limit_exceeded(self, qapp):
        widget = EventLogWidget()
        for i in range(_MAX_ROWS + 5):
            widget.append_event(_evt(i))
        assert widget._truncated_count == 5
        assert widget._total == _MAX_ROWS + 5
        assert widget._list.count() == _MAX_ROWS
        text = widget._count_lbl.text()
        assert text == f"{_MAX_ROWS + 5} esemény (5 régebbi törölve, {_MAX_ROWS} látható)"

    def test_list_widget_never_exceeds_max_rows(self, qapp):
        widget = EventLogWidget()
        for i in range(_MAX_ROWS + 50):
            widget.append_event(_evt(i))
        assert widget._list.count() == _MAX_ROWS

    def test_clear_resets_truncated_count(self, qapp):
        widget = EventLogWidget()
        for i in range(_MAX_ROWS + 5):
            widget.append_event(_evt(i))
        widget.clear()
        assert widget._truncated_count == 0
        assert widget._total == 0
        assert widget._count_lbl.text() == "0 esemény"


class TestInferSeverityHonorsExplicitValue:
    def test_explicit_critical_severity_is_not_dropped(self):
        """[regresszió, MAX verifier F-1 pontosítás] worker.py
        CHECKPOINT_RESUME_BLOCKED_BY_EMERGENCY_STOP és relax_controller.py
        RELAX_TEMPERATURE_DMM_FAULT explicit severity="CRITICAL"-t adnak —
        ezt korábban a widget csendben eldobta (nem szerepelt
        _SEVERITY_BG-ben), és egy event_code-alapú, esetlegesen téves
        találgatásra esett vissza."""
        event = {
            "event_code": "RELAX_TEMPERATURE_DMM_FAULT",
            "event_message": "x",
            "severity": "CRITICAL",
        }
        assert _infer_severity(event) == "CRITICAL"

    def test_critical_severity_has_a_background_color(self):
        assert _SEVERITY_BG.get("CRITICAL") is not None

    def test_explicit_known_severities_still_honored(self):
        for sev in ("INFO", "WARNING", "FAULT", "CHECKPOINT"):
            assert _infer_severity({"event_code": "X", "severity": sev}) == sev

    def test_unknown_severity_falls_back_to_code_based_inference(self):
        """Egy TÉNYLEGESEN ismeretlen (nem csak eddig hiányzó) severity
        érték változatlanul a kód-alapú találgatásra esik vissza — ez a
        meglévő, szándékos fallback-viselkedés, nem tört el."""
        event = {"event_code": "SOME_FAULT_CODE", "severity": "NOT_A_REAL_SEVERITY"}
        assert _infer_severity(event) == "FAULT"

    def test_no_severity_key_falls_back_to_code_based_inference(self):
        assert _infer_severity({"event_code": "GENERIC_WARNING_HIGH"}) == "WARNING"


class TestInferSeverityFailureFamilyFallback:
    """[regresszió, BATCH 4.2b F-1, 2026-09-13, DECISION_REGISTER.md
    GOV-024, "5. csoport"] a legtöbb produkciós hiba-esemény
    (test_runner.py::CONTROLLER_ABORT_FAILED/SAFE_OFF_RESULT_UNCERTAIN_
    LOGGING_FAILED/stb., traditional_learning_controller.py LEARNING_*_
    FAILED/NOT_CONFIRMED/EXCEEDED/TIMEOUT eseményei) NEM ad explicit
    severity-t -- korábban ezek a fallback-ágon egyszerű INFO-ként
    jelentek meg, színezés nélkül, megkülönböztethetetlenül a rutin
    eseményektől. Diszkrimináló: a régi kódon mindegyik lenti eset
    "INFO"-t adna, nem "FAULT"-ot."""

    def test_controller_abort_failed_infers_as_fault(self):
        assert _infer_severity({"event_code": "CONTROLLER_ABORT_FAILED"}) == "FAULT"

    def test_safe_off_result_uncertain_logging_failed_infers_as_fault(self):
        assert (
            _infer_severity({"event_code": "SAFE_OFF_RESULT_UNCERTAIN_LOGGING_FAILED"})
            == "FAULT"
        )

    def test_learning_not_confirmed_infers_as_fault(self):
        assert _infer_severity({"event_code": "LEARNING_QEN_NOT_CONFIRMED"}) == "FAULT"

    def test_learning_exceeded_infers_as_fault(self):
        assert _infer_severity({"event_code": "LEARNING_RELAX_TIME_EXCEEDED"}) == "FAULT"

    def test_learning_timeout_infers_as_fault(self):
        assert _infer_severity({"event_code": "LEARNING_IT_ENABLE_TIMEOUT"}) == "FAULT"

    def test_safe_off_uncertain_without_failed_suffix_infers_as_fault(self):
        assert _infer_severity({"event_code": "SAFE_OFF_RESULT_UNCERTAIN"}) == "FAULT"

    def test_explicit_severity_still_takes_priority_over_failed_pattern(self):
        """Ha egy hívóhely explicit (nem-FAULT) severity-t ad, az nem
        íródik felül a kód-alapú FAILED-mintával."""
        event = {"event_code": "SOME_LOGGING_FAILED", "severity": "WARNING"}
        assert _infer_severity(event) == "WARNING"


class TestFormatTsHandlesMalformedTimestamp:
    """[regresszió, MAX verifier P3-B lelete (f9027e3 utóellenőrzés, batch
    4.3)] a testvér device_error_panel.py::append_device_error() már
    (ValueError, TypeError)-t fog el ugyanerre a fromisoformat()-mintára —
    itt korábban csak ValueError volt elkapva (CLAUDE.md §8
    sibling-keresés)."""

    def test_valid_iso_timestamp_formatted(self):
        assert _format_ts({"timestamp_iso": "2026-01-01T12:34:56+00:00"}) == "12:34:56"

    def test_non_string_timestamp_falls_back_without_crashing(self):
        result = _format_ts({"timestamp_iso": 12345})
        assert isinstance(result, str) and len(result) == 8  # HH:MM:SS

    def test_malformed_string_timestamp_falls_back_without_crashing(self):
        result = _format_ts({"timestamp_iso": "NOT-A-TIMESTAMP"})
        assert isinstance(result, str) and len(result) == 8

    def test_missing_timestamp_falls_back_without_crashing(self):
        result = _format_ts({})
        assert isinstance(result, str) and len(result) == 8


class TestR11B4OvertemperatureSeverity:
    def test_overtemperature_is_fault(self):
        assert _infer_severity({"event_code": "BATTERY_OVERTEMPERATURE"}) == "FAULT"

    def test_live_panel_preserves_fault_text(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from Prog.gui.panels.live_panel import LivePanel

        label = Mock()
        LivePanel.set_fault(SimpleNamespace(_fault_lbl=label), "BATTERY_OVERTEMPERATURE")
        label.setText.assert_called_once_with("BATTERY_OVERTEMPERATURE")


def test_r16b26b_config_fault_is_visible_as_fault(qapp):
    event = {"event_code": "DISCHARGE_CONFIG_INVALID", "event_message": "GOV-029: config"}
    assert _infer_severity(event) == "FAULT"
    widget = EventLogWidget()
    widget.append_event(event)
    item = widget._list.item(0)
    assert "DISCHARGE_CONFIG_INVALID" in item.text()
    assert item.background().color().name() == "#ffebee"
