VÉGSÕ REVIEW SABLON — AKKUTESZTER PROJEKT
=========================================

Cél
---
Ez a sablon az Akkuteszter projekt végsõ, éles használat elõtti review-jához készült.
A review célja nem általános kódszépítés, hanem annak bizonyítása, hogy a program:

  1. nem tud veszélyes hardverállapotba kerülni,
  2. nem tud hamis DONE / PASS / CHARGE_DONE / DISCHARGE_DONE eredményt adni,
  3. metrológiailag értelmezhetõ Ah / Wh / U / I / T adatokat rögzít,
  4. a tervekben rögzített funkciókat ténylegesen implementálja,
  5. a terv_vs_implementacio_osszehasonlitas megállapításait feldolgozza,
  6. friss, aktuális verziós fizikai futásokkal is igazolt,
  7. csak ezután tekinthetõ véglegesnek / éles használatra késznek.

Fontos: régi session eredményeket nem szabad bizonyítékként használni a jelenlegi verzióra.
Régi log csak történeti referencia lehet. A végsõ döntéshez aktuális verzióval készült friss log kell.


Feltöltött projekt alapján releváns fõ elemek
---------------------------------------------

Projekt gyökér:
  Akkuteszter/

Program:
  Prog/src/
  Prog/drivers/
  Prog/gui/
  Prog/tools/
  Prog/tests/
  Prog/config/

Tervek:
  Folyamatok/tervek/BY550 char.txt
  Folyamatok/tervek/BY550.txt
  Folyamatok/tervek/akku_golden_image_labor_metodus_bq_nelkul.txt
  Folyamatok/tervek/dmm_alapu_dioda_kompenzalt_akkutoltes_szoftver_spec.txt
  Folyamatok/tervek/fiamm_agm_alapu_bq_parameter_frissites.txt
  Folyamatok/tervek/gui_tervezet_akku_laborhoz.txt
  Folyamatok/tervek/implementacios_terv_v1.txt
  Folyamatok/tervek/implementacios_terv_v1.2.txt
  Folyamatok/tervek/implementacios_terv_v1.2.1.txt
  Folyamatok/tervek/muszerevezerles_2220_2380_34465A_spec.txt
  Folyamatok/tervek/preflight_checklist.md

Review-k és összehasonlítások:
  Folyamatok/review-k/re1.txt
  Folyamatok/review-k/terv_vs_implementacio_osszehasonlitas.txt
  Folyamatok/külsõ-review-k/rev1.txt
  Folyamatok/külsõ-review-k/rev1.2.txt
  Folyamatok/külsõ-review-k/rev2.txt
  Folyamatok/külsõ-review-k/rev3.txt

Döntések:
  Folyamatok/döntések/d1.txt ... d12.txt

Jegyzõkönyvek:
  Folyamatok/jegyzõkönyvek/scpi_command_test_20260606_182745.csv
  Folyamatok/jegyzõkönyvek/scpi_command_test_20260606_183715.csv

Forrásdokumentáció:
  Leírások/TUDÁSBÁZIS.txt
  Leírások/akku_adatlapok/Engineering_Manual_AGM_EN.txt
  Leírások/akku_adatlapok/FG_FOLDER_EN.txt
  Leírások/akku_adatlapok/FGH-FGHL_FOLDER_EN.txt
  Leírások/akku_adatlapok/VRLA_Non-spillable_EN.txt

Claude review parancsok:
  .claude/commands/review.md
  .claude/commands/embedded-review.md
  .claude/commands/hwrev.MD
  .claude/commands/batrev.md
  .claude/commands/bq-review.md
  .claude/commands/kulso-review.md
  .claude/commands/kb-search.md
  .claude/commands/plan.md


Elõzetes futtatási megjegyzés a feltöltött csomag alapján
---------------------------------------------------------

A feltöltött csomagon Linux sandboxban lefuttatott gyors teszt eredménye:

  pytest -q Prog/tests

Eredmény:
  - GUI tesztek gyûjtése megszakadt, mert nincs telepítve PySide6.
  - Ez környezeti hiány, nem feltétlenül programhiba, de végsõ release elõtt
    Windows / cél PC környezetben PySide6-tal futtatni kell.

GUI nélküli, felsõ szintû tesztek:

  find Prog/tests -maxdepth 1 -name 'test_*.py' -print0 | xargs -0 pytest -q

Eredmény:
  - 417 passed
  - 2 failed
  - a 2 hiba oka: Prog.main importálásakor nincs PySide6
  - 7 pytest collection warning a Test* nevû osztályok miatt

Következtetés:
  - Jó jel, hogy a nem-GUI core tesztek nagy része lefut.
  - A végsõ review-ban kötelezõ a teljes tesztkészlet futtatása olyan gépen,
    ahol PySide6 és minden dependency telepítve van.
  - Amíg teljes tesztfutás nincs, a program nem minõsíthetõ véglegesnek.


Kötelezõ review-módszer
-----------------------

1. Ne csak a Prog/ kódot nézd.
2. Elõször olvasd el a releváns terveket és review-kat.
3. A terv_vs_implementacio_osszehasonlitas.txt megállapításait külön dolgozd fel.
4. Ellenõrizd, hogy a tervben „késznek” jelölt részek tényleg késznek tekinthetõk-e.
5. Ellenõrizd, hogy a tervben „hiányzó / részleges / nem igazolható” részek jelenleg milyen állapotban vannak.
6. A review végén adj egy egyértelmû döntést:

   - ÉLESRE KÉSZ
   - FELTÉTELESEN KÉSZ
   - NEM KÉSZ

7. Ha a döntés nem „ÉLESRE KÉSZ”, sorold fel a minimális javításokat.
8. Ne javasolj felesleges refaktort csak esztétika miatt.
9. Csak olyan változtatást kérj, ami javítja:

   - safety-t,
   - mérési helyességet,
   - hibaállapot-kezelést,
   - hamis DONE/PASS elkerülését,
   - recovery-t,
   - valós hardveres megbízhatóságot,
   - diagnosztikát,
   - release reprodukálhatóságát.


Kötelezõen feldolgozandó review parancsok
-----------------------------------------

A review során ezek tartalmát be kell építeni:

1. .claude/commands/hwrev.MD
   Fõ témák:
     - Safety / hardvervédelem
     - Metrológiai helyesség
     - Állapotgépek
     - TestRunner / idõzítés / workflow
     - Logger / checkpoint / recovery
     - Mûszerdriverek / SCPI
     - GUI / worker / thread safety
     - Config / repo higiénia
     - HW elõkészítõ eszközök
     - Tesztek
     - Review prioritás

2. .claude/commands/batrev.md
   Fõ témák:
     - FIAMM FG / FGH / FGHL AGM VRLA paraméterek
     - C10 / C20 kapacitás keverésének kizárása
     - cellaszám és pack feszültségek
     - float / boost / taper / hõkompenzáció
     - OCV-SOC korlátai
     - belsõ ellenállás / Rb értelmezése
     - ripple / töltõrendszer
     - storage / mélykisütés / biztonság

3. .claude/commands/review.md
   Általános kód review szabályok.

4. .claude/commands/embedded-review.md
   Embedded/laborautomatizálási kockázatok:
     - integer / timing / state machine / fail-safe logika
     - hardveres következmények

5. .claude/commands/kulso-review.md
   Külsõ review-k feldolgozása:
     - melyik pont jogos,
     - melyik nem releváns,
     - melyik már javítva,
     - melyikbõl lett új feladat.

6. .claude/commands/bq-review.md
   BQ34Z110 / golden image fizikai ciklusok:
     - charge / relax / discharge sorrend,
     - kézi checkpoint,
     - BQ nélküli elõkészítõ folyamat korlátai.

7. .claude/commands/kb-search.md
   Tudásbázis és forrásanyag keresése:
     - ne emlékezetbõl dönts adatlapértékekrõl,
     - ellenõrizd a Leírások/TUDÁSBÁZIS.txt és adatlap txt-k alapján.

8. .claude/commands/plan.md
   Ha blocker vagy high kockázatú eltérés van, javítás elõtt készíts tervet.


Terv vs implementáció kötelezõ feldolgozása
-------------------------------------------

Kiemelt bemenet:
  Folyamatok/review-k/terv_vs_implementacio_osszehasonlitas.txt

Ezt a fájlt nem szabad csak „háttéranyagként” kezelni.
Külön kell készíteni egy táblázatot:

  Tervpont | Megállapítás a régi összehasonlításban | Jelenlegi kód állapota | Döntés | Teendõ

Kötelezõ kérdések:

[ ] A régi összehasonlítás commit d2a647c-re hivatkozik. A mostani feltöltött verzióhoz képest friss-e?
[ ] Ami akkor KÉSZ volt, az most is mûködik?
[ ] Ami akkor RÉSZLEGES volt, az most teljes?
[ ] Ami akkor HIÁNY volt, az most implementált?
[ ] Ami akkor „nem igazolható” volt, az most igazolható kódból, tesztbõl vagy friss logból?
[ ] Minden tervpontnak van megfelelõ kódbeli helye?
[ ] Minden kódbeli új funkció visszavezethetõ tervre vagy döntésre?

Külön figyeld:

[ ] CC/CV töltés DMM-alapú diódaesés-kompenzációval.
[ ] CC kisütés terminate feszültségig.
[ ] Relaxáció timer és opcionális dV/dt alapján.
[ ] Ah/Wh integrálás readback alapján.
[ ] CSV + SQLite + checkpoint logolás.
[ ] Safety vészleállítás.
[ ] PSU mód kompatibilitás.
[ ] BY550 series drop és diode power kezelés.
[ ] GUI és worker thread modell.
[ ] connection_test hardverbiztonsága.
[ ] report generator mérési korlátok feltüntetése.


Kötelezõ tervmappa-összevetés
-----------------------------

A Folyamatok/tervek/ mappa minden releváns fájlját hasonlítsd össze a programmal.

1. implementacios_terv_v1.txt
2. implementacios_terv_v1.2.txt
3. implementacios_terv_v1.2.1.txt

Ellenõrizd:
[ ] Minden safety requirement implementálva van?
[ ] Minden állapotgép átmenet megfelel a tervnek?
[ ] A módosítások nem törtek-e el korábbi döntést?
[ ] A régi tervben szereplõ, de késõbb elvetett pontok dokumentáltan el lettek-e vetve?

2. dmm_alapu_dioda_kompenzalt_akkutoltes_szoftver_spec.txt

Ellenõrizd:
[ ] PSU sense a dióda elõtt / DMM az akkut méri logika helyesen dokumentált?
[ ] A szoftveres CV kompenzáció lassú, korlátozott és overvoltage-safe?
[ ] DMM hiba azonnali leállítás?
[ ] PSU set voltage clamp megfelelõ?
[ ] CV belépésnél nincs túllövésveszély?
[ ] max_step_up és max_step_down értékek indokoltak?

3. muszerevezerles_2220_2380_34465A_spec.txt

Ellenõrizd:
[ ] Keithley 2220-30-1 driver biztonságos connect után?
[ ] Keithley 2380 driver input_off biztos?
[ ] Keysight 34465A DMM DCV és FRTD konfiguráció megfelelõ?
[ ] SCPI timeout / error queue / invalid válasz kezelve?
[ ] SERIES / PARALLEL / INDEPENDENT mód parancsai valós jegyzõkönyvvel igazoltak?
[ ] Output ON és channel enable kettõs réteg nem keveredik?

4. akku_golden_image_labor_metodus_bq_nelkul.txt

Ellenõrizd:
[ ] BQ learning physical workflow helyes?
[ ] Kézi BQ checkpoint helyes helyen van?
[ ] A program nem állítja, hogy BQ-kommunikációt végez, ha nem végez?
[ ] A riportban szerepel a BQ nélküli módszer korlátja?

5. fiamm_agm_alapu_bq_parameter_frissites.txt

Ellenõrizd:
[ ] FIAMM FG/FGH/FGHL paraméterek helyesek?
[ ] FG20121 esetén C10 = 1.09 Ah, C20 = 1.2 Ah nem keveredik?
[ ] charge current = 0.25*C10?
[ ] taper = 0.03*C10?
[ ] float = 2.27 V/cell @ 20 °C?
[ ] boost = 2.40 V/cell @ 20 °C?
[ ] temp comp = -2.5 mV/cell/°C?

6. gui_tervezet_akku_laborhoz.txt

Ellenõrizd:
[ ] GUI nem hív közvetlen mûszerdrivert?
[ ] Worker thread kezeli a TestRunnert?
[ ] Emergency Stop mindig elérhetõ?
[ ] GUI bezárás futó mérés alatt safe stop?
[ ] CheckpointPanel helyesen jelzi a resume lehetõséget?
[ ] EventLogWidget megjeleníti kritikus eseményeket?

7. BY550.txt és BY550 char.txt

Ellenõrizd:
[ ] Dióda polaritás és bekötés megfelel?
[ ] Mért diódaesés alapján warning/fault határok reálisak?
[ ] 3 A körül teljesítmény és hûtés warning megfelelõ?
[ ] diode_power csak warning, ha ez volt a döntés?
[ ] series_drop fault tényleg fault?

8. preflight_checklist.md

Ellenõrizd:
[ ] A program és a config támogatja a preflight checklist minden pontját?
[ ] connection_test nem kapcsol OUTPUT ON-t és LOAD INPUT ON-t?
[ ] PARALLEL/SERIES módhoz hardware_wiring_confirmed kell?
[ ] Elsõ fizikai futtatás kisárammal indul?


Kódfájlok szerinti review
-------------------------

1. Safety / hardvervédelem

Fájlok:
  Prog/src/safety.py
  Prog/src/charge_controller.py
  Prog/src/discharge_controller.py
  Prog/src/instrument_manager.py
  Prog/drivers/device_psu.py
  Prog/drivers/device_load.py

Ellenõrizd:
[ ] safe_all_off sorrend: Load OFF -> PSU OFF.
[ ] safe_all_off idempotens.
[ ] emergency_stop minden controllerben safe állapotot eredményez.
[ ] PSU és Load egyszerre aktív állapot tiltott.
[ ] DMM feedback lost nem tud DONE-t okozni.
[ ] PSU current readback hiba nem válik 0.0 A érvényes értékké.
[ ] Load current readback hiba nem válik 0.0 A érvényes értékké.
[ ] max_charge_time minden töltési állapotban fut.
[ ] max_charge_Ah minden töltési állapotban fut.
[ ] max_discharge_Ah kisütés alatt fut.
[ ] battery overvoltage azonnali fault.
[ ] terminate voltage kisütésnél mûködik.
[ ] temperature DMM fault policy tényleg be van kötve.
[ ] BY550 series_drop fault mûködik.
[ ] diode_power warning/fault döntés konzisztens a tervvel.

Megjegyzés a feltöltött kód gyors olvasása alapján:
[ ] safety.py-ban check_diode_power a diode_power_fault_W felett WarningCode.DIODE_POWER_TOO_HIGH értéket ad vissza, nem FaultCode-ot. Ezt össze kell vetni a döntéssel: ha szándékosan warning, dokumentálni kell; ha faultnak kellene lennie, javítandó.
[ ] charge_controller.py és discharge_controller.py readback exception után emergency_stop történik, majd 0.0-t ad vissza. A hívók jelenleg ellenõrzik a FAULT állapotot, de ezt külön teszttel bizonyítani kell, hogy a 0.0 soha nem integrálódik vagy nem okoz hamis taper/DONE állapotot.
[ ] DischargeController FAULT reasonként MAX_DISCHARGE_TIME_REACHED / MAX_DISCHARGE_AH_REACHED szerepelhet, de FaultCode enum-ban ezek nincsenek külön definiálva. Ez nem feltétlen hiba, de a fault flag / report / event egységességet ellenõrizni kell.

2. Metrológiai helyesség

Fájlok:
  Prog/src/integrator.py
  Prog/src/test_runner.py
  Prog/src/charge_controller.py
  Prog/src/discharge_controller.py
  Prog/src/relax_controller.py
  Prog/src/report_generator.py
  Prog/drivers/device_dmm.py

Ellenõrizd:
[ ] Ah/Wh integrátor actual dt_s alapján számol.
[ ] dt_s perf_counter alapú, nem fix tick.
[ ] sample tartalmazza a dt_s / elapsed_s mezõket, vagy legalább visszaellenõrizhetõ az idõalap.
[ ] Töltõáram forrása: PSU_READBACK.
[ ] Kisütõáram forrása: LOAD_READBACK.
[ ] Ha nincs külsõ kalibrált shunt, ez szerepel a riportban.
[ ] DMM voltage tényleg akkukapcson mér.
[ ] DMM DCV konfiguráció megfelelõ.
[ ] DMM PT100/FRTD konfiguráció megfelelõ.
[ ] NPLC értékek megfelelnek a mérési célnak.
[ ] OCV mérésnél szerepel, hogy nincs galvanikus leválasztás, ha ez igaz.
[ ] Rb mérésnél külön szerepel: DC pulse R / dynamic impedance / scope transient.
[ ] Rb számítás nem keveri a kábel, relé, Faston, panel ellenállását az akkuval kalibráció nélkül.

3. BatteryProfile és akkuparaméterek

Fájlok:
  Prog/src/battery_profile.py
  Prog/config/default_config.yaml
  Prog/config/local_config.template.yaml
  Leírások/akku_adatlapok/*.txt

Ellenõrizd:
[ ] nominal_capacity_Ah jelentése explicit C10?
[ ] capacity_C10_Ah / C20 külön mezõ hiánya okozhat-e félreértést?
[ ] charge_voltage_per_cell_V default 2.40 V/cell helyes-e az adott tesztmódhoz?
[ ] float_voltage_per_cell_V default 2.27 V/cell.
[ ] terminate_voltage_per_cell_V default 1.80 V/cell.
[ ] batt_absolute_max_V_per_cell = 2.425 megfelel-e a safety döntésnek.
[ ] compensated_charge_voltage_V clamp [2.30, 2.425] V/cell megfelel-e FIAMM és safety szempontból.
[ ] temp_comp_mV_per_cell_per_degC = -2.5.
[ ] effective_max_charge_A = 0.25 * nominal_capacity_Ah.
[ ] effective_taper_A = 0.03 * nominal_capacity_Ah.
[ ] FG20121 / FG20721 / FG21201 profilok konkrétan validálva vannak-e.

4. ChargeController állapotgép

Fájl:
  Prog/src/charge_controller.py

Ellenõrizd:
[ ] INIT -> PRECHECK -> PSU_PRESET -> CHARGE_CC -> CHARGE_CV_DMM_CONTROL -> TAPER_HOLD -> CHARGE_DONE.
[ ] Minden fault safe OFF.
[ ] PRECHECK DMM valid nélkül nem enged tovább.
[ ] PRECHECK mélykisült akkut tilt, ha recovery nincs implementálva.
[ ] PSU_PRESET elõtt load OFF garantált.
[ ] CC->CV belépésnél nem marad túl magas PSU set voltage veszélyes ideig.
[ ] _regulate_cv clampeli a PSU set voltage-et.
[ ] max_step_up és max_step_down dokumentált és biztonságos.
[ ] TAPER feltétel: DMM valid + U target közel + I <= taper.
[ ] TAPER timer reset jó.
[ ] TAPER_HOLD alatt felfelé szabályozás tiltása helyes.
[ ] CHARGE_DONE elõtt PSU output_off tényleg megtörténik.
[ ] max_charge_time és max_charge_Ah CV/TAPER alatt is fut.
[ ] PSU readback hiba nem okoz hamis TAPER-t.

5. DischargeController állapotgép

Fájl:
  Prog/src/discharge_controller.py

Ellenõrizd:
[ ] Load bekapcsolás elõtt PSU all_outputs_off.
[ ] CC setup elõtt DMM valid.
[ ] terminate_voltage_pack_V elérésekor load input_off.
[ ] DMM hiba fault.
[ ] load current readback hiba fault.
[ ] max_discharge_time fut.
[ ] max_discharge_Ah fut.
[ ] Rb 1s/10s/30s számítás érvényes és dokumentált.
[ ] Rb számítás áramforrása set_current vagy readback? Ha set_current, szerepeljen korlátként.
[ ] DISCHARGE_DONE nem keletkezhet érvénytelen DMM mérésbõl.

6. RelaxController és OCV-SOC

Fájlok:
  Prog/src/relax_controller.py
  Prog/src/ocv_soc_controller.py

Ellenõrizd:
[ ] Relax alatt PSU OFF és Load OFF.
[ ] DMM hiba kezelése megfelelõ.
[ ] dV/dt csak valid baseline után aktív.
[ ] OCV-SOC nem állít pontos SOC-t nem pihent akkunál.
[ ] OCV-SOC riportban szerepel a módszer korlátja.
[ ] OCV-SOC mérés alatt nincs rejtett terhelés/töltés.

7. TestRunner / workflow / idõzítés

Fájl:
  Prog/src/test_runner.py

Ellenõrizd:
[ ] start_step_index validáció van-e. Negatív vagy túl nagy index ne okozzon félrevezetõ futást.
[ ] emergency_stop minden tickben ellenõrzött.
[ ] graceful stop csak safe pontokon hat.
[ ] MANUAL_CHECKPOINT eventet küld.
[ ] CHECKPOINT_STOPPED kezelés korrekt.
[ ] logger.close minden lezárási ágon megtörténik.
[ ] controller reset történik új step elõtt, ahol szükséges.
[ ] TestRunner session-once vagy reusable? Dokumentált?
[ ] actual_dt_s elsõ tick közel 0; integráció ekkor tényleg nem fut veszélyesen.
[ ] device_error_poll_interval_s ésszerû. 2 s tick mellett a 60 s poll túl ritka vagy megfelelõ? Dönteni kell.
[ ] device error poll fault esetén logolódik-e.

8. Logger / checkpoint / recovery

Fájl:
  Prog/src/logger.py

Ellenõrizd:
[ ] CSV flush mûködik.
[ ] SQLite commit intervallumos, nem túl gyakori.
[ ] checkpoint írás atomi: temp + os.replace.
[ ] checkpoint valid JSON marad megszakítás után.
[ ] critical event azonnal flush/commit.
[ ] logger.close idempotens.
[ ] session_meta van-e. Ha nincs vagy hiányos, release blocker lehet.
[ ] session_meta tartalmazza-e:
     - program verzió / commit / build idõ,
     - test_type,
     - battery profile,
     - psu_mode,
     - mûszer IDN/resource,
     - temp compensation mode,
     - series diode,
     - measurement limitations,
     - current source: PSU/LOAD readback, not calibrated shunt.
[ ] local_config érzékeny adatai nem kerülnek riportba.

9. Report generator

Fájl:
  Prog/src/report_generator.py

Ellenõrizd:
[ ] A riport nem állít többet, mint amit a mérési lánc igazol.
[ ] Jelzi, hogy az áram PSU/Load readbackbõl származik.
[ ] Jelzi, ha integration_valid False vagy DEGRADED.
[ ] Jelzi, ha hõmérséklet mérés kiesett.
[ ] Jelzi, ha nincs külsõ kalibrált shunt.
[ ] Jelzi, ha régi / megszakított / nem teljes mérés.
[ ] PASS/FAIL/DONE fogalmak egyértelmûek.
[ ] BQ nélküli golden image elõkészítés korlátai szerepelnek.

10. InstrumentManager és driverek

Fájlok:
  Prog/src/instrument_manager.py
  Prog/drivers/device_psu.py
  Prog/drivers/device_load.py
  Prog/drivers/device_dmm.py

Ellenõrizd:
[ ] connect után minden mûszer safe állapotban van.
[ ] all_outputs_off valóban minden PSU kimenetet kikapcsol.
[ ] output_on csak controlleren keresztül történhet.
[ ] load input_on csak controlleren keresztül történhet.
[ ] PSU SERIES/PARALLEL/INDEPENDENT parancsok valós mûszeren tesztelve vannak.
[ ] A csatorna enable és output on kettõs réteg helyesen kezelt.
[ ] Timeout, invalid válasz, üres válasz, NaN/Inf kezelve.
[ ] DMM overload felismerése implementált.
[ ] device error queue lekérdezés nem okoz mellékhatást.
[ ] connection_test nem hagyja SERIES/PARALLEL módban a mûszert, ha tesztelte.

11. GUI / worker / thread safety

Fájlok:
  Prog/gui/main_window.py
  Prog/gui/worker.py
  Prog/gui/panels/* ha van

Ellenõrizd:
[ ] GUI nem hív közvetlen drivert.
[ ] TestRunner nem importál PySide6-ot.
[ ] Worker thread kezeli a futást.
[ ] GUI widget csak GUI threadbõl frissül.
[ ] Emergency Stop gomb mindig aktív futás közben.
[ ] Start/Stop többszöri használata után nincs duplán kötött signal.
[ ] QThread cleanup: quit/wait/deleteLater.
[ ] GUI bezárás futás közben safe stopot kér.
[ ] CHECKPOINT_STOPPED és resume_possible korrektül látható.
[ ] EventLogWidget kritikus eseményeket mutat.
[ ] Status bar nem ad félrevezetõ állapotot.

12. Config / repo higiénia / build

Fájlok:
  .gitignore
  requirements.txt
  pytest.ini
  akkuteszter.spec
  INSTALL.md
  Prog/config/default_config.yaml
  Prog/config/local_config.template.yaml
  Prog/config/local_config.yaml

Ellenõrizd:
[ ] local_config.yaml nincs git-követve.
[ ] local_config.template.yaml van.
[ ] default_config.yaml és template nem mondanak egymásnak ellent.
[ ] requirements.txt tartalmazza: PySide6, pyqtgraph, pyvisa, PyYAML, pytest.
[ ] PyInstaller spec mûködik.
[ ] Exe indul tiszta Windows gépen.
[ ] Config hiány esetén érthetõ hibaüzenet.
[ ] VISA hiány esetén érthetõ hibaüzenet.
[ ] Log mappa létrejön.
[ ] Program Files alatti írási jog probléma kezelve.
[ ] Verziószám / build azonosító látható.

13. Tools / connection_test

Fájlok:
  Prog/tools/connection_test.py
  Prog/tools/command_test.py

Ellenõrizd:
[ ] connection_test list_resources mûködik.
[ ] PLACEHOLDER resource skip.
[ ] IDN lekérés minden mûszeren.
[ ] safe_off csak OFF irányú.
[ ] Nem kapcsol OUTPUT ON-t.
[ ] Nem kapcsol LOAD INPUT ON-t.
[ ] DMM alap mérés mûködik.
[ ] Summary PASS/SKIP/FAIL.
[ ] Exit code helyes.
[ ] --config opció hasznos.
[ ] pyvisa hiányra érthetõ üzenet.
[ ] command_test után a PSU normál / independent módba visszaáll, ha módteszt volt.

14. Tesztek

Fájlok:
  Prog/tests/

Kötelezõ tesztcsoportok:
[ ] SafetyManager unit tesztek.
[ ] ChargeController fault tesztek.
[ ] DischargeController fault tesztek.
[ ] DMM lost tesztek.
[ ] PSU readback exception tesztek.
[ ] Load readback exception tesztek.
[ ] series_drop fault teszt.
[ ] diode_power warning/fault döntés teszt.
[ ] max_charge_Ah CV/TAPER alatt.
[ ] max_charge_time CV/TAPER alatt.
[ ] max_discharge_Ah.
[ ] integrátor actual dt_s teszt.
[ ] checkpoint atomic write teszt.
[ ] TestRunner checkpoint/resume teszt.
[ ] Worker signal teszt.
[ ] connection_test mock teszt.
[ ] GUI tesztek PySide6-tal célkörnyezetben.

Kötelezõ futtatás release elõtt:

  cd Akkuteszter
  python -m pytest -q Prog/tests

Elvárt:
  0 failed
  0 error

Ha GUI dependency hiányzik, az nem végleges release környezet.


Friss fizikai validáció kötelezõ
--------------------------------

A program csak kód review alapján nem tekinthetõ véglegesnek.
Kell legalább:

[ ] connection_test aktuális PC-n PASS.
[ ] Mûszer IDN-ek rögzítve.
[ ] Preflight checklist kitöltve.
[ ] Elsõ futás kisárammal PASS.
[ ] Legalább egy teljes aktuális verziós charge -> relax -> discharge -> relax ciklus PASS.
[ ] Legalább egy emergency stop teszt töltés közben PASS.
[ ] Legalább egy emergency stop teszt kisütés közben PASS.
[ ] Legalább egy DMM disconnect / timeout fault injection PASS.
[ ] Legalább egy PSU communication fault injection PASS, ha biztonságosan szimulálható.
[ ] Legalább egy Load communication fault injection PASS, ha biztonságosan szimulálható.
[ ] Checkpoint / recovery teszt PASS.
[ ] Report generálás PASS.

Aktuális logcsomag elvárt tartalma:

  session_meta.json
  samples.csv
  events.csv
  device_errors.csv
  checkpoint.json
  session.db
  ocv_soc_table.csv, ha OCV-SOC futott
  generált riport


Fault injection mátrix
----------------------

Kötelezõ végignézni:

1. DMM voltage timeout töltés közben
   Elvárt: DMM_FEEDBACK_LOST, Load OFF, PSU OFF, FAULT, event log.

2. DMM temperature timeout ENABLED módban
   Elvárt: TEMPERATURE_MONITOR_LOST_CRITICAL, safe all off.

3. DMM temperature timeout MONITOR_ONLY módban
   Elvárt: warning timeoutig, utána fault a beállított policy szerint.

4. PSU measure current exception
   Elvárt: PSU_COMM_LOST, safe all off, nincs hamis taper, nincs 0.0 A integráció.

5. Load measure current exception
   Elvárt: LOAD_COMM_LOST, safe all off, nincs 0.0 A integráció.

6. Battery overvoltage
   Elvárt: BATTERY_OVERVOLTAGE, safe all off.

7. Battery undervoltage / terminate voltage
   Elvárt: kisütésnél DISCHARGE_DONE csak valid DMM mérésbõl.

8. Deeply discharged precheck
   Elvárt: DEEPLY_DISCHARGED_RECOVERY_NOT_IMPLEMENTED, nincs töltésindítás.

9. PSU+Load egyszerre commanded on
   Elvárt: CONCURRENT_PSU_LOAD_ON fault.

10. Checkpoint írás megszakítása
    Elvárt: valid régi vagy új checkpoint, korrupt JSON nem maradhat.

11. GUI bezárás futás közben
    Elvárt: safe stop / emergency stop, nincs futó mûszerállapot.

12. PC sleep / program crash szimuláció
    Elvárt: mûszerek fail-safe állapota és helyreállítható log / checkpoint, amennyire lehetséges.


Release döntési mátrix
----------------------

ÉLESRE KÉSZ csak akkor, ha:

[ ] Nincs safety blocker.
[ ] Nincs olyan ismert út, ahol PSU és Load egyszerre aktív maradhat.
[ ] Nincs olyan ismert út, ahol DMM hiba hamis DONE-t okozhat.
[ ] Nincs olyan ismert út, ahol PSU/Load readback hiba 0.0 A érvényes mérésként szerepel.
[ ] Nincs hamis CHARGE_DONE / DISCHARGE_DONE / PASS lehetõség.
[ ] Minden töltési/kisütési fault safe all off-ot eredményez.
[ ] Ah/Wh integráció actual dt_s alapján történik.
[ ] A riport jelzi a mérési forrásokat és korlátokat.
[ ] A FIAMM FG paraméterek C10 alapon validáltak.
[ ] Teljes tesztkészlet PASS célkörnyezetben.
[ ] Friss valós futási log PASS.
[ ] Emergency stop friss fizikai teszt PASS.
[ ] connection_test friss fizikai teszt PASS.
[ ] Exe / build validált azon a PC-n, ahol használni fogod.

FELTÉTELESEN KÉSZ, ha:

[ ] Nincs safety blocker,
[ ] de van dokumentált medium/low hiány,
[ ] és ezek nem veszélyeztetik a hardvert vagy mérési eredményt,
[ ] és a kezelõi dokumentációban szerepelnek korlátozásként.

NEM KÉSZ, ha bármelyik igaz:

[ ] Safety blocker van.
[ ] DMM/PSU/Load hiba hamis DONE/PASS állapotot okozhat.
[ ] PSU vagy Load rossz állapotban bekapcsolhat.
[ ] Nincs friss aktuális verziós fizikai futás.
[ ] Nincs teljes tesztfutás célkörnyezetben.
[ ] A terv_vs_implementacio_osszehasonlitas nyitott kritikus pontjai nincsenek lezárva.
[ ] A riport félrevezetõen pontosnak mutat nem kalibrált adatot.


Hibabesorolás
-------------

BLOCKER:
  - Hardver veszélyeztetés.
  - PSU/Load egyszerre aktív lehet.
  - Emergency stop nem biztos.
  - DMM hiba hamis DONE/PASS állapotot okozhat.
  - Akku overvoltage nem állítja le a töltést.
  - Readback exception 0.0 A-ként integrálódhat.
  - Nincs friss fizikai validáció.

HIGH:
  - Mérési eredmény jelentõsen torzulhat.
  - Checkpoint/recovery megbízhatatlan hosszú mérésnél.
  - Report nem jelzi a mérési korlátokat.
  - GUI thread safety bizonytalan.
  - SERIES/PARALLEL mûszerparancs nincs valósan igazolva.

MEDIUM:
  - Diagnosztika hiányos.
  - Warning nem elég informatív.
  - Config validáció hiányos.
  - Tesztfedés nem teljes, de safety-t nem érint közvetlenül.

LOW:
  - Esztétikai, dokumentációs, kisebb karbantarthatósági észrevétel.


Végsõ review kimenet elvárt formája
-----------------------------------

A review végén ilyen struktúrában adj választ:

1. Összefoglaló döntés

   Döntés: ÉLESRE KÉSZ / FELTÉTELESEN KÉSZ / NEM KÉSZ
   Rövid indoklás: ...

2. Blocker hibák

   ID | Terület | Fájl | Hiba | Kockázat | Javasolt javítás | Release feltétel

3. High hibák

   ID | Terület | Fájl | Hiba | Kockázat | Javasolt javítás

4. Medium / Low észrevételek

   ID | Terület | Fájl | Észrevétel | Javaslat

5. Terv vs implementáció eredmény

   Tervfájl | Funkció | Implementálva? | Tesztelve? | Megjegyzés

6. terv_vs_implementacio_osszehasonlitas feldolgozás

   Régi megállapítás | Jelenlegi állapot | Döntés | Teendõ

7. FIAMM FG/AGM paraméter review

   Paraméter | Elvárt | Programban | Döntés

8. Safety állapotgépek eredménye

   Charge | Discharge | Relax | OCV-SOC | TestRunner

9. Metrológiai eredmény

   Ah/Wh | U/I/T | Rb | Report | Korlátok

10. Tesztfedettség

   Unit | GUI | Fault injection | HIL | Friss session log

11. Minimális teendõk a véglegesítéshez

   Csak a release-hez szükséges pontokat sorold fel.


Külsõ review / Claude Code prompt
---------------------------------

Másold be Claude Code-ba vagy külsõ review-ba:

"""
Végezz végsõ release review-t az Akkuteszter projekten.

Cél: eldönteni, hogy a program éles használatra kész-e.
Ne általános kódszépséget nézz, hanem safety, metrológia, állapotgépek,
SCPI mûszervezérlés, logger/checkpoint/recovery, GUI/thread safety,
config/build és tesztfedettség szempontjából vizsgáld.

Kötelezõen olvasd el és dolgozd fel:

- CLAUDE.md
- README_PROJEKT_STRUKTURA.md
- .claude/commands/review.md
- .claude/commands/embedded-review.md
- .claude/commands/hwrev.MD
- .claude/commands/batrev.md
- .claude/commands/bq-review.md
- .claude/commands/kulso-review.md
- .claude/commands/kb-search.md
- Folyamatok/review-k/terv_vs_implementacio_osszehasonlitas.txt
- Folyamatok/review-k/re1.txt
- Folyamatok/külsõ-review-k/rev1.txt
- Folyamatok/külsõ-review-k/rev1.2.txt
- Folyamatok/külsõ-review-k/rev2.txt
- Folyamatok/külsõ-review-k/rev3.txt
- Folyamatok/tervek/*.txt
- Folyamatok/tervek/preflight_checklist.md
- Folyamatok/döntések/*.txt
- Folyamatok/jegyzõkönyvek/*.csv
- Leírások/TUDÁSBÁZIS.txt
- Leírások/akku_adatlapok/*.txt

Ezután vesd össze a terveket a meglévõ programmal:

- Prog/src/safety.py
- Prog/src/battery_profile.py
- Prog/src/charge_controller.py
- Prog/src/discharge_controller.py
- Prog/src/relax_controller.py
- Prog/src/ocv_soc_controller.py
- Prog/src/test_runner.py
- Prog/src/instrument_manager.py
- Prog/src/integrator.py
- Prog/src/logger.py
- Prog/src/report_generator.py
- Prog/drivers/device_psu.py
- Prog/drivers/device_load.py
- Prog/drivers/device_dmm.py
- Prog/gui/main_window.py
- Prog/gui/worker.py
- Prog/tools/connection_test.py
- Prog/tools/command_test.py
- Prog/config/default_config.yaml
- Prog/config/local_config.template.yaml
- Prog/tests/

Külön dolgozd fel a terv_vs_implementacio_osszehasonlitas.txt minden pontját.
Ne fogadd el automatikusan, hogy ami ott késznek van jelölve, az most is kész.
A jelenlegi kódban ellenõrizd újra.

Külön nézd:

1. Safety / hardvervédelem
2. Metrológiai helyesség
3. Charge/Discharge/Relax/OCV állapotgépek
4. TestRunner / idõzítés / workflow
5. Logger / checkpoint / recovery
6. SCPI driverek és valós mûszerbiztonság
7. GUI / worker thread safety
8. Config / repo / build / exe használhatóság
9. connection_test és preflight megfelelés
10. FIAMM FG/AGM paramétervalidáció
11. Tesztek és fault injection lefedettség
12. Friss fizikai futási logok megléte

A review végén adj egyértelmû döntést:

- ÉLESRE KÉSZ
- FELTÉTELESEN KÉSZ
- NEM KÉSZ

Blockernek minõsíts minden olyan hibát, ahol:

- PSU vagy Load rosszkor bekapcsolhat,
- PSU és Load egyszerre aktív lehet,
- emergency stop nem garantált,
- DMM hiba hamis DONE/PASS állapotot okozhat,
- readback hiba 0.0 A-ként kezelõdhet,
- battery overvoltage nem állítja le a töltést,
- metrológiai eredmény félrevezetõ,
- nincs friss aktuális verziós fizikai validáció.

Ne írj új kódot.
Csak hibákat, kockázatokat, bizonyítékokat és javítási javaslatokat adj.
A kimenet legyen strukturált release review jelentés.
"""


Rövid végsõ ellenõrzõlista
--------------------------

[ ] Teljes forrásanyag feldolgozva.
[ ] terv_vs_implementacio_osszehasonlitas feldolgozva.
[ ] Minden tervfájl összevetve a programmal.
[ ] Minden review parancs tartalma beépítve.
[ ] Safety review kész.
[ ] Metrológiai review kész.
[ ] Állapotgép review kész.
[ ] SCPI driver review kész.
[ ] GUI/thread review kész.
[ ] Logger/checkpoint/recovery review kész.
[ ] Config/build/exe review kész.
[ ] FIAMM FG paraméter review kész.
[ ] Teljes pytest PASS célkörnyezetben.
[ ] Friss connection_test PASS.
[ ] Friss teljes fizikai ciklus PASS.
[ ] Friss emergency/fault injection PASS.
[ ] Nincs blocker.
[ ] Végsõ döntés dokumentálva.


Végsõ alapelv
-------------

A program akkor tekinthetõ véglegesnek, ha nem csak a kód látszik jónak,
hanem a tervekhez képest igazoltan teljes, a hibák biztonságos leállást okoznak,
a mérési eredmények nem félrevezetõk, és a jelenlegi verzió friss valós hardveres
futással bizonyított.