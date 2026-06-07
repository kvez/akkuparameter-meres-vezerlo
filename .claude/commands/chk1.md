VÉGSÕ REVIEW SABLON – PYTHON AKKU PARAMÉTEREZÕ / VALIDÁLÓ PROGRAM
==================================================================

Cél
---
Ez a sablon arra való, hogy a projekt aktuális állapotát összevesse:

  1. a meglévõ programmal,
  2. a tervek/ mappában lévõ tervezési dokumentumokkal,
  3. a terv_vs_implementacio_osszehasonlitas anyaggal,
  4. a review parancsokban szereplõ elvárásokkal,
  5. az FG / FGH / FGHL AGM VRLA akkukhoz releváns gyártói követelményekkel,
  6. a valós hardveres mûködéshez szükséges safety, metrológiai és üzemeltetési feltételekkel.

A review eredménye alapján dönthetõ el, hogy a program:

  - éles használatra alkalmas,
  - csak korlátozással használható,
  - vagy további javítás nélkül nem használható biztonságosan.

Fontos alapelv
--------------
A program csak akkor tekinthetõ véglegesnek, ha:

  - nem tud veszélyes PSU / LOAD / BATTERY állapotot létrehozni,
  - minden hiba biztonságos OFF állapotba visz,
  - nem tud hamis DONE / PASS / CHARGE_DONE / CAPACITY_OK eredményt adni,
  - az Ah / Wh / feszültség / hõmérséklet adatok metrológiailag értelmezhetõk,
  - a tervdokumentumokban vállalt funkciók vagy implementálva vannak, vagy tudatosan kizárásra kerültek,
  - a riport egyértelmûen jelzi a mérési módszer korlátait,
  - a kezelõi hibák nem vezetnek veszélyes mûködéshez,
  - friss, aktuális verzióból származó valós futási eredmény is igazolja a mûködést.


0. REVIEW INPUTOK
=================

A review-hoz szükséges bemenetek:

  [ ] Aktuális program ZIP / teljes repo
  [ ] src/ mappa
  [ ] drivers/ mappa
  [ ] gui/ mappa
  [ ] tools/ mappa
  [ ] tests/ mappa
  [ ] configs/ vagy config template
  [ ] requirements.txt vagy pyproject.toml
  [ ] build script / exe build leírás
  [ ] README / kezelési dokumentáció
  [ ] tervek/ mappa teljes tartalma
  [ ] terv_vs_implementacio_osszehasonlitas fájl
  [ ] jelenlegi review parancsok / review anyagok
  [ ] friss connection_test eredmény
  [ ] friss valós mérési log legalább egy sikeres futásból
  [ ] friss fault / megszakított / emergency stop futás logja
  [ ] használt mûszerek listája
  [ ] bekötési vázlat vagy rövid hardverleírás
  [ ] akku típusok listája: FG / FGH / FGHL / egyéb

Nem szabad véglegesnek minõsíteni, ha csak régi session logok állnak rendelkezésre.
Régi log csak történeti információ, nem bizonyítja az aktuális verzió mûködését.


1. TERVEK ÉS IMPLEMENTÁCIÓ ÖSSZEVETÉSE
======================================

Vizsgálandó források:

  - tervek/ mappa
  - terv_vs_implementacio_osszehasonlitas
  - aktuális programkód
  - README / dokumentáció
  - config template
  - tesztek

Feladat:

  [ ] Gyûjtsd ki a tervek/ mappából az összes vállalt funkciót.
  [ ] Gyûjtsd ki a terv_vs_implementacio_osszehasonlitas megállapításait.
  [ ] Minden tervponthoz keresd meg az implementáció helyét.
  [ ] Ha nincs implementáció, jelöld: HIÁNYZIK.
  [ ] Ha részleges, jelöld: RÉSZLEGES.
  [ ] Ha a terv elavult, jelöld: TERV ELAVULT, indoklással.
  [ ] Ha az implementáció eltér a tervtõl, döntsd el, hogy jogos eltérés vagy hiba.
  [ ] Keresd meg, hogy van-e teszt az adott funkcióhoz.
  [ ] Keresd meg, hogy van-e log / riport nyoma az adott funkciónak.

Kimeneti táblázat:

  Tervpont | Elvárt viselkedés | Implementáció helye | Státusz | Teszt | Log/riport | Kockázat | Teendõ
  ---------|-------------------|---------------------|---------|-------|------------|----------|-------

Státusz értékek:

  - OK
  - RÉSZLEGES
  - HIÁNYZIK
  - ELTÉRÉS_JOGOS
  - ELTÉRÉS_HIBA
  - TERV_ELAVULT
  - NEM_RELEVÁNS

Review kérdések:

  [ ] Van olyan tervben szereplõ safety funkció, ami nincs implementálva?
  [ ] Van olyan tervben szereplõ metrológiai korrekció, ami csak GUI-ban látszik, de nem hat a számításra?
  [ ] Van olyan opció, ami választható, de nincs mögötte mûködõ logika?
  [ ] Van olyan implementált funkció, ami nincs dokumentálva?
  [ ] Van olyan implementált funkció, ami ellentmond a tervnek?
  [ ] A terv_vs_implementacio_osszehasonlitas minden pontjára van lezárt döntés?

Blokkoló hibák:

  [ ] Safety funkció hiányzik vagy csak részleges.
  [ ] GUI-ban bekapcsolható olyan funkció, ami valójában nincs implementálva.
  [ ] Töltési/kisütési határérték terv szerint létezik, de controller szinten nincs kikényszerítve.
  [ ] Dokumentáció szerint mûködik valami, de a kód alapján nem.


2. SAFETY / HARDVERVÉDELEM REVIEW
=================================

Vizsgálandó fájlok:

  - src/safety.py
  - src/charge_controller.py
  - src/discharge_controller.py
  - src/instrument_manager.py
  - src/test_runner.py
  - drivers/device_psu.py
  - drivers/device_load.py
  - drivers/device_dmm.py
  - gui/worker.py
  - gui/main_window.py

Alapelv:

  A safety nem GUI funkció, hanem programlogikai és lehetõség szerint hardverlogikai védelem.
  A GUI csak kérhet mûveletet, de nem kerülheti meg a SafetyManager / Controller réteget.

Ellenõrizendõ:

  [ ] PSU OUTPUT soha nem kapcsolhat be rossz állapotban.
  [ ] LOAD INPUT soha nem lehet aktív töltés közben.
  [ ] PSU és LOAD nem lehet egyszerre aktív.
  [ ] emergency_stop minden állapotból safe_all_off-ot hív.
  [ ] safe_all_off sorrendje: LOAD OFF, majd PSU OFF.
  [ ] safe_all_off idempotens.
  [ ] safe_all_off exception esetén is megpróbál minden kimenetet lekapcsolni.
  [ ] DMM feedback lost esetén töltés/kisütés leáll.
  [ ] PSU current readback hiba nem alakulhat 0.0 A értékké.
  [ ] LOAD current readback hiba nem alakulhat 0.0 A értékké.
  [ ] NaN / Inf / None / overflow mérési érték faultot okoz.
  [ ] battery overvoltage azonnali fault.
  [ ] terminate voltage kisütésnél kötelezõen mûködik.
  [ ] max_charge_time minden töltési fázisban fut.
  [ ] max_charge_Ah minden töltési fázisban fut.
  [ ] max_discharge_Ah kisütés alatt fut.
  [ ] temperature DMM fault policy tényleg be van kötve.
  [ ] túlmelegedés töltést/kisütést tilt vagy leállít.
  [ ] no battery / disconnected battery detektálva van.
  [ ] reverse polarity detektálás vagy kezelõi preflight tiltás van.
  [ ] 24 V pack csak SERIES módban engedélyezett, ha ez a hardverkövetelmény.
  [ ] 12 V pack SERIES mód csak warninggal vagy extra megerõsítéssel indulhat.
  [ ] BY550 / soros dióda feszültségesés fault vagy warning logikája egyértelmû.
  [ ] diode_power számítás warningként vagy faultként dokumentált.
  [ ] minden fault állapot végén kimenetek OFF állapotban vannak.

Fault injection minimum:

  [ ] DMM timeout töltés közben.
  [ ] DMM timeout kisütés közben.
  [ ] PSU readback exception CC állapotban.
  [ ] PSU readback exception CV/TAPER állapotban.
  [ ] LOAD readback exception kisütés közben.
  [ ] Akkufeszültség hirtelen irreálisan nagy.
  [ ] Akkufeszültség hirtelen irreálisan kicsi.
  [ ] PT100 szakadás / irreális hõmérséklet.
  [ ] Emergency Stop INIT/PRECHECK/CC/CV/TAPER/DISCHARGE/RELAX közben.
  [ ] GUI bezárása futó mérés alatt.
  [ ] VISA eszköz eltûnik futás közben.

Blokkoló hibák:

  [ ] PSU és LOAD egyszerre aktív lehet.
  [ ] Readback hiba 0.0 értékként kezelõdik.
  [ ] Emergency stop nem garantáltan kapcsol le.
  [ ] DMM hiba ellenére DONE/PASS állapot jöhet létre.
  [ ] Overvoltage nem azonnali fault.
  [ ] Fault után bármelyik kimenet ON maradhat.


3. METROLÓGIAI HELYESSÉG REVIEW
===============================

Vizsgálandó fájlok:

  - src/integrator.py
  - src/test_runner.py
  - src/charge_controller.py
  - src/discharge_controller.py
  - src/relax_controller.py
  - src/report_generator.py
  - drivers/device_dmm.py
  - config fájlok

Ellenõrizendõ:

  [ ] Ah integrátor valódi dt_s alapján számol.
  [ ] Wh integrátor valódi dt_s alapján számol.
  [ ] dt_s perf_counter alapú, nem névleges tick.
  [ ] dt_s / elapsed_s logolva van.
  [ ] negatív vagy nulla dt_s kezelve van.
  [ ] túl nagy dt_s gap kezelve van.
  [ ] charge current forrása dokumentált: PSU_READBACK vagy külsõ shunt.
  [ ] discharge current forrása dokumentált: LOAD_READBACK vagy külsõ shunt.
  [ ] ha nincs külsõ kalibrált shunt, ez a riportban szerepel.
  [ ] DMM voltage tényleg akkukapcson mér.
  [ ] DMM DCV konfiguráció minden mérés elõtt biztosított.
  [ ] DMM temperature konfiguráció PT100/FRTD módban biztosított.
  [ ] NPLC értékek ésszerûek az idõzítéshez képest.
  [ ] OCV mérésnél jelölve van, hogy csak pihent akkun értelmezhetõ.
  [ ] OCV mérésnél jelölve van, ha nincs galvanikus leválasztás.
  [ ] hõkompenzáció választása tényleg módosítja a célfeszültséget.
  [ ] ha hõkompenzáció nincs implementálva, nem választható.
  [ ] Rb / belsõ ellenállás mérésnél külön van választva: DC pulse R, dynamic impedance, scope transient.
  [ ] kábel / relé / panel / csatlakozó ellenállás kalibrációja külön kezelhetõ.
  [ ] mérési bizonytalanság / korlátok megjelennek a riportban.

Metrológiai validáció minimum:

  [ ] DMM feszültség összevetve referencia DMM-mel.
  [ ] PSU readback áram összevetve külsõ méréssel.
  [ ] LOAD readback áram összevetve külsõ méréssel.
  [ ] Ah integrátor ellenõrizve ismert árammal és idõvel.
  [ ] Wh integrátor ellenõrizve ismert U/I profillal.
  [ ] PT100 / hõmérséklet összevetve referencia hõmérõvel.
  [ ] Report megmondja, hogy a kapacitás milyen árammérési forrásból származik.

Blokkoló hibák:

  [ ] Ah/Wh névleges tick alapján számol.
  [ ] Áram readback exception 0.0-ként kerül integrálásra.
  [ ] DMM overload / NaN / Inf érvényes adatként kerül logolásra.
  [ ] Report nem közli a mérési forrásokat és korlátokat.
  [ ] Hõkompenzáció GUI opció, de nem hat a célfeszültségre.


4. AKKU PARAMÉTEREK / FIAMM FG-FGH-FGHL VALIDÁCIÓ
=================================================

Vizsgálandó források:

  - FG_FOLDER_EN.pdf
  - FGH-FGHL_FOLDER_EN.pdf
  - Engineering_Manual_AGM_EN.pdf
  - VRLA_Non-spillable_EN.pdf
  - akku profil config
  - GUI akkuválasztó
  - charge/discharge paraméterezés

Ellenõrizendõ:

  [ ] FG, FGH, FGHL típusok nincsenek összekeverve.
  [ ] 6 V és 12 V akku cellaszám helyesen van kezelve.
  [ ] C10 és C20 kapacitás nincs összekeverve.
  [ ] Töltõáram számítása C10 alapján történik, ha gyártói töltési korlát C10-re vonatkozik.
  [ ] FG20121 esetén C10 = 1.09 Ah, C20 = 1.2 Ah, nem fordítva.
  [ ] Float cél: 2.27 V/cell @ 20 °C.
  [ ] Boost cél: 2.40 V/cell @ 20 °C.
  [ ] Boost áramlimit: max. 0.25 C10.
  [ ] Boost stop: 0.03 C10 vagy hõmérsékleti feltétel.
  [ ] Hõkompenzáció: -2.5 mV/cell/°C, ha engedélyezett.
  [ ] Kisütési végfeszültség kapacitástesztnél: 1.80 V/cell C10 esetben.
  [ ] OCV-SOC csak pihent akkun értelmezett.
  [ ] FG general purpose, nem high-rate UPS típus.
  [ ] FGH high-rate típus, nem azonos FG-vel.
  [ ] FGHL long-life / flame retardant casing jelleg külön kezelve, ha releváns.
  [ ] Akkuk telepítési iránya, csatlakozó típusa, Faston/Flag terhelhetõség dokumentált.
  [ ] Zárt doboz / szellõzés / hidrogénképzõdés kockázata kezelve.
  [ ] Mélykisütés elleni védelem implementálva.
  [ ] Hosszabb áramtalanítás esetén külsõ fogyasztók leválasztására van kezelõi figyelmeztetés.

Blokkoló hibák:

  [ ] C20 kapacitásból számol töltõáramot C10 helyett.
  [ ] 12 V / 24 V pack tévesen paraméterezhetõ megerõsítés nélkül.
  [ ] Boost feszültség vagy áram túllépi a gyártói ajánlást.
  [ ] Hõmérséklet-kompenzáció hiánya ellenére magas hõmérsékleten agresszív töltés fut.
  [ ] Mélykisütött akkura kontroll nélküli nagyáramú töltést enged.


5. ÁLLAPOTGÉPEK REVIEW
======================

ChargeController állapotok:

  - INIT
  - PRECHECK
  - PSU_PRESET
  - CC
  - CV
  - TAPER_HOLD
  - DONE
  - FAULT

Ellenõrizendõ:

  [ ] minden állapotból van biztonságos kilépés.
  [ ] nincs végtelen CV állapot.
  [ ] nincs végtelen TAPER_HOLD állapot.
  [ ] TAPER feltétel csak valid DMM és valid PSU readback mellett igaz.
  [ ] TAPER feltétel: U_batt >= target - tolerance.
  [ ] TAPER feltétel: I_charge <= taper.
  [ ] TAPER timer reset feltételek helyesek.
  [ ] DMM hiba nem okozhat hamis DONE-t.
  [ ] PSU readback hiba nem okozhat hamis TAPER-t.
  [ ] max_charge_time és max_charge_Ah minden állapotban aktív.
  [ ] DONE elõtt minden elvárt leállási feltétel teljesül.

DischargeController:

  [ ] Load bekapcsolás elõtt PSU OFF.
  [ ] terminate voltage mûködik.
  [ ] DMM hiba fault.
  [ ] load current readback hiba fault.
  [ ] max_discharge_Ah limit mûködik.
  [ ] DONE/FAULT esetén Load OFF.
  [ ] feszültségesés / rossz kontaktus detektálás van vagy dokumentált hiány.

RelaxController:

  [ ] PSU OFF.
  [ ] Load OFF.
  [ ] DMM hiba kezelése.
  [ ] dV/dt csak valid baseline után számolódik.
  [ ] OCV/SOC jelölés csak relax után érvényes.

Blokkoló hibák:

  [ ] Bármely állapotból úgy lehet DONE, hogy mérési adat invalid.
  [ ] CV/TAPER idõlimit nélkül fut.
  [ ] FAULT nem kapcsol le minden kimenetet.
  [ ] Manual stop és emergency stop összekeveredik.


6. TESTRUNNER / WORKFLOW / IDÕZÍTÉS REVIEW
==========================================

Vizsgálandó:

  - src/test_runner.py
  - src/instrument_manager.py
  - src/logger.py
  - gui/worker.py

Ellenõrizendõ:

  [ ] run() blokkoló, de GUI-tól független.
  [ ] TestRunner nem importál PySide6-ot.
  [ ] start_step_index validáció korrekt.
  [ ] CHECKPOINT_STOPPED helyes kezelése.
  [ ] MANUAL_CHECKPOINT eventet küld.
  [ ] step_changed események jó indexet adnak.
  [ ] actual dt_s korrekt.
  [ ] sleep csak a maradék idõre fut.
  [ ] emergency_stop minden tickben ellenõrzött.
  [ ] graceful stop csak biztonságos pontokon hat.
  [ ] logger.close() minden kilépési úton megtörténik.
  [ ] ugyanaz a TestRunner újrahasználható-e, vagy session-once objektumként dokumentált.
  [ ] kivételek esetén safe_all_off fut.

Blokkoló hibák:

  [ ] Exception esetén nem garantált safe_all_off.
  [ ] Emergency stop csak GUI eseményre reagál, tickben nem ellenõrzött.
  [ ] Logger nem záródik fault esetén.
  [ ] Resume hibás állapotból újraindítja a kimeneteket.


7. LOGGER / CHECKPOINT / RECOVERY REVIEW
========================================

Vizsgálandó:

  - src/logger.py
  - src/report_generator.py
  - checkpoint kezelés
  - session output struktúra

Ellenõrizendõ:

  [ ] CSV flush mûködik.
  [ ] SQLite commit stratégia ésszerû.
  [ ] checkpoint.json írás atomi: temp file + os.replace.
  [ ] checkpoint valid JSON marad megszakítás után is.
  [ ] events.csv megkapja az eseményeket.
  [ ] emergency / fault esemény logolódik.
  [ ] logger.close() meghívódik.
  [ ] session_meta tartalmazza: test_type, battery profile, psu_mode, resource stringek.
  [ ] session_meta tartalmazza: temperature compensation mode.
  [ ] session_meta tartalmazza: series diode / BY550 beállítás.
  [ ] session_meta tartalmazza: measurement limitations.
  [ ] local_config érzékeny részei nem kerülnek riportba.
  [ ] riport külön jelzi, ha régi verziójú logból készült.
  [ ] report generator nem tud hamis PASS-t adni hiányos adatból.

Blokkoló hibák:

  [ ] Checkpoint sérülés után félrevezetõ resume lehetséges.
  [ ] Report PASS-t ad invalid vagy hiányos mérésre.
  [ ] Fault event nem kerül logba.
  [ ] session_meta nem tartalmazza a kritikus mérési beállításokat.


8. SCPI / MÛSZERDRIVER REVIEW
=============================

PSU driver:

  [ ] connect után safe állapot.
  [ ] output_off parancs biztos.
  [ ] all_outputs_off tényleg mindent lekapcsol.
  [ ] SERIES/PARALLEL/INDEPENDENT mód kezelése validált.
  [ ] CH1 / CH2 / combined readback logika helyes.
  [ ] timeoutok ésszerûek.
  [ ] hibás SCPI válasz exception/fault, nem 0.0.
  [ ] error queue kezelése nem blokkolja a fõ ciklust.
  [ ] connection_test nem kapcsol OUTPUT ON-t.

LOAD driver:

  [ ] input_off biztos.
  [ ] input_on csak controlleren keresztül.
  [ ] set_current elõtt mód helyes.
  [ ] readback exception fault.
  [ ] soft-start / slew rate igény eldöntve.
  [ ] connection_test nem kapcsol INPUT ON-t.

DMM driver:

  [ ] DCV konfiguráció explicit.
  [ ] PT100/FRTD konfiguráció explicit.
  [ ] NPLC beállítás explicit.
  [ ] overload felismerés.
  [ ] NaN/Inf kezelés.
  [ ] voltage jump detector vagy irreális ugrás ellenõrzés.
  [ ] timeout esetén nincs valid adat.

Blokkoló hibák:

  [ ] Connect automatikusan bekapcsol kimenetet.
  [ ] Invalid SCPI válasz 0.0-ként tér vissza.
  [ ] PSU mód parancs sikertelenségét nem ellenõrzi.
  [ ] Driverbõl közvetlenül megkerülhetõ a safety réteg.


9. GUI / WORKER / THREAD SAFETY REVIEW
======================================

Vizsgálandó:

  - gui/main_window.py
  - gui/worker.py
  - gui/panels/live_panel.py
  - gui/panels/checkpoint_panel.py
  - gui/panels/event_log_widget.py

Ellenõrizendõ:

  [ ] GUI nem hív közvetlenül drivert.
  [ ] mûszerek csak worker threadbõl érhetõk el.
  [ ] worker signalokat küld.
  [ ] GUI widget csak GUI threadbõl frissül.
  [ ] QThread cleanup: quit, wait, deleteLater.
  [ ] többszöri Start/Stop után nincs duplán bekötött signal.
  [ ] Emergency Stop gomb mindig aktív futás közben.
  [ ] CheckpointPanel helyesen kezeli a terminal checkpointot.
  [ ] resume_possible false esetén nem enged resume-ot.
  [ ] event_ready látható EventLogWidgetben.
  [ ] status bar nem ad félrevezetõ állapotot.
  [ ] GUI bezárás futás közben safe stop / emergency stop logikát indít.

Blokkoló hibák:

  [ ] GUI threadbõl történik mûszervezérlés.
  [ ] Emergency Stop gomb bizonyos állapotban nem mûködik.
  [ ] Többszöri Start után duplán futó worker jön létre.
  [ ] GUI félrevezetõen DONE-t mutat FAULT után.


10. CONFIG / REPO / BUILD HIGIÉNIA
==================================

Ellenõrizendõ:

  [ ] local_config.yaml nincs verziózva.
  [ ] local_config.template.yaml van.
  [ ] .gitignore tartalmazza: __pycache__, *.pyc, .pytest_cache, local_config.yaml, mérési logok, *.db.
  [ ] requirements.txt vagy pyproject.toml van.
  [ ] dependency-k dokumentáltak: PySide6, pyqtgraph, pyvisa, PyYAML, pytest.
  [ ] build script mûködik.
  [ ] exe buildnél config fájl kezelése egyértelmû.
  [ ] verziószám megjelenik GUI-ban / riportban.
  [ ] régi config verzió kezelése megoldott.
  [ ] hiányzó config esetén nincs hardveraktiválás.
  [ ] hibás config esetén nincs hardveraktiválás.

Blokkoló hibák:

  [ ] local_config érzékeny adattal verziózva van.
  [ ] Hibás configgal is elindulhat töltés/kisütés.
  [ ] Exe-ben nem egyértelmû, honnan tölti a configot.


11. CONNECTION_TEST / PREFLIGHT REVIEW
======================================

connection_test.py:

  [ ] list_resources mûködik.
  [ ] placeholder resource skip.
  [ ] IDN lekérés.
  [ ] safe_off csak OFF irányú.
  [ ] nem kapcsol OUTPUT ON-t.
  [ ] nem kapcsol LOAD INPUT ON-t.
  [ ] DMM alap mérés mûködik.
  [ ] summary PASS/SKIP/FAIL.
  [ ] exit code helyes.
  [ ] --config opció hasznos.
  [ ] pyvisa hiányra érthetõ üzenet.

preflight checklist:

  [ ] BY550 irány.
  [ ] PSU mód.
  [ ] 12V/24V pack ellenõrzés.
  [ ] DMM Kelvin pont.
  [ ] PT100 rögzítés.
  [ ] akku polaritás.
  [ ] load polaritás.
  [ ] biztosíték megléte.
  [ ] szellõzés.
  [ ] elsõ futás kisárammal.
  [ ] emergency stop próba.

Blokkoló hibák:

  [ ] connection_test bármilyen ON parancsot kiad.
  [ ] preflight nélkül indítható veszélyes profil.


12. TESZTEK / TESZTFEDÉS REVIEW
===============================

Kötelezõ tesztcsoportok:

  [ ] SafetyManager unit tesztek.
  [ ] ChargeController fault tesztek.
  [ ] DischargeController fault tesztek.
  [ ] RelaxController tesztek.
  [ ] DMM lost tesztek.
  [ ] PSU readback exception tesztek.
  [ ] LOAD readback exception tesztek.
  [ ] series_drop fault teszt.
  [ ] max_charge_time teszt.
  [ ] max_charge_Ah CC/CV/TAPER teszt.
  [ ] max_discharge_Ah teszt.
  [ ] integrátor dt_s teszt.
  [ ] checkpoint atomic write teszt.
  [ ] TestRunner checkpoint/resume teszt.
  [ ] Worker signal teszt.
  [ ] connection_test mock teszt.
  [ ] config validation teszt.
  [ ] report generator invalid-data teszt.

Fõ kérdés:

  Van-e teszt minden olyan hibára, ami hardvert vagy mérési eredményt veszélyeztet?

Blokkoló hibák:

  [ ] SafetyManager nincs tesztelve.
  [ ] Readback exception nincs tesztelve.
  [ ] Emergency stop nincs tesztelve.
  [ ] Integrátor nincs valódi dt_s-sel tesztelve.
  [ ] Report PASS invalid adatra nincs tesztelve.


13. VALÓS HARDVERES VALIDÁCIÓ
=============================

Fokozatok:

  [ ] Mock driver teszt.
  [ ] Valós mûszerek, akku nélkül, csak connection_test.
  [ ] Valós mûszerek dummy terheléssel / dummy akkuval.
  [ ] Kis FG akku kis árammal.
  [ ] Névleges árammal teljes ciklus.
  [ ] Emergency stop teszt valós hardveren.
  [ ] DMM kihúzás / kommunikációs hiba teszt biztonságos körülmények között.
  [ ] PSU/load kommunikációs hiba teszt biztonságos körülmények között.
  [ ] Checkpoint / resume próba.
  [ ] Report validálás.

Éles elõtti minimum:

  [ ] Legalább egy teljes aktuális verziós sikeres futás.
  [ ] Legalább egy aktuális verziós fault/emergency futás.
  [ ] connection_test PASS.
  [ ] kezelõi preflight PASS.
  [ ] report PASS csak valid feltételekkel.

Blokkoló hibák:

  [ ] Csak régi verziós log van.
  [ ] Csak mock teszt van, valós mûszeres futás nincs.
  [ ] Emergency stop nem lett valós hardveren ellenõrizve.


14. FELHASZNÁLÓI HIBÁK ÉS UX SAFETY
===================================

Ellenõrizendõ:

  [ ] Rossz akkutípus kiválasztása ellen védelem.
  [ ] 12 V akku 24 V profilban tiltva vagy extra megerõsítés.
  [ ] 24 V pack rossz PSU módban tiltva.
  [ ] túl nagy töltõáram kis FG akkuhoz tiltva.
  [ ] túl alacsony terminate voltage warning/fault.
  [ ] nincs DMM csatlakoztatva -> nem indul.
  [ ] nincs PT100, de hõvédelem kötelezõ -> nem indul vagy korlátozott mód.
  [ ] rossz VISA resource -> nem indul.
  [ ] hiányzó config -> nem indul.
  [ ] hibás config -> nem indul.
  [ ] GUI egyértelmûen jelzi: MOCK / REAL hardware mód.
  [ ] GUI egyértelmûen jelzi: profile, pack voltage, C10, current limits.

Blokkoló hibák:

  [ ] Kezelõi téves profil veszélyes töltést indíthat.
  [ ] MOCK és REAL mód összekeverhetõ.
  [ ] Hiányzó DMM mellett is mérés indulhat.


15. DOKUMENTÁCIÓ / KEZELÕI CHECKLIST
====================================

Kötelezõ dokumentumok:

  [ ] telepítés menete.
  [ ] elsõ indítás menete.
  [ ] connection_test használata.
  [ ] akku bekötési ellenõrzés.
  [ ] mérés indítása.
  [ ] emergency stop használata.
  [ ] checkpoint/resume használata.
  [ ] report értelmezése.
  [ ] PASS/FAIL/FAULT/DONE jelentése.
  [ ] mit kell tenni fault után.
  [ ] milyen esetben nem szabad folytatni a mérést.
  [ ] akku biztonsági figyelmeztetések.
  [ ] mérési korlátok.
  [ ] verziókezelés / release notes.

Blokkoló hibák:

  [ ] Nincs kezelõi checklist.
  [ ] Emergency stop használata nincs dokumentálva.
  [ ] Report státuszok jelentése nincs dokumentálva.


16. FMEA / KOCKÁZATELEMZÉS
==========================

Legalább az alábbi hibákra legyen FMEA jellegû gondolkodás:

  [ ] DMM 0 V-ot ad vissza.
  [ ] DMM timeout.
  [ ] PSU readback exception.
  [ ] LOAD readback exception.
  [ ] PSU output beragad ON állapotban.
  [ ] LOAD input beragad ON állapotban.
  [ ] Akku fordított polaritással csatlakozik.
  [ ] Akku nincs csatlakoztatva.
  [ ] PT100 leesik az akkuról.
  [ ] PT100 szakadt.
  [ ] Túl magas hõmérséklet.
  [ ] Túl nagy töltõáram.
  [ ] Rossz pack feszültség kiválasztás.
  [ ] GUI lefagy.
  [ ] Program exceptiont dob.
  [ ] PC alvó állapotba megy.
  [ ] USB/VISA kapcsolat megszakad.
  [ ] Checkpoint sérül.
  [ ] Report hiányos adatból készül.

FMEA táblázat:

  Hiba | Következmény | Detektálás | Védelem | Maradék kockázat | Teendõ
  -----|--------------|------------|---------|------------------|-------


17. VÉGLEGESÍTÉSI DÖNTÉSI MÁTRIX
================================

Minõsítés:

  A. ÉLES HASZNÁLATRA KÉSZ
  B. KORLÁTOZOTTAN HASZNÁLHATÓ
  C. NEM HASZNÁLHATÓ ÉLESBEN

A. ÉLES HASZNÁLATRA KÉSZ, ha minden teljesül:

  [ ] Nincs blokkoló safety hiba.
  [ ] Nincs blokkoló metrológiai hiba.
  [ ] Nincs hamis DONE/PASS lehetõség ismert hibából.
  [ ] Tervek és implementáció eltérései lezártak.
  [ ] terv_vs_implementacio_osszehasonlitas minden pontja feldolgozva.
  [ ] Unit/integrációs tesztek PASS.
  [ ] connection_test PASS valós mûszerekkel.
  [ ] legalább egy friss teljes valós futás PASS.
  [ ] legalább egy friss fault/emergency futás biztonságosan zárult.
  [ ] Report tartalmazza a mérési forrásokat és korlátokat.
  [ ] Kezelõi checklist kész.
  [ ] Config/build/release dokumentált.

B. KORLÁTOZOTTAN HASZNÁLHATÓ, ha:

  [ ] nincs közvetlen safety blokkhiba,
  [ ] de vannak dokumentált metrológiai vagy UX korlátok,
  [ ] csak alacsony árammal / felügyelet mellett / adott akkutípussal engedhetõ,
  [ ] a riportban és dokumentációban a korlátozás egyértelmû.

C. NEM HASZNÁLHATÓ ÉLESBEN, ha bármelyik igaz:

  [ ] PSU és LOAD egyszerre aktív lehet.
  [ ] Emergency stop nem garantált.
  [ ] Readback exception 0.0 értékként kezelhetõ.
  [ ] DMM hiba hamis DONE/PASS állapotot okozhat.
  [ ] Overvoltage nem azonnali fault.
  [ ] Fault után kimenet ON maradhat.
  [ ] Töltési paraméter túl tudja lépni a gyártói ajánlást.
  [ ] Nincs friss valós mérési bizonyíték.
  [ ] A terv_vs_implementacio_osszehasonlitas kritikus pontjai nincsenek lezárva.


18. JAVÍTÁSI PRIORITÁS
======================

Javítási sorrend:

  1. safety.py
  2. charge_controller.py
  3. discharge_controller.py
  4. test_runner.py
  5. instrument_manager.py
  6. drivers/device_psu.py
  7. drivers/device_load.py
  8. drivers/device_dmm.py
  9. integrator.py
  10. logger.py
  11. report_generator.py
  12. gui/worker.py
  13. gui/main_window.py
  14. tools/connection_test.py
  15. tests/
  16. documentation / checklist

Csak olyan változtatást érdemes kérni, ami:

  - safety-t javít,
  - mérési pontosságot javít,
  - hamis DONE / PASS állapotot akadályoz meg,
  - hosszú mérés adatvesztését akadályozza meg,
  - valós HW használatot tesz megbízhatóbbá,
  - diagnosztikát javít,
  - vagy a terv és implementáció közötti eltérést tisztázza.

Nem érdemes csak esztétikai vagy divat alapú refaktort kérni, ha nem javítja a fenti célokat.


19. AJÁNLOTT REVIEW PROMPT / PARANCS
====================================

Az alábbi prompt használható Claude Code / külsõ review / belsõ review indítására.

--- PROMPT KEZDETE ---

Senior Python + mûszerautomatizálási + safety-kritikus tesztrendszer reviewer szerepben vizsgáld át ezt a projektet.

Projekt cél:
Python alapú akku parametrizáló / töltés-kisütés validáló program FIAMM FG / FGH / FGHL AGM VRLA akkukhoz. A rendszer PSU-t, elektronikus terhelést, DMM-et, hõmérõt és GUI-t használ. A fõ kockázat nem a kódstílus, hanem a veszélyes hardverállapot, hamis DONE/PASS eredmény, hibás Ah/Wh mérés, rossz akkuparaméter és hosszú mérés adatvesztése.

Kötelezõen dolgozd fel:

  - tervek/ mappa teljes tartalma
  - terv_vs_implementacio_osszehasonlitas fájl
  - review parancsok / review anyagok
  - aktuális programkód
  - config/template fájlok
  - tests/ mappa
  - tools/connection_test.py
  - logger/report/checkpoint kimenetek, ha vannak

Ne általános kódszépség-review-t készíts. A következõ kérdésekre adj bizonyíték-alapú választ:

  1. A tervekben vállalt funkciók implementálva vannak-e?
  2. A terv_vs_implementacio_osszehasonlitas minden pontja le van-e zárva?
  3. Van-e eltérés a terv és a program között, ami safety vagy metrológiai kockázat?
  4. Nem tud-e rosszkor PSU/LOAD bekapcsolni?
  5. Minden hiba biztonságos OFF állapotba visz-e?
  6. Nem tud-e hamis DONE / CHARGE_DONE / PASS / kapacitás eredmény keletkezni?
  7. A mérési adatok metrológiailag értelmezhetõk-e?
  8. A DMM/PSU/LOAD valós mûszeren biztosan konfigurált állapotban van-e?
  9. A logger/checkpoint/report alkalmas-e hosszú mérésre és recovery-re?
  10. A GUI nem tud-e rossz threadbõl belenyúlni mûszerbe?
  11. A tesztek ténylegesen a hardver- és mérési kockázatokat fedik-e?
  12. Az akkuparaméterek megfelelnek-e az FG/FGH/FGHL AGM VRLA gyártói logikának?
  13. Van-e olyan GUI/config opció, ami választható, de nincs ténylegesen implementálva?
  14. Mi hiányzik ahhoz, hogy a program véglegesnek legyen tekinthetõ?

Kiemelt fájlok:

  - src/safety.py
  - src/charge_controller.py
  - src/discharge_controller.py
  - src/relax_controller.py
  - src/test_runner.py
  - src/instrument_manager.py
  - src/integrator.py
  - src/logger.py
  - src/report_generator.py
  - drivers/device_psu.py
  - drivers/device_load.py
  - drivers/device_dmm.py
  - gui/worker.py
  - gui/main_window.py
  - gui/panels/*
  - tools/connection_test.py
  - tests/*

A válasz szerkezete legyen:

  1. Executive summary
  2. Véglegesnek tekinthetõ-e? IGEN / NEM / KORLÁTOZOTTAN
  3. Blokkoló hibák
  4. Safety review
  5. Metrológiai review
  6. Terv vs implementáció review
  7. Akkuparaméter review
  8. Állapotgép review
  9. SCPI / mûszerdriver review
  10. Logger / checkpoint / report review
  11. GUI / worker thread review
  12. Tesztfedés review
  13. Valós hardveres validációs hiányok
  14. Javítási prioritás
  15. Release checklist

Minden megállapításhoz add meg:

  - fájl / függvény / osztály,
  - mi a probléma,
  - miért kockázat,
  - milyen körülmények között jelentkezhet,
  - javasolt javítás,
  - blokkoló-e az éles használat szempontjából.

Külön jelöld:

  - BLOCKER: éles használatot tiltja,
  - HIGH: safety vagy mérési eredmény veszélyeztetett,
  - MEDIUM: megbízhatóság / diagnosztika / recovery kockázat,
  - LOW: tisztaság / karbantarthatóság.

Ne javasolj felesleges refaktort. Csak olyan módosítást kérj, ami safety-t, metrológiát, megbízhatóságot, recovery-t, diagnosztikát vagy terv-implementáció konzisztenciát javít.

A végén adj egy döntést:

  - ÉLES HASZNÁLATRA KÉSZ
  - KORLÁTOZOTTAN HASZNÁLHATÓ
  - NEM HASZNÁLHATÓ ÉLESBEN

És sorold fel pontosan, milyen feltételekkel változhat a döntés ÉLES HASZNÁLATRA KÉSZ állapotra.

--- PROMPT VÉGE ---


20. REVIEW KIMENETI SABLON
==========================

Executive summary:

  Döntés:
  Fõ kockázatok:
  Blokkoló hibák száma:
  High hibák száma:
  Friss valós futási bizonyíték van-e:
  Terv_vs_implementáció feldolgozva-e:
  Élesítés feltétele:

Blokkoló hibák:

  ID | Terület | Fájl | Probléma | Kockázat | Javítás | Élesítést tiltja
  ---|---------|------|----------|----------|---------|----------------

Terv vs implementáció:

  Tervpont | Implementáció | Státusz | Teszt | Kockázat | Teendõ
  ---------|---------------|---------|-------|----------|-------

Safety:

  Ellenõrzés | Eredmény | Bizonyíték | Hiány | Teendõ
  ----------|----------|-----------|-------|-------

Metrológia:

  Mérés | Forrás | Kalibráció | Logolás | Riport | Kockázat
  ------|--------|------------|---------|--------|---------

Tesztfedés:

  Kockázat | Van teszt? | Teszt neve | Eredmény | Hiány
  ---------|-----------|-----------|---------|------

Release checklist:

  [ ] minden blocker javítva
  [ ] minden high safety hiba javítva vagy indokoltan korlátozva
  [ ] tests PASS
  [ ] connection_test PASS
  [ ] friss teljes futás PASS
  [ ] friss fault/emergency futás biztonságosan zárt
  [ ] report validált
  [ ] dokumentáció kész
  [ ] verzió tag / build azonosító rögzítve


21. RÖVID ZÁRÓ KRITÉRIUM
========================

A program akkor tekinthetõ véglegesnek, ha a review után az alábbi három állítás bizonyított:

  1. Veszélyes hardverállapot nem jöhet létre egyetlen ismert szoftveres, kommunikációs vagy kezelõi hiba miatt sem.

  2. Hamis jó eredmény nem jöhet létre invalid mérésbõl, readback hibából, DMM hibából, checkpoint sérülésbõl vagy részleges futásból.

  3. A mért eredmények forrása, korlátja, pontossága és érvényességi feltétele a riportból és a logból utólag egyértelmûen visszakövethetõ.

Ha bármelyik nem bizonyított, a program nem végleges, legfeljebb korlátozottan használható.