Nézd át a jelenlegi módosításokat ebben az akku labor automatizálási projektben.

Ne módosíts semmit.

Először futtasd / ellenőrizd, ha elérhető:
- git status
- git diff
- git diff --cached, ha van staged változás

Fókusz:
- logikai hiba
- edge case
- race condition
- hibás error handling
- safety probléma
- túlkomplikált megoldás
- törékeny teszt
- hibás mértékegység
- rossz állapotgép-átmenet
- DMM-feedback elvesztése
- PSU / load egyszerre aktív állapota
- reléhiba
- headroom-limit
- diódaesés-kompenzáció hibája
- rossz timeout kezelés
- hibás logolás vagy reprodukálhatatlan mérés

Külön ellenőrizd:
1. Tápegység safe_off
2. Elektronikus terhelés safe_off
3. DMM mérési timeout
4. Emergency stop sorrend
5. Töltés végfeltétel
6. Kisütés végfeltétel
7. Relax alatt minden aktív elem OFF-e
8. Ah / Wh integrálás helyessége
9. Konfigurációs értékek tartományellenőrzése
10. Gitben véletlenül bekerülő log, mérési adat, secret vagy lokális beállítás

Kimeneti forma:

# Review eredmény

## 1. Vezetői összefoglaló
- Elfogadható / feltételesen elfogadható / nem elfogadható
- Top kockázatok

## 2. Kritikus
Minden pontnál:
- Azonosító
- Érintett fájl
- Probléma
- Miért veszélyes
- Javasolt javítás
- Szükséges teszt

## 3. Fontos
Ugyanilyen formában.

## 4. Apróságok / karbantarthatóság

## 5. Tesztelési javaslat

## 6. Nyitott kérdések

Ne dicsérj általánosan.
Ha nincs bizonyíték, írd: NEM IGAZOLHATÓ.
