"""Delade hjälpare: textstädning, kodningar, md-tabeller, språkheuristik, säker skrivning."""
from __future__ import annotations

import hashlib
import os
import re
import sys
import unicodedata
from pathlib import Path

# Windows-konsolen är cp1252 som default -> å/ä/ö och unicode-minus kraschar print().
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _find_tool(env: str, names: list[str], candidates: list[str]) -> str:
    """Miljövariabel -> PATH -> kända installationsplatser. Tom sträng om inget hittas.
    (På Windows lägger Tesseract-installern sig INTE i PATH.)"""
    import shutil

    if os.environ.get(env) and Path(os.environ[env]).exists():
        return os.environ[env]
    for n in names:
        hit = shutil.which(n)
        if hit:
            return hit
    for c in candidates:
        if Path(c).exists():
            return c
    return ""


TESSERACT_EXE = _find_tool("XT_TESSERACT", ["tesseract"], [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract"])
SOFFICE_EXE = _find_tool("XT_SOFFICE", ["soffice", "libreoffice"], [
    r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice", "/usr/bin/soffice", "/usr/bin/libreoffice"])

if os.name == "nt":
    _cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
else:
    _cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
CACHE_DIR = Path(os.environ.get("XT_CACHE", _cache_root / "extract-this" / "cache"))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Läsning av textfiler
# ---------------------------------------------------------------------------

# latin-1 avkodar ALLT utan fel, så den måste ligga sist. cp1252 före latin-1:
# svenska Windows-filer har ofta cp1252-specifika tecken (– ’ “ €) som latin-1
# tyst gör om till kontrolltecken.
ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")
_BOMS = ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
         (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))


def bom_encoding(head: bytes) -> str | None:
    """UTF-16/32 (t.ex. Excels "Unicode-text"-export) måste kännas igen på BOM –
    annars "lyckas" cp1252 och ger 'ÿþH\\x00e\\x00j…' med NUL-tecken."""
    for bom, enc in _BOMS:
        if head.startswith(bom):
            return enc
    return None


def read_text_file(path: Path) -> tuple[str, str]:
    data = Path(path).read_bytes()
    enc = bom_encoding(data[:4])
    if enc:
        return data.decode(enc, errors="replace"), enc
    for enc in ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8 (replace)"


# ---------------------------------------------------------------------------
# Textstädning
# ---------------------------------------------------------------------------

LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
# Ord som får stå kvar efter ett avstavningsbindestreck: "för- och nackdelar",
# "pre- and post-". Utan detta blir det "föroch".
_KEEP_HYPHEN_NEXT = {"och", "eller", "samt", "resp", "respektive", "and", "or", "to", "till"}
_HYPH_RE = re.compile(r"(\w)[-\u2010][ \t]*\n[ \t]*([a-zåäöéü]\w*)")  # bara EN radbrytning, inte styckesgräns
_SOFT_HYPH_RE = re.compile(r"\u00ad\s*")


def strip_control(s: str) -> str:
    """Ta bort kontrolltecken (t.ex. \\x08-ligaturartefakter i Paizo-PDF:er) men
    behåll radbrytning/tab."""
    return "".join(c for c in s if c in "\n\t" or unicodedata.category(c)[0] != "C")


def clean_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    s = _SOFT_HYPH_RE.sub("", s)  # "par\u00ad ticularly" -> "particularly"
    for k, v in LIGATURES.items():
        s = s.replace(k, v)
    s = strip_control(s)

    def _join(m: re.Match) -> str:
        nxt = m.group(2)
        if nxt.lower() in _KEEP_HYPHEN_NEXT:
            return f"{m.group(1)}- {nxt}"
        return m.group(1) + nxt

    s = _HYPH_RE.sub(_join, s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def dehyphenate_join(prev: str, nxt: str) -> str:
    """Slå ihop två rader i ett stycke med korrekt hantering av avstavning."""
    if prev.endswith(("-", "\u2010")) and nxt[:1].islower():
        first = re.match(r"\w+", nxt)
        if first and first.group().lower() in _KEEP_HYPHEN_NEXT:
            return prev + " " + nxt
        return prev[:-1] + nxt
    return prev + " " + nxt


# ---------------------------------------------------------------------------
# Språk- och skräpheuristik (stoppordsandel)
# ---------------------------------------------------------------------------

STOP_EN = set("the and you with this that for are from was have your not his her they which".split())
STOP_SV = set("och att det som en är på för med av till den har de inte om ett var men kan så jag eller vid från".split())
_WORD_RE = re.compile(r"[A-Za-zÅÄÖåäöÉéÜü]+")

# Kalibrerat i extract_weg v4: korrupt gammal OCR (WEG40141) 0,086, frisk engelska
# 0,12-0,17, tabelltung fan-PDF (Medical Sourcebook) 0,0997 med ren text.
# Gränsen 0,093 ligger mellan kända skräp- och äkta-punkter.
GARBAGE_STOPWORD_RATIO = 0.093


def stopword_stats(text: str) -> dict:
    words = [w.lower() for w in _WORD_RE.findall(text)]
    n = len(words)
    if not n:
        return {"words": 0, "en": 0.0, "sv": 0.0, "best": 0.0, "lang": None}
    en = sum(w in STOP_EN for w in words) / n
    sv = sum(w in STOP_SV for w in words) / n
    return {"words": n, "en": en, "sv": sv, "best": max(en, sv), "lang": "swe" if sv > en else "eng"}


_TOK_RE = re.compile(r"[A-Za-zÅÄÖåäöÉéÜüÆØæø]{3,}")
_known_cache: dict[str, bool] = {}

try:
    from wordfreq import zipf_frequency as _zipf
except ImportError:  # pragma: no cover
    _zipf = None

# Kalibrerat 2026-10-10 på 28 böcker (WEG, Darkstryder, EON): frisk text 0,91-0,99
# (även stat-tabeller: D6 Starships Stats 0,96 trots stoppordsandel 0,02!),
# korrupt OCR-lager (WEG40141) 0,695. Stoppord duger INTE som skräpdetektor.
GARBAGE_KNOWN = 0.85
# Dolt OCR-lager bakom en scan: egen OCR var lika bra eller bättre på varje
# testad sida (0,79->0,88, 0,857->0,934 kända ord, och lagret tappade ord).
SCAN_LAYER_MIN_KNOWN = 0.96


def known_word_ratio(text: str) -> tuple[float, int]:
    """(andel ord med längd >=3 som finns i engelsk eller svensk ordlista, antal ord)."""
    toks = _TOK_RE.findall(text)
    if not toks:
        return 0.0, 0
    if _zipf is None:  # reserv utan wordfreq: stoppordsheuristik omräknad
        st = stopword_stats(text)
        return min(1.0, st["best"] / GARBAGE_STOPWORD_RATIO * GARBAGE_KNOWN), len(toks)
    k = 0
    for w in toks:
        lw = w.lower()
        hit = _known_cache.get(lw)
        if hit is None:
            hit = _zipf(lw, "en") > 0 or _zipf(lw, "sv") > 0
            _known_cache[lw] = hit
        k += hit
    return k / len(toks), len(toks)


# Synlig (förlagssatt) text blir bara skräp vid trasig fontkodning -> kvot nära 0,1.
# Fantasy-svenska med latin (EON Mystik & Magi s.14: "hydrotropi", "Ebhron") når
# bara 0,82 per sida – får ALDRIG skickas till OCR. Därför lägre gräns här.
GARBAGE_KNOWN_VISIBLE = 0.60


def word_zipf(w: str) -> float:
    """Ordfrekvens (zipf, 0 = okänt, ~7 = 'och'/'the'), max av engelska och svenska."""
    if _zipf is None:
        return 0.0
    lw = w.lower()
    return max(_zipf(lw, "en"), _zipf(lw, "sv"))


def looks_like_garbage(text: str, min_words: int = 80, visible: bool = False) -> bool:
    """True om texten ser ut som korrupt OCR/trasig kodning (för få riktiga ord).
    visible=True: synlig text (renderingsläge 0) – kräver mycket lägre kvot."""
    r, n = known_word_ratio(text)
    return n >= min_words and r < (GARBAGE_KNOWN_VISIBLE if visible else GARBAGE_KNOWN)


def tesseract_lang(lang_hint: str | None) -> str:
    return {"swe": "swe+eng", "eng": "eng"}.get(lang_hint or "", "eng+swe")


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def md_cell(v) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\r", "").replace("\n", "<br>").strip()


def md_table(rows: list[list], header: list | None = None) -> str:
    rows = [list(r) for r in rows if any(md_cell(c) for c in r)]
    if header is None:
        if not rows:
            return ""
        header, rows = rows[0], rows[1:]
    ncol = max([len(header)] + [len(r) for r in rows]) if (header or rows) else 0
    if not ncol:
        return ""
    header = list(header) + [""] * (ncol - len(header))
    out = ["| " + " | ".join(md_cell(c) for c in header) + " |", "|" + "---|" * ncol]
    for r in rows:
        r = list(r) + [""] * (ncol - len(r))
        out.append("| " + " | ".join(md_cell(c) for c in r) + " |")
    return "\n".join(out)


def yaml_str(s) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Filer
# ---------------------------------------------------------------------------

def file_fingerprint(path: Path) -> str:
    """Snabb, stabil nyckel för OCR-cachen: storlek + sha1 av första/sista MB."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(1 << 20))
        if size > 1 << 20:  # slut + mitt: två scans med samma storlek och början ska inte krocka
            f.seek(max(1 << 20, size - (1 << 20)))
            h.update(f.read())
            f.seek(size // 2)
            h.update(f.read(1 << 16))
    return h.hexdigest()[:16]


def safe_filename(name: str, maxlen: int = 120) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:maxlen] or "fil"


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """'1-5,8,10-' -> 0-baserade index. None -> alla sidor."""
    if not spec:
        return list(range(page_count))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else page_count
            out.extend(range(a - 1, min(b, page_count)))
        else:
            n = int(part)
            if 1 <= n <= page_count:
                out.append(n - 1)
    return sorted(set(i for i in out if 0 <= i < page_count))
