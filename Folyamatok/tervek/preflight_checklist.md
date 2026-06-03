# Pre-flight checklist — Első fizikai futtatás előkészítése

Ez a dokumentum az első hardveres futtatás előtti ellenőrzési lista.
Minden sor elvégzése után tedd ki a jelölést: `[x]`.

---

## 1. Hardver bekötés (PSU mód szerint)

### INDEPENDENT mód (12V pack, max 1.5A)

```
PSU CH1+ ──► BY550 anód ──► BY550 katód ──► Akku+
PSU CH1– ────────────────────────────────► Akku–
PSU CH2: nincs bekötve / kikapcsolva
```

- [ ] CH1+ → BY550 anód bekötve
- [ ] BY550 katód → Akku+ bekötve
- [ ] CH1– → Akku– bekötve
- [ ] CH2 lekapcsolva / nem bekötve
- [ ] `local_config.yaml`: `combination_mode: INDEPENDENT`

### PARALLEL mód (12V pack, max 3A — fizikai jumper szükséges!)

```
PSU CH1+ ──┐
           ├──► BY550 anód ──► BY550 katód ──► Akku+
PSU CH2+ ──┘  (jumper CH1+–CH2+ között)
PSU CH1– ──┐
           └────────────────────────────────► Akku–
PSU CH2– ──┘  (jumper CH1––CH2– között)
```

- [ ] Jumper CH1+–CH2+ bekötve
- [ ] Jumper CH1––CH2– bekötve
- [ ] BY550 bekötve a közös + kimenetről
- [ ] `local_config.yaml`: `combination_mode: PARALLEL`
- [ ] `local_config.yaml`: `hardware_wiring_confirmed: true`
- [ ] GUI-ban megerősítés elvégezve (PARALLEL mód váltáskor kötelező)

### SERIES mód (24V pack, max 1.5A — fizikai bekötés szükséges!)

```
PSU CH1+ ──► BY550 anód ──► BY550 katód ──► Akku+
PSU CH1– ──► PSU CH2+ (belső összekötés)
PSU CH2– ────────────────────────────────► Akku–
```

- [ ] CH1– → CH2+ összekötve (soros bekötés)
- [ ] CH1+ → BY550 anód bekötve
- [ ] CH2– → Akku– bekötve
- [ ] `local_config.yaml`: `combination_mode: SERIES`
- [ ] `local_config.yaml`: `hardware_wiring_confirmed: true`
- [ ] GUI-ban megerősítés elvégezve (SERIES mód váltáskor kötelező)

---

## 2. BY550 dióda ellenőrzés

- [ ] Polaritás helyes: anód a PSU CH1+ felé, katód az Akku+ felé
- [ ] Vizuális integritás: nincs repedés, sérülés, érintkezési hiba
- [ ] 3A felett (PARALLEL mód): hűtőborda felszerelve
  - Referencia: @1.5A → ~0.60V esés, @3.0A → ~0.85V esés (mért, BY550 char.txt)
  - 3A-nél disszipáció: ~2.55W — hőelvezetés ajánlott

---

## 3. USB/LAN kapcsolat előkészítés

- [ ] PSU USB kábel bekötve a PC-hez
- [ ] Load USB kábel bekötve a PC-hez
- [ ] NI-VISA vagy pyvisa-py + pyusb telepítve (`pip install pyvisa pyvisa-py pyusb`)
- [ ] DMM1 (voltage) LAN IP beállítva a műszeren
- [ ] DMM2 (temperature) LAN IP beállítva a műszeren
- [ ] DMM1 IP ping-elható: `ping 192.168.X.X`
- [ ] DMM2 IP ping-elható: `ping 192.168.X.X`

---

## 4. `local_config.yaml` kitöltési ellenőrzés

- [ ] Fájl létezik: `Prog/config/local_config.yaml`
  - Létrehozás: `copy Prog\config\local_config.template.yaml Prog\config\local_config.yaml`
- [ ] PSU resource string kitöltve (nem PLACEHOLDER)
- [ ] Load resource string kitöltve (nem PLACEHOLDER)
- [ ] DMM_V resource string kitöltve (nem PLACEHOLDER)
- [ ] DMM_T resource string kitöltve (nem PLACEHOLDER)
- [ ] `combination_mode` egyezik a fizikai bekötéssel
- [ ] `hardware_wiring_confirmed: true` (PARALLEL/SERIES módban)

---

## 5. `connection_test.py` futtatása

```bash
cd C:\Users\Mate\Desktop\teszt\Akkuteszter
python Prog/tools/connection_test.py
```

Elvárt kimenet minden bekötött műszerre:
```
  ✅ PSU   : KEITHLEY INSTRUMENTS,MODEL 2220-30-1,...
  ✅ Load  : KEITHLEY INSTRUMENTS,MODEL 2380-120-60,...
  ✅ DMM_V : Keysight Technologies,34465A,... | Mért: X.XXXX V
  ✅ DMM_T : Keysight Technologies,34465A,... | Mért: XX.XX °C
```

- [ ] Minden műszer ✅ OK
- Ha `⚠️ SKIP`: töltsd ki a `local_config.yaml` resource stringjét
- Ha `❌ FAIL`: ellenőrizd USB/LAN kapcsolatot, VISA drivert, IP-t

---

## 6. Első futás előtti safety ellenőrzés

- [ ] Akkumulátor OCV feszültsége mérve DMM-mel (manuálisan)
  - Elvárt 12V pack: 11.8V–12.9V (töltöttségtől függően)
  - Elvárt 24V pack: 23.6V–25.8V
- [ ] Kábelbilincsek szorosan, rövidzár vizuálisan kizárva
- [ ] GUI-ban Safe Off / Emergency Stop gomb látható és ismert
- [ ] Első futáshoz ajánlott: kis kapacitású akku (≤12Ah), CHARACTERIZATION plan
