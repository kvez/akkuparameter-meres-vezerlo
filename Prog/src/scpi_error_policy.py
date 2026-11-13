"""
SCPI hiba kritikusság-osztályozás — pure, műszer-független policy réteg.

INS-003: aktív energiaátadás (CHARGE/DISCHARGE lépés) alatt egy nem-null SCPI
parancs-/konfigurációs hiba alapból FAULT-ot kell kiváltson. Csak dokumentált,
forrással igazolt whitelist-bejegyzés minősülhet nem kritikusnak — nem szabad
instrument-specifikus stringvizsgálatot szétszórni a controllerekben, ezért ez
a döntés egyetlen, önállóan tesztelhető helyre került.
"""
from __future__ import annotations

# Dokumentáltan, forrással igazoltan ártalmatlan SCPI hibaüzenet-részletek,
# amik aktív energiaátadás alatt NEM váltanak ki FAULT-ot.
#
# ÜRES ALAPÉRTELMEZÉS: jelenleg nincs a projektben forrással (gyári kézikönyv
# vagy dokumentált fizikai mérés) igazolt, ártalmatlan SCPI hiba — ezért a
# konzervatív alapállapot: MINDEN SCPI hiba kritikus aktív energiaátadás alatt.
# Új bejegyzést csak dokumentált műszaki indokkal (pl. Folyamatok/döntések/)
# szabad felvenni ide, l. a szabályrendszer INS-003 pontja.
NON_CRITICAL_SCPI_ERROR_SUBSTRINGS: frozenset[str] = frozenset()


def is_critical_scpi_error(error_message: str) -> bool:
    """True, ha az adott SCPI hibaüzenet aktív energiaátadás alatt FAULT-ot
    kell kiváltson. Üres/None üzenet sosem kritikus (nincs is hiba)."""
    if not error_message:
        return False
    return not any(
        benign in error_message for benign in NON_CRITICAL_SCPI_ERROR_SUBSTRINGS
    )
