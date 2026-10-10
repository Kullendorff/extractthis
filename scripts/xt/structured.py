"""Verktyg för att bygga EGNA strukturerade extraktorer (statblock, besvärjelser,
tabeller -> JSON). Generaliserad från dungeoncrawl/tools/pdfio.py (PF2e), där
mönstret validerades mot Bestiary 1: 409/411 CREATURE-block hittade rent.

Typiskt recept (se references/structured-recipes.md):

    import sys; sys.path.insert(0, r"C:/Users/kulle/.claude/skills/extract-this/scripts")
    from xt.structured import Book
    import re
    b = Book(r"D:/.../Bestiary.pdf", code="B1")
    start, end = b.toc_range("Monsters", level=1)
    lines = b.lines(start, end)                   # [(sida, rad), ...] utan sidhuvud/fot
    for ch in b.chunk_by_pattern(lines, re.compile(r"^CREATURE ([-−–]?\\d+)$")):
        body, cut = trim_chunk_body(ch["lines"][1:])
        ...                                       # egen regex per fält
        rec["source"] = b.source(ch["page"])      # {"book": "B1", "page": 21}
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path

import pymupdf

# PDF:er använder olika minustecken beroende på font: ASCII '-', EN DASH U+2013
# ("Cha –2") och MINUS SIGN U+2212 ("Int −2"). Använd SIGN i alla tal-regexar.
SIGN = "[+−–-]"


def to_int(s: str) -> int:
    return int(s.replace("–", "-").replace("−", "-").replace("+", "").strip())


def clean_line(line: str) -> str:
    """Ta bort kontrolltecken (t.ex. 'AXIOMITE\\x08') men inget annat."""
    return "".join(c for c in line if unicodedata.category(c)[0] != "C").rstrip()


class Book:
    def __init__(self, path, code: str | None = None, chrome_runs: list[str] | None = None,
                 drop_lines: set[str] | None = None, auto_chrome: bool = True):
        """chrome_runs: EXAKT radsekvens som ska bort där den förekommer i följd
        (t.ex. en kapitelnavigeringslist i marginalen). drop_lines: enskilda
        rader som alltid tas bort (bokens titel i sidfoten). auto_chrome: ta även
        bort rader som återkommer i sidornas kanter på >=30 % av sidorna."""
        self.path = Path(path)
        self.code = code or self.path.stem
        self.doc = pymupdf.open(self.path)
        self.chrome_runs = chrome_runs
        self.drop_lines = set(drop_lines or ())
        self._auto = self._detect_edges() if auto_chrome else set()

    # -- grundläggande --------------------------------------------------
    def page_lines(self, page_1based: int):
        for raw in self.doc[page_1based - 1].get_text("text").split("\n"):
            line = clean_line(raw)
            if line:
                yield page_1based, line

    EDGE = 2
    _PAGENUM_RE = re.compile(r"^(?:page|sida|s\.)?\s*\d{1,4}$", re.I)

    def _detect_edges(self) -> set[str]:
        """Rader som återkommer EXAKT i sidkanterna på >=30 % av sidorna. Siffror
        normaliseras inte (då försvann varje tabellcell som bara är ett tal);
        rena sidnummer hanteras separat i _strip_page."""
        n = self.doc.page_count
        if n < 8:
            return set()
        step = max(1, n // 60)
        cnt: Counter = Counter()
        sampled = 0
        for p in range(1, n + 1, step):
            ls = [l for _, l in self.page_lines(p)]
            sampled += 1
            for l in set(ls[:self.EDGE] + ls[-self.EDGE:]):
                cnt[l.strip()] += 1
        return {k for k, v in cnt.items() if v >= max(3, 0.3 * sampled) and len(k) <= 60}

    def _strip_page(self, lines):
        """Ta bort sidkrom bara i sidans topp/botten: auto-detekterade rader,
        drop_lines och rena sidnummer."""
        n = len(lines)
        out = []
        for k, (p, l) in enumerate(lines):
            edge = k < self.EDGE or k >= n - self.EDGE
            s = l.strip()
            if edge and (s in self._auto or s in self.drop_lines or self._PAGENUM_RE.match(s)):
                continue
            out.append((p, l))
        return out

    def _strip(self, lines):
        lines = list(lines)
        if self.chrome_runs:
            n, out, i = len(self.chrome_runs), [], 0
            while i < len(lines):
                if [l for _, l in lines[i:i + n]] == self.chrome_runs:
                    i += n
                else:
                    out.append(lines[i])
                    i += 1
            lines = out
        return lines

    def lines(self, start_page: int | None = None, end_page: int | None = None):
        """(sida, rad) för sidintervall (1-baserat, inklusivt), utan sidkrom."""
        first, last = start_page or 1, end_page or self.doc.page_count
        out = []
        for p in range(first, last + 1):
            out.extend(self._strip_page(list(self.page_lines(p))))
        return self._strip(out)

    # -- innehållsförteckning ---------------------------------------------
    def toc(self):
        return self.doc.get_toc()

    def toc_range(self, title: str, level: int | None = None, after_page: int | None = None):
        """(start, slut) för en TOC-post inkl. hela underträdet. level/after_page
        skiljer på dubbletter (t.ex. 'Weapons' i både kapitel 6 och skattkapitlet,
        'Focus Spells' både som intro s.301 och lista s.387 i PF2 CRB)."""
        toc = self.toc()
        for i, (lvl, ttl, page) in enumerate(toc):
            if ttl.strip() == title and (level is None or lvl == level) and (after_page is None or page > after_page):
                end = self.doc.page_count
                for lvl2, _t, page2 in toc[i + 1:]:
                    if lvl2 <= lvl:
                        end = max(page, page2 - 1)  # nästa post på samma sida -> (10, 10), inte (10, 9)
                        break
                return page, end
        raise KeyError(f"TOC-post ej hittad: {title!r} (level={level}) i {self.code}")

    def source(self, page: int | None = None) -> dict:
        """Källhänvisning per post – en sida man kan slå upp vid regeltvist."""
        if page is not None:
            return {"book": self.code, "page": page}
        st = self.path.stat()
        return {"book": self.code, "file": self.path.name, "size": st.st_size, "mtime": int(st.st_mtime)}

    # -- chunkning ----------------------------------------------------------
    @staticmethod
    def chunk_by_pattern(lines, pattern, name_offset: int = -1):
        """Dela radlistan i block som börjar vid rader som matchar pattern.

        name_offset=-1: postens namn står på raden FÖRE markören (Bestiary:
        'ANIMATED BROOM' / 'CREATURE –1'). 0: namnet finns i matchen själv.
        Varje chunk: {'name', 'page', 'match', 'lines': [(sida, rad), ...]}.

        VARNING: en missad markör (fel minustecken!) gör att nästa post sväljs
        in i föregående. Räkna alltid träffar mot förväntat antal."""
        lines = list(lines)
        starts = [i for i, (_, l) in enumerate(lines) if pattern.match(l.strip())]
        chunks = []
        for k, i in enumerate(starts):
            end = starts[k + 1] if k + 1 < len(starts) else len(lines)
            name = None
            if name_offset != 0 and 0 <= i + name_offset < len(lines):
                name = lines[i + name_offset][1].strip()
            chunks.append({"name": name, "page": lines[i][0], "match": pattern.match(lines[i][1].strip()),
                           "lines": lines[i:end]})
        return chunks


def block_text(chunk) -> str:
    return "\n".join(l for _, l in chunk["lines"])


_DIGIT_ONLY_RE = re.compile(r"^\d+$")
_CHROME_CAP_RE = re.compile(r"^[A-Z][A-Z '&\-]*$")


def trim_chunk_body(body, max_lines: int = 40, caps_needed: int = 2):
    """Kapa en chunks kropp vid första tecknet på sidfot/nästa-kapitel-brus:
    ensam sifferrad (efter att brödtext börjat) eller `caps_needed` versalrader
    i rad efter brödtext. Versalrader FÖRE brödtext (traits) räknas inte.
    caps_needed=1 för böcker där sidopaneler börjar med EN versalrad.
    Returnerar (body, truncated)."""
    out, caps, prose, cut = [], 0, False, False
    for p, l in body:
        s = l.strip()
        if _DIGIT_ONLY_RE.match(s):
            if prose:
                break
            continue  # sidnummer insprängt i fältblocket vid sidbrytning
        chrome = bool(_CHROME_CAP_RE.match(s)) and len(s) <= 25
        if chrome and prose:
            caps += 1
            if caps >= caps_needed:
                break
            continue
        caps = 0
        out.append((p, l))
        if not chrome and any(c.islower() for c in s):
            prose = True
        if len(out) >= max_lines:
            cut = True
            break
    return out, cut


def fields_in_order(lines, labels: list[str]):
    """Skanna ett block, plocka kända 'Etikett värde'-fält var de än ligger och
    samla allt övrigt som fritext-spann (från extract_monsters: formåge-stycken
    UTAN etikett ligger mellan kända fält). Rader som inte börjar med en etikett
    räknas som fortsättning på föregående fält om de börjar med gemen.
    Returnerar (fields: dict, free: list[(efter_etikett, [rader])])."""
    rx = re.compile(r"^(" + "|".join(map(re.escape, labels)) + r")\b(.*)$")
    fields, free, cur, buf, fbuf, last = {}, [], None, [], [], None

    def flush():
        if cur:
            fields[cur] = " ".join(buf).strip()

    for _p, l in lines:
        s = l.strip()
        m = rx.match(s)
        if m:
            flush()
            if fbuf:
                free.append((last, list(fbuf)))
                fbuf.clear()
            cur, buf, last = m.group(1), [m.group(2).strip()], m.group(1)
        elif cur and s[:1].islower():
            buf.append(s)
        else:
            flush()
            cur, buf = None, []
            fbuf.append(s)
    flush()
    if fbuf:
        free.append((last, list(fbuf)))
    return fields, free
