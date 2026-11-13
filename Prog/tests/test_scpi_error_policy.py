"""
scpi_error_policy unit tesztek.
INS-003: aktív energiaátadás alatt egy nem-whitelistelt SCPI hiba kritikus.
"""
from Prog.src.scpi_error_policy import (
    is_critical_scpi_error,
    NON_CRITICAL_SCPI_ERROR_SUBSTRINGS,
)


class TestIsCriticalScpiError:
    def test_empty_message_not_critical(self):
        assert is_critical_scpi_error("") is False

    def test_none_message_not_critical(self):
        assert is_critical_scpi_error(None) is False  # type: ignore[arg-type]

    def test_arbitrary_scpi_error_is_critical_by_default(self):
        assert is_critical_scpi_error('-222,"Data out of range" [last: SOUR:CURR 5.0]') is True

    def test_unknown_command_error_is_critical(self):
        assert is_critical_scpi_error('-113,"Undefined header"') is True


class TestWhitelistPolicy:
    def test_whitelist_empty_by_default(self):
        """SUP0x/GAP-OLD-002 elv: nincs forrással igazolt ártalmatlan SCPI hiba
        ebben a projektben — a whitelist üres, amíg nincs dokumentált kivétel."""
        assert NON_CRITICAL_SCPI_ERROR_SUBSTRINGS == frozenset()

    def test_whitelisted_substring_not_critical(self, monkeypatch):
        import Prog.src.scpi_error_policy as policy
        monkeypatch.setattr(
            policy, "NON_CRITICAL_SCPI_ERROR_SUBSTRINGS", frozenset({"BENIGN_TEST_CODE"})
        )
        assert policy.is_critical_scpi_error("-100,\"BENIGN_TEST_CODE harmless\"") is False
