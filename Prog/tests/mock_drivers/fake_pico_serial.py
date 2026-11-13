"""
FakePicoSerial — WP-AK-06: in-memory szimulált soros port a PicoBQTransport
teszteléséhez.

A DRÓTON LÉVŐ JSON Lines protokollt beszéli (nem importál semmit a TF1 repo
`Prog/pico_bq_rpc.py`-jából — két külön Git repo, a wire-formátum a
kontraktus a két oldal között, nem egy közös Python-import). Programozható
válaszokkal (metódusnév -> eredmény vagy hiba), a kérés `id`-jét automatikusan
visszaadja (mint egy valós RPC-partner tenné).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakePicoSerial:
    # metódusnév -> eredmény (a "capabilities"-t a service() tölti fel alapból)
    results: dict[str, Any] = field(default_factory=dict)
    # metódusnév -> (code, message) - ha jelen van, hibaválasz megy eredmény helyett
    errors: dict[str, tuple[str, str]] = field(default_factory=dict)
    # metódusnév -> nyers (nem feltétlenül dict) "error" mező - malformed
    # Pico-válasz szimulálásához (l. Teljes kódbázis review 12. kör,
    # Hardver/Műszervezérlés P1 lelet: _call() ne feltételezze, hogy
    # response["error"] mindig dict)
    errors_raw: dict[str, Any] = field(default_factory=dict)

    simulate_write_failure: bool = False
    simulate_no_response: bool = False
    simulate_garbage_response: bool = False
    # Kör 20 HW20-08: a Pico válaszából hiányzik az "ok" kulcs (pl. az idle,
    # friendly REPL-ben van, nincs RPC szerver futva) - MÁS eset, mint egy
    # explicit ok:false hibaválasz.
    simulate_missing_ok_key: bool = False
    # ha megadva, EZ az id kerül a válaszba a kérés id-je helyett (id-eltérés teszteléshez)
    force_response_id: int | None = None

    written_requests: list[dict] = field(default_factory=list, init=False)
    protocol_version: str = "1.0"
    _pending_response: bytes = field(default=b"", init=False, repr=False)
    reset_input_buffer_calls: int = field(default=0, init=False)
    # Review 2026-09-15 (C-02 MAX verifier F1 lelete): a SerialLike Protocol
    # (pico_bq_transport.py) close()-t deklarál (a valós pyserial.Serial-nek
    # és a `disconnect()`/`_reopen_serial()` hívóhelyeknek megfelelően) — ez
    # a mock korábban HIÁNYZÓ close()-ja mypy structural-typing hibát
    # okozott 7 hívóhelyen (PicoBQTransport(serial=FakePicoSerial(...))).
    closed: bool = field(default=False, init=False)

    def close(self) -> None:
        self.closed = True

    def __post_init__(self) -> None:
        # Kör 21 F1/ARC21-3(b): korábban `read_only: True` volt fixen beírva,
        # `write_methods` kulcs NÉLKÜL -- ez egy SOSEM létező, csak-olvasó
        # firmware-t modellezett, holott minden valós, megfelelően
        # provisionált firmware (Prog/pico_firmware/pico_bq_rpc.py
        # _WRITE_METHOD_NAMES) MINDIG hordozza az összes write-metódust. Az
        # alapértelmezett válasz MOST ezt a valósághű, teljes-funkciós
        # állapotot tükrözi -- egy csak-olvasó/hiányos firmware-t szimuláló
        # teszt explicit `results={"capabilities": {...}}`-tal írja felül
        # (l. test_pico_bq_transport.py::TestNegotiate új tesztjei).
        from Prog.drivers.pico_bq_transport import (
            CALIBRATION_WRITE_METHODS,
            LEARNING_SESSION_WRITE_METHODS,
        )
        write_methods = sorted(LEARNING_SESSION_WRITE_METHODS | CALIBRATION_WRITE_METHODS)
        self.results.setdefault(
            "capabilities",
            {
                "protocol_version": self.protocol_version,
                "methods": list(self.results.keys()),
                "read_only": len(write_methods) == 0,
                "write_methods": write_methods,
            },
        )

    def write(self, data: bytes) -> int:
        if self.simulate_write_failure:
            raise OSError("FakePicoSerial: simulated write failure")
        request = json.loads(data.decode("utf-8").strip())
        self.written_requests.append(request)
        self._pending_response = self._build_response(request)
        return len(data)

    def readline(self) -> bytes:
        if self.simulate_no_response:
            return b""
        if self.simulate_garbage_response:
            return b"{not valid json\n"
        return self._pending_response

    def reset_input_buffer(self) -> None:
        self.reset_input_buffer_calls += 1
        self._pending_response = b""

    def _build_response(self, request: dict) -> bytes:
        req_id = self.force_response_id if self.force_response_id is not None else request.get("id")
        method = request.get("method")

        if method in self.errors_raw:
            payload = {"id": req_id, "ok": False, "error": self.errors_raw[method]}
        elif method in self.errors:
            code, message = self.errors[method]
            payload = {"id": req_id, "ok": False, "error": {"code": code, "message": message}}
        elif method in self.results:
            value = self.results[method]
            # Ha a beprogramozott érték hívható, a params-szal hívjuk (pl.
            # data_flash_block, ahol a válasz subclass/block-tól függ) -
            # egyébként statikus értékként visszaadjuk.
            result = value(request.get("params", {})) if callable(value) else value
            payload = {"id": req_id, "ok": True, "result": result}
        else:
            payload = {
                "id": req_id, "ok": False,
                "error": {"code": "UNKNOWN_METHOD", "message": f"unknown method: {method}"},
            }
        if self.simulate_missing_ok_key:
            payload.pop("ok", None)
        return (json.dumps(payload) + "\n").encode("utf-8")
