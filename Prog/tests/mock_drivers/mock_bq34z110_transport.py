"""
MockBq34z110Transport — determinisztikus szimulált BQ34Z110 I2C/HDQ transport
tesztéléshez. SUP03-APP-002/TEST-004.

WP-AK-06: KÉT külön regiszter-névtér (standard command ÉS Control()
alparancs — l. `Prog/drivers/device_bq34z110.py` docstring "KÉT KÜLÖN
CÍMTÉR" szakasza), hogy azonos numerikus értékű, de eltérő jelentésű címek
(pl. STD_CMD_VOLTAGE=0x08 vs MAC_CHEM_ID=0x0008) ne írhassák felül egymást.
A Data Flash blokkokat (subclass, block) kulcs-pár azonosítja (nem egyetlen
"subcommand"), a valós `pico_bq_service.py::bq_df_read()` címzési modelljét
követve.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from Prog.src.exceptions import InstrumentTimeoutError


class Bq34z110ChecksumError(Exception):
    """Data Flash / block olvasás checksum-hiba (SUP03-TEST-004)."""


@dataclass
class MockBq34z110Transport:
    standard_registers: dict[int, int] = field(default_factory=dict)
    control_registers: dict[int, int] = field(default_factory=dict)
    blocks: dict[tuple[int, int], bytes] = field(default_factory=dict)
    simulate_timeout: bool = False
    simulate_checksum_error: bool = False
    calls: list[str] = field(default_factory=list)

    def read_standard_command_word(self, command: int) -> int:
        self.calls.append(f"read_standard_command_word(0x{command:02X})")
        if self.simulate_timeout:
            raise InstrumentTimeoutError(
                f"MockBq34z110Transport: simulated timeout on standard command 0x{command:02X}"
            )
        return self.standard_registers.get(command, 0)

    def read_control_subcommand_word(self, subcommand: int) -> int:
        self.calls.append(f"read_control_subcommand_word(0x{subcommand:04X})")
        if self.simulate_timeout:
            raise InstrumentTimeoutError(
                f"MockBq34z110Transport: simulated timeout on Control() subcommand 0x{subcommand:04X}"
            )
        return self.control_registers.get(subcommand, 0)

    def read_data_flash_block(self, subclass: int, block: int) -> bytes:
        self.calls.append(f"read_data_flash_block(subclass={subclass}, block={block})")
        if self.simulate_timeout:
            raise InstrumentTimeoutError(
                f"MockBq34z110Transport: simulated timeout on subclass={subclass} block={block}"
            )
        if self.simulate_checksum_error:
            raise Bq34z110ChecksumError(
                f"MockBq34z110Transport: simulated checksum error on "
                f"subclass={subclass} block={block}"
            )
        return self.blocks.get((subclass, block), b"")

    def set_register(self, command: int, value: int) -> None:
        """Kényelmi metódus — a STANDARD command névtérbe ír (a leggyakoribb
        eset a meglévő tesztekben). Control() alparancshoz l. `set_control_register()`."""
        self.standard_registers[command] = value

    def set_control_register(self, subcommand: int, value: int) -> None:
        self.control_registers[subcommand] = value

    def set_block(self, subclass: int, block: int, data: bytes) -> None:
        self.blocks[(subclass, block)] = data

    def reset_calls(self) -> None:
        self.calls.clear()
