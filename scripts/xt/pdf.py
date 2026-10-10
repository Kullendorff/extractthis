"""PDF -> markdown. Hybrid per sida:

  frisk textlagersida  -> pymupdf4llm (GNN-layout: läsordning, rubriker, tabeller,
                          sidhuvud/sidfot bort)
  tom/inskannad sida   -> Tesseract-OCR (ocr.py)
  korrupt textlager    -> Tesseract-OCR (gammal dålig OCR bakom scannade sidor,
                          upptäcks via stoppordsandel – syns INTE på fontstorlekar)
"""
from __future__ import annotations

import hashlib
import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore", message=".*fitz.*deprecated.*")

import pymupdf  # noqa: E402

from . import ocr as ocrmod  # noqa: E402
from .common import (  # noqa: E402
    GARBAGE_KNOWN, GARBAGE_KNOWN_VISIBLE, SCAN_LAYER_MIN_KNOWN, clean_text, file_fingerprint, known_word_ratio, log,
    looks_like_garbage, parse_pages, stopword_stats, tesseract_lang, word_zipf,
)

try:
    import pymupdf4llm
    HAVE_LAYOUT = True
except Exception:  # fel PyMuPDF-version ger ImportError vid import
    HAVE_LAYOUT = False

MIN_TEXT_CHARS = 50  # färre tecken än så på en sida med bild = inskannad sida
PAGE_CHUNK = 40      # sidor per block – håller minnet konstant för jätteböcker


@dataclass
class PageOut:
    page: int          # 1-baserad
    method: str        # layout | text | ocr | blank
    text: str
    conf: float | None = None


@dataclass
class Result:
    pages: list[PageOut]
    meta: dict
    warnings: list[str] = field(default_factory=list)
    outline: list = field(default_factory=list)
    images: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Analys
# ---------------------------------------------------------------------------

def open_pdf(path: Path, password: str | None = None):
    doc = pymupdf.open(path)
    if doc.needs_pass:
        if not doc.authenticate(password or ""):
            raise PermissionError("PDF:en är lösenordsskyddad (ange --password)")
    return doc


def classify_doc(doc, sample: int = 24) -> dict:
    """Dokumentnivå: 'digital' | 'hidden_ocr' | 'none' + nyckeltal (från WEG v4).

    hidden_ocr = textlager med 'utsmetade' fontstorlekar (gömd OCR bakom scan).
    none       = inget användbart textlager, eller korrupt (låg stoppordsandel)."""
    n = doc.page_count
    if not n:
        return {"class": "none", "chars_per_page": 0, "known_words": 0.0, "size_concentration": 0.0,
                "hidden_text": 0.0, "lang": None, "garbage_layer": False}
    step = max(1, n // sample)
    idx = list(range(0, n, step))
    sizes, text, hidden = [], [], []
    for i in idx:
        page = doc[i]
        text.append(page.get_text())
        hidden.append(hidden_text_ratio(page))
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for ln in b["lines"]:
                t = "".join(s["text"] for s in ln["spans"]).strip()
                if len(t) > 5:
                    sizes.append(round(max(s["size"] for s in ln["spans"]), 1))
    joined = "\n".join(text)
    cpp = len(joined.strip()) / len(idx)
    st = stopword_stats(joined)
    known, ntok = known_word_ratio(joined)
    mostly_hidden = sum(hidden) / len(hidden) > 0.5
    garbage = ntok > 200 and known < (GARBAGE_KNOWN if mostly_hidden else GARBAGE_KNOWN_VISIBLE)
    conc = (sum(c for _, c in Counter(sizes).most_common(4)) / len(sizes)) if sizes else 0.0
    if cpp < 100 or not sizes:
        cls = "none"
    elif garbage:
        cls = "none"
    elif mostly_hidden:
        cls = "scan+ocr_layer"  # osynligt OCR-lager bakom inskannade sidor
    elif conc >= 0.6:
        cls = "digital"
    else:
        cls = "hidden_ocr"
    return {"class": cls, "chars_per_page": round(cpp), "known_words": round(known, 3),
            "size_concentration": round(conc, 2), "hidden_text": round(sum(hidden) / len(hidden), 2),
            "lang": st["lang"] if st["words"] > 200 else None, "garbage_layer": garbage}


def hidden_text_ratio(page) -> float:
    """Andel textspann med renderingsläge 3 (osynlig text) = OCR-lager bakom en
    scan. Synlig text ovanpå en bakgrundsbild (vanligt i moderna rollspels-
    PDF:er, t.ex. WEG40120 med 76 % bildtäckning) har läge 0."""
    try:
        tr = page.get_texttrace()
    except Exception:
        return 0.0
    return sum(1 for t in tr if t.get("type") == 3) / len(tr) if tr else 0.0


def has_fullpage_image(page) -> bool:
    area = page.rect.width * page.rect.height or 1
    return any((i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]) >= 0.7 * area
               for i in page.get_image_info())


SCAN_STAMP_MAX_CHARS = 300  # så lite synlig text på en helsidesbild = stämpel/sidhuvud, inte innehåll


def is_scanned_page(page) -> bool:
    """Inskannad sida = bild över >=70 % av sidan OCH (ingen text, osynlig text,
    eller bara lite synlig text). Det sista fallet: skannad journalsida med en
    påförd stämpel "Inkom 2024-03-05 Dnr 123/2024 sida 3 av 12" – annars
    extraheras bara stämpeln och sidans innehåll försvinner tyst."""
    if not has_fullpage_image(page):
        return False
    txt = page.get_text().strip()
    return not txt or hidden_text_ratio(page) > 0.5 or len(txt) < SCAN_STAMP_MAX_CHARS


def page_plan(doc, indices, ocr_mode: str, doc_info: dict) -> dict[int, str]:
    """Bestäm metod per sida: 'text' (textlager) | 'ocr' | 'blank'.

    Ett textlager bakom en inskannad sida är någon annans OCR (skanner,
    sjukhusets dokumenthantering, Adobe …) av okänd kvalitet. Det litas bara på
    om det ser ut som riktigt språk; annars gör vi om OCR med egen pipeline."""
    plan = {}
    for i in indices:
        page = doc[i]
        txt = page.get_text().strip()
        if ocr_mode == "force":
            plan[i] = "ocr"
            continue
        if ocr_mode == "off":
            plan[i] = "text" if txt else "blank"
            continue
        scan = is_scanned_page(page)
        if len(txt) < MIN_TEXT_CHARS or (scan and len(txt) < SCAN_STAMP_MAX_CHARS):
            plan[i] = "ocr" if (scan or page.get_images()) else ("text" if txt else "blank")
        elif doc_info.get("garbage_layer") or looks_like_garbage(txt, visible=hidden_text_ratio(page) <= 0.5):
            plan[i] = "ocr"
        elif scan:
            kr, n = known_word_ratio(txt)
            plan[i] = "ocr" if (n < 40 or kr < SCAN_LAYER_MIN_KNOWN) else "text"
        else:
            plan[i] = "text"
    return plan


# ---------------------------------------------------------------------------
# Textlagersidor
# ---------------------------------------------------------------------------

_HYPH_SPACE_RE = re.compile(r"(\w)- ([a-zåäöéü]{2,})")
_KEEP = {"och", "eller", "samt", "and", "or", "to", "till"}


_STRAY_CAP_RE = re.compile(r"\b([A-ZÅÄÖ])(?=[A-ZÅÄÖ][a-zåäö])")
_LOWER_START_RE = re.compile(r"^([a-zåäö][a-zåäö]+)")


def _fix_dropcaps(md: str) -> str:
    """Anfang som hamnat fel i layoututdata: "essa regler … DSom spelledare"
    (EON Mystik & Magi s.33). Om ett stycke börjar med gemen och en ensam versal
    sitter fastklistrad framför ett ord längre fram, och versal+början ger ett
    känt ord ("D"+"essa" = "Dessa"), flyttas versalen tillbaka."""
    out = []
    for para in md.split("\n\n"):
        m = _LOWER_START_RE.match(para)
        # Villkor (annars blir "in the old days, PCs" -> "Pin … Cs"):
        # versal+första ordet är ett VANLIGT ord och klart vanligare än ordet
        # ensamt ("Dessa" zipf ~5 mot "essa" ~1), och ordet versalen sitter på är
        # ett känt ord UTAN den ("Som", inte "Cs"/"Vn").
        if m and len(m.group(1)) >= 2:
            first = m.group(1)
            for c in _STRAY_CAP_RE.finditer(para[:600]):
                rest = re.match(r"[A-ZÅÄÖ][a-zåäö]+", para[c.end():])
                if not rest:
                    continue
                joined = word_zipf(c.group(1) + first)
                if joined >= 3.0 and joined >= word_zipf(first) + 1.5 and word_zipf(rest.group(0)) >= 3.0:
                    para = c.group(1) + para[:c.start()] + para[c.end():]
                    break
        out.append(para)
    return "\n\n".join(out)


def _post_layout(md: str) -> str:
    md = clean_text(md)
    md = _fix_dropcaps(md)
    # pymupdf4llm slår ihop radbrutna rader med mellanslag -> "par- ticularly"
    md = _HYPH_SPACE_RE.sub(lambda m: m.group(0) if m.group(2) in _KEEP else m.group(1) + m.group(2), md)
    md = re.sub(r"^\s*[•▪●◦]\s+", "- ", md, flags=re.M)
    md = re.sub(r"[ \t]+$", "", md, flags=re.M)
    return md.strip()


def layout_pages(doc, indices: list[int], batch: int = 25, fallbacks: list | None = None) -> dict[int, str]:
    """pymupdf4llm per omgång. Kraschar layoutmotorn på en omgång (t.ex.
    tabelldetektion på en trasig sida) tas just den omgången med plain-motorn –
    en dålig sida ska inte fälla en hel bok."""
    out: dict[int, str] = {}
    for s in range(0, len(indices), batch):
        chunk = indices[s:s + batch]
        try:
            res = pymupdf4llm.to_markdown(doc, pages=chunk, page_chunks=True, use_ocr=False,
                                          header=False, footer=False, write_images=False)
            for c in res:
                out[c["metadata"]["page_number"] - 1] = _post_layout(c["text"])
        except Exception as e:
            log(f"    [!] layoutmotorn föll på s. {chunk[0] + 1}-{chunk[-1] + 1} ({type(e).__name__}: {e}) – plain")
            if fallbacks is not None:
                fallbacks.append((chunk[0] + 1, chunk[-1] + 1))
            out.update(plain_pages(doc, chunk))
    return out


def plain_pages(doc, indices: list[int]) -> dict[int, str]:
    return {i: clean_text(doc[i].get_text("text", sort=True)) for i in indices}


# ---------------------------------------------------------------------------
# Återkommande sidhuvuden/sidfötter (för OCR- och plain-sidor)
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d+")


def _edge_key(line: str, page_idx: int):
    """Nyckel för att känna igen sidkrom. Sidnummerrad = EXAKT ett tal som följer
    sidindex med konstant offset ("Sida 13" på s.12, "Sida 14" på s.13 -> samma
    nyckel). Allt annat jämförs exakt. Att bara ersätta alla siffror med '#'
    (förra versionen) raderade "Datum: 2024-03-04" och "Blodtryck: 123/80" från
    varje journalsida utom den första – tyst dataförlust."""
    s = line.strip()
    nums = _NUM_RE.findall(s)
    if len(nums) == 1 and len(s) <= 40 and len(nums[0]) <= 4:
        return ("pagenum", _NUM_RE.sub("#", s.lower()), int(nums[0]) - page_idx)
    return ("exact", s.lower())


def strip_repeated_edges(pages: dict[int, str], edge: int = 2) -> dict[int, str]:
    """Generaliserad pdfio.strip_chrome: rader i sidans topp/botten som återkommer
    på många sidor (bokens titel, kapitelnamn, 'sida 12') tas bort – första
    förekomsten behålls."""
    if len(pages) < 6:
        return pages
    cnt: Counter = Counter()
    for i, t in pages.items():
        ls = [l for l in t.splitlines() if l.strip()]
        if len(ls) <= 3:
            continue
        for l in set(ls[:edge] + ls[-edge:]):
            cnt[_edge_key(l, i)] += 1
    limit = max(3, int(0.3 * len(pages)))
    rep = {k for k, v in cnt.items() if v >= limit and len(k[1]) <= 80}
    if not rep:
        return pages
    out, seen = {}, set()
    for i in sorted(pages):
        ls = pages[i].splitlines()
        nonempty = [j for j, l in enumerate(ls) if l.strip()]
        edges = set(nonempty[:edge] + nonempty[-edge:]) if len(nonempty) > 3 else set()
        keep = []
        for j, l in enumerate(ls):
            k = _edge_key(l, i)
            if j in edges and k in rep:
                # Första förekomsten behålls – i journaler/brev bär sidhuvudet
                # identitet (namn, personnummer, enhet) som inte får försvinna.
                if k in seen:
                    continue
                seen.add(k)
            keep.append(l)
        out[i] = "\n".join(keep).strip()
    return out


# ---------------------------------------------------------------------------
# Bilder
# ---------------------------------------------------------------------------

def extract_images(doc, indices, out_dir: Path, stem: str, min_px: int = 80) -> list[dict]:
    """Bildobjekt per sida (från WEG v4): hoppar små ikoner och helsidesrastrar
    (= inskannade sidor), dedupar på innehållshash, JPX -> PNG."""
    out_dir.mkdir(parents=True, exist_ok=True)
    seen, recs = set(), []
    for i in indices:
        page = doc[i]
        area = page.rect.width * page.rect.height
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref")
            if not xref:
                continue
            x0, y0, x1, y1 = info["bbox"]
            w, h = x1 - x0, y1 - y0
            if w < min_px or h < min_px or w * h > 0.7 * area:
                continue
            try:
                base = doc.extract_image(xref)
            except Exception:
                continue
            if not base:
                continue
            raw, ext = base["image"], base["ext"]
            if ext in ("jpx", "jp2", "j2k") or base.get("smask"):
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if base.get("smask"):
                        pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, base["smask"]))
                    if pix.n - pix.alpha > 3:
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                    raw, ext = pix.tobytes("png"), "png"
                except Exception:
                    pass
            hsh = hashlib.md5(raw).hexdigest()[:12]
            if hsh in seen:
                continue
            seen.add(hsh)
            fn = f"{stem}_s{i + 1:03d}_{len(recs) + 1:03d}.{ext}"
            (out_dir / fn).write_bytes(raw)
            recs.append({"file": fn, "page": i + 1, "w": round(w), "h": round(h), "bytes": len(raw)})
    return recs


def render_pages(doc, indices, out_dir: Path, stem: str, dpi: int = 150) -> list[dict]:
    """Hela sidan som JPEG – för kartor/deckplans där sidan ÄR innehållet.
    JPEG q85, inte PNG: scannat brus komprimerar dåligt lossless (~10 MB/sida)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    recs = []
    for i in indices:
        pix = doc[i].get_pixmap(dpi=dpi)
        fn = f"{stem}_s{i + 1:03d}_sida.jpg"
        (out_dir / fn).write_bytes(pix.tobytes("jpeg", jpg_quality=85))
        recs.append({"file": fn, "page": i + 1, "w": pix.width, "h": pix.height})
    return recs


def detect_scan_lang(doc, ocr_idx) -> str | None:
    """Ren scan utan textlager: snabb-OCR:a upp till 2 sidor med eng+swe och
    avgör språket på stoppord, så att resten körs med rätt språkmodell."""
    import io

    from PIL import Image

    sample = ocr_idx[: min(2, len(ocr_idx))] if len(ocr_idx) < 4 else [ocr_idx[1], ocr_idx[len(ocr_idx) // 2]]
    text = []
    for i in sample:
        r = doc[i].rect
        pix = doc[i].get_pixmap(dpi=max(50, min(200, int(2500 * 72 / max(r.width, r.height, 1)))),
                                colorspace=pymupdf.csGRAY)
        r = ocrmod.ocr_image(Image.open(io.BytesIO(pix.tobytes("png"))), "eng+swe")
        text.append(" ".join(w["t"] for w in r["words"]))
    st = stopword_stats(" ".join(text))
    if st["words"] < 40 or st["best"] < 0.03:
        return None  # för lite/oläsligt (handskrift?) -> behåll eng+swe
    return st["lang"]


# ---------------------------------------------------------------------------
# Huvudfunktion
# ---------------------------------------------------------------------------

def extract_pdf(path: Path, *, pages: str | None = None, ocr: str = "auto", lang: str = "auto",
                engine: str = "auto", dpi: int = 300, images_dir: Path | None = None,
                page_images: bool = False, password: str | None = None, effort: str = "best",
                review_dir: Path | None = None, mark_uncertain: bool = False) -> Result:
    path = Path(path)
    doc = open_pdf(path, password)
    try:  # stäng alltid – annars förblir källfilen låst (Windows) resten av en batch
        return _extract_open(doc, path, pages=pages, ocr=ocr, lang=lang, engine=engine, dpi=dpi,
                             images_dir=images_dir, page_images=page_images, effort=effort,
                             review_dir=review_dir, mark_uncertain=mark_uncertain)
    finally:
        doc.close()


def _extract_open(doc, path: Path, *, pages, ocr, lang, engine, dpi, images_dir, page_images,
                  effort, review_dir, mark_uncertain) -> Result:
    idx = parse_pages(pages, doc.page_count)
    info = classify_doc(doc)
    warns: list[str] = []
    layout_fallbacks: list = []
    ocr_errors: list[int] = []

    if engine == "auto":
        engine = "layout" if HAVE_LAYOUT else "plain"
    if engine == "layout" and not HAVE_LAYOUT:
        warns.append("pymupdf4llm saknas/fel version – använder plain-motorn")
        engine = "plain"

    # Sidblock: minnet hålls konstant oavsett boklängd – efter varje block sparas
    # bara färdig text per sida, inga OCR-ordlistor eller bilder.
    plan: dict[int, str] = {}
    texts: dict[int, str] = {}
    plain_md: dict[int, str] = {}
    ocr_md: dict[int, str] = {}
    confs: dict[int, float] = {}
    review: dict[int, dict] = {}
    fixes: dict[int, dict] = {}
    blanks: set[int] = set()
    tl, bh, fp = None, None, None
    n_ocr_wanted = 0
    big = len(idx) > PAGE_CHUNK
    for s in range(0, len(idx), PAGE_CHUNK):
        chunk = idx[s:s + PAGE_CHUNK]
        cplan = page_plan(doc, chunk, ocr, info)
        plan.update(cplan)
        t_ids = [i for i in chunk if cplan[i] == "text"]
        o_ids = [i for i in chunk if cplan[i] == "ocr"]
        n_ocr_wanted += len(o_ids)
        if t_ids:
            if engine == "layout":
                texts.update(layout_pages(doc, t_ids, fallbacks=layout_fallbacks))
            else:
                plain_md.update(plain_pages(doc, t_ids))
        if o_ids and ocrmod.HAVE_OCR:
            if tl is None:
                hint = info.get("lang")
                if lang == "auto" and hint is None:
                    hint = detect_scan_lang(doc, o_ids)  # bara OCR-sidor, inte textsidor
                    info["lang"] = hint
                tl = tesseract_lang(hint) if lang == "auto" else lang
                fp = file_fingerprint(path)
            res = ocrmod.ocr_pdf_pages(doc, o_ids, fp, tl, dpi, effort=effort,
                                       review_dir=review_dir, review_stem=path.stem)
            if bh is None:  # rubrikkalibrering från första blocket MED brödtext, återanvänds sedan
                bh = ocrmod.body_height([ocrmod.paragraphs_from_ocr(r) for r in res.values()], default=None)
            ocr_md.update(ocrmod.ocr_pages_to_markdown(res, mark_uncertain, body_h=bh))
            for i, r in res.items():
                if r.get("error"):
                    ocr_errors.append(i + 1)
                if r.get("blank"):
                    blanks.add(i)
                    continue
                confs[i] = ocrmod.mean_conf(r)
                if ocrmod.needs_review(r):
                    review[i + 1] = {"conf": round(confs[i], 1), "image": r.get("review_image"),
                                     "reason": ocrmod.review_reason(r)}
                if r.get("rotated") or r.get("skew"):
                    fixes[i + 1] = {"rotated": r.get("rotated"), "skew": r.get("skew")}
            del res
        pymupdf.TOOLS.store_shrink(100)  # PyMuPDF:s bildcache växer annars till ~250 MB
        if big:
            log(f"    sidor {min(s + PAGE_CHUNK, len(idx))}/{len(idx)} klara"
                + (f" (OCR hittills: {len(confs) + len(blanks)})" if n_ocr_wanted else ""))

    texts.update(strip_repeated_edges(plain_md))
    texts.update(strip_repeated_edges(ocr_md))
    text_idx = [i for i in idx if plan[i] == "text"]
    ocr_idx = [i for i in idx if plan[i] == "ocr"]
    if ocr_idx and not ocrmod.HAVE_OCR:
        warns.append(f"{len(ocr_idx)} sidor behöver OCR men pytesseract saknas")
        ocr_idx = []
    if review:
        warns.append(f"{len(review)} OCR-sidor behöver visuell granskning (låg konfidens/handskrift?): "
                     f"s. {sorted(review)[:12]}")
    if ocr_errors:
        warns.append(f"OCR misslyckades på {len(ocr_errors)} sidor (s. {ocr_errors[:12]}) – granska dem")
    if layout_fallbacks:
        warns.append(f"Layoutmotorn föll på s. {layout_fallbacks} – de sidorna togs som råtext")

    out_pages = []
    for i in idx:
        m = plan[i]
        method = "layout" if (m == "text" and engine == "layout") else ("text" if m == "text" else m)
        if m == "ocr" and (i not in texts or i in blanks):
            method = "blank"
        out_pages.append(PageOut(i + 1, method, texts.get(i, ""), confs.get(i)))

    imgs: list[dict] = []
    if images_dir is not None:
        imgs = (render_pages(doc, idx, images_dir, path.stem) if page_images
                else extract_images(doc, idx, images_dir, path.stem))

    if info.get("garbage_layer"):
        warns.append(f"Textlagret är korrupt gammal OCR (kända ord {info['known_words']}) – OCR:at om")
    if info["class"] == "scan+ocr_layer" and text_idx:
        warns.append(f"{len(text_idx)} inskannade sidor använder befintligt OCR-lager (såg OK ut) – "
                     "kör med --ocr force om texten ser fel ut")
    if info["class"] == "hidden_ocr" and engine == "layout":
        warns.append("Textlagret ser ut som gömd OCR (utsmetade fontstorlekar) – rubriker kan vara opålitliga")

    meta = {
        "pages_total": doc.page_count, "pages_extracted": len(idx), "text_layer": info["class"],
        "engine": engine, "ocr_pages": len(ocr_idx), "lang": info.get("lang"),
        "title": (doc.metadata or {}).get("title") or None,
        "author": (doc.metadata or {}).get("author") or None,
        "is_form": bool(doc.is_form_pdf),
        "review": review, "page_fixes": fixes,
    }
    return Result(out_pages, meta, warns, doc.get_toc(), imgs)
