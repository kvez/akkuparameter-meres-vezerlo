AKKUTESZTER / AKKU LABOR PROGRAM – ÖSSZEVONT VÉGSÕ REVIEW ÉS VALIDÁCIÓS CHECKLIST
=================================================================================

Verzió: 2026-06-08
Cél: a feltöltött review pontok, FIAMM AGM VRLA akkuparaméter-validálás,
BQ34Z110/BQ34Z100 golden image elõkészítési szempontok, általános programozási
checklist és Python-specifikus ellenõrzések összevonása egyetlen használható
TXT dokumentumba.

A dokumentum célja nem általános kódszépítés, hanem annak igazolása, hogy a program:

  1. nem tud veszélyes PSU / LOAD / BATTERY állapotot létrehozni,
  2. minden hiba esetén biztonságos OFF állapotba jut,
  3. nem tud hamis DONE / PASS / CHARGE_DONE / DISCHARGE_DONE / CAPACITY_OK eredményt adni,
  4. metrológiailag értelmezhetõ Ah / Wh / U / I / T adatokat rögzít,
  5. a tervekben és review-kban rögzített funkciókat ténylegesen implementálja,
  6. a FIAMM FG / FGH / FGHL AGM VRLA akkuk gyártói paramétereivel összhangban mûködik,
  7. BQ34Z110/BQ34Z100 Impedance Track learning / golden image elõkészítéshez
     fizikailag helyes laborfolyamatot biztosít,
  8. világosan jelzi, hogy mit tud bizonyítani a laborprogram, és mit kell külön,
     kézi vagy külsõ BQ eszközzel validálni,
  9. friss, aktuális verzióból származó valós futásokkal is igazolt,
 10. reprodukálhatóan buildelhetõ, tesztelhetõ és auditálható.


0. ALAPELVEK ÉS REVIEW-SZABÁLYOK
=================================

[ ] Ne módosíts kódot review közben, ha a feladat csak ellenõrzés.
[ ] Minden állításhoz legyen bizonyíték: kód, teszt, log, jegyzõkönyv, adatlap vagy terv.
[ ] Ha nincs bizonyíték, a státusz legyen: NEM IGAZOLHATÓ.
[ ] Ha feltételezés alapján történik megállapítás: FELTÉTELEZÉS.
[ ] Ha hiányzik a validáláshoz szükséges bemenet: HIÁNYZÓ BIZONYÍTÉK / REVIEW STOP.
[ ] Régi session log csak történeti információ; aktuális verzió mûködését nem igazolja.
[ ] Végsõ döntéshez friss, aktuális kódból futtatott teszt és friss fizikai mérési log kell.
[ ] Ne dicsérj általánosan. Csak hibákat, kockázatokat, bizonyítékokat és szükséges teendõket írj.
[ ] Ne javasolj esztétikai refaktort, ha nem javít safety-t, mérési helyességet,
    hibaállapot-kezelést, diagnosztikát, reprodukálhatóságot vagy üzemeltetési biztonságot.
[ ] A review végén egyértelmû döntést kell adni:
      - ÉLESRE KÉSZ
      - FELTÉTELESEN KÉSZ
      - NEM KÉSZ

Döntési alap:

ÉLESRE KÉSZ csak akkor adható, ha:
[ ] nincs ismert BLOCKER vagy CRITICAL hiba,
[ ] minden safety útvonal tesztelt,
[ ] DMM / PSU / LOAD hiba esetén garantált a safe_all_off,
[ ] nincs hamis DONE/PASS lehetõség invalid mérési adatból,
[ ] Ah/Wh integrálás validált valós dt_s alapján,
[ ] töltési/kisütési határok gyártói adatokkal igazoltak,
[ ] BQ nélküli korlátok dokumentáltak,
[ ] aktuális verzióval készült valós futási log rendelkezésre áll,
[ ] teljes tesztkészlet futott a célkörnyezetben.

FELTÉTELESEN KÉSZ, ha:
[ ] nincs közvetlen veszélyes hardverállapotot okozó hiba,
[ ] de vannak NEM IGAZOLHATÓ vagy részleges részek,
[ ] csak korlátozott mérési tartományban vagy felügyelettel használható,
[ ] a hiányzó validációk listája egyértelmû.

NEM KÉSZ, ha:
[ ] PSU és LOAD egyszerre aktív lehet,
[ ] emergency_stop / safe_all_off nem garantált,
[ ] DMM feedback lost után DONE/PASS létrejöhet,
[ ] overvoltage nem azonnali fault,
[ ] readback exception 0.0 A-ként vagy 0.0 V-ként érvényes mérésként integrálódhat,
[ ] töltési/kisütési paraméterek adatlap szerint tévesek,
[ ] release elõtt nincs friss fizikai log.


1. REVIEW INPUTOK
=================

A teljes review-hoz szükséges bemenetek:

[ ] Aktuális program ZIP vagy teljes Git repository.
[ ] src/ mappa.
[ ] drivers/ mappa.
[ ] gui/ mappa.
[ ] tools/ mappa.
[ ] tests/ mappa.
[ ] config/ mappa vagy config template.
[ ] requirements.txt, pyproject.toml vagy dependency lista.
[ ] build script / exe build leírás.
[ ] README / kezelõi dokumentáció.
[ ] tervek/ mappa teljes tartalma.
[ ] terv_vs_implementacio_osszehasonlitas dokumentum.
[ ] korábbi review-k és külsõ review-k.
[ ] döntési dokumentumok.
[ ] friss connection_test eredmény.
[ ] friss valós mérési log legalább egy sikeres futásból.
[ ] friss fault / megszakított / emergency stop futás logja.
[ ] használt mûszerek listája.
[ ] bekötési vázlat vagy rövid hardverleírás.
[ ] PSU csatorna- és módleírás: independent / series / parallel.
[ ] elektronikus terhelés típusa és limitjei.
[ ] DMM típusa, mérési módjai, NPLC, timeout.
[ ] relék, diódák, soros elemek, shuntök, kábelek és csatlakozók leírása.
[ ] akku típusok listája: FG / FGH / FGHL / egyéb.
[ ] használt akkuprofilok és config értékek.
[ ] BQ34Z110/BQ34Z100 külsõ ellenõrzési módszer leírása.
[ ] BQ learning / golden image kézi jegyzõkönyv sablonja.

Ha bármelyik safety, mérési vagy BQ-validációhoz szükséges bemenet hiányzik,
az adott pontnál NEM IGAZOLHATÓ vagy REVIEW STOP jelölést kell használni.


2. ELSÕ LÉPÉSEK – GIT, DIFF, REPRODUKÁLHATÓSÁG
===============================================

Futtatandó, ha Git repository elérhetõ:

[ ] git status
[ ] git diff
[ ] git diff --cached
[ ] git log --oneline -n 20
[ ] git branch --show-current
[ ] git rev-parse HEAD

Ellenõrizendõ:

[ ] A review pontos commit hash-hez kötött.
[ ] Nincs véletlenül staged, de nem commitolt változás.
[ ] Nincs véletlenül bekerült log, mérési adat, secret, lokális config, token vagy path.
[ ] A local_config nincs verziózva, csak template.
[ ] A mérési sessionök, SQLite DB-k, CSV-k, checkpointok kizártak vagy tudatosan mintafájlok.
[ ] A requirements / pyproject pontosan reprodukálja a környezetet.
[ ] A buildelt exe verziója visszavezethetõ a commitra.
[ ] A riport tartalmaz commit hash-t, config hash-t és programverziót.

Blokkoló:

[ ] Ismeretlen vagy nem reprodukálható kódból készült release.
[ ] Secret / lokális mûszer resource / privát útvonal bekerül Gitbe.
[ ] Nem egyértelmû, melyik commitot review-ezzük.


3. TERV VS IMPLEMENTÁCIÓ ÖSSZEVETÉS
===================================

Kötelezõ források:

[ ] Folyamatok/tervek/ mappa.
[ ] terv_vs_implementacio_osszehasonlitas.
[ ] döntések mappa.
[ ] aktuális kód.
[ ] aktuális tesztek.
[ ] aktuális README / kezelési dokumentáció.
[ ] aktuális log / riport.

Feladat:

[ ] Gyûjtsd ki a tervekben vállalt összes funkciót.
[ ] Minden tervponthoz keresd meg az implementáció helyét.
[ ] Minden tervponthoz keresd meg a tesztet.
[ ] Minden tervponthoz keresd meg, hogy a riport/log bizonyítja-e.
[ ] Ha nincs implementáció: HIÁNYZIK.
[ ] Ha részleges: RÉSZLEGES.
[ ] Ha a terv elavult: TERV_ELAVULT, indoklással.
[ ] Ha az implementáció eltér a tervtõl: ELTÉRÉS_JOGOS vagy ELTÉRÉS_HIBA.
[ ] Minden korábbi külsõ review pontra legyen döntés:
      - jogos és javítandó,
      - jogos és már javítva,
      - nem releváns,
      - téves megállapítás,
      - további bizonyíték kell.

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
- NEM_IGAZOLHATÓ

Blokkoló kérdések:

[ ] Van tervben szereplõ safety funkció, ami nincs implementálva?
[ ] Van tervben szereplõ mérési korrekció, ami csak GUI-ban látszik, de nem hat a számításra?
[ ] Van opció, ami választható, de nincs mögötte mûködõ logika?
[ ] Van implementált funkció, ami nincs dokumentálva?
[ ] Van implementált funkció, ami ellentmond a tervnek?
[ ] Van olyan „késznek” jelölt rész, amit aktuális log nem igazol?
[ ] Van olyan korábbi RÉSZLEGES / HIÁNYZIK pont, ami még mindig nincs lezárva?


4. ÁLTALÁNOS PROGRAMOZÁSI CHECKLIST
===================================

4.1 Architektúra és rétegek
---------------------------

[ ] A GUI nem hív közvetlenül mûszerdrivert.
[ ] A mûszervezérlést controller / service / worker réteg végzi.
[ ] A safety logika nem GUI-függõ.
[ ] A safety manager megkerülhetetlen minden kimenetkapcsolás elõtt.
[ ] A TestRunner GUI-tól függetlenül futtatható.
[ ] A driver réteg nem tartalmaz üzleti döntést, csak biztonságos low-level mûveleteket.
[ ] A config és hardverprofil külön rétegben van.
[ ] Az akkuprofil nem keveredik a tesztprogram logikájával.
[ ] Az állapotgépek explicit állapotokkal és átmenetekkel dolgoznak.
[ ] Nincs rejtett globális állapot, ami több futás között megmarad.
[ ] A dependency direction tiszta: GUI -> workflow -> controller -> driver, nem fordítva.

4.2 Logikai helyesség
---------------------

[ ] Minden állapotnak van explicit belépési feltétele.
[ ] Minden állapotnak van explicit kilépési feltétele.
[ ] Nincs implicit success kivétel vagy timeout után.
[ ] Nincs olyan default ág, ami csendben DONE-t vagy OK-t ad.
[ ] Hibás, hiányzó vagy érvénytelen adat nem alakulhat 0.0 érvényes értékké.
[ ] None / NaN / Inf / overload / timeout mindenhol fault vagy invalid.
[ ] A mértékegységek explicit jelöltek: V, A, Ah, Wh, s, °C, ?, W.
[ ] C-ráta számításnál egyértelmû, hogy Ah vagy A alapján történik.
[ ] C10 / C20 / C5 fogalmak nem keverednek.
[ ] 6 V / 12 V / 24 V pack cellaszáma explicit.
[ ] A függvények visszatérési értékei egyértelmûek: mérés, státusz, fault, warning.
[ ] Nem keveredik warning és fault jelentése.

4.3 Hiba- és kivételkezelés
---------------------------

[ ] Minden hardverkommunikáció timeouttal védett.
[ ] Minden timeout faultot vagy kontrollált retry-t okoz.
[ ] Retry nem kapcsolhat vissza automatikusan kimenetet emberi döntés nélkül, ha safety fault volt.
[ ] A kivételkezelés nem nyeli el csendben a hibát.
[ ] A catch-all exception csak logolás + safe_all_off mellett megengedett.
[ ] A fault reason megmarad a riportban.
[ ] A warning reason megmarad a riportban.
[ ] A hibák reprodukálhatóan logolódnak: idõ, állapot, mérési érték, mûszer, parancs.
[ ] A logger hibája nem akadályozhatja meg a safe_all_off végrehajtását.
[ ] safe_all_off hiba esetén is megpróbál minden kimenetet lekapcsolni.

4.4 Erõforrás-kezelés
---------------------

[ ] Soros portok lezáródnak minden kilépési úton.
[ ] VISA / LAN kapcsolatok lezáródnak minden kilépési úton.
[ ] Fájlok lezáródnak minden kilépési úton.
[ ] Logger close() fut DONE, FAULT, STOP és exception esetén is.
[ ] GUI bezárás futó mérés alatt safe stop vagy emergency stop irányba megy.
[ ] Többszöri start/stop után nincs duplán nyitott port vagy duplán bekötött signal.
[ ] Hosszú futásnál nincs memóriaszivárgás.
[ ] Hosszú futásnál a logolás nem fogyaszt korlátlan RAM-ot.

4.5 Idõzítés és ciklusidõ
-------------------------

[ ] elapsed_s monotonic/perf_counter alapú.
[ ] dt_s valódi mérési idõközbõl számolt, nem névleges tick.
[ ] sleep csak a ciklusból megmaradt idõre fut.
[ ] Ha a ciklus túlcsúszik, ez logolódik.
[ ] Túl nagy dt_s gap kezelve van.
[ ] Negatív vagy nulla dt_s fault vagy ignore.
[ ] A timeout értékek összhangban vannak a DMM NPLC-vel és mûszer válaszidejével.
[ ] A relax idõzítés valódi eltelt idõ alapján fut.
[ ] A max_charge_time minden töltési fázisban aktív.
[ ] A max_discharge_time minden kisütési fázisban aktív.
[ ] A taper_hold idõzítõ csak valid feltételek mellett gyûlik.
[ ] A taper_hold idõzítõ resetelõdik, ha U/I feltétel megszûnik vagy mérés invalid.

4.6 Numerikus helyesség
-----------------------

[ ] Nincs integer overflow, ha C/C++ kód is van.
[ ] Pythonban sincs implicit típuskeverés, ami mértékegységhibát okoz.
[ ] signed/unsigned logika C/C++ esetén ellenõrzött.
[ ] Floating point összehasonlítás toleranciával történik.
[ ] NaN és Inf explicit kiszûrt.
[ ] Kerekítést csak kijelzésnél használ a program, döntési logikában nem.
[ ] A log nyers értéket is tartalmaz, nem csak kerekítettet.
[ ] Wh integrálás U*I*dt alapján történik.
[ ] Ah integrálás I*dt alapján történik.
[ ] Ah/Wh elõjel-konvenció egyértelmû: charge/discharge külön vagy elõjeles.
[ ] A diódaesés, kábel, relé, panel ellenállás és akku belsõ ellenállás nincs összekeverve.

4.7 Konfiguráció és validálás
-----------------------------

[ ] Minden config mezõ tartományellenõrzött.
[ ] Kötelezõ mezõ hiánya indítási hibát okoz, nem default veszélyes értéket.
[ ] 12 V / 24 V pack választás ellenõrzött.
[ ] Series / parallel / independent PSU mód validált.
[ ] Cellaszám és nominális feszültség konzisztens.
[ ] C10/C20 kapacitásmezõk egyértelmûek.
[ ] Töltési feszültség nem lépheti túl adatlap szerinti határt.
[ ] Töltõáram nem lépheti túl adatlap szerinti határt.
[ ] Kisütõáram nem lépheti túl akku, load, kábel, relé, dióda és csatlakozó limitjét.
[ ] Hõmérsékleti limitek külön validáltak töltésre és kisütésre.
[ ] Local config nem írhat felül safety limitet rejtetten.

4.8 Naplózás és auditálhatóság
------------------------------

[ ] Minden futás egyedi session ID-t kap.
[ ] Session meta tartalmazza: programverzió, commit, config, akkuprofil, mûszerlista.
[ ] CSV log tartalmazza: elapsed_s, dt_s, állapot, U, I, T, Ah, Wh, fault, warning.
[ ] SQLite log vagy alternatív adatbázis konzisztens a CSV-vel.
[ ] events.csv tartalmazza az állapotváltásokat és hibákat.
[ ] Emergency stop külön eseményként szerepel.
[ ] Manual checkpoint külön eseményként szerepel.
[ ] A report jelzi a mérési forrásokat: DMM, PSU readback, LOAD readback, külsõ shunt hiánya.
[ ] A report jelzi a módszer korlátait.
[ ] A report nem állít többet, mint amit a program bizonyítani tud.

4.9 Tesztelhetõség
------------------

[ ] Van unit test integrátorra.
[ ] Van unit test akkuprofil validációra.
[ ] Van unit test állapotgép-átmenetekre.
[ ] Van unit test safety managerre.
[ ] Van unit test config hibákra.
[ ] Van fake/mock driver a PSU/LOAD/DMM szimulálására.
[ ] Van fault injection teszt DMM timeouttal.
[ ] Van fault injection teszt PSU timeouttal.
[ ] Van fault injection teszt LOAD timeouttal.
[ ] Van emergency stop teszt minden fõ állapotból.
[ ] Van hosszú futás / dt gap teszt.
[ ] Van log/recovery/checkpoint teszt.
[ ] Van GUI nélküli core teszt.
[ ] Van célkörnyezeti GUI teszt PySide6-tal.


5. PYTHON-SPECIFIKUS CHECKLIST
==============================

5.1 Projektstruktúra és dependency
----------------------------------

[ ] Egyértelmû entry point: main.py vagy console script.
[ ] requirements.txt vagy pyproject.toml teljes.
[ ] Python verzió rögzített.
[ ] PySide6 verzió rögzített, ha GUI kell.
[ ] pyvisa / pyserial / yaml / numpy / pytest verziók rögzítettek.
[ ] Windows célgépen is fut a dependency install.
[ ] PyInstaller / exe build script reprodukálható.
[ ] Build után a config template és szükséges resource fájlok bekerülnek.
[ ] Relatív path kezelés pathlib alapú, nem munkakönyvtár-függõ.

5.2 Típusok, dataclassok, enumok
--------------------------------

[ ] Állapotok Enum típusok, nem string literálok szórva.
[ ] FaultCode és WarningCode külön Enum.
[ ] BatteryProfile dataclass vagy hasonló explicit mezõkkel.
[ ] Minden fizikai mennyiség mezõnévben jelzi az egységet: voltage_V, current_A, capacity_Ah.
[ ] Optional értékek ellenõrzöttek használat elõtt.
[ ] mypy/pyright legalább alap szinten futtatható.
[ ] Dict[str, Any] csak határfelületen van, belsõ logika typed objektumokat használ.

5.3 Kivételkezelés Pythonban
----------------------------

[ ] Bare except nincs, vagy csak safe_all_off + log + re-raise/controlled fault mellett.
[ ] Exception lenyelése nincs.
[ ] finally blokkban safe_all_off és resource close lefut.
[ ] Driver exception nem fordul át érvényes 0.0 mérési adattá.
[ ] Timeout exception külön kezelve.
[ ] Parse error külön kezelve.
[ ] Kommunikációs hiba külön kezelve.
[ ] A GUI worker exception signalon át megjelenik és safe stopot okoz.

5.4 I/O, fájlkezelés, checkpoint
--------------------------------

[ ] Fájlírás with open(...) context managerrel.
[ ] CSV flush rendszeresen történik.
[ ] SQLite commit stratégia nem túl ritka és nem túl pazarló.
[ ] checkpoint.json írás atomi: temp file + flush + fsync + os.replace.
[ ] Sérült checkpoint felismerhetõ.
[ ] Checkpoint verziózott.
[ ] Checkpoint csak kézi BQ checkpoint / tervezett megállás folytatására használható, ha valódi power-loss recovery nincs.
[ ] A program nem állít valódi power-loss recovery-t, ha nincs implementálva.
[ ] Logger close fault esetén is fut.

5.5 Threading / PySide6 / GUI
-----------------------------

[ ] GUI csak GUI threadbõl frissít widgetet.
[ ] Worker thread futtatja a TestRunnert.
[ ] Mûszerdriver csak worker threadbõl hívódik.
[ ] Signal-slot kapcsolat nem duplikálódik többszöri Start/Stop után.
[ ] QThread cleanup: quit, wait, deleteLater.
[ ] Emergency Stop gomb mindig elérhetõ.
[ ] GUI bezárása futó mérés alatt safe stopot kér.
[ ] GUI fagyás nem akadályozhatja az emergency stop hardveres vagy worker oldali útját.
[ ] GUI állapotsor nem jelezhet félrevezetõ DONE/PASS állapotot.
[ ] Checkpoint panel világosan jelzi: manual checkpoint, resume_possible, terminal checkpoint.

5.6 Statikus ellenõrzések
-------------------------

Ajánlott futtatások:

[ ] python -m compileall .
[ ] pytest -q
[ ] pytest -q Prog/tests vagy projekt szerinti tesztútvonal
[ ] ruff check .
[ ] mypy . vagy pyright
[ ] pip-audit vagy dependency audit, ha van internetes / release folyamat
[ ] pyinstaller build próba célgépen

Ellenõrizendõ:

[ ] Nincs import-time hardverkapcsolódás.
[ ] A tesztek nem igényelnek valós mûszert, kivéve explicit hardware integration test.
[ ] A mock driver és valós driver interfésze azonos.
[ ] A GUI tesztek futnak olyan környezetben, ahol PySide6 telepítve van.
[ ] A nem-GUI core tesztek külön is futtathatók.


6. SAFETY / HARDVERVÉDELEM
==========================

Ez a legfontosabb review-terület.

6.1 Kimenetek és veszélyes állapotok
------------------------------------

[ ] PSU OUTPUT soha nem kapcsolhat be rossz állapotban.
[ ] LOAD INPUT soha nem lehet aktív töltés közben.
[ ] PSU és LOAD nem lehet egyszerre aktív.
[ ] Relax alatt PSU OFF és LOAD OFF.
[ ] DONE után az adott folyamatnak megfelelõ kimenetek OFF állapotban vannak.
[ ] FAULT után minden kimenet OFF állapotban van.
[ ] STOP után biztonságos állapot van.
[ ] PSU output OFF nem azonos galvanikus leválasztással; ezt dokumentálni kell.
[ ] Relékkel vagy külsõ leválasztással kapcsolatos feltételek dokumentáltak.
[ ] Relé zárás/bontás sorrendje nem okoz rövidzárat, reverse currentet vagy load/psu ütközést.

6.2 safe_all_off
----------------

Kötelezõ sorrend:

  1. Elektronikus terhelés INPUT OFF.
  2. Tápegység OUTPUT OFF / all outputs off.
  3. Relék biztonságos alaphelyzetbe.
  4. Állapot és esemény logolása.

Ellenõrizendõ:

[ ] safe_all_off minden fault és emergency útvonalon meghívódik.
[ ] safe_all_off idempotens: többször hívva sem okoz hibát vagy visszakapcsolást.
[ ] safe_all_off exception esetén is tovább próbálja a többi kimenetet lekapcsolni.
[ ] safe_all_off végén visszaellenõrzés van, ha a mûszer támogatja.
[ ] safe_all_off nem függ a GUI állapotától.
[ ] safe_all_off logolja, ha valamelyik lekapcsolás nem igazolható.

Blokkoló:

[ ] safe_all_off bármely hiba esetén megszakad úgy, hogy maradhat aktív kimenet.
[ ] LOAD és PSU egyszerre bekapcsolva maradhat.
[ ] Emergency stop nem garantáltan jut el safe_all_off-ig.

6.3 Emergency stop
------------------

[ ] Emergency stop minden állapotból mûködik: INIT, PRECHECK, CC, CV, TAPER, DISCHARGE, RELAX, CHECKPOINT.
[ ] Emergency stop minden tickben ellenõrzött.
[ ] Emergency stop nem csak GUI eseményre támaszkodik.
[ ] Emergency stop után nincs automatikus resume.
[ ] Emergency stop után új futás csak új preflighttal indulhat.
[ ] Emergency stop esemény bekerül events.csv-be / reportba.
[ ] Emergency stop alatt a logger hibája nem akadályozza a lekapcsolást.

6.4 DMM feedback lost
---------------------

[ ] DMM timeout töltés közben azonnali fault.
[ ] DMM timeout kisütés közben azonnali fault.
[ ] DMM timeout relax közben nem eredményez OCV OK-t.
[ ] DMM overload / NaN / Inf invalid mérés.
[ ] DMM invalid adat nem válhat 0.0 V értékké.
[ ] DMM invalid adat nem okozhat hamis taper feltételt.
[ ] DMM invalid adat nem okozhat hamis terminate voltage döntést.
[ ] DMM konfiguráció minden kritikus mérés elõtt ellenõrzött vagy garantált.

6.5 Overvoltage / undervoltage / polarity
-----------------------------------------

[ ] Battery overvoltage azonnali fault.
[ ] Töltési célfeszültség clampelve van.
[ ] DMM-alapú diódaesés-kompenzáció nem lépheti túl a max PSU set voltage értéket.
[ ] PSU headroom-limit warning/fault logika létezik.
[ ] Reverse polarity detektálás vagy kezelõi preflight tiltás van.
[ ] No battery / disconnected battery detektálás van.
[ ] Mélykisütött akkunál kontroll nélküli nagyáramú boost tiltott.

6.6 BY550 / soros dióda / series drop
-------------------------------------

[ ] Dióda polaritása dokumentált.
[ ] DMM az akkuoldali feszültséget méri.
[ ] PSU sense / PSU kimenet és akkuoldali DMM logika egyértelmû.
[ ] series_drop túl nagy érték esetén fault.
[ ] diode_power warning vagy fault döntése dokumentált.
[ ] 3 A körüli diódateljesítmény és hûtés figyelmeztetés reális.
[ ] Diódaesés-kompenzáció lassú, korlátozott és overvoltage-safe.
[ ] max_step_up és max_step_down indokolt.
[ ] Kompenzáció nem tud oszcillálni vagy túllõni.


7. MÛSZERDRIVEREK / SCPI
========================

7.1 PSU driver
--------------

[ ] Connect után safe állapotba megy.
[ ] Output off parancs biztos.
[ ] all_outputs_off tényleg minden csatornát lekapcsol.
[ ] Channel enable és output on kettõs réteg nem keveredik.
[ ] SERIES / PARALLEL / INDEPENDENT mód parancsai valós mûszeren igazoltak.
[ ] Teszt végén a mûszer normal/independent módba visszaáll.
[ ] Readback CH1 / combined logika dokumentált.
[ ] Readback timeout fault.
[ ] Hibás SCPI válasz nem alakul 0.0 A/V értékké.
[ ] Error queue lekérdezés megfelelõ gyakoriságú.
[ ] PSU OVP/OCP állapot felismerhetõ.
[ ] PSU remote/local állapot kezelve.

7.2 Elektronikus terhelés driver
--------------------------------

[ ] Connect után input_off.
[ ] input_on csak controlleren keresztül történhet.
[ ] set_current elõtt a mód helyes: CC mód.
[ ] Current limit a config és akkuprofil szerint clampelve.
[ ] Power limit ellenõrzött.
[ ] Voltage limit / undervoltage limit ellenõrzött.
[ ] Load readback exception fault.
[ ] Load input off DONE és FAULT esetén is.
[ ] Soft-start vagy slew rate szükségessége eldöntött.
[ ] Elektronikus terhelés nem aktív, ha PSU aktív.

7.3 DMM driver
--------------

[ ] DCV konfiguráció biztosított.
[ ] PT100/FRTD konfiguráció biztosított, ha hõmérsékletet mér.
[ ] NPLC illeszkedik a ciklusidõhöz.
[ ] Timeout illeszkedik az NPLC-hez.
[ ] Overload érték felismerése implementált.
[ ] NaN/Inf felismerés implementált.
[ ] Voltage jump detector vagy irreális mérés szûrés van.
[ ] DMM mérés helye dokumentált: akkukapocs, nem PSU kapocs.
[ ] DMM hibája nem okozhat DONE/PASS állapotot.


8. METROLÓGIAI HELYESSÉG
========================

8.1 Ah / Wh integrálás
----------------------

[ ] Ah integrátor valódi dt_s alapján számol.
[ ] Wh integrátor valódi dt_s alapján számol.
[ ] dt_s perf_counter/monotonic alapú.
[ ] dt_s logolva van.
[ ] elapsed_s logolva van.
[ ] Túl nagy dt_s gap kezelve van.
[ ] Negatív/0 dt_s kezelve van.
[ ] Charge current forrása dokumentált: PSU_READBACK vagy külsõ shunt.
[ ] Discharge current forrása dokumentált: LOAD_READBACK vagy külsõ shunt.
[ ] Ha nincs külsõ kalibrált shunt, ez szerepel a riportban.
[ ] Readback exception nem integrálódhat 0.0-ként.
[ ] Ah és Wh nullázás, session kezdés és resume logika ellenõrzött.

8.2 Feszültségmérés
-------------------

[ ] DMM voltage tényleg akkukapcson mér.
[ ] Töltésnél a döntési feszültség DMM akkuoldali feszültség.
[ ] PSU readback feszültség nem helyettesíti az akkukapocs feszültséget.
[ ] Kisütésnél terminate voltage DMM alapján történik.
[ ] Relax OCV DMM alapján történik.
[ ] Voltage measurement uncertainty szerepel a riportban.
[ ] Kábel és relé feszültségesés nem keveredik az akkufeszültséggel.

8.3 Árammérés
-------------

[ ] Töltõáram forrása dokumentált.
[ ] Kisütõáram forrása dokumentált.
[ ] PSU readback és LOAD readback pontossága ismert vagy riportban korlátozásként szerepel.
[ ] Külsõ referencia méréssel validált az áramreadback.
[ ] Kis áramú taper tartományban a PSU readback felbontása elegendõ.
[ ] Float áram nem használt önmagában SOC indikátornak.

8.4 Hõmérsékletmérés
--------------------

[ ] Akkuhõmérséklet mérése közvetlen vagy a hiánya kockázatként jelölt.
[ ] Környezeti hõmérséklet és akkuhõmérséklet nincs összekeverve.
[ ] PT100/FRTD beállítás validált.
[ ] Hõmérsékleti szenzor szakadás/short detektált.
[ ] Magas hõmérsékleten boost tiltás vagy korlátozás van.
[ ] Töltés közbeni dT/dt vagy akku-környezet delta T figyelés megvalósított vagy hiányként jelölt.

8.5 Belsõ ellenállás / Rb
-------------------------

[ ] Rb mérésnél külön van választva:
      - DC pulse resistance,
      - dynamic impedance,
      - scope transient,
      - adatlap szerinti IEC mérés.
[ ] Kábel / relé / panel / Faston / csatlakozó ellenállása kalibrálható vagy korlátozásként szerepel.
[ ] A program nem minõsít jó akkunak csak OCV alapján.
[ ] Terhelés alatti feszültségesés trendet figyeli vagy dokumentáltan nem.
[ ] Belsõ ellenállás értelmezése SOC és hõmérsékletfüggõként szerepel.


9. ÁLLAPOTGÉPEK REVIEW
======================

9.1 ChargeController
--------------------

Javasolt állapotok:

  INIT -> PRECHECK -> PSU_PRESET -> CC -> CV -> TAPER_HOLD -> DONE
                                      \-> FAULT bármely pontból

Ellenõrizendõ:

[ ] Minden állapotból van biztonságos kilépés.
[ ] INIT nem kapcsol kimenetet validáció elõtt.
[ ] PRECHECK ellenõrzi az akkufeszültséget, polaritást, hõmérsékletet, configot.
[ ] PSU_PRESET nem okoz akku túlfeszültséget.
[ ] CC állapotban az áramlimit helyes.
[ ] CV állapotban a DMM akkuoldali feszültség a döntési alap.
[ ] CV állapotnak van idõlimitje.
[ ] TAPER_HOLD feltétel csak valid DMM és valid PSU readback mellett igaz.
[ ] TAPER feltétel: U_batt >= target - tolerance.
[ ] TAPER feltétel: I_charge <= taper_current.
[ ] TAPER timer resetelõdik, ha U/I feltétel megszûnik.
[ ] TAPER_HOLD-nak van maximális idõlimitje.
[ ] max_charge_time minden töltési fázisban fut.
[ ] max_charge_Ah minden töltési fázisban fut.
[ ] DONE elõtt minden elvárt leállási feltétel teljesül.
[ ] DONE nem jöhet létre DMM hiba, PSU hiba vagy invalid mérés után.
[ ] FAULT minden esetben safe_all_off.

9.2 DischargeController
-----------------------

[ ] Load bekapcsolás elõtt PSU OFF.
[ ] Load current setpoint a C-ráta és hardware limit alapján validált.
[ ] C/20 kapacitásteszt külön kezelhetõ.
[ ] C/5 learning kisütés külön kezelhetõ.
[ ] Terminate voltage mûködik.
[ ] Terminate voltage DMM alapján történik.
[ ] DMM hiba fault.
[ ] Load current readback hiba fault.
[ ] max_discharge_time mûködik.
[ ] max_discharge_Ah mûködik.
[ ] Load power limit mûködik.
[ ] DONE/FAULT esetén Load OFF.
[ ] Feszültség-visszaemelkedés terhelés levétele után nem minõsíti át automatikusan a kisütést.

9.3 RelaxController
-------------------

[ ] Relax alatt PSU OFF.
[ ] Relax alatt LOAD OFF.
[ ] Reléállapot biztonságos.
[ ] DMM OCV mérés validált.
[ ] dV/dt csak valid baseline után számolódik.
[ ] OCV/SOC jelölés csak megfelelõ relax után érvényes.
[ ] Töltés utáni relax idõ külön paraméter.
[ ] Kisütés utáni relax idõ külön paraméter.
[ ] Relax megszakítás esetén a következõ BQ checkpoint nem tekinthetõ validnak.

9.4 TestRunner / Workflow
-------------------------

[ ] run() blokkoló, de GUI-tól független.
[ ] start_step_index validáció korrekt.
[ ] CHECKPOINT_STOPPED helyes kezelése.
[ ] MANUAL_CHECKPOINT eventet küld.
[ ] step_changed események jó indexet adnak.
[ ] actual dt_s korrekt.
[ ] emergency_stop minden tickben ellenõrzött.
[ ] graceful stop csak biztonságos pontokon hat.
[ ] logger.close() minden kilépési úton megtörténik.
[ ] Ugyanaz a TestRunner újrahasználható-e, vagy session-once objektumként dokumentált.
[ ] Kivétel esetén safe_all_off fut.
[ ] Resume hibás állapotból nem kapcsolhat újra kimenetet preflight nélkül.


10. FIAMM FG / FGH / FGHL AGM VRLA AKKU PARAMÉTEREK
====================================================

10.1 Akkutípus és kapacitás
---------------------------

[ ] A programban szereplõ akkutípus valóban a kiválasztott sorozat: FG, FGH, FGHL, FGL vagy más.
[ ] FG általános célú AGM VRLA, nem kifejezetten high-rate UPS típus.
[ ] FGH high-rate típus, nem azonos FG-vel.
[ ] FGHL long-life / flame-retardant casing jelleg külön kezelve, ha releváns.
[ ] A konkrét típus pontosan azonosított: pl. FG20121, FG20721, FG21201.
[ ] A program nem keveri a C10 és C20 adatokat.
[ ] C10 értelmezése:
      - 10 órás kisütés,
      - 1.80 V/cell végfeszültségig,
      - referencia hõmérséklet dokumentált.
[ ] C20 értelmezése:
      - 20 órás kisütés,
      - 1.75 V/cell végfeszültségig,
      - referencia hõmérséklet dokumentált.
[ ] Töltõáram, boost áramlimit, kisütési teszt és SOC számítás lehetõleg C10 alapján történik.
[ ] Ha csak capacity_Ah mezõ van, dokumentált, hogy C10 vagy C20.
[ ] Javasolt mezõk:
      - capacity_C10_Ah
      - capacity_C20_Ah
      - capacity_reference_temperature_C
      - capacity_end_voltage_V_per_cell

Példa FG20121 esetén:

[ ] Névleges feszültség: 12 V.
[ ] C10 kapacitás: 1.09 Ah.
[ ] C20 kapacitás: 1.2 Ah.
[ ] C10 alapú számításokhoz 1.09 Ah használandó, nem 1.2 Ah.

10.2 Cellaszám és feszültségszintek
-----------------------------------

[ ] 6 V-os FG akku: 3 cella.
[ ] 12 V-os FG akku: 6 cella.
[ ] 24 V pack két 12 V blokk esetén 12 cella, de pack topológia külön validálandó.
[ ] Minden V/cell érték a helyes cellaszámmal szorzódik.
[ ] Nem keveredik:
      - névleges feszültség,
      - OCV / nyugalmi feszültség,
      - float feszültség,
      - boost feszültség,
      - terhelés alatti feszültség,
      - kisütési végfeszültség,
      - mélykisütési tiltási határ.

12 V-os FG alapértékek 20 °C-on:

[ ] Float: 2.27 V/cell x 6 = 13.62 V.
[ ] Boost: 2.40 V/cell x 6 = 14.40 V.
[ ] C10 kapacitásteszt végfeszültség: 1.80 V/cell x 6 = 10.80 V.
[ ] C20 adatlap szerinti végfeszültség: 1.75 V/cell x 6 = 10.50 V.

6 V-os FG alapértékek 20 °C-on:

[ ] Float: 2.27 V/cell x 3 = 6.81 V.
[ ] Boost: 2.40 V/cell x 3 = 7.20 V.
[ ] C10 kapacitásteszt végfeszültség: 1.80 V/cell x 3 = 5.40 V.
[ ] C20 adatlap szerinti végfeszültség: 1.75 V/cell x 3 = 5.25 V.

10.3 Float töltés
-----------------

Gyártói elv:

  Float cél: 2.27 V/cell @ 20 °C.
  Hõmérséklet-kompenzáció: -2.5 mV/cell/°C.
  Float áram teljesen feltöltött AGM akkunál tipikusan kicsi, de nem megbízható SOC indikátor.

Ellenõrizendõ:

[ ] 12 V-os FG akkunál 20 °C-on kb. 13.62 V float célérték.
[ ] 6 V-os FG akkunál 20 °C-on kb. 6.81 V float célérték.
[ ] Van hõmérséklet-kompenzáció vagy a hiánya dokumentált kockázat.
[ ] 12 V-os akku hõkompenzációja: -15 mV/°C blokk szinten.
[ ] 6 V-os akku hõkompenzációja: -7.5 mV/°C blokk szinten.
[ ] Képlet 12 V esetén: float_voltage = 13.62 V + (20 °C - T_batt) x 0.015 V.
[ ] Képlet 6 V esetén: float_voltage = 6.81 V + (20 °C - T_batt) x 0.0075 V.
[ ] Magas hõmérsékleten nem marad túl magas float feszültség.
[ ] Tartós túlfeszültség figyelve van.
[ ] Szokatlanul magas float áram warning/fault.
[ ] Float áram önmagában nem SOC döntési alap.

10.4 Boost / recharge
---------------------

Gyártói elv:

  Boost cél: 2.40 V/cell @ 20 °C.
  Max. áramlimit: 0.25 C10.
  Boost stop: ha az áram 0.03 C10 alá esik, vagy hõmérsékleti feltétel teljesül.
  Hõkompenzáció a float kritérium szerint.

Ellenõrizendõ:

[ ] 12 V-os FG akkunál boost cél 20 °C-on kb. 14.40 V.
[ ] 6 V-os FG akkunál boost cél 20 °C-on kb. 7.20 V.
[ ] I_boost_max = 0.25 x C10.
[ ] I_boost_stop = 0.03 x C10.
[ ] FG20121 példánál C10 = 1.09 Ah:
      - I_boost_max kb. 0.27 A,
      - I_boost_stop kb. 33 mA.
[ ] Boost módnak maximális idõkorlátja van.
[ ] Boost nem maradhat aktív szenzorhiba, öreg akku vagy hibás akku esetén.
[ ] Mélykisütött akkunál nem indul azonnal teljes boost validáció nélkül.
[ ] Boost csak indokolt esetben fut: kisütés után, karbantartáskor, szerviz/teszt módban.
[ ] Normál standby alapállapot float.
[ ] Boost közben figyelt:
      - akku feszültség,
      - töltõáram,
      - akkuhõmérséklet,
      - környezeti hõmérséklet,
      - dT/dt, ha van,
      - túlfeszültség,
      - túláram,
      - no battery / szakadás.

10.5 Kisütési határok és SOC
----------------------------

[ ] Terhelés alatti feszültségbõl a program nem számol közvetlen SOC-t kompenzáció nélkül.
[ ] Töltés alatti feszültségbõl a program nem számol közvetlen SOC-t.
[ ] OCV-alapú SOC csak pihent akkun érvényes.
[ ] OCV-SOC mérésnél szerepel, hogy legalább hosszú relax / leválasztás szükséges.
[ ] A program különbözteti:
      - charging voltage,
      - loaded voltage,
      - resting voltage / OCV,
      - recovery voltage.
[ ] C10 kapacitásteszt végpont: 1.80 V/cell.
[ ] 12 V-os akkunál ez 10.80 V, de nem automatikusan üzemi low-battery cutoff.
[ ] Több fokozat van:
      - elõriasztás,
      - low battery,
      - load shed,
      - akkuterhelés tiltása,
      - mélykisütés lockout.
[ ] Feszültség-visszaemelkedés kezelve van.
[ ] Üresjárásban jó, de kis terhelésre összeesõ akku nem minõsülhet jónak.

10.6 Hõmérséklet, élettartam, zárt doboz
----------------------------------------

[ ] Ajánlott üzemi tartomány külön kezelve.
[ ] Hideg miatti kapacitáscsökkenés figyelembe vett vagy riportban jelölt.
[ ] Meleg miatti élettartamcsökkenés figyelmeztetésben szerepel.
[ ] Magas hõmérsékleten boost korlátozott vagy tiltott.
[ ] Zárt fém dobozban az akku saját melegedése és az elektronika hõje együtt értékelt.
[ ] Hidrogénképzõdés / szellõzés / VRLA biztonsági szelep kockázata dokumentált.
[ ] AGM akku mechanikai rögzítése, telepítési iránya, csatlakozó típusa dokumentált.


11. HELYES LABORFOLYAMATOK – TÖLTÉS, KISÜTÉS, RELAX
===================================================

11.1 Preflight minden futás elõtt
---------------------------------

[ ] Akkutípus kiválasztva és fizikailag ellenõrizve.
[ ] Cellaszám és pack feszültség ellenõrizve.
[ ] Polaritás ellenõrizve.
[ ] Akku kezdeti feszültség mért DMM-mel.
[ ] Akkuhõmérséklet vagy környezeti hõmérséklet rögzítve.
[ ] PSU mód helyes: independent / series / parallel.
[ ] 24 V pack esetén hardware_wiring_confirmed szükséges.
[ ] 12 V pack series módban csak warninggal / extra megerõsítéssel indulhat.
[ ] DMM az akkukapcson mér.
[ ] Load és PSU kimenet OFF állapotban.
[ ] Relék alapállapotban.
[ ] Emergency stop elérhetõ.
[ ] Elsõ fizikai futás kis árammal történik.
[ ] Config értékek validáltak.
[ ] Log session elindult.

11.2 Helyes töltési menet AGM VRLA akkunál
------------------------------------------

Ajánlott fizikai logika:

  1. Preflight.
  2. PSU OUTPUT OFF állapotban célfeszültség és áramlimit beállítása.
  3. DMM akkufeszültség ellenõrzése.
  4. PSU OUTPUT ON csak valid precheck után.
  5. CC szakasz: áramlimit szerint tölt, akkuoldali DMM feszültséget figyel.
  6. CV szakasz: DMM akkufeszültség alapján tartja a célfeszültséget.
  7. Diódaesés-kompenzáció csak lassan, clampelve, DMM feedback mellett.
  8. Taper figyelés: I_charge <= 0.03*C10 és U_batt közel célfeszültséghez.
  9. Taper hold idõ teljesülése.
 10. Töltés vége: PSU OFF vagy float mód, a workflow céljától függõen.
 11. Esemény és mérési eredmény logolása.

Töltés közbeni kötelezõ faultok:

[ ] DMM timeout.
[ ] Battery overvoltage.
[ ] PSU readback hiba.
[ ] PSU headroom-limit túllépés.
[ ] Series drop túl nagy.
[ ] Túláram.
[ ] Túlmelegedés.
[ ] No battery / szakadás.
[ ] Max charge time.
[ ] Max charge Ah.
[ ] Emergency stop.

Töltés vége csak akkor OK, ha:

[ ] DMM valid.
[ ] PSU readback valid.
[ ] U_batt célfeszültség közelében.
[ ] I_charge taper alatt.
[ ] Taper hold idõ teljesült.
[ ] Nem volt aktív fault.
[ ] Ah/Wh log konzisztens.

11.3 Helyes kisütési menet
--------------------------

Ajánlott fizikai logika:

  1. Preflight.
  2. PSU biztosan OFF.
  3. Load INPUT OFF állapotban CC mód és áram beállítása.
  4. DMM akkufeszültség validálása.
  5. Load INPUT ON csak valid precheck után.
  6. CC kisütés a kiválasztott C-ráta szerint.
  7. Folyamatos DMM akkufeszültség figyelés.
  8. Terminate voltage elérésekor LOAD OFF.
  9. Ah/Wh eredmény számítása valódi dt_s alapján.
 10. Relax vagy checkpoint következik.

Kisütési típusok:

[ ] C/20 kapacitásteszt: adatlap-közeli, lassabb kapacitásmérés.
[ ] C/10 kapacitásteszt: FIAMM C10 paraméterekhez illeszkedõ ellenõrzés.
[ ] C/5 learning kisütés: BQ Impedance Track learning fizikai elõkészítéshez.

Kisütés közbeni kötelezõ faultok:

[ ] DMM timeout.
[ ] Load readback hiba.
[ ] Load power limit.
[ ] Akkufeszültség irreálisan nagy/kicsi.
[ ] Terminate voltage alá esés.
[ ] Túlmelegedés.
[ ] Max discharge time.
[ ] Max discharge Ah.
[ ] Emergency stop.

Kisütés vége csak akkor OK, ha:

[ ] DMM valid.
[ ] LOAD readback valid.
[ ] Terminate voltage szabályosan elérve.
[ ] LOAD OFF igazolt.
[ ] Ah/Wh integrálás valid.
[ ] Nem volt kommunikációs vagy safety fault.

11.4 Relax / pihentetés
-----------------------

Töltés utáni relax:

[ ] BQ learning fizikai folyamatnál tipikus cél: legalább 2 h relax.
[ ] PSU OFF.
[ ] LOAD OFF.
[ ] Akku nyugalmi feszültség DMM-mel mérve.
[ ] OCV mérés csak relax után értelmezett.
[ ] dV/dt opcionálisan értékelhetõ.

Kisütés utáni relax:

[ ] BQ learning fizikai folyamatnál tipikus cél: legalább 5 h relax.
[ ] PSU OFF.
[ ] LOAD OFF.
[ ] DMM OCV mérés.
[ ] dV/dt opcionálisan értékelhetõ.
[ ] BQ állapot kézi ellenõrzése csak érvényes relax után.

Relax alatt tiltott:

[ ] PSU output ON.
[ ] Load input ON.
[ ] Automatikus töltés/kisütés újraindítás.
[ ] OCV-SOC érvényesként jelölése, ha relax megszakadt vagy DMM invalid.


12. BQ34Z110 / BQ34Z100 LEARNING ÉS GOLDEN IMAGE FIZIKAI ELÕKÉSZÍTÉS
====================================================================

Fontos projektmegkötés:

[ ] A program alapértelmezés szerint NEM kommunikál közvetlenül a BQ-val.
[ ] A program a fizikai laborfolyamatokat vezérli:
      - töltés,
      - kisütés,
      - relax,
      - kapacitásmérés,
      - OCV-SOC mérés,
      - C/5 learning ciklus fizikai elõkészítése.
[ ] A BQ státuszokat kézi vagy külsõ eszköz validálja.
[ ] A program nem állíthatja, hogy golden image-et validált, ha nem olvas BQ adatokat.
[ ] A reportban szerepelnie kell a BQ nélküli módszer korlátjának.

12.1 BQ-hoz szükséges akkuprofil és fizikai paraméterek
------------------------------------------------------

[ ] Cellaszám.
[ ] Névleges kapacitás.
[ ] C10 / C20 kapacitás.
[ ] C/20, C/10, C/5 áramok.
[ ] Töltési célfeszültség.
[ ] Terminate voltage.
[ ] Taper current.
[ ] Hõmérsékleti tartomány.
[ ] Maximális töltõáram.
[ ] Maximális kisütõáram.
[ ] AGM/VRLA kémia és FIAMM sorozat megnevezése.
[ ] ChemID kiválasztáshoz szükséges adatok külön dokumentálva.

12.2 BQ learning fizikai ciklus – ajánlott menet
------------------------------------------------

Egy tipikus BQ Impedance Track learning elõkészítõ fizikai workflow:

  1. Akkuprofil és config validálás.
  2. Teljes töltés CC/CV módszerrel AGM/VRLA paraméterek szerint.
  3. Töltés vége taper current alapján.
  4. PSU OFF.
  5. Töltés utáni relax, legalább 2 h.
  6. Kézi BQ checkpoint:
       - BQ kommunikáció külsõ eszközzel,
       - UpdateStatus ellenõrzés,
       - feszültség / áram / hõmérséklet ellenõrzés,
       - log mentés.
  7. C/5 kisütés terminate voltage-ig.
  8. LOAD OFF.
  9. Kisütés utáni relax, legalább 5 h.
 10. Kézi BQ checkpoint:
       - UpdateStatus,
       - Qmax,
       - Ra table,
       - learned status,
       - hibaflag-ek.
 11. Szükség esetén ciklus ismétlés.
 12. Golden image export csak külsõ BQ validáció után.
 13. Laborprogram report + BQ kézi jegyzõkönyv együtt archiválandó.

12.3 Mit tud bizonyítani a laborprogram BQ-kommunikáció nélkül?
--------------------------------------------------------------

A laborprogram bizonyíthatja:

[ ] A fizikai töltési profil lefutott.
[ ] A fizikai kisütési profil lefutott.
[ ] A relax idõ megtörtént.
[ ] Az akkukapocs feszültség DMM-mel mért volt.
[ ] A töltõ- és kisütõáram a program által ismert forrásból logolva volt.
[ ] Ah/Wh kapacitásmérés történt.
[ ] Terminate voltage elérése dokumentált.
[ ] Hiba esetén safe_all_off történt.
[ ] A BQ learninghez szükséges fizikai feltételek részben vagy egészben teljesültek.

A laborprogram NEM bizonyíthatja közvetlen BQ-kommunikáció nélkül:

[ ] UpdateStatus értékét.
[ ] Qmax frissülését.
[ ] Ra table frissülését.
[ ] Gauging státuszokat.
[ ] Golden image belsõ BQ állapotának helyességét.
[ ] ChemID tényleges megfelelõségét BQ oldalról.
[ ] DataFlash konfiguráció helyességét.
[ ] BQ safety flag-ek, permanent fail vagy hibakódok hiányát.

12.4 Kézi BQ checkpoint tartalma
--------------------------------

Minden kézi BQ checkpoint jegyzõkönyv tartalmazza:

[ ] Dátum/idõ.
[ ] Laborprogram session ID.
[ ] Akkutípus és sorozatszám, ha van.
[ ] BQ típus: BQ34Z110 vagy BQ34Z100.
[ ] Használt BQ eszköz / szoftver.
[ ] ChemID.
[ ] Design Capacity.
[ ] Cellaszám / konfiguráció.
[ ] UpdateStatus.
[ ] Qmax értékek.
[ ] Ra table állapot.
[ ] Voltage, Current, Temperature BQ szerint.
[ ] Flags / SafetyStatus / OperationStatus.
[ ] Learning ciklus fázisa.
[ ] Golden image export fájl neve és hash-e, ha elkészült.
[ ] Megjegyzés: PASS / FAIL / NEM IGAZOLHATÓ.


13. LOGGER / CHECKPOINT / RECOVERY
==================================

[ ] CSV flush mûködik.
[ ] SQLite commit stratégia megfelelõ.
[ ] checkpoint.json atomi írású.
[ ] checkpoint valid JSON marad megszakítás esetén is.
[ ] events.csv megkapja az eseményeket.
[ ] emergency/fault esemény logolódik.
[ ] logger.close() meghívódik minden kilépési úton.
[ ] session_meta tartalmazza:
      - test_type,
      - battery profile,
      - psu_mode,
      - resource stringek,
      - temperature compensation mode,
      - series diode,
      - measurement limitations,
      - BQ nélküli korlát.
[ ] local_config nem kerül riportba érzékeny módon.
[ ] Checkpoint/resume csak dokumentált célra használható.
[ ] Ha csak manual BQ checkpoint resume van, akkor ez szerepel a GUI-ban és README-ben.
[ ] Valódi power-loss recovery hiánya dokumentált, ha nincs implementálva.

Blokkoló:

[ ] Sérült checkpointból veszélyes kimenetkapcsolás indulhat.
[ ] Resume preflight nélkül bekapcsolhat PSU-t vagy LOAD-ot.
[ ] Logger hiba miatt eltûnik a fault oka.
[ ] Report nem tartalmazza a mérési korlátokat.


14. GUI / KEZELÕI HIBÁK / UX SAFETY
===================================

[ ] GUI nem engedi veszélyes kombináció kiválasztását.
[ ] 12 V/24 V pack választás egyértelmû.
[ ] Akku sorozat és konkrét típus látható.
[ ] C10/C20 kapacitás jelentése látható.
[ ] PSU mode látható.
[ ] DMM feedback állapota látható.
[ ] Emergency Stop mindig jól látható.
[ ] Start elõtt preflight checklist megjelenik.
[ ] Critical warning extra megerõsítést kér.
[ ] Hibánál a GUI nem csak „Failed”, hanem konkrét fault reason-t mutat.
[ ] Status bar nem ad félrevezetõ állapotot.
[ ] CheckpointPanel szövege egyértelmû:
      „BQ checkpoint — a program megállt a kézi BQ mûveletek elvégzéséhez.
       A folytatás gombra kattintva a laborfolyamat a következõ lépéssel folytatható.
       Ez nem valódi power-loss recovery.”
[ ] GUI bezárás futó mérés alatt safe stop / emergency stop kezelést kér.
[ ] Kezelõi stop és emergency stop jelentése nem keveredik.


15. TESZTTERV
=============

15.1 Unit tesztek
-----------------

[ ] BatteryProfile validáció.
[ ] C-ráta számítások: C/20, C/10, C/5, 0.25C10, 0.03C10.
[ ] Cellaszám és V/cell szorzás.
[ ] Float/boost hõkompenzáció.
[ ] Ah integrátor ismert I és dt mellett.
[ ] Wh integrátor ismert U/I/dt profillal.
[ ] NaN/Inf/None mérés invalid.
[ ] Config tartományhibák.
[ ] FaultCode és WarningCode konzisztencia.
[ ] Checkpoint írás/olvasás.
[ ] Report generálás mérési korlátokkal.

15.2 Állapotgép tesztek mock driverekkel
----------------------------------------

[ ] Charge: normál CC -> CV -> TAPER -> DONE.
[ ] Charge: DMM timeout CC alatt -> FAULT + safe_all_off.
[ ] Charge: PSU readback hiba CV alatt -> FAULT + safe_all_off.
[ ] Charge: battery overvoltage -> azonnali FAULT.
[ ] Charge: taper feltétel megszûnik -> taper timer reset.
[ ] Charge: max_charge_time -> FAULT.
[ ] Charge: max_charge_Ah -> FAULT.
[ ] Discharge: normál CC -> terminate voltage -> DONE.
[ ] Discharge: DMM timeout -> FAULT + load off.
[ ] Discharge: load readback hiba -> FAULT + load off.
[ ] Discharge: max_discharge_Ah -> FAULT.
[ ] Relax: PSU/LOAD off végig.
[ ] Relax: DMM timeout -> OCV invalid.
[ ] Emergency stop minden fõ állapotból.

15.3 Fault injection minimum
----------------------------

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
[ ] Logger írási hiba.
[ ] Sérült checkpoint.
[ ] PSU és LOAD egyszerre aktív állapot szimulált tiltása.

15.4 Hardveres bring-up tesztek
-------------------------------

[ ] Connection test OUTPUT ON és LOAD INPUT ON nélkül.
[ ] PSU output_off validálása.
[ ] LOAD input_off validálása.
[ ] DMM DCV mérés referencia feszültségen.
[ ] DMM PT100/FRTD mérés referencia hõmérsékleten.
[ ] PSU readback összevetés referencia DMM/áramméréssel.
[ ] LOAD readback összevetés referencia méréssel.
[ ] Kisáramú töltési próba mûterheléssel / dummy akkuval.
[ ] Kisáramú kisütési próba dummy terheléssel / ismert forrással.
[ ] Relé sorrend ellenõrzése.
[ ] BY550 diódaesés mérés.
[ ] Series/parallel/independent PSU mód visszaállítás normal állapotba.

15.5 Akkus fizikai tesztek
--------------------------

[ ] Egy rövid, felügyelt töltési próba kis árammal.
[ ] Egy teljes CC/CV töltés valós akkuval.
[ ] Töltés utáni relax log.
[ ] C/20 kapacitásteszt.
[ ] C/5 learning kisütési próba.
[ ] Kisütés utáni relax log.
[ ] Emergency stop valós futás közben, biztonságos körülmények között.
[ ] Fault utáni kimenet OFF visszaellenõrzés.
[ ] Report összevetés nyers CSV-vel.

15.6 BQ-specifikus teszt
------------------------

[ ] Teljes töltés -> 2 h relax -> kézi BQ checkpoint.
[ ] C/5 kisütés -> 5 h relax -> kézi BQ checkpoint.
[ ] UpdateStatus dokumentálása.
[ ] Qmax dokumentálása.
[ ] Ra table dokumentálása.
[ ] Golden image export csak BQ oldali validáció után.
[ ] Laborprogram report és BQ jegyzõkönyv együtt archiválva.


16. RELEASE / EXE / ÉLES HASZNÁLAT ELÕTTI CHECKLIST
===================================================

[ ] Teljes pytest fut célgépen.
[ ] GUI tesztek futnak PySide6-tal.
[ ] Nem-GUI core tesztek külön futnak.
[ ] Ruff/mypy vagy választott statikus ellenõrzés lefut.
[ ] Exe build elkészül.
[ ] Exe indítható tiszta gépen.
[ ] Config template mellékelve.
[ ] README tartalmazza az indítás, preflight, stop, emergency stop, checkpoint menetét.
[ ] Kezelõi dokumentáció tartalmazza a BQ nélküli korlátokat.
[ ] Mûszer resource beállítás dokumentált.
[ ] Elsõ éles mérés kisárammal és felügyelettel történik.
[ ] Aktuális release commit hash és build hash dokumentált.
[ ] Release ZIP nem tartalmaz secretet, lokális configot vagy felesleges mérési adatot.


17. REVIEW KIMENETI SABLON
==========================

# Review eredmény

## 1. Vezetõi összefoglaló

Döntés:
- ÉLESRE KÉSZ / FELTÉTELESEN KÉSZ / NEM KÉSZ

Top kockázatok:
1.
2.
3.
...

Review stop pontok:
- Nincs / van: ...

## 2. Kritikus hibák

Minden pontnál:

- Azonosító:
- Súlyosság: BLOCKER / CRITICAL / MAJOR / MINOR
- Érintett fájl / folyamat:
- Probléma:
- Miért veszélyes:
- Bizonyíték:
- Dokumentációs vagy mérési elv:
- Javasolt javítási irány:
- Szükséges teszt:
- Státusz:

## 3. Fontos hibák

Ugyanilyen formában.

## 4. Apróságok / karbantarthatóság

Csak olyan pont kerüljön ide, ami hosszabb távon csökkenti a hibakockázatot.

## 5. Konfigurációs megfelelõség

Paraméter | Talált érték | Elvárt logika | Forrás / indoklás | Státusz
----------|--------------|---------------|-------------------|--------

## 6. Laborfolyamat review

- Töltés:
- Kisütés:
- Relax:
- OCV-SOC:
- C/5 learning ciklus:
- DMM-kompenzáció:
- BQ kézi checkpoint:

## 7. Tesztelési javaslat

- Unit:
- Mock / fault injection:
- Hardveres bring-up:
- Teljes akkus mérés:
- BQ checkpoint:
- Release teszt:

## 8. Nyitott kérdések

- ...

## 9. Minimális javítások éles használat elõtt

1.
2.
3.


18. BLOKKOLÓ HIBÁK RÖVID LISTÁJA
================================

Az alábbiak bármelyike esetén a döntés nem lehet ÉLESRE KÉSZ:

[ ] PSU és LOAD egyszerre aktív lehet.
[ ] safe_all_off nem garantált.
[ ] Emergency stop nem minden állapotból mûködik.
[ ] DMM hiba ellenére DONE/PASS állapot jöhet létre.
[ ] Readback exception 0.0 értékként integrálódhat.
[ ] Battery overvoltage nem azonnali fault.
[ ] Terminate voltage kisütésnél nem kötelezõ.
[ ] CV/TAPER idõlimit nélkül fut.
[ ] max_charge_time vagy max_charge_Ah nem minden fázisban aktív.
[ ] max_discharge_Ah nem aktív kisütés alatt.
[ ] Hõkompenzáció GUI opció, de nem hat a célfeszültségre.
[ ] C20 kapacitásból számol töltõáramot C10 helyett.
[ ] Boost feszültség vagy áram túllépi a gyártói ajánlást.
[ ] Mélykisütött akkura kontroll nélküli nagyáramú töltést enged.
[ ] Ah/Wh névleges tick alapján számol.
[ ] Report nem közli a mérési forrásokat és korlátokat.
[ ] BQ golden image validálást állít BQ-kommunikáció nélkül.
[ ] Sérült checkpointból veszélyes resume indulhat.
[ ] Logger nem záródik fault esetén.
[ ] Teljes release teszt nem futott célkörnyezetben.


19. AJÁNLOTT PRIORITÁSI SORREND
===============================

1. Safety / hardvervédelem.
2. Emergency stop és safe_all_off.
3. DMM feedback lost és invalid mérés kezelése.
4. PSU/LOAD kölcsönös kizárása.
5. Töltés és kisütés végfeltételei.
6. Ah/Wh integrálás metrológiai helyessége.
7. FIAMM akkuparaméterek validálása.
8. BQ learning fizikai workflow és kézi checkpoint.
9. Logger / checkpoint / recovery.
10. GUI worker/thread safety.
11. Mûszerdriver valós SCPI viselkedés.
12. Config/repo/release higiénia.
13. Karbantarthatósági refaktor csak ezután.


20. ZÁRÓ MEGJEGYZÉS
===================

Ez a checklist akkor hasznos, ha nem egyszerre „kipipálásra”, hanem bizonyítékgyûjtésre
használod. Minden pont mellé érdemes odaírni:

  - OK,
  - NEM OK,
  - RÉSZLEGES,
  - NEM IGAZOLHATÓ,
  - NEM RELEVÁNS,
  - REVIEW STOP.

A program végsõ minõsítése csak akkor legyen pozitív, ha a safety, a mérési helyesség,
az akkuparaméterek és a BQ nélküli folyamatkorlátok egyszerre rendben vannak.