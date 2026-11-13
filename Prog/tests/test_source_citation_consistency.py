"""
Kör 20 Batch E18 (§6.1 structural-prevention, DFAM-020 `structural_
prevention_status: REVIEWED_TEST_ONLY_ADOPTED`): read-only, test-only
citation-konzisztencia őr.

DFAM-020 (stale/dangling source citation) 7 előfordulást ért el egyetlen
körben (F-1, F-3, F-4, F-5, F-6, F-7, F-9). Ezek közül EMPIRIKUSAN (MAX
verifier saját, futtatott mérésével, nem levezetéssel) csak KETTŐ (F-4,
F-5) tartozik ehhez a mechanikusan-észrevehető alakzathoz: két vagy több
kódhely UGYANAZT a "<dok> <start>-<end>. sor" forrás-sortartományt idézi,
DE KÜLÖNBÖZŐ nyomtatott lapszámot állít hozzá. F-7 (egyetlen lapszám,
sor-tartomány nélkül) és F-9 (konténer-név, nem lapszám-eltérés) NEM
tartozik ide -- a teszt ezeket SOHA nem fogta volna el, l. lent a pontos
hatókört. Ez a teszt PONTOSAN az F-4/F-5-alakzatot őrzi: csoportosítja
minden "<dok> <start>-<end>. sor, nyomtatott <page>. o./oldal" alakú
idézetet (dokumentum, sor-tartomány) szerint, és elbukik, ha két
testvér-idézet ugyanahhoz a (dokumentum, sor-tartomány)-hoz KÜLÖNBÖZŐ
lapszámot állít.

NEM fogja el (l. `ROUND20_DEFECT_FAMILY_MINING_ARCHIVE.md` §6.1 — ne
állítsunk többet, mint amit tényleg tud): egyedi, egyetlen kódhelyen élő
idézeteket (F-1, F-3, F-6, F-8), mert azokhoz gyártói dokumentum-hozzáférés
és önálló pdftotext-kinyerés szükséges; konténer-név-hibákat (F-9), mert
azokhoz nincs "nyomtatott <page>" rész; egyetlen-lapszámos (sor-tartomány
nélküli) idézeteket (F-7) -- ez a teszt KIZÁRÓLAG a MÁR MEGLÉVŐ,
TÖBBSZÖRÖS, sor-tartományt ÉS lapszámot EGYÜTT hordozó site-ok közötti
BELSŐ ellentmondást tudja ellenőrizni, sosem a külső forrás helyességét.

Az "uo." (ugyanott) hivatkozást a KÖZVETLENÜL MEGELŐZŐ, explicit
dokumentumnévre oldja fel, ugyanazon fájlon belül, sorrendben — 1:1 azzal,
ahogy egy ember olvasná a kommentblokkot.

Zéró produkciós kód, zéró futásidejű útvonal, zéró konfigurációs szabadság,
zéró viselkedésváltozás — l. `.claude/rules/50-review.md` §8 teszt (csak
akkor javasolt refaktor/prevenció, ha csökkenti a duplikált
állapotot/hand-forwarded értéket a viselkedési tér növelése nélkül).

Kör 21 Batch F9 (ARC21-5 + §6.1(i)): a fájlkör (`_iter_py_files()`) mostantól
DERIVÁLT, nem kézzel karbantartott -- minden `Prog/**/*.py` egy explicit,
nevesített kizárási lista (`_EXCLUDED_TOP_LEVEL_DIRS`) mínusz. Az egyetlen
kizárt tag `pico_firmware/`, `open_findings[8]`-ra hivatkozva (BLOCKED,
fizikai Pico-feltöltésre vár, l. archívum §2.1). A korábbi, kézzel írt
`{src, drivers, gui, tools}` felsorolás csendben kihagyta `Prog/tests/`-t
(SRC21-04 blind spotja) és a `Prog/` gyökér .py fájljait.
"""
from __future__ import annotations

import re
from pathlib import Path

_CITATION_RE = re.compile(
    r"(?P<doc>[\w][\w.\-]*\.(?:txt|pdf)|uo\.)\s*"
    r"(?P<start>\d+)-(?P<end>\d+)\.\s*sor,?\s*nyomtatott\s*(?P<page>\d+)\.\s*(?:o\.|oldal)",
    re.IGNORECASE,
)


def _strip_comment_markers(text: str) -> str:
    """Egy '#'-fejlécű, több soros kommentblokk logikailag EGY folyamatos
    szöveg -- a nyers '#' karakterek eltávolítása nélkül egy idézet, ami a
    sortörés UTÁN egy új '# '-sorral folytatódik, nem lenne felismerhető
    egyetlen regex-mintával (a '#' nem whitespace, tehát megszakítaná a
    `\\s*` illesztést a "sor," és a "nyomtatott" token között)."""
    out_lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            rest = stripped[1:].removeprefix(" ")
            indent = line[: len(line) - len(line.lstrip())]
            out_lines.append(indent + rest)
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


_EXCLUDED_TOP_LEVEL_DIRS = {
    # Kör 21 ARC21-5 + §2.1/§6.1(i): BLOCKED on physical presence
    # (open_findings[8], currently 2 sites, archívum §1.1/§7.2 szerint 14-re
    # bővítendő -- az a bővítés Tier (d), NEM ebben a batchben történik meg).
    # Editing `pico_firmware/` -- even in a comment -- creates repo-vs-chip
    # divergence that this project's standing practice
    # (feedback_pico_firmware_requires_physical_reupload) closes only with
    # backup + upload + hash-verify + reload on the physical Pico.
    # PONTOSÍTÁS (MAX verifier F-V2, Kör 21 F9): a MOST futó, csak
    # lapszám-konzisztenciát néző `_CITATION_RE` MA nulla találatot ad
    # `pico_firmware/` alatt (nincs "nyomtatott N. oldal" alakú idézet ott) --
    # a fenti bővítés önmagában NEM tenne piros tesztet. Ez a kizárás egy
    # ELŐVÉTELI VÉDELEM egy JÖVŐBELI, még nem megvalósított ellenőrzéshez
    # (archívum §6.1(ii), a "dangling-anchor check", amely azt vizsgálná,
    # hogy egy idézett sor-tartomány TÉNYLEG létezik-e a célfájlban -- AZ
    # már 14 pirosat adna, l. §2.1), NEM ennek a batchnek (§6.1(i), csak a
    # scope-deriválás) tényleges viselkedéséből fakadó szükségszerűség.
    # Named and commented so the exclusion is an explicit decision, not a
    # silent gap -- akkor is, ha a mai teszt ezt még nem igényelné.
    "pico_firmware",
}


def _iter_py_files():
    """Kör 21 ARC21-5 + §6.1(i): every `Prog/**/*.py` minus the named
    exclusion list above -- DERIVED, not hand-maintained. The prior
    `_source_dirs()` hand-listed `{src, drivers, gui, tools}`, silently
    omitting `Prog/tests/` (SRC21-04's blind spot: a stale citation lived
    there, unswept) and any root-level `Prog/*.py` module; a future new
    top-level package would have been skipped the same way without anyone
    deciding to skip it."""
    prog_root = Path(__file__).resolve().parent.parent
    for path in sorted(prog_root.rglob("*.py")):
        if path.relative_to(prog_root).parts[0] in _EXCLUDED_TOP_LEVEL_DIRS:
            continue
        yield path


def _collect_citation_groups() -> dict[tuple[str, str, str], list[tuple[str, int, str]]]:
    """{(dokumentum.lower(), start, end): [(fájl, sorszám, lapszám), ...]}"""
    groups: dict[tuple[str, str, str], list[tuple[str, int, str]]] = {}
    for path in _iter_py_files():
        raw = path.read_text(encoding="utf-8")
        text = _strip_comment_markers(raw)
        current_doc: str | None = None
        for m in _CITATION_RE.finditer(text):
            doc = m.group("doc")
            if doc.lower() != "uo.":
                current_doc = doc
            if current_doc is None:
                continue  # "uo." egy fájl ELSŐ idézete előtt nem oldható fel
            key = (current_doc.lower(), m.group("start"), m.group("end"))
            # MAX verifier (Kör 20 E18) lelete: `m` a STRIPPELT `text`-ben
            # keletkezik, nem a `raw`-ban -- a '#'+szóköz eltávolítása
            # soronként 1-2 karaktert rövidít, ezért a `raw`-ra alkalmazott
            # offset szisztematikusan hibás sorszámot adott (a diagnosztikai
            # üzenetben, a csoportosítás KULCSÁT nem érintette). `text` és
            # `raw` sorszáma megegyezik (a stripping sosem távolít el/ad
            # hozzá sortörést), ezért a helyes sorszámot `text`-ből kell
            # számolni.
            line_no = text[: m.start()].count("\n") + 1
            groups.setdefault(key, []).append((str(path), line_no, m.group("page")))
    return groups


class TestSourceCitationPageConsistency:
    """l. modul docstring a teljes indoklásért és korlátokért."""

    def test_same_document_line_range_cites_same_printed_page_everywhere(self):
        groups = _collect_citation_groups()
        conflicts = [
            (doc, start, end, entries)
            for (doc, start, end), entries in groups.items()
            if len({page for _, _, page in entries}) > 1
        ]
        assert not conflicts, (
            "Ellentmondó nyomtatott-lapszám-idézetek UGYANAHHOZ a "
            "(dokumentum, sor-tartomány)-hoz -- válasszon ki EGY helyes "
            "értéket önálló pdftotext-kinyeréssel (l. rules/00-source-"
            "authority.md, ne találgasson) és igazítsa mindet ugyanarra:\n"
            + "\n".join(
                f"  {doc} {start}-{end}. sor: {entries}"
                for doc, start, end, entries in conflicts
            )
        )

    def test_at_least_one_multi_site_citation_group_exists(self):
        """Guard a guard-teszt ellen -- ha a citation-minták formátuma
        valaha annyira megváltozik, hogy a regex már semmit sem talál
        (pl. minden idézet átkerül egy másik idézési konvencióba), a
        fenti teszt HAMISAN zöldelne (üres halmazon trivi igaz). Legalább
        egy, TÖBB helyen élő (dokumentum, sor-tartomány) csoportnak
        MINDIG lennie kell, amíg a `34460-70-Manual.txt "<start>-<end>.
        sor, nyomtatott <page>. o."` idézési minta létezik a kódban."""
        groups = _collect_citation_groups()
        multi_site = {k: v for k, v in groups.items() if len(v) > 1}
        assert multi_site, (
            "Nem található több-site-os (dokumentum, sor-tartomány) "
            "citation-csoport -- a regex-minta elavult, vagy a teszt "
            "csendben üres halmazon fut."
        )
        assert len(multi_site) >= 3, (
            f"Csak {len(multi_site)} több-site-os csoport található -- "
            f"a Kör 20 E18 lezárásakor pontosan 3 volt (34460-70-Manual.txt "
            f"14028-14052. sor 3 site-tal, 9067-9072. sor 2 site-tal, "
            f"11428-11433. sor 2 site-tal). Ha ez csökkent, ellenőrizze, "
            f"hogy egy refaktor nem törölte-e a testvér-idézetek egyikét "
            f"szándék nélkül."
        )


class TestKor21BatchF9DerivedScopeNotHandMaintained:
    """Kör 21 Batch F9 (ARC21-5 + §6.1(i)): `_iter_py_files()` most minden
    `Prog/**/*.py`-t bejár egy explicit, nevesített kizárási lista mínusz --
    NEM egy kézzel karbantartott {src, drivers, gui, tools} felsorolást.

    PONTOSÍTÁS (MAX verifier F-V1, Kör 21 F9): a régi `_source_dirs()`
    (visszaállítva: `[prog_root / sub for sub in ("src", "drivers", "gui",
    "tools")]`) mellett a lenti NÉGY teszt közül csak KETTŐ bukott volna el
    -- `test_tests_directory_itself_is_now_covered` és
    `test_prog_root_level_modules_are_now_covered` (mutáció-teszttel
    igazolva). A másik kettő NEM változás-detektor, hanem INVARIÁNS-őr: a
    régi kód is kizárta `pico_firmware`-t és is fedte a {src, drivers, gui,
    tools} négyest, tehát ott a régi kóddal szemben nincs mit
    diszkriminálni -- a saját diszkriminálló mutációjuk egy JÖVŐBELI
    hiba (a kizárás törlése, illetve a kizárási lista túlbővítése), nem a
    Kör 21 F9 által javított régi alakzat."""

    def test_pico_firmware_is_explicitly_excluded_and_named(self):
        assert "pico_firmware" in _EXCLUDED_TOP_LEVEL_DIRS
        scanned = set(_iter_py_files())
        prog_root = Path(__file__).resolve().parent.parent
        pico_firmware_files = list((prog_root / "pico_firmware").rglob("*.py"))
        assert pico_firmware_files, "a pico_firmware/ csomagnak léteznie kell .py fájlokkal"
        assert not (set(pico_firmware_files) & scanned), (
            "pico_firmware/ egy .py fájlja bekerült a scanbe -- ez az explicit, "
            "nevesített kizárás (l. _EXCLUDED_TOP_LEVEL_DIRS fenti indoklása, "
            "open_findings[8], archívum §2.1) sérült."
        )

    def test_tests_directory_itself_is_now_covered(self):
        """SRC21-04 blind spotja: `Prog/tests/` korábban NEM volt a
        scope-ban -- ez a fájl (önmaga) a bizonyíték, hogy most már benne
        van."""
        scanned = set(_iter_py_files())
        assert Path(__file__).resolve() in scanned

    def test_prog_root_level_modules_are_now_covered(self):
        prog_root = Path(__file__).resolve().parent.parent
        scanned = set(_iter_py_files())
        assert (prog_root / "main.py") in scanned
        assert (prog_root / "app_paths.py") in scanned

    def test_previously_covered_directories_remain_covered(self):
        prog_root = Path(__file__).resolve().parent.parent
        scanned = set(_iter_py_files())
        for sub in ("src", "drivers", "gui", "tools"):
            sub_files = list((prog_root / sub).rglob("*.py"))
            assert sub_files, f"{sub}/ csomagnak léteznie kell .py fájlokkal"
            assert set(sub_files) <= scanned, f"{sub}/ egy fájlja kimaradt a derivált scope-ból"
