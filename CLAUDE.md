# Claude Code projekt szabályok - Akku labor automatizálás

Ez a projekt akkumulátor laborfolyamatok automatizálására szolgál.

Fő célok:
- AGM / VRLA akkumulátor töltési, kisütési és relaxációs ciklusok vezérlése.
- BQ34Z110/BQ34Z100 golden image előkészítő fizikai ciklusok támogatása BQ-kommunikáció nélkül.
- DMM-alapú diódaesés-kompenzált akkutöltés megvalósítása.
- Labor műszerek vezérlése:
  - Keithley 2220-30-1 tápegység USB-n
  - Keithley 2380-120-60 elektronikus terhelés USB-n
  - Keysight 34465A DMM LAN-on
- Reprodukálható mérési logok, riportok és fejlesztési döntések dokumentálása.

## Mappastruktúra

A projektben a mappastruktúrát tartsd következetesen:

```text
.
├── CLAUDE.md
├── .gitignore
├── .claude/
│   ├── settings.local.json
│   └── commands/
│       ├── plan.md
│       ├── review.md
│       ├── embedded-review.md
│       ├── kb-search.md
│       ├── kulso-review.md
│       └── bq-review.md
├── Leírások/
│   ├── TUDÁSBÁZIS.txt
│   ├── műszer_manualok/
│   ├── akku_adatlapok/
│   ├── TI_BQ/
│   └── kapcsolások/
├── Folyamatok/
│   ├── tervek/
│   ├── review-k/
│   ├── külső-review-k/
│   ├── döntések/
│   └── jegyzőkönyvek/
├── Prog/
│   ├── src/
│   ├── tests/
│   ├── drivers/
│   └── config/
├── Mérések/
│   ├── nyers_logok/
│   ├── feldolgozott/
│   └── grafikonok/
├── Riportok/
└── Eszközök/
```

A `Leírások/` mappa tartalmazza a forrásdokumentációt.
A `Folyamatok/` mappa tartalmazza a terveket, review-kat, döntéseket, jegyzőkönyveket.
A `Prog/` mappa tartalmazza a programkódot.
A `Mérések/` mappa tartalmazza a mérési logokat és feldolgozott adatokat.

## Workflow

1. Először olvasd át a releváns fájlokat.
2. Nagyobb módosítás előtt készíts rövid implementációs tervet.
3. Csak jóváhagyás után módosíts több fájlt.
4. Minden módosítás után futtasd az elérhető ellenőrzéseket.
5. Mérési, töltési vagy kisütési logikát érintő módosítás után mindig készüljön tesztelési terv.
6. Kritikus töltési/kisütési védelmet érintő változtatásnál ne csak build legyen, hanem hiba-injektálási tesztterv is.

## Git használat

Git telepítve van, ezért minden nagyobb módosítás előtt használd:

```bash
git status
git diff
```

Módosítás előtt javasolt mentési pont:

```bash
git status
git add <módosított_fájlok>
git commit -m "mentési pont: <rövid leírás>"
```

Szabályok:
- Ne commitolj automatikusan, csak ha erre külön kérek.
- Ne futtass destruktív git parancsot külön engedély nélkül.
- Tilos automatikusan:
  - `git reset --hard`
  - `git clean -fd`
  - `git checkout -- .`
  - fájlok törlése jóváhagyás nélkül
- Review előtt mindig ellenőrizd:
  - `git status`
  - `git diff`
  - ha van staged változás: `git diff --cached`

## Memory kezelés

A Claude memória rendszere a munkameneteken átívelő kontextust tartja fenn.
Helye: `~/.claude/projects/C--Users-Mate-Desktop-teszt-Akkuteszter/memory/`
(Ez a mappa NEM git-követett — a projekt mappán kívül él, gépenként helyi.)

### Mikor kell memóriát frissíteni

Memóriát frissíts minden munkamenet végén, ha az alábbiak bármelyike teljesül:

- **Modul elkészült** (pl. egy driver, egy állapotgép): frissítsd a `project_summary.md`-t
- **Tervezési döntés született** (pl. float feszültség, kommunikációs protokoll, hibakezelési stratégia): `project_summary.md` + ha visszatérő döntés, a `Folyamatok/döntések/` fájlba is
- **Hibás megközelítés derült ki**, amit korrigáltunk: `feedback_*.md` fájl
- **Új forrás, referencia kerül a projektbe** (adatlap, manual, TI app note): `reference_*.md` fájl
- **A projekt státusza változik** (pl. „kód üres" → „drivers kész", „tesztelés folyamatban"): `project_summary.md`

### Mit tárolj hol

| Mi | Hova |
|----|------|
| Projekt státusz, kész modulok, fő paraméterek | `memory/project_summary.md` |
| Visszatérő munkafolyamat-szabályok, elkerülendő minták | `memory/feedback_*.md` |
| Külső források, manualok elérési útja | `memory/reference_*.md` |
| Formális döntések, tervezési indoklások | `Folyamatok/döntések/` (git-követett!) |
| Elvégzett review-k | `Folyamatok/review-k/` (git-követett!) |
| Mérési eredmények, kapacitásteszt logok | `Mérések/` (git-követett!) |

### Memory és git szinkronizálása

A memory NEM helyettesíti a git-et — kiegészíti. Szabályok:

1. Ha commitolsz, gondold át: kell-e memóriát is frissíteni?
2. Fontos döntést (`Folyamatok/döntések/`) commit elé rögzíts, hogy a git history értelmezhetővé váljon.
3. A memória a Claude tájékozódási eszköze; a git és a `Folyamatok/` a projekt valódi forrása.
4. Ha a memória és a kód ellentmond egymásnak, a kód és a git log az igazság — frissítsd a memóriát.

### Jelenlegi memory fájlok

- `MEMORY.md` — index (ezt olvasd először)
- `project_summary.md` — projekt célok, modulok, státusz
- `user_profile.md` — munkastílus, elvárások
- `reference_agm_engineering_manual.md` — FIAMM AGM Manual kulcsadatai

### Projekt státusz naprakészen tartása

A `project_summary.md` **Státusz** szekcióját munkamenetek után frissítsd:
- Mi készült el (modul neve, fájl elérési útja)
- Mi van folyamatban
- Mi a következő lépés
- Nyitott kérdések, ismert hibák

Ez az egyetlen hely ahol a Claude egy új munkamenetben gyorsan megérti, hol tart a projekt.

## Coding style

- A meglévő kódstílust kövesd.
- Egyszerű, karbantartható megoldást írj.
- Ne refaktorálj feleslegesen.
- Ne változtass publikus API-t külön kérés nélkül.
- Ne írj workaroundot, keresd meg a gyökérokot.
- Műszervezérlési driverben legyen külön:
  - connect / disconnect
  - safe_off
  - check_error
  - self_test vagy identity check
  - parancslogolás
- Töltési/kisütési állapotgépnél legyenek explicit állapotok és fail-safe útvonalak.

## Safety

- A program elsődleges biztonsági filozófiája:
  - hiba esetén először elektronikus terhelés OFF,
  - utána tápegység output OFF,
  - utána relé bontás,
  - minden hiba logolva legyen.
- Akkutöltésnél a DMM szerinti akkufeszültség legyen a fő döntési referencia.
- DMM-feedback elvesztése esetén töltés azonnal álljon le.
- Tápegység `output off` nem feltétlenül jelent galvanikus leválasztást; relé vagy külön leválasztás szükséges lehet.
- Soros dióda / ideális dióda esetén külön figyelni kell:
  - PSU readback
  - DMM akkuoldali feszültség
  - számított soros esés
  - headroom-limit
- Ne olvass vagy módosíts `.env`, kulcs, token, cert fájlokat engedély nélkül.
- Ne futtass destruktív parancsot jóváhagyás nélkül.
- Ne törölj fájlt külön engedély nélkül.
- Ne commitolj automatikusan.

## Tudásforrások

A projekt hardveres és dokumentációs forrásai:

- `Leírások/TUDÁSBÁZIS.txt`: **INDEX fájl** — strukturált mutató, nem teljes manual.
- `Leírások/műszer_manualok/`: műszerenként almappák, teljes TXT és PDF fájlok.
- `Leírások/akku_adatlapok/`: akkumulátor katalógusok TXT és PDF formátumban.
- `Leírások/TI_BQ/`: TI BQ dokumentáció (jelenleg üres).
- `Folyamatok/`: saját tervek, review-k, döntések, jegyzőkönyvek.

### TUDÁSBÁZIS.txt — szerkezet és szerepe

Kétszintű dokumentációs rendszer:

```
TUDÁSBÁZIS.txt  ← BELÉPÉSI PONT (INDEX)
  ├── forrásfájlok listája és elérési útja
  ├── kulcsparaméterek per eszköz (feszültség, áram, pontosság, határértékek)
  ├── SCPI parancsok összefoglalója per eszköz
  ├── projekt-specifikus keresztreferenciák (DMM-kompenzáció, C-ráta, BQ ciklus)
  └── grep-kulcsszavak a gyors kereséshez  (=== SZEKCIÓCÍM === formátum)

Leírások/műszer_manualok/**/*.txt  ← TELJES TARTALOM
  ├── 2220-30-1/prog.txt           (160 KB — teljes SCPI lista)
  ├── 2220-30-1/spec.txt           (117 KB — részletes spec)
  ├── 2380-120-60/2380-...-900-01_A_Nov_2015.txt  (255 KB — 2380 fő manual)
  ├── 34465A/34460-70-Manual.txt   (765 KB — DMM Operating & Service Guide)
  └── akku_adatlapok/*.txt         (katalógus kivonatok)
```

### Használati szabályok (kötelező):

1. **Ne töltsd be automatikusan a teljes TUDÁSBÁZIS.txt-t** — csak célzottan keress.
2. **Munkamenet:**
   - Keresd a releváns szekciót a TUDÁSBÁZIS.txt-ben (`=== ESZKÖZ / TÉMA ===`).
   - Ha az adat nem elegendő, nyisd meg a hivatkozott forrásfájlt, olvasd célzottan (offset/limit).
   - Ha kódot módosítasz, jelezd, melyik forrás alapján döntöttél.
3. **Az akku katalógus TXT-ek** (FGH, FG) főleg képes PDF-kivonatok — korlátozott tartalom. Konkrét kapacitás/töltőfeszültség adatokhoz a PDF-et nyisd meg.
4. Ha nincs elég adat: kérdezz, vagy jelöld `FELTÉTELEZÉS` / `NEM IGAZOLHATÓ`.

## Tesztparancsok

Ha léteznek, ezeket használd:

```bash
python -m pytest
python -m compileall Prog
python -m ruff check Prog
python -m mypy Prog
```

Ha nincs tesztkörnyezet, legalább:
- statikus átnézés,
- import/próba futtatás,
- konfigurációs fájl validáció,
- műszer nélküli dry-run / mock driver teszt legyen.

## Projekt-specifikus kritikus témák

Különösen figyelj ezekre:

1. DMM-alapú diódaesés-kompenzáció
   - lassú szabályozás,
   - deadband,
   - max lépés,
   - DMM timeout fail-safe,
   - battery overvoltage fail-safe,
   - PSU headroom-limit.

2. Keithley 2220-30-1 tápegység
   - USBTMC/SCPI,
   - output ON/OFF,
   - voltage/current set,
   - readback,
   - safe_off.

3. Keithley 2380-120-60 elektronikus terhelés
   - CC mód,
   - input ON/OFF,
   - voltage/current/power readback,
   - túlterhelés és teljesítménylimit.

4. Keysight 34465A DMM
   - LAN/SCPI,
   - DCV mérés,
   - fix range,
   - NPLC,
   - READ/INIT/FETCH,
   - timeout kezelés.

5. Akku laborfolyamatok
   - teljes töltés,
   - C/20 kapacitásteszt,
   - C/5 BQ-learning fizikai kisütés,
   - relax idő,
   - OCV-SOC lépcsőzés,
   - terheléses impedancia-jellegű mérés.

6. BQ34Z110/BQ34Z100 golden image előkészítés
   - ebben a projektben a laborfolyamat BQ-kommunikáció nélkül fut,
   - BQ állapotok ellenőrzése külön kézi vagy külső eszközös lépés,
   - a szoftver célja a fizikai ciklusok reprodukálása és dokumentálása.

## Kimeneti elvárás review/terv esetén

Legyen strukturált:
- vezetői összefoglaló,
- kritikus kockázatok,
- konkrét érintett fájlok,
- javítási irány,
- tesztelési terv,
- rollback lehetőség,
- nyitott kérdések.

Ne írj általános dicséretet.
Ha nincs bizonyíték, írd: `NEM IGAZOLHATÓ`.
Ha feltételezés, írd: `FELTÉTELEZÉS`.
Ha biztonsági okból nem folytatható, írd: `REVIEW STOP`.
