Dolgozd fel az alábbi külső review-t.

Fontos:
- Ne módosíts kódot automatikusan.
- Először validáld a review állításait.
- Ne fogadj el vakon semmilyen javaslatot.
- Csak bizonyított vagy erősen indokolt hibát javasolj javításra.

Feladatmenet:

1. Olvasd el a külső review teljes szövegét.

2. Azonosítsd a review pontjait:
   - konkrét hiba
   - lehetséges kockázat
   - félreértés / nem releváns megjegyzés
   - stílus vagy karbantarthatósági javaslat
   - hiányzó dokumentációs bizonyíték
   - hiányzó teszt

3. Keresd meg az érintett részeket:
   - módosított fájlok
   - driver függvények
   - állapotgépek
   - konfigurációs konstansok
   - safety/fail-safe logika
   - DMM / PSU / Load vezérlés
   - logolás
   - riport generálás

4. Ellenőrizd a teljes kontextust:
   - hívási lánc
   - timeout kezelés
   - mértékegységek
   - szélsőértékek
   - állapotgép átmenetek
   - init sorrend
   - safe_off sorrend
   - emergency stop
   - DMM feedback szerepe
   - relé állapotok
   - headroom-limit
   - mérési log reprodukálhatósága

5. Ha releváns, keress célzottan a projekt dokumentációjában:
   - `Leírások/TUDÁSBÁZIS.txt`
   - `Leírások/`
   - `CLAUDE.md`
   - `Folyamatok/`

Ne olvasd be automatikusan a teljes tudásbázist.

Kimeneti forma:

## Review pont feldolgozás

### 1. [rövid cím]

**Review állítás:**  
...

**Érintett kód / folyamat:**  
- fájl:
- függvény / modul:
- konfiguráció:
- állapot:

**Értékelés:**  
- Valós hiba / Valós kockázat / Nem bizonyított / Téves / Csak stílus

**Indoklás:**  
...

**Javasolt teendő:**  
- Javítani kell / Figyelni kell / Nem kell módosítani

**Kockázat:**  
- Kritikus / Magas / Közepes / Alacsony

## Javítási terv
Prioritás szerint:
1. Kritikus hibák
2. Valós működési hibák
3. Edge case-ek
4. Karbantarthatósági javítások

## Tesztelési terv
Térj ki:
- build / import teszt
- mock driver
- DMM timeout
- PSU timeout
- Load timeout
- relé hiba
- túlfeszültség
- soros esés túl nagy
- headroom-limit
- megszakított mérés
- log integritás

A végén kérdezd meg:
„Kéred, hogy a javítási terv alapján implementáljam a módosításokat?”
