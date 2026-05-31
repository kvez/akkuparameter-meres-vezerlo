Készíts implementációs tervet ehhez az akku labor automatizálási projekthez.

Ne módosíts fájlt.

A tervben térj ki erre:

1. Cél és hatókör
- Pontosan milyen laborfolyamatot / műszervezérlést / mérési funkciót kell megvalósítani?
- Érinti-e a töltést, kisütést, relaxációt, DMM-kompenzációt, logolást vagy riportot?

2. Releváns fájlok
- mely fájlokat kell olvasni
- mely fájlokat kell módosítani
- mely konfigurációs fájlokat érinti
- mely dokumentációs fájlokat kell célzottan keresni a `Leírások/` mappában

3. Dokumentációs ellenőrzés
- keress célzottan a `Leírások/TUDÁSBÁZIS.txt` fájlban, ha a feladat műszert, akkutöltést, DMM-et, tápegységet vagy elektronikus terhelést érint
- ne olvasd be automatikusan az egész tudásbázist
- sorold fel, milyen dokumentációs bizonyíték szükséges

4. Tervezett megoldás
- állapotgép módosítás
- driver módosítás
- safety/fail-safe módosítás
- logolási módosítás
- konfigurációs módosítás
- felhasználói paraméterek

5. Kritikus kockázatok
Külön vizsgáld:
- DMM-feedback elvesztése
- akkufeszültség túllépés
- tápegység headroom-limit
- soros dióda / ideális dióda feszültségesés
- egyszerre aktív PSU és elektronikus terhelés
- relé hibás állapota
- mérési egységhibák: V/mV, A/mA, Ah/mAh, s/h
- timeout és kommunikációs hiba
- hibás konfiguráció

6. Tesztelési terv
Adj konkrét teszteket:
- mock driver teszt
- műszer nélküli dry-run
- tápegység driver teszt
- DMM driver teszt
- 2380 load driver teszt
- töltési állapotgép teszt
- kisütési állapotgép teszt
- DMM-kompenzáció teszt
- hiba-injektálás
- power-cycle / megszakított mérés
- log/riport ellenőrzés

7. Rollback
- hogyan lehet visszaállni
- milyen git lépést javasolsz
- milyen fájlokról kell mentés
- milyen állapotban tilos commitolni

8. Jóváhagyási pont
A végén kérdezd meg:
„Kéred, hogy a terv alapján implementáljam a módosításokat?”
