# Akku labor projekt

Ez a projekt akkumulátor laborfolyamatok automatizálására szolgál: töltési, kisütési és relaxációs ciklusok vezérlése AGM/VRLA akkumulátorokhoz, DMM-alapú diódaesés-kompenzált töltéssel és BQ34Z110/BQ34Z100 golden image előkészítő fizikai ciklusok támogatásával.

## Fő mappák

- `Leírások/`: adatlapok, manualok, application note-ok, TUDÁSBÁZIS.txt
- `Folyamatok/`: tervek, review-k, döntések, jegyzőkönyvek
- `Prog/`: programkód, driverek, tesztek, konfiguráció
- `Mérések/`: mérési logok és feldolgozott adatok
- `Riportok/`: generált riportok
- `Eszközök/`: segédszkriptek, műszerkereső vagy karbantartó eszközök

## Telepítés / használat

1. Másold a fájlokat a projekt gyökerébe.
2. Ellenőrizd:
   ```bash
   git status
   ```
3. Ha új projekt, inicializáld:
   ```bash
   git init
   git add CLAUDE.md .gitignore .claude Leírások Folyamatok Prog Mérések Riportok Eszközök
   git commit -m "projekt sablon: akku labor automatizálás"
   ```
4. A `Leírások/` mappába tedd az adatlapokat és manualokat.
5. A `Folyamatok/` mappába kerüljenek a tervek, review-k és jegyzőkönyvek.
6. A `Prog/` mappába kerüljön a tényleges program.

## Claude parancsok

- `/plan`: implementációs terv
- `/review`: aktuális módosítások review-ja
- `/embedded-review`: embedded/laborautomatizálási kódreview
- `/kb-search`: célzott keresés a tudásbázisban
- `/kulso-review`: külső review validálása
- `/bq-review`: BQ/golden image/laborfolyamat review

## Fontos biztonsági elv

A DMM-alapú diódaesés-kompenzált töltésnél:
- PSU sense a dióda előtt marad,
- DMM az akkut méri,
- a szoftver lassan korrigálja a PSU set voltage értékét,
- DMM hiba esetén azonnali leállítás,
- battery overvoltage esetén azonnali leállítás,
- relé adja a valódi leválasztást.
