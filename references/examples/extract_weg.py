#!/usr/bin/env python3
"""
WEG Star Wars D6 PDF -> Obsidian Markdown extraktor v4
========================================================

Generaliserad batch-extraktor för hela _source_pdfs/-biblioteket. Ersätter v3,
som var hårdkodad för en enda bok (Tatooine Manhunt / WEG40005) och kraschade
på PDF:er utan textlager.

Textlagerklassificering (viktigt — tre klasser, inte två):
  "digital"    — riktigt textlager med tydliga, diskreta fontstorlekar.
  "hidden_ocr" — textlager finns (ofta en gömd OCR-text bakom en scannad
                 sida) men storlekarna är "utsmetade": rubrik och brödtext
                 kan skilja så lite som ~1,08x i storlek. Fasta kvot-
                 trösklar (v3:s ansats) hittar då NOLL rubriker. Måste
                 hanteras med samma poängbaserade heuristik som OCR-sidor.
  "none"       — inget användbart textlager alls -> Tesseract-OCR.

Rubrikdetektion är därför enhetlig: alla tre klasser producerar samma
Line-representation (text, höjd, fet, y, konfidens) och går genom samma
poängfunktion (score_heading) istället för v3:s fasta storlekskvoter.

Övriga nyheter mot v3:
- BOOKS-registry längst ner: en post per käll-PDF, kör valfri delmängd via
  --code, eller allt via --all. --probe kör klassificering + rubrikräkning
  UTAN att skriva något, för kalibrering innan en riktig körning.
- OCR-fallback (pytesseract + Tesseract) med resumable, per-sida cache i
  tools/.ocr_cache/.
- Windows-säkra filnamn.
- Skriver direkt i vaultens befintliga platta mappar (aventyr/ eller regler/,
  assets/, image_notes/) — inte det per-bok-träd som v3-erans
  WORKFLOW_DOKUMENTATION.md föreslog.
- Bildpolicy: helsidesrastrar hoppas över som default. För böcker där
  helsidan ÄR innehållet (Stock Ships deckplans) renderas sidan som en
  sammansatt PNG istället för att extrahera de underliggande MRC-lagren
  (bakgrund/förgrund/mask) som separata, trasiga bildobjekt.
- Bildnot skapas bara när bilden har caption eller kontexttext.
- safe_write(): vägrar skriva över en befintlig fil utan --force. De fem
  regler/WEG*.md-filer som ERSÄTTS av denna batch (se MANIFEST.md /
  Johans beslut) körs explicit med --force; allt annat är skyddat mot
  oavsiktlig overwrite.
- Drop-cap-fix körs FÖRE skräpfiltreringen (v3:s ordning gjorde fixen till
  död kod, eftersom is_junk() kastade bort ensamma versaler innan mergen
  någonsin såg dem) och har en guard mot att dubblera bokstaven
  ("TTatooine"-buggen).

Beroenden: pymupdf, pytesseract (+ Tesseract OCR installerat på systemet).
Tesseract-sökväg på den här maskinen:
    C:\\Program Files\\Tesseract-OCR\\tesseract.exe   (ligger inte i PATH)

Användning:
    python extract_weg.py --probe --all              # klassificera, skriv inget
    python extract_weg.py --code WEG40065 WEG40150
    python extract_weg.py --all
    python extract_weg.py --code WEG40095 --force     # tillåt overwrite av drive_ocr-fil
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import pymupdf

try:
    import pytesseract
    _TESS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_TESS_PATH):
        pytesseract.pytesseract.tesseract_cmd = _TESS_PATH
    HAVE_OCR = True
except ImportError:
    HAVE_OCR = False

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ===========================================================================
# Vault-sökvägar
# ===========================================================================

TOOLS_DIR = Path(__file__).resolve().parent
VAULT_ROOT = TOOLS_DIR.parent
PDF_DIR = VAULT_ROOT / "_source_pdfs"
ASSETS_DIR = VAULT_ROOT / "assets"
IMAGE_NOTES_DIR = VAULT_ROOT / "image_notes"
OCR_CACHE_DIR = TOOLS_DIR / ".ocr_cache"
METADATA_DIR = TOOLS_DIR / "extraction_metadata"

SYSTEM = "Star Wars D6 (WEG)"


def safe_write(path: Path, content, force=False, binary=False):
    if path.exists() and not force:
        print(f"    [SKIP] {path.name} finns redan (kör med --force för att skriva över)")
        return False
    if binary:
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return True


# ===========================================================================
# Textstädning
# ===========================================================================

JUNK_PATTERNS = [
    re.compile(r'^[\-\s]*STAR[\.\s]*$', re.IGNORECASE),
    re.compile(r'^W\s*AR[TS]?\s*$', re.IGNORECASE),
    re.compile(r'^[■▪●◆\s\.\-\|]+$'),
    re.compile(r'^[\d\s\.\-]+$'),
    re.compile(r'^[^a-zA-Z]{2,}$'),
    re.compile(r'^\s*[ivxlcdm]+\s*$', re.IGNORECASE),
]


def is_junk(text, min_len=3):
    t = text.strip()
    if len(t) < min_len:
        return True
    if any(p.match(t) for p in JUNK_PATTERNS):
        return True
    # Dekorativa separatorer/dingbats ("----<O>----", "~-a", "••1••••••••'II")
    # har för låg andel bokstäver för att vara riktig text.
    non_space = re.sub(r'\s', '', t)
    if non_space:
        letters = sum(1 for c in non_space if c.isalpha())
        if letters / len(non_space) < 0.5:
            return True
    return False


def clean_text(text):
    """Städa mjuka bindestreck, avstavning över radbrytning, dubbla mellanslag,
    ligaturer och den escapade markdown som drive_ocr-filerna led av."""
    t = unicodedata.normalize("NFC", text)
    t = t.replace("\u00ad", "")                 # soft hyphen
    t = t.replace("\ufb01", "fi").replace("\ufb02", "fl")
    t = re.sub(r'(\w)-\s+(\w)', r'\1\2', t)      # "an­ nouncing" -> "announcing"
    t = re.sub(r'^\\([#*_>`\-])', r'\1', t)      # "\# " -> "# " (escapad markdown)
    t = re.sub(r'\s{2,}', ' ', t)
    return t.strip()


def sanitize_filename(name):
    """Gör en sträng Windows-filnamnssäker (kolon m.fl. förbjudna tecken)."""
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.replace(" - ", "_").replace(", ", "_").replace(" ", "_")
    name = re.sub(r'_+', '_', name).strip('_')
    return name


# ===========================================================================
# Textlagerklassificering
# ===========================================================================

_STOPWORD_RE = re.compile(r'\b(the|and|you|with|this|that|for|are|from|was|have|your|not)\b',
                           re.IGNORECASE)
_WORD_RE = re.compile(r'[A-Za-z]+')

# Under denna kvot räknas textlagret som korrupt skräp (se motivering nedan)
# oavsett hur mycket text eller hur diskreta fontstorlekar det innehåller.
# Kalibrerad mot uppmätta värden: korrupt (WEG40141) låg på 0,086; friska
# böcker låg på 0,121-0,165 — men "Medical Sourcebook" (fan-supplement,
# tabell-/listtungt format, färre bindeord per sida) mätte 0,0997 med genuint
# ren text. 0,10 var för nära den siffran och gav en falsk positiv. 0,093
# ligger mitt emellan de kända punkterna (0,086 skräp / 0,0997 äkta).
GARBAGE_STOPWORD_RATIO = 0.093


def classify_text_layer(doc, sample=20):
    """'digital' | 'hidden_ocr' | 'none'

    'none' betyder "lita inte på det inbäddade textlagret, kör Tesseract-OCR
    istället" — av två olika anledningar:
      1. Inget textlager finns alls (ren scan).
      2. Textlagret FINNS och är rikligt, men är i sig självt korrupt gammal
         OCR (ett vanligt problem i skannade WEG-PDF:er från tidigt 2000-tal,
         t.ex. WEG40141: "COYel' Deilgn & lIvenor~", "Sock YT·1.:l00
         TlcrtSpon"). Detta upptäcks INTE av fontstorleksanalys — WEG40141
         har perfekt diskreta fontstorlekar, texten bakom dem är bara fel.
         Detekteras istället via andelen vanliga engelska stoppord (the/and/
         you/...) bland extraherade "ord": frisk engelsk text ligger på
         ~12–17%, den uppmätta korrupta filen låg på 8,6%.
    """
    n = min(sample, doc.page_count)
    if n == 0:
        return "none"
    step = max(1, doc.page_count // n)
    sizes, total_chars, total_words, total_stops = [], 0, 0, 0
    for i in range(0, doc.page_count, step):
        page = doc[i]
        txt = page.get_text().strip()
        total_chars += len(txt)
        total_words += len(_WORD_RE.findall(txt))
        total_stops += len(_STOPWORD_RE.findall(txt))
        d = page.get_text("dict")
        for block in d["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                t = "".join(s["text"] for s in line["spans"]).strip()
                if len(t) > 5:
                    sizes.append(round(max(s["size"] for s in line["spans"]), 1))

    n_samples = len(range(0, doc.page_count, step))
    chars_per_page = total_chars / max(1, n_samples)
    if chars_per_page < 100:
        return "none"

    if total_words > 200:
        stopword_ratio = total_stops / total_words
        if stopword_ratio < GARBAGE_STOPWORD_RATIO:
            return "none"

    if not sizes:
        return "none"
    hist = Counter(sizes)
    top4 = sum(c for _, c in hist.most_common(4))
    concentration = top4 / len(sizes)
    # Diskreta storlekar (digital) samlar de flesta tecken i ett fåtal bins;
    # "utsmetade" storlekar (hidden_ocr) sprider sig över många närliggande värden.
    if concentration >= 0.6:
        return "digital"
    return "hidden_ocr"


# ===========================================================================
# Rå radextraktion -> gemensam Line-representation
# Line: {text, h (storlek/höjd), y, bold, conf}
# ===========================================================================

def extract_lines_native(doc):
    """Alla rader (INGEN junk-filtrering här — det sker efter drop-cap-mergen)."""
    pages = []
    for pn, page in enumerate(doc):
        d = page.get_text("dict")
        lines_out = []
        for block in d["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                spans = line["spans"]
                text = "".join(s["text"] for s in spans)
                if not text.strip():
                    continue
                mx = max(s["size"] for s in spans)
                bold = any("bold" in s["font"].lower() or "black" in s["font"].lower() for s in spans)
                lines_out.append({
                    "text": text, "h": mx, "y": line["bbox"][1],
                    "x": line["bbox"][0], "bold": bold, "conf": 100.0,
                })
        pages.append(lines_out)
    return pages


def _ocr_cache_path(code, page_no):
    return OCR_CACHE_DIR / code / f"p{page_no:04d}.json"


def _ocr_page(doc, page_no, code, dpi, lang):
    cache_path = _ocr_cache_path(code, page_no)
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    if not HAVE_OCR:
        raise RuntimeError("pytesseract är inte installerat — kan inte OCR:a.")

    page = doc[page_no - 1]
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    from PIL import Image
    import io as _io
    img = Image.open(_io.BytesIO(pix.tobytes("png")))

    tsv = pytesseract.image_to_data(img, lang=lang, config="--oem 3 --psm 3",
                                     output_type=pytesseract.Output.DICT)
    words = []
    for i in range(len(tsv["text"])):
        txt = tsv["text"][i].strip()
        try:
            conf = float(tsv["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if not txt or conf < 30:
            continue
        words.append({
            "text": txt, "left": tsv["left"][i], "top": tsv["top"][i],
            "height": tsv["height"][i], "conf": conf,
            "line_num": tsv["line_num"][i], "block_num": tsv["block_num"][i],
            "par_num": tsv["par_num"][i],
        })
    result = {"page": page_no, "words": words}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def extract_lines_ocr(doc, code, dpi, lang):
    print(f"    OCR: {doc.page_count} sidor @ {dpi} dpi (cache: {OCR_CACHE_DIR / code})")
    pages = []
    for pn in range(1, doc.page_count + 1):
        result = _ocr_page(doc, pn, code, dpi, lang)
        grouped = {}
        for w in result["words"]:
            key = (w["block_num"], w["par_num"], w["line_num"])
            grouped.setdefault(key, []).append(w)
        lines_out = []
        # Sortera på Tesseracts EGNA (block_num, par_num, line_num) — inte på
        # absolut y-position. Sortering på y-position blandar ihop vänster-
        # och högerspalt i tvåspaltiga sourcebooks (en rad längst upp i höger
        # spalt kan ha lägre "top" än en rad längre ner i vänster spalt).
        # Tesseracts blockindelning är kolumnmedveten vid PSM 3 (auto-segmentering).
        for key in sorted(grouped):
            ws = sorted(grouped[key], key=lambda w: w["left"])
            text = " ".join(w["text"] for w in ws)
            heights = [w["height"] for w in ws]
            confs = [w["conf"] for w in ws]
            lines_out.append({
                "text": text, "h": sum(heights) / len(heights), "y": min(w["top"] for w in ws),
                "x": min(w["left"] for w in ws), "bold": False,
                "conf": sum(confs) / len(confs),
            })
        pages.append(lines_out)
        if pn % 25 == 0 or pn == doc.page_count:
            print(f"      ...sida {pn}/{doc.page_count}")
    return pages


# ===========================================================================
# Drop cap-fix — körs FÖRE junk-filtrering, med guard mot dubblering
# ===========================================================================

def merge_dropcaps(pages_lines):
    for lines in pages_lines:
        i = 0
        while i < len(lines) - 1:
            curr, nxt = lines[i], lines[i + 1]
            ct, nt = curr["text"].strip(), nxt["text"]
            if (len(ct) == 1 and ct.isalpha()
                    and curr["h"] > nxt["h"] * 1.35
                    and nt and nt[0].islower()
                    and abs(curr["y"] - nxt["y"]) < max(20, curr["h"])
                    # guard: hoppa om nästa rad redan börjar med samma bokstav
                    # (skulle annars kunna dubblera den, t.ex. "TT...")
                    and not nt.lower().startswith(ct.lower())):
                nxt["text"] = ct + nt
                lines.pop(i)
                continue
            i += 1
    return pages_lines


def clean_pages(pages_lines):
    """Junk-filtrering + textstädning. Körs EFTER drop-cap-mergen."""
    out = []
    for lines in pages_lines:
        cleaned = []
        for l in lines:
            t = clean_text(l["text"])
            if not t or is_junk(t):
                continue
            l2 = dict(l)
            l2["text"] = t
            cleaned.append(l2)
        out.append(cleaned)
    return out


# ===========================================================================
# Rubrikdetektion — enhetlig poängmodell för digital/hidden_ocr/OCR
# ===========================================================================

def calibrate_body_height(pages_lines):
    heights = []
    for lines in pages_lines:
        for l in lines:
            if len(l["text"]) > 15:
                heights.append(round(l["h"] * 2) / 2)  # 0.5-bins
    if not heights:
        return 10.0
    return Counter(heights).most_common(1)[0][0]


def _ratio_tier(ratio):
    """Absoluta (ej percentil-skalade) höjdkvot-trösklar. En percentil-skalad
    modell visade sig ge falska positiva på nästan varenda kroppstextrad,
    eftersom PDF-radbrytning gör att så gott som alla rader redan är 'korta'
    och saknar avslutande skiljetecken — det är kolumnbredden som styr, inte
    om raden är en rubrik. Höjden är det enda pålitliga primärsignalen."""
    if ratio >= 1.6:
        return 3.5
    if ratio >= 1.3:
        return 2.2
    if ratio >= 1.15:
        return 1.0
    return 0.0


def score_heading(line, body_h, gap_above=0.0):
    s = 0.0
    ratio = line["h"] / body_h if body_h else 1.0
    tlen = len(line["text"])
    short = tlen <= 50

    s += _ratio_tier(ratio)
    if line.get("bold") and short:
        s += 1.0
    if line["text"].isupper() and 3 < tlen <= 80:
        s += 1.2
    if gap_above > 1.4 * body_h:
        s += 1.0
    if short:
        s += 0.3
    if not line["text"].rstrip().endswith((".", ",", ";", ":")):
        s += 0.2
    else:
        s -= 0.5
    if re.search(r'\d\s*$', line["text"]) and tlen < 8:
        s -= 1.5  # trolig sidnummer
    if line.get("conf", 100) < 55:
        s -= 2.0  # trolig OCR-skräp
    return s


def classify_headings(pages_lines):
    """Returnerar pages_data i samma form som v3 (level: h1/h2/body) via poängmodell.
    Tröskeln är medvetet hög (>=3.0) — se _ratio_tier-kommentaren om varför
    svaga signaler (kort rad, inget skiljetecken) inte får avgöra ensamma."""
    body_h = calibrate_body_height(pages_lines)
    H2_CUT, H1_CUT = 3.0, 5.5

    pages_data = []
    for pn, lines in enumerate(pages_lines, start=1):
        out = []
        prev_bottom = None
        for l in lines:
            gap = (l["y"] - prev_bottom) if prev_bottom is not None else 0.0
            prev_bottom = l["y"] + l["h"]
            sc = score_heading(l, body_h, gap_above=gap)
            if sc >= H1_CUT:
                level = "h1"
            elif sc >= H2_CUT:
                level = "h2"
            else:
                level = "body"
            out.append({"text": l["text"], "level": level, "y": l["y"]})
        pages_data.append({"page": pn, "lines": out})
    return pages_data, body_h


# ===========================================================================
# Bildextraktion
# ===========================================================================

def extract_images_normal(doc, code, force):
    """Standardläge: extrahera enskilda bildobjekt, hoppa helsidesrastrar."""
    records = []
    seen_hashes = set()
    counter = 0

    for pn in range(doc.page_count):
        page = doc[pn]
        page_area = page.rect.width * page.rect.height
        img_infos = page.get_image_info(xrefs=True)
        text_blocks = [(b[0], b[1], b[2], b[3], b[4].strip())
                       for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]

        for info in img_infos:
            xref = info.get("xref")
            if not xref:
                continue
            bbox = info["bbox"]
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if w < 80 or h < 80:
                continue
            if (w * h) > 0.7 * page_area:
                continue  # helsidesraster, hoppas i standardläge

            try:
                base = doc.extract_image(xref)
            except Exception as e:
                print(f"    [!] kunde inte extrahera xref={xref} s.{pn+1}: {e}")
                continue
            if not base:
                continue
            raw, ext = base["image"], base["ext"]
            if ext in ("jpx", "jp2", "j2k"):
                try:
                    pix = pymupdf.Pixmap(raw)
                    raw = pix.tobytes("png")
                    ext = "png"
                except Exception:
                    pass

            hsh = hashlib.md5(raw).hexdigest()[:16]
            if hsh in seen_hashes:
                continue
            seen_hashes.add(hsh)
            counter += 1

            iy0, iy1 = bbox[1], bbox[3]
            above, below, caption = [], [], []
            for tx0, ty0, tx2, ty1, txt in text_blocks:
                if iy1 < ty0 < iy1 + 40:
                    caption.append(txt)
                elif iy0 - 80 < ty1 < iy0:
                    above.append(txt)
                elif iy1 < ty0 < iy1 + 120:
                    below.append(txt)

            ctx = caption or above or below
            desc = _describe_image(ctx, pn + 1, counter, w, h)
            safe = re.sub(r'[^\w\-]', '_', desc)[:45].strip('_')
            fname = f"{code}_p{pn+1:03d}_{safe}.{ext}"
            safe_write(ASSETS_DIR / fname, raw, force=True, binary=True)  # unikt filnamn -> alltid ok

            significant = (w > 200 and h > 200) or (w * h > 80000)
            records.append({
                "filename": fname, "page": pn + 1, "width": w, "height": h,
                "size_kb": round(len(raw) / 1024, 1), "description": desc,
                "caption": " ".join(caption)[:200], "context_above": " ".join(above)[:200],
                "context_below": " ".join(below)[:200], "significant": significant,
            })
    return records


def extract_images_full_pages(doc, code, dpi=200):
    """Alternativt läge (full_page_images=True): rendera hela sidan som en
    sammansatt JPEG. Undviker att extrahera MRC-lagerobjekt (bakgrund/förgrund/
    mask) som separata, trasiga halvbilder — det som gav de kända
    jpeg+png-dubbletterna i v3:s output.

    JPEG, inte PNG: en scannad sida är fotografiskt brus, inte platta färger —
    lossless PNG komprimerar sådant dåligt (~9,6 MB/sida uppmätt på WEG40150).
    JPEG q85 ger ~0,7-0,9 MB/sida med fullt godtagbar läsbarhet för deckplans."""
    records = []
    for pn in range(doc.page_count):
        page = doc[pn]
        pix = page.get_pixmap(dpi=dpi)
        raw = pix.tobytes("jpeg", jpg_quality=85)
        fname = f"{code}_p{pn+1:03d}_helsida.jpeg"
        safe_write(ASSETS_DIR / fname, raw, force=True, binary=True)
        records.append({
            "filename": fname, "page": pn + 1, "width": pix.width, "height": pix.height,
            "size_kb": round(len(raw) / 1024, 1), "description": f"helsida_s{pn+1}",
            "caption": "", "context_above": "", "context_below": "", "significant": True,
        })
    return records


def _describe_image(ctx, pg, n, w, h):
    if not ctx:
        if w > h * 2:
            return f"banner_s{pg}"
        return f"bild_s{pg}_{n}"
    words = [wd for wd in re.findall(r'[a-zA-Z]{4,}', " ".join(ctx))
             if wd.lower() not in ("the", "and", "for", "with", "this", "that", "from",
                                    "are", "they", "have", "been", "were", "will", "into", "their")]
    if words:
        return f"{'_'.join(words[:2]).lower()}_s{pg}"
    return f"bild_s{pg}_{n}"


# ===========================================================================
# Dokumentstruktur & rendering
# ===========================================================================

def join_paragraphs(body_items):
    result, current = [], []
    for typ, val in body_items:
        if typ == "text":
            current.append(val)
        else:
            if current:
                result.append(("para", " ".join(current)))
                current = []
            result.append((typ, val))
    if current:
        result.append(("para", " ".join(current)))
    return result


def build_document(pages_data, image_records, book_title):
    imgs_by_page = {}
    for img in image_records:
        imgs_by_page.setdefault(img["page"], []).append(img)
    placed = set()

    sections = []
    cur = {"title": book_title, "level": 1, "page": 1, "body": []}

    for pd in pages_data:
        pg = pd["page"]
        for line in pd["lines"]:
            if line["level"] in ("h1", "h2"):
                if cur["body"] or cur["title"] != book_title:
                    sections.append(cur)
                lvl = 1 if line["level"] == "h1" else 2
                cur = {"title": line["text"], "level": lvl, "page": pg, "body": []}
            else:
                cur["body"].append(("text", line["text"]))
        if pg in imgs_by_page:
            for img in imgs_by_page[pg]:
                if img["filename"] not in placed:
                    cur["body"].append(("img", img))
                    placed.add(img["filename"])
    sections.append(cur)
    return sections


TAG_RULES = {
    "#regel": ["difficulty number", "skill check", "die code", "wild die", "opposed roll"],
    "#karaktär": ["dexterity", "knowledge", "mechanical", "perception", "strength", "technical",
                  "force points", "character points", "template"],
    "#ras": ["species", "homeworld"],
    "#utrustning": ["blaster", "weapon", "armor", "damage:", "range:"],
    "#fartyg": ["starship", "hyperdrive", "hull", "shields", "maneuverability"],
    "#plats": ["tatooine", "mos eisley", "cantina", "docking bay", "desert", "starport"],
    "#äventyr": ["episode", "encounter", "scene", "read aloud"],
    "#SL": ["gamemaster", "behind the scenes", "gm notes"],
    "#NPC": ["bounty hunter", "smuggler captain"],
}


def infer_tags(text, base_tags):
    tags = set(base_tags)
    c = text.lower()
    for tag, kws in TAG_RULES.items():
        if any(kw in c for kw in kws):
            tags.add(tag)
    return sorted(tags)


def render_markdown(sections, book, extraction_method):
    lines = [
        "---",
        f'title: "{book["title"]}"',
        f'book_code: "{book["code"]}"',
        f'weg_id: "{book["code"]}"',
        f'year: {book["year"]}',
        f'authors: "{book["authors"]}"',
        f'system: "{SYSTEM}"',
        f'type: {book["type"]}',
        f'source: "extract_weg.py v4 (PDF, {extraction_method})"',
        "tags:",
    ]
    for t in book["tags"]:
        lines.append(f'  - "{t}"')
    lines += ["aliases:", f'  - "{book["code"]}"', f'  - "{book["title"]}"', "---", ""]

    for sec in sections:
        pfx = "#" if sec["level"] == 1 else "##"
        lines.append(f'{pfx} {sec["title"]}')
        lines.append("")

        joined = join_paragraphs(sec["body"])
        full_text = " ".join(v for t, v in joined if t == "para")
        tags = infer_tags(full_text, book["tags"])
        if tags:
            lines.append(f'`{" ".join(tags)}`')
            lines.append("")

        for typ, val in joined:
            if typ == "para":
                lines.append(val)
                lines.append("")
            elif typ == "img":
                img = val
                desc = img["description"].replace("_", " ").title()
                lines.append(f'![[{img["filename"]}]]')
                lines.append(f'*{desc} — s. {img["page"]}*')
                lines.append("")
        lines.append(f'> [!note]- Källa: {book["code"]}, s. {sec["page"]}')
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def write_image_notes(records, book, force):
    count = 0
    for img in records:
        if not img["significant"]:
            continue
        if not (img["caption"] or img["context_above"] or img["context_below"]):
            continue  # ingen kontext -> ingen not (jfr WEG40005:s tomma noter)

        desc = img["description"].replace("_", " ").title()
        ctx = f'{img["caption"]} {img["context_above"]} {img["context_below"]}'
        tags = infer_tags(ctx, [])

        lines = [
            "---", f'title: "{desc}"', f'source: "{book["code"]}"',
            f'source_title: "{book["title"]}"', f'page: {img["page"]}',
            "type: bildnotering",
            f'dimensions: "{img["width"]}x{img["height"]}"',
            "tags:",
        ]
        for t in (tags or ["#bild"]):
            lines.append(f'  - "{t}"')
        lines += [
            "---", "", f"# {desc}", "", f'![[{img["filename"]}]]', "",
            f'**Källa:** [[{book["main_alias"]}|{book["title"]}]], sida {img["page"]}',
            f'**Dimensioner:** {img["width"]} × {img["height"]} px', "",
        ]
        if img["caption"]:
            lines += ["## Bildtext", f'> {img["caption"]}', ""]
        if img["context_above"] or img["context_below"]:
            lines.append("## Kontext från PDF")
            if img["context_above"]:
                lines.append(f'> **Text ovanför:** {img["context_above"][:250]}')
            if img["context_below"]:
                lines.append(f'> **Text under:** {img["context_below"][:250]}')
            lines.append("")

        note_name = img["filename"].rsplit(".", 1)[0] + "_note.md"
        if safe_write(IMAGE_NOTES_DIR / note_name, "\n".join(lines), force=force):
            count += 1
    return count


def write_index(sections, records, book, out_dir, force):
    lines = [
        "---", f'title: "Index — {book["title"]}"', "type: index",
        "tags:", '  - "#index"', '  - "#WEG"', "---", "",
        f'# Index — {book["title"]}', "", "## Innehållsförteckning", "",
    ]
    for s in sections:
        indent = "" if s["level"] == 1 else "  "
        lines.append(f'{indent}- **{s["title"]}** (s. {s["page"]})')

    seen_gallery = set()
    gallery_lines = []
    for img in records:
        if img["significant"]:
            desc = img["description"].replace("_", " ").title()
            key = (desc, img["page"])
            if key in seen_gallery:
                continue
            seen_gallery.add(key)
            note_path = IMAGE_NOTES_DIR / (img["filename"].rsplit(".", 1)[0] + "_note.md")
            if note_path.exists():
                note = img["filename"].rsplit(".", 1)[0] + "_note"
                gallery_lines.append(f'- [[{note}|{desc}]] — s. {img["page"]}')
            else:
                gallery_lines.append(f'- ![[{img["filename"]}]] — {desc}, s. {img["page"]}')

    if gallery_lines:
        lines += ["", "## Bildgalleri", ""] + gallery_lines

    path = out_dir / f'{book["code"]}_index.md'
    safe_write(path, "\n".join(lines), force=force)
    return path


# ===========================================================================
# Huvudflöde per bok
# ===========================================================================

def probe_book(code, cfg):
    pdf_path = PDF_DIR / cfg.get("pdf_file", f"{code}.pdf")
    if not pdf_path.exists():
        print(f"{code}: PDF SAKNAS ({pdf_path})")
        return
    doc = pymupdf.open(pdf_path)
    cls = classify_text_layer(doc)
    if cls == "none":
        print(f"{code}: {doc.page_count:4d} sidor | textlager=none -> OCR krävs | {cfg['title']}")
        doc.close()
        return
    lines = extract_lines_native(doc)
    lines = merge_dropcaps(lines)
    lines = clean_pages(lines)
    pages_data, body_h = classify_headings(lines)
    n_h1 = sum(1 for p in pages_data for l in p["lines"] if l["level"] == "h1")  # noqa: E501
    n_h2 = sum(1 for p in pages_data for l in p["lines"] if l["level"] == "h2")
    print(f"{code}: {doc.page_count:4d} sidor | textlager={cls:10s} | body_h={body_h:.1f} | "
          f"h1={n_h1:3d} h2={n_h2:3d} | {cfg['title']}")
    doc.close()


def process_book(code, cfg, dpi, lang, force=False):
    pdf_path = PDF_DIR / cfg.get("pdf_file", f"{code}.pdf")
    if not pdf_path.exists():
        print(f"  [!] Hittar inte {pdf_path}, hoppar över.")
        return None

    print(f"\n=== {code}: {cfg['title']} ===")
    doc = pymupdf.open(pdf_path)
    print(f"  {doc.page_count} sidor")

    out_dir = VAULT_ROOT / cfg["target"]
    main_fname = f'{code}_{sanitize_filename(cfg["title"])}.md'
    main_alias = main_fname[:-3]

    book = dict(cfg)
    book["code"] = code
    book["main_alias"] = main_alias

    cls = classify_text_layer(doc)
    if cls == "none":
        if not HAVE_OCR:
            print("  [!] Inget textlager och pytesseract saknas — hoppar över.")
            doc.close()
            return None
        print("  Metod: OCR (Tesseract) — inget textlager")
        lines = extract_lines_ocr(doc, code, dpi, lang)
        method = "ocr"
    else:
        print(f"  Metod: textlager ({cls})")
        lines = extract_lines_native(doc)
        method = cls

    lines = merge_dropcaps(lines)
    lines = clean_pages(lines)
    pages_data, body_h = classify_headings(lines)

    print("  Extraherar bilder...")
    if cfg.get("full_page_images", False):
        images = extract_images_full_pages(doc, code, dpi=cfg.get("image_dpi", 200))
    else:
        images = extract_images_normal(doc, code, force)
    sig = sum(1 for i in images if i["significant"])
    print(f"    {len(images)} bilder ({sig} signifikanta)")

    sections = build_document(pages_data, images, cfg["title"])
    print(f"  {len(sections)} sektioner")

    md = render_markdown(sections, book, method)
    wrote = safe_write(out_dir / main_fname, md, force=force)
    if wrote:
        print(f"  Skrev {out_dir / main_fname} ({len(md)} tecken)")
    else:
        print(f"  [!] {main_fname} skrevs INTE (finns redan, ingen --force)")

    n_notes = write_image_notes(images, book, force)
    print(f"  {n_notes} bildnoter")

    idx_path = write_index(sections, images, book, out_dir, force)
    print(f"  Index: {idx_path}")

    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "book": book, "pages": doc.page_count, "extraction_method": method,
        "total_images": len(images), "significant_images": sig,
        "sections": len(sections), "section_titles": [s["title"] for s in sections],
    }
    (METADATA_DIR / f"{code}_metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    doc.close()
    return {"code": code, "main_doc": str(out_dir / main_fname), "method": method,
            "sections": len(sections), "images": sig, "wrote": wrote}


# ===========================================================================
# Bok-registry
# ===========================================================================

BOOKS = {
    "WEG40065": dict(
        title="Star Wars: The Roleplaying Game — Gamemaster Handbook (2nd Edition)",
        year=1993, authors="Eric S. Trautmann, Chuck Truett, Bill Smith, John Terra, Steven H. Lorenz",
        type="referens", target="regler", full_page_images=False,
        tags=["#regel", "#SL-verktyg", "#sourcebook"]),
    "WEG40069": dict(
        title="Galaxy Guide 7: Mos Eisley", year=1993, authors="Martin Wixted",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#plats", "#sourcebook"]),
    "WEG40073": dict(
        title="Galaxy Guide 10: Bounty Hunters", year=1994, authors="Rick D. Stuart",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#NPC", "#sourcebook"]),
    "WEG40075": dict(
        title="Galaxy Guide 11: Criminal Organizations", year=1993, authors="Rick D. Stuart",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#kriminella", "#sourcebook"]),
    "WEG40086": dict(
        title="Heroes and Rogues", year=1995, authors="Paul Sudlow, Rick Stuart, Eric S. Trautmann",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#karaktär", "#sourcebook"]),
    "WEG40091": dict(
        title="Rebel Alliance Sourcebook", year=1990, authors="Greg Gorden",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#sourcebook"]),
    # Titelkorrigering: den befintliga drive_ocr-filen (regler/WEG40092.md)
    # hade fel titel i frontmatter ("SW RPG 2nd Ed R&E") — troligen en
    # copy-paste-miss mot WEG40120. Brödtexten i samma fil, och PDF:ens eget
    # textlager, säger "Imperial Sourcebook" konsekvent genom hela boken.
    "WEG40092": dict(
        title="Imperial Sourcebook (2nd Edition)", year=1994, authors="Greg Gorden, Bill Slavicsek",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#sourcebook"]),
    "WEG40095": dict(
        title="Galaxy Guide 6: Tramp Freighters", year=1990, authors="Mark Rein-Hagen, Stewart Wieck",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#fartyg", "#smuggling", "#sourcebook"]),
    "WEG40107": dict(
        title="Platt's Starport Guide", year=1995, authors="Michael Stern",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#plats", "#smuggling", "#sourcebook"]),
    "WEG40113": dict(
        title="Rules of Engagement: The Rebel SpecForce Handbook", year=1997, authors="Timothy S. O'Brien",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#sourcebook"]),
    "WEG40116": dict(
        title="Cynabar's Fantastic Technology: Droids", year=1997, authors="Michael Mikaelian",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#utrustning", "#sourcebook"]),
    "WEG40120": dict(
        title="Star Wars: The Roleplaying Game, 2nd Edition (Revised & Expanded)",
        year=1996, authors="Bill Smith, Peter Schweighofer, et al.",
        type="regel", target="regler", full_page_images=False,
        tags=["#regel", "#grundregler"]),
    "WEG40122": dict(
        title="Shadows of the Empire Sourcebook", year=1996, authors="Peter Schweighofer",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#sourcebook"]),
    "WEG40141": dict(
        title="Platt's Smugglers Guide", year=1994, authors="Michael Stern",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#smuggling", "#sourcebook"]),
    "WEG40143": dict(
        title="Pirates & Privateers", year=1998, authors="Timothy S. O'Brien",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#smuggling", "#sourcebook"]),
    "WEG40150": dict(
        title="Stock Ships",
        year=1999, authors="Pablo Hidalgo, Steve Miller, Timothy S. O'Brien, Paul Sudlow, Eric S. Trautmann",
        type="sourcebook", target="regler", full_page_images=True, image_dpi=200,
        tags=["#regel", "#fartyg", "#sourcebook"]),
    "WEG40153": dict(
        title="Wretched Hives of Scum and Villainy", year=2005, authors="West End Games",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#plats", "#sourcebook"]),
    "WEG40158": dict(
        title="Gundark's Fantastic Technology: Personal Gear", year=1998, authors="Peter Schweighofer",
        type="sourcebook", target="regler", full_page_images=False,
        tags=["#regel", "#utrustning", "#sourcebook"]),
    "WEG40212": dict(
        title="Mos Eisley Adventure Set", year=2000, authors="West End Games",
        # Trots miniatyrboxad-förpackningen är innehållet fristående scripted
        # scenarier ("Spies in the Shadows", "The Prison Shuttle", "Planning
        # the Rescue", ...) i samma struktur som WEG40005 Tatooine Manhunt —
        # ett äventyr, inte ett referensverk. Rättad 2026-08-12 efter att
        # Johan påpekade att aventyr/ bara innehöll en bok.
        type="adventure", target="aventyr", full_page_images=False,
        tags=["#äventyr", "#plats", "#WEG"]),
    # Fan-supplement, inte officiellt WEG-material — ingen WEG-kod finns, så
    # "FANMED01" är en egen identifierare. pdf_file åsidosätter standardgissningen
    # {code}.pdf eftersom källfilen redan hette "Medical Sourcebook.pdf".
    "FANMED01": dict(
        title="Medical Sourcebook", year=2015,
        authors="Nicholas L. (Luwingo_spince), Mike Fett, Justin Vanderhoof, David Stairs, m.fl. (fan-supplement)",
        type="sourcebook", target="regler", full_page_images=False,
        pdf_file="Medical Sourcebook.pdf",
        tags=["#regel", "#utrustning", "#karaktär", "#sourcebook"]),
    # Fan-kompendium, inte officiellt WEG-material — ingen WEG-kod. 336 sidor
    # skeppsstats hopsamlade fran flera hall (WEG, WotC, SWRPGNetwork m.fl.),
    # "Censored Edition" saknar en del fan-konstverk pa begaran av upphovsmannen.
    "FANSHIPS01": dict(
        title="Starships Stats — Revised & Expanded (Censored Edition)",
        year=2007,
        authors="Thiago S. Aranha (sammanstallning); stats fran West End Games, "
                 "Wizards of the Coast, Gary Astleford, Dean Magill, Christopher Deck, "
                 "SWRPGNetwork, Adventure Journal (fan-supplement)",
        type="sourcebook", target="regler", full_page_images=False,
        pdf_file="D6 Starships Stats.pdf",
        tags=["#regel", "#fartyg", "#sourcebook"]),
}

# Böcker som ersätter en befintlig regler/WEG*.md (drive_ocr) — kräver --force
# eftersom filen med samma kodnamn redan finns.
SUPERSEDES_EXISTING = {
    "WEG40073", "WEG40075", "WEG40086", "WEG40091", "WEG40092", "WEG40095",
    "WEG40107", "WEG40113", "WEG40116", "WEG40120", "WEG40122", "WEG40141",
    "WEG40158",
}


def main():
    ap = argparse.ArgumentParser(description="WEG PDF -> Obsidian markdown-extraktor v4")
    ap.add_argument("--code", nargs="+", help="Specifika WEG-koder att köra")
    ap.add_argument("--all", action="store_true", help="Kör alla böcker i registryn")
    ap.add_argument("--probe", action="store_true", help="Klassificera + räkna rubriker, skriv INGET")
    ap.add_argument("--dpi", type=int, default=300, help="OCR-rendering DPI (default 300)")
    ap.add_argument("--lang", default="eng", help="Tesseract-språk (default eng)")
    ap.add_argument("--force", action="store_true", help="Tillåt overwrite av befintliga filer")
    args = ap.parse_args()

    if not args.all and not args.code:
        ap.error("Ange --code WEG40xxx [...] eller --all")

    codes = list(BOOKS.keys()) if args.all else args.code
    unknown = [c for c in codes if c not in BOOKS]
    if unknown:
        ap.error(f"Okända koder (inte i BOOKS-registryn): {unknown}")

    if args.probe:
        for code in codes:
            probe_book(code, BOOKS[code])
        return

    for d in (ASSETS_DIR, IMAGE_NOTES_DIR, OCR_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)

    results = []
    for code in codes:
        force = args.force or (code in SUPERSEDES_EXISTING)
        r = process_book(code, BOOKS[code], args.dpi, args.lang, force=force)
        if r:
            results.append(r)

    if results:
        print("\n=== KLART ===")
        for r in results:
            status = "" if r["wrote"] else " [EJ SKRIVEN]"
            print(f"  {r['code']}: {r['method']}, {r['sections']} sektioner, {r['images']} bilder"
                  f" -> {r['main_doc']}{status}")


if __name__ == "__main__":
    main()
