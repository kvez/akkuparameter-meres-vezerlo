# Akkuteszter — Telepítési útmutató

## Előfeltétel: NI-VISA telepítése

Az USB-s műszerek (Keithley PSU, Load) NI-VISA drivert igényelnek Windows alatt.

1. Töltsd le: https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html
   - Ajánlott: NI-VISA 24.x vagy újabb
2. Telepítés után **indítsd újra a PC-t**
3. Dugd be az USB eszközöket (PSU, Load)

LAN-os eszközök (Keysight DMM) NI-VISA-val is működnek (TCP/IP backend).

---

## Első futtatás

1. NI-VISA telepítve → USB eszközök bedugva → PC újraindítva
2. **`akkuteszter.exe`** futtatása
   - Első indításkor automatikusan létrejön a `local_config.yaml` ebben a mappában
3. **Konfiguráció tab → "Eszközök keresése…" gomb**
   - Listázza az elérhető VISA eszközöket és azonosítójukat (`*IDN?`)
4. **`local_config.yaml` szerkesztése** (pl. Notepad):
   - Másold be a PSU, Load, DMM resource stringeket a keresési eredményből
5. Újraindítás után a resource stringek automatikusan betöltődnek

---

## local_config.yaml példa

```yaml
instruments:
  psu:
    resource: "USB0::0x05E6::0x2220::9204604::INSTR"
    combination_mode: INDEPENDENT
    hardware_wiring_confirmed: false
  load:
    resource: "USB0::0x05E6::0x2380::802436052777870003::INSTR"
  dmm_voltage:
    resource: "TCPIP0::192.168.2.80::inst0::INSTR"
  dmm_temperature:
    resource: "TCPIP0::192.168.2.79::inst0::INSTR"
```

---

## Mérési logok helye

A session logok (`Mérések/session_*/`) az `akkuteszter.exe` melletti mappában jönnek létre.

---

## Hibaelhárítás

| Hiba | Ok | Megoldás |
|------|----|----------|
| "NI-VISA / pyvisa hiba" | NI-VISA nincs telepítve | Telepítsd, indítsd újra |
| USB eszköz nem látszik | Driver nem töltött be | Device Manager ellenőrzés |
| LAN DMM nem elérhető | IP / tűzfal | `ping 192.168.x.x`, tűzfal szabály |
| YAML szintaxis hiba | Rossz behúzás | Notepad-ban javítsd |
