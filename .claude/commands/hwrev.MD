AJÁNLOTT REVIEW TERÜLETEK
=========================

1. Safety / hardvervédelem
--------------------------

Ez a legfontosabb.

Ellenőrizni kell:

  - PSU OUTPUT soha ne kapcsoljon be rossz állapotban
  - Load INPUT soha ne legyen aktív töltés közben
  - PSU és Load ne lehessen egyszerre aktív
  - emergency_stop mindig biztosan safe_all_off-ot hívjon
  - safe_all_off sorrend:
      Load OFF
      PSU OFF
  - safe_all_off idempotens legyen
  - DMM feedback lost esetén töltés/kisütés leálljon
  - PSU current readback hiba ne legyen 0.0A
  - Load current readback hiba ne legyen 0.0A
  - max_charge_time minden töltési fázisban fusson
  - max_charge_Ah minden töltési fázisban fusson
  - max_discharge_Ah fusson kisütés alatt
  - battery overvoltage azonnali fault
  - terminate voltage működjön kisütésnél
  - PSU mód kompatibilitás:
      24V pack csak SERIES
      12V pack SERIES csak warning / extra megerősítés
  - BY550 series_drop fault működjön
  - diode_power csak warning, ha így döntöttünk
  - temperature DMM fault policy tényleg be legyen kötve

Különösen nézendő fájlok:

  src/safety.py
  src/charge_controller.py
  src/discharge_controller.py
  src/instrument_manager.py
  drivers/device_psu.py
  drivers/device_load.py


2. Metrológiai helyesség
------------------------

Itt azt kell nézni, hogy a mérés tényleg azt méri-e, amit állít.

Ellenőrizni kell:

  - Ah/Wh integrátor valódi dt_s alapján számol-e
  - dt_s nem névleges tick, hanem perf_counter alapú-e
  - sample-ben logolható-e a dt_s / elapsed_s
  - charge current forrása dokumentált-e:
      PSU_READBACK
  - discharge current forrása dokumentált-e:
      LOAD_READBACK
  - nincs külső kalibrált shunt → riportban szerepel-e
  - DMM voltage tényleg akkukapcson mér-e
  - DMM voltage konfigurálva van-e DCV módra
  - DMM temperature konfigurálva van-e PT100/FRTD módra
  - NPLC értékek ésszerűek-e
  - OCV mérésnél jelölve van-e:
      nincs galvanikus leválasztás
  - hőkompenzáció:
      ha ENABLED választható, tényleg módosítja-e a célfeszültséget
      ha nincs implementálva, ne legyen választható
  - Rb / belső ellenállás mérésnél külön van-e választva:
      DC pulse R
      dynamic impedance
      scope transient

Különösen nézendő fájlok:

  src/integrator.py
  src/test_runner.py
  src/charge_controller.py
  src/discharge_controller.py
  src/relax_controller.py
  src/report_generator.py
  drivers/device_dmm.py


3. Állapotgépek
---------------

A charge / discharge / relax állapotgépeket nagyon alaposan érdemes nézni.

ChargeController:

  - INIT
  - PRECHECK
  - PSU_PRESET
  - CC
  - CV
  - TAPER_HOLD
  - DONE
  - FAULT

Ellenőrizni kell:

  - minden állapotból van biztonságos kilépés
  - nincs végtelen CV állapot
  - TAPER feltétel helyes:
      DMM valid
      U_batt >= target - tolerance
      I_charge <= taper
  - TAPER timer reset feltételek jók
  - minden fault tényleg safe_off-ot eredményez
  - DMM hiba nem tud hamis DONE-t okozni
  - PSU readback hiba nem tud hamis TAPER-t okozni

DischargeController:

  - Load bekapcsolás előtt PSU OFF
  - terminate voltage működik
  - DMM hiba fault
  - load current readback hiba fault
  - max discharge Ah limit
  - load safe_off DONE/FAULT esetén

RelaxController:

  - PSU OFF
  - Load OFF
  - DMM hiba kezelése
  - dV/dt csak valid baseline után legyen használva


4. TestRunner / időzítés / workflow
-----------------------------------

Ez a központi orchestrator, ezért kritikus.

Ellenőrizni kell:

  - run() blokkoló, de GUI-tól független
  - start_step_index validáció
  - CHECKPOINT_STOPPED helyes kezelése
  - MANUAL_CHECKPOINT eventet küld-e
  - step_changed események jó indexet adnak-e
  - actual dt_s korrekt-e
  - sleep csak a maradék időre fut-e
  - emergency_stop minden tickben ellenőrzött-e
  - graceful stop csak megfelelő pontokon hat-e
  - logger.close() megtörténik-e
  - ugyanaz a TestRunner újrahasználható-e vagy session-once objektum
  - ezt dokumentálták-e

Különösen nézendő:

  src/test_runner.py


5. Logger / checkpoint / recovery
---------------------------------

Hosszú méréseknél ez nagyon fontos.

Ellenőrizni kell:

  - CSV flush működik
  - SQLite commit nem minden mintánál történik feleslegesen
  - checkpoint.json írás atomi-e:
      temp file + os.replace
  - checkpoint valid JSON marad-e
  - events.csv megkapja-e az eseményeket
  - emergency / fault esemény logolódik-e
  - logger.close() meghívódik-e
  - session_meta tartalmazza-e:
      test_type
      battery profile
      psu_mode
      resource stringek
      temperature compensation mode
      series diode
      measurement limitations
  - local_config ne kerüljön riportba érzékeny módon, csak ami kell

Különösen nézendő:

  src/logger.py
  src/report_generator.py


6. Műszerdriverek / SCPI
------------------------

Itt azt kell nézni, hogy a driver nem csak mockkal jó-e, hanem valós műszeren is biztonságos.

PSU driver:

  - connect után safe állapot
  - output_off parancs biztos
  - all_outputs_off tényleg mindent lekapcsol
  - SERIES/PARALLEL/INDEPENDENT mód kezelése
  - readback CH1/combined logika
  - timeoutok
  - hibás SCPI válasz kezelése

Load driver:

  - input_off biztos
  - input_on csak controlleren keresztül
  - set_current előtt mód helyes-e
  - readback exception kezelése
  - soft-start / slew rate igény

DMM driver:

  - DCV konfiguráció
  - PT100 konfiguráció
  - NPLC
  - timeout
  - overload érték felismerése
  - NaN/Inf kezelés
  - voltage jump detector

Különösen nézendő:

  drivers/device_psu.py
  drivers/device_load.py
  drivers/device_dmm.py


7. GUI / worker / thread safety
-------------------------------

GUI-nál nem a kinézet a legfontosabb, hanem hogy ne fagyjon, és ne hívjon rossz threadből műszert.

Ellenőrizni kell:

  - TestRunner nem importál PySide6-ot
  - GUI nem hív közvetlenül drivert
  - műszerek csak worker threadből érhetők el
  - worker signalokat küld
  - GUI widget csak GUI threadből frissül
  - QThread cleanup:
      quit
      wait
      deleteLater
  - többszöri Start/Stop után nincs duplán bekötött signal
  - Emergency Stop gomb mindig működik
  - CheckpointPanel helyesen kezeli:
      terminal checkpoint
      resume_possible false
  - event_ready valóban látható EventLogWidgetben
  - status bar nem ad félrevezető állapotot

Különösen nézendő:

  gui/main_window.py
  gui/worker.py
  gui/panels/live_panel.py
  gui/panels/checkpoint_panel.py
  gui/panels/event_log_widget.py


8. Config / repo higiénia
-------------------------

Ezek nem látványosak, de professzionális rendszernél fontosak.

Ellenőrizni kell:

  - local_config.yaml nincs verziózva
  - local_config.template.yaml van
  - .gitignore rendben:
      __pycache__
      *.pyc
      .pytest_cache
      local_config.yaml
      mérési logok
      *.db
  - requirements.txt vagy pyproject.toml van
  - dependency-k:
      PySide6
      pyqtgraph
      pyvisa
      PyYAML
      pytest
  - build script működik
  - exe buildnél config fájl kezelése egyértelmű


9. HW előkészítő eszközök
-------------------------

connection_test.py review:

  - list_resources működik
  - placeholder resource skip
  - IDN lekérés
  - safe_off csak OFF irányú
  - nem kapcsol OUTPUT ON-t
  - nem kapcsol LOAD INPUT ON-t
  - DMM alap mérés
  - summary PASS/SKIP/FAIL
  - exit code helyes
  - --config opció hasznos
  - pyvisa hiányra érthető üzenet

preflight checklist review:

  - BY550 irány
  - PSU mód
  - 12V/24V pack ellenőrzés
  - DMM Kelvin pont
  - PT100 rögzítés
  - akku polaritás
  - load polaritás
  - első futás kisárammal


10. Tesztek
-----------

A tesztekben nem csak darabszámot kell nézni, hanem hogy a valódi hibákat fogják-e.

Fontos tesztcsoportok:

  - SafetyManager unit tesztek
  - ChargeController fault tesztek
  - DischargeController fault tesztek
  - DMM lost tesztek
  - PSU/load readback exception tesztek
  - series_drop fault teszt
  - max_charge_Ah CV/TAPER teszt
  - integrátor dt_s teszt
  - checkpoint atomic write teszt
  - TestRunner checkpoint teszt
  - Worker signal teszt
  - connection_test mock teszt

A legfontosabb kérdés:

  Van-e teszt minden olyan hibára, ami hardvert vagy mérési eredményt veszélyeztet?

Ha nincs, oda kell tesztet írni.


11. Review prioritás
====================

Ha nem akarunk mindent egyszerre review-zni, akkor ez a sorrend:

  1. safety.py
  2. charge_controller.py
  3. discharge_controller.py
  4. test_runner.py
  5. instrument_manager.py
  6. drivers/device_psu.py
  7. drivers/device_load.py
  8. drivers/device_dmm.py
  9. logger.py
  10. report_generator.py
  11. gui/worker.py
  12. gui/main_window.py
  13. tools/connection_test.py
  14. tests/


12. Mit nem érdemes piszkálni csak azért, hogy legyen változás?
===============================================================

Nem érdemes cserélni:

  - jól működő dataclassokat pydanticra csak divatból
  - string státuszokat enumra, ha minden teszt stabil és nincs félreértés
  - PySide6 QThreadet asyncio-ra
  - onedir exe-t onefile exe-re, ha a config miatt onedir praktikusabb
  - meglévő logger struktúrát, ha működik
  - mock drivereket bonyolultabb frameworkre
  - GUI layoutot csak esztétikai okból

Csak olyan változtatás érdemes, ami:

  - safety-t javít
  - mérési pontosságot javít
  - hamis DONE / hamis PASS állapotot akadályoz meg
  - hosszú mérés adatvesztését akadályozza meg
  - valós HW használatot tesz megbízhatóbbá
  - diagnosztikát javít


Röviden
=======

Egy ilyen kód review-ja főleg ezekről szóljon:

  1. Nem tud-e rosszkor PSU/Load bekapcsolni?
  2. Minden hiba biztonságos OFF állapotba visz-e?
  3. Nem tud-e hamis DONE / hamis CHARGE_DONE / hamis kapacitás keletkezni?
  4. A mérési adatok metrológiailag értelmezhetők-e?
  5. A DMM/PSU/Load valós műszeren biztosan konfigurált állapotban van-e?
  6. Hosszú mérés után visszaállíthatók-e az adatok?
  7. A GUI nem tud-e rossz threadből belenyúlni műszerbe?
  8. A tesztek a tényleges hardver- és mérési kockázatokat fedik-e?

Ez alapján érdemes review-zni, nem általános „szép-e a kód” szemmel.
