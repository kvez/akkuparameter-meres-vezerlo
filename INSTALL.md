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

1. NI-VISA telepítve → USB eszközök bedugva → PC újraindítva.
   A Keithley 2220 előlapján az Output Recall legyen Off (Menu → User
   Settings); a program ezt nem ellenőrzi.
2. Az első indítás előtt készítsd el a valós **`local_config.yaml`**-t az exe
   mellé. Ha még nincs, másold ide az
   `_internal/Prog/config/local_config.template.yaml` sablont ezen a néven.
   Töltsd ki a PSU, Load és a két DMM resource-stringjét, és állítsd a
   `temperature_sensor_mode`-ot a ténylegesen bekötött szondához. A template
   placeholder-címeket és RTD-alapértéket tartalmaz; hiányzó helyi konfig
   esetén az alkalmazás is ebből készít másolatot.
3. Csak a README build-eljárásával frissített **`akkuteszter.exe`**-t indítsd.
4. A Konfiguráció fülön ellenőrizd az adatokat; PARALLEL/SERIES módban a
   bekötés ellenőrzése után jelöld be a **„Fizikai bekötés megerősítve”** mezőt.
   A műszerkeresés `*IDN?`-t küld az elérhető VISA-resource-okra, soros
   resource-okra is; ismert címeknél közvetlenül töltsd ki a konfigot.

---

## local_config.yaml példa

```yaml
temperature_sensor_mode: RTD_4W_PT100

instruments:
  psu:
    resource: "USB0::0x05E6::0x2220::XXXXXXXX::INSTR"
    combination_mode: INDEPENDENT
  load:
    resource: "USB0::0x05E6::0x2380::XXXXXXXX::INSTR"
  dmm_voltage:
    resource: "TCPIP0::192.168.X.X::inst0::INSTR"
  dmm_temperature:
    resource: "TCPIP0::192.168.X.X::inst0::INSTR"
```

**`temperature_sensor_mode`**: `RTD_4W_PT100` (gyári 4-eres PT100) vagy
`THERMISTOR_2W_NTC_10K_B3950` (2-eres NTC pótszonda sérült RTD esetén). **Ennek
összhangban kell lennie a ténylegesen bekötött szondával** — eltérés esetén a
program `TEMPERATURE_MONITOR_LOST_CRITICAL` faulttal azonnal leáll. Session
indítása előtt a GUI-ban is módosítható (Konfiguráció → "Hőkompenzáció" →
"Szenzor típus"); session közben a Konfiguráció fül zárolt. A YAML-érték
csak az induló (alap) értéket adja.

**`combination_mode`**: `INDEPENDENT` (12V pack, nincs extra bekötés) /
`PARALLEL` (12V pack, CH1-CH2 jumperrel összekötve, max 3A) / `SERIES`
(24V+ pack, max 60V/1,5A). **PARALLEL vagy SERIES módban a
Konfiguráció fül „Fizikai bekötés megerősítve” checkboxát kell bejelölni**,
a valós bekötés ellenőrzése után. Profil- vagy PSU-módváltás törli a pipát.
A YAML `hardware_wiring_confirmed` kulcsát a betöltő nem olvassa;
a GUI-megerősítés a biztonsági kapu, a fizikai bekötést a program nem
ellenőrzi automatikusan.

---

## BQ34Z110 (Pico) kapcsolat — csak BQ-vel kommunikáló módoknál

A BQ Diagnosztika fülhöz (és az onnan nyíló „Gauge kalibráció…” ablakhoz), a
Tanítási ciklushoz és a Golden GG méréshez szükség van egy Raspberry Pi
Pico-ra, ami a BQ34Z110-zel beszél. A Pico soros portját (pl. `COM8`) **nem a
`local_config.yaml`-ban** kell megadni: a Tanítási ciklusnál és a Golden GG-nél
session indításakor a Konfiguráció → "BQ (Pico) port" mezőben, a BQ
Diagnosztika fülön és a kalibrációhoz a fül saját portmezőjében. A Pico
firmware-je az alkalmazással érkezik (Eszközök → "Pico előkészítése
BQ34Z110-hez…", bekötési útmutatóval). Ha a port nem elérhető vagy rossz, a
kapcsolódás azonnal, egyértelmű hibával visszautasít (protokoll-egyeztetés
`negotiate()` hívással).

---

## Mérési logok helye

A session logok (`Mérések/Sessions/session_*/`) az `akkuteszter.exe` melletti mappában jönnek létre.
A diagnosztikai napló az exe melletti `logs/akkuteszter.log`; a konfigurációval
és a `Mérések/` tartalmával együtt ezt is őrizd meg frissítéskor (README build-eljárás).

---

## Hibaelhárítás

| Hiba | Ok | Megoldás |
|------|----|----------|
| "NI-VISA / pyvisa hiba" | NI-VISA nincs telepítve | Telepítsd, indítsd újra |
| USB eszköz nem látszik | Driver nem töltött be | Device Manager ellenőrzés |
| LAN DMM nem elérhető | IP / tűzfal | `ping 192.168.x.x`, tűzfal szabály |
| YAML szintaxis hiba | Rossz behúzás | Notepad-ban javítsd |
| `TEMPERATURE_MONITOR_LOST_CRITICAL` | A `temperature_sensor_mode` nincs összhangban a ténylegesen bekötött szondával (pl. RTD-t vár, de thermistor van bekötve) | Állítsd a helyes értékre a `local_config.yaml`-ban VAGY a GUI "Hőkompenzáció" panelen |
| Start után „Konfiguráció hiba”: `hardware_wiring_confirmed = True kötelező PARALLEL módban` (vagy `SERIES`) | A GUI-ban nincs megerősítve a bekötés | A fizikai jumperelés/soros bekötés ellenőrzése után jelöld be a Konfiguráció fül „Fizikai bekötés megerősítve” checkboxát |
