Végezz professzionális review-t a BQ34Z110/BQ34Z100 golden image előkészítő és akkumulátor-labor folyamatok szempontjából.

Fontos projektmegkötés:
- Ebben a szoftverben alapértelmezés szerint NINCS közvetlen BQ-kommunikáció.
- A szoftver a fizikai laborfolyamatokat vezérli:
  - töltés,
  - kisütés,
  - relax,
  - kapacitásmérés,
  - OCV-SOC mérés,
  - C/5 learning ciklus fizikai előkészítése.
- A BQ állapotokat külön kézi vagy külső eszközös ellenőrzés validálja.

A review célja:
Ellenőrizni kell, hogy a laborfolyamat, mérési logika és dokumentáció alkalmas-e
BQ34Z110/BQ34Z100 Impedance Track learning / golden image előkészítéshez.

Használható források:
- `Leírások/TUDÁSBÁZIS.txt`
- `Leírások/TI_BQ/`
- `Folyamatok/`
- a projekt kódja és konfigurációja
- csak hivatalos TI dokumentumokra vagy a projektben szereplő dokumentációra támaszkodj

Ha valamire nincs bizonyíték:
- NEM IGAZOLHATÓ

Ha feltételezés:
- FELTÉTELEZÉS

Ha hiányzik a validáláshoz szükséges információ:
- HIÁNYZÓ BIZONYÍTÉK / REVIEW STOP

Vizsgálandó területek:

1. Akkuprofil és fizikai paraméterek
- cellaszám
- névleges kapacitás
- töltési célfeszültség
- terminate voltage
- C/20, C/10, C/5 áramok
- hőmérsékleti tartomány
- maximális töltőáram
- maximális kisütőáram

2. Töltési folyamat
- CC/CV profil
- AGM/VRLA töltőfeszültség
- taper current
- DMM szerinti akkuoldali feszültség
- diódaesés-kompenzáció
- tápegység headroom
- töltés vége

3. Kisütési folyamat
- C/5 learning kisütés
- C/20 kapacitásteszt
- terminate voltage
- megszakítás kezelése
- Ah/Wh integrálás
- load power limit

4. Relaxáció
- töltés utáni 2 h relax
- kisütés utáni 5 h relax
- DMM OCV mérés
- dV/dt opcionális értékelés
- töltő és terhelés leválasztása

5. BQ learning fizikai ciklus
- teljes töltés
- relax
- C/5 kisütés
- relax
- ismétlés
- kézi BQ checkpoint:
  - UpdateStatus
  - Qmax
  - Ra table
  - golden image export

6. Golden image előkészítés
- mit tud a laborprogram bizonyítani
- mit nem tud BQ-kommunikáció nélkül bizonyítani
- milyen kézi jegyzőkönyv szükséges
- milyen mérési logokat kell megőrizni

7. Safety / fail-safe
- DMM feedback elvesztése
- battery overvoltage
- series drop túl nagy
- PSU headroom-limit
- reléhiba
- PSU és load egyszerre aktív
- kommunikációs hiba
- túlmelegedés

Kimeneti forma:

1. Vezetői összefoglaló
- Elfogadható / feltételesen elfogadható / nem elfogadható
- Top 10 kockázat
- Review stop pontok

2. Kritikus hibák
Minden hibánál:
- Azonosító
- Súlyosság: BLOCKER / CRITICAL / MAJOR / MINOR
- Érintett fájl / folyamat
- Probléma
- Miért veszélyes
- Dokumentációs vagy mérési elv
- Javasolt javítási irány
- Szükséges teszt

3. Konfigurációs megfelelőség
Táblázat:
- Paraméter
- Talált érték
- Elvárt logika
- Forrás / indoklás
- Státusz: OK / NEM OK / NEM IGAZOLHATÓ

4. Laborfolyamat review
- töltés
- kisütés
- relax
- OCV-SOC
- C/5 learning ciklus
- DMM-kompenzáció

5. BQ-specifikus korlátok
- mit nem tud a program BQ-kommunikáció nélkül
- milyen kézi ellenőrzés kell

6. Tesztterv
- bring-up
- DMM mérés
- PSU töltés
- 2380 kisütés
- C/20 kapacitás
- C/5 learning ciklus
- relax
- hiba-injektálás
- megszakítás / power loss
- golden image kézi checkpoint

7. Javítási prioritási lista

8. Nyitott kérdések
