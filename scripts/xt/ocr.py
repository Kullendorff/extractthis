"""Tesseract-OCR: rendering, parallell körning, per-sida-cache och ombyggnad till
stycken + rubriker. Bygger på extract_weg v4 (SW D6-vaulten), generaliserad."""
from __future__ import annotations

import io
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .common import CACHE_DIR, TESSERACT_EXE, dehyphenate_join, log

try:
    import pytesseract
    from PIL import Image

    if TESSERACT_EXE:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE
    # Python-paketet räcker inte – utan Tesseract-programmet kraschar varje OCR-anrop
    HAVE_OCR = bool(TESSERACT_EXE)
except ImportError:  # pragma: no cover
    HAVE_OCR = False

MIN_WORD_CONF = 30  # under detta är Tesseract-ord nästan alltid brus


def available_langs() -> set[str]:
    if not HAVE_OCR:
        return set()
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception:
        return set()


def _normalize_lang(lang: str) -> str:
    have = available_langs()
    parts = [p for p in lang.split("+") if p in have]
    return "+".join(parts) or "eng"


def _is_real_word(tok: str) -> bool:
    from .common import known_word_ratio

    core = re.sub(r"^[^\wÅÄÖåäö]+|[^\wÅÄÖåäö]+$", "", tok)
    if len(core) < 3 or not core.isalpha():
        return False
    r, n = known_word_ratio(core)
    return n == 1 and r == 1.0


def ocr_image(img, lang: str = "eng+swe", psm: int = 3) -> dict:
    """OCR av en PIL-bild -> {'words': [...], 'width', 'height'}.

    PSM 3 (auto-segmentering) är kolumnmedveten: Tesseracts block_num följer
    spalterna, så tvåspaltiga sidor kommer i rätt läsordning om man sorterar på
    (block, par, line) – INTE på y-position, som blandar ihop spalterna."""
    if not HAVE_OCR:
        raise RuntimeError("pytesseract/Pillow saknas – pip install pytesseract pillow")
    lang = _normalize_lang(lang)
    d = pytesseract.image_to_data(img, lang=lang, config=f"--oem 1 --psm {psm}",
                                  output_type=pytesseract.Output.DICT)
    words = []
    for i, raw in enumerate(d["text"]):
        txt = (raw or "").strip()
        try:
            conf = float(d["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not txt or conf < 0:
            continue
        # Låg konfidens men ett riktigt ord = oftast rätt läst text över en blek
        # bakgrundsbild (WEG40122 s.61: "captured", "reward"). Behåll dem.
        if conf < MIN_WORD_CONF and not (conf >= 5 and _is_real_word(txt)):
            continue
        words.append({
            "t": txt, "x": d["left"][i], "y": d["top"][i], "w": d["width"][i], "h": d["height"][i], "c": conf,
            "b": d["block_num"][i], "p": d["par_num"][i], "l": d["line_num"][i],
        })
    return {"words": words, "width": img.width, "height": img.height, "lang": lang}


PIPELINE_VERSION = "r9"
MAX_RENDER_PX = 4500  # bumpa när ocr_robust ändras -> gammal cache ignoreras
GOOD_CONF = 82          # medelkonfidens där vi slutar prova fler varianter
GOOD_COVERAGE = 0.90    # ...OCH minst så stor andel av sidans tecken täckta av ord
REVIEW_CONF = 72        # under detta flaggas sidan för visuell granskning
REVIEW_COVERAGE = 0.85  # tappade rader syns INTE i konfidensen – bara i täckningen


def quality(res: dict) -> float:
    """Poäng för att jämföra OCR-varianter av SAMMA sida: belönar både fler ord
    och högre konfidens; lågkonfidensbrus bidrar nästan inget."""
    return sum(max(0.0, w["c"] - 45) for w in res["words"] if len(w["t"]) > 1 or w["t"].isalnum())


def ocr_robust(img, lang: str = "eng+swe", effort: str = "best") -> dict:
    """OCR med förbehandling och flera försök; returnerar bästa varianten.

    1. rotation (OSD) + uträtning
    2. belysningsnormaliserad gråskala, PSM 3 (auto)    -> klar om konf >= GOOD_CONF
    3. adaptiv binarisering, PSM 3                        (bleka/fläckiga kopior)
    4. normaliserad, PSM 4 (en kolumn, varierande rader)  (journalanteckningar, listor)
    5. normaliserad, PSM 6 (ett textblock)                (formulär, tabellrester)
    6. uppskalad x1.6 binariserad, PSM 3                  (liten text, fax)
    7. PSM 11 (gles text)                                 (blanketter, spridda fält)
    effort='fast' kör bara steg 1-2."""
    from PIL import Image as _Image

    from . import preprocess as pp

    gray = pp.to_gray(img)
    if pp.ink_ratio(gray) < 0.002:
        return {"words": [], "width": img.width, "height": img.height, "lang": lang, "blank": True,
                "variant": "blank", "attempts": 0}
    orig = gray
    gray, info = pp.prepare(gray)
    if info["rotated"]:
        # OSD har fel ibland (EON s.14: en rak sida vändes 180° -> spegelvänd
        # skräptext). Verifiera med snabb-OCR i halv upplösning: behåll
        # rotationen bara om den faktiskt ger bättre text.
        straight = pp.rotate_small(orig, pp.estimate_skew(orig))

        def quick(a):
            small = _Image.fromarray(pp.normalize_illumination(a)).reduce(2)
            return quality(ocr_image(small, lang, 3))

        if quick(straight) >= quick(gray):
            gray, info = straight, {"rotated": 0, "skew": pp.estimate_skew(orig), "osd_rejected": True}
    norm = pp.normalize_illumination(gray)
    blobs = pp.char_blobs(norm)

    def run(arr, psm, name, scale=1.0):
        r = ocr_image(_Image.fromarray(arr), lang, psm)
        r["variant"] = name
        if scale != 1.0:  # koordinater tillbaka till originalskala
            for w in r["words"]:
                for k in ("x", "y", "w", "h"):
                    w[k] = int(w[k] / scale)
        r["coverage"] = round(pp.coverage(r["words"], blobs), 3)
        return r

    def good(r):
        return mean_conf(r) >= GOOD_CONF and len(r["words"]) >= 15 and r["coverage"] >= GOOD_COVERAGE

    def score(r):  # täckning väger in: en variant som tappar rader ska förlora
        return quality(r) * (0.5 + 0.5 * r["coverage"])

    best = run(norm, 3, "norm/psm3")
    attempts = 1
    if effort != "fast" and not good(best):
        binar = pp.binarize_adaptive(norm)
        plans = [(lambda: binar, 3, "adaptiv/psm3", 1.0), (lambda: norm, 4, "norm/psm4", 1.0),
                 (lambda: norm, 6, "norm/psm6", 1.0),
                 (lambda: pp.binarize_adaptive(pp.upscale(norm, 1.6)), 3, "uppskalad/psm3", 1.6),
                 (lambda: norm, 11, "norm/psm11", 1.0)]
        for mk, psm, name, scale in plans:
            r = run(mk(), psm, name, scale)
            attempts += 1
            if score(r) > score(best) * 1.03:
                best = r
            if good(best):
                break
    best.update(info)
    best["unread_lines"] = pp.unread_lines(norm, best["words"])
    best["attempts"] = attempts
    best["width"], best["height"] = img.width, img.height
    return best


def _cache_path(fingerprint: str, page_index: int, lang: str, dpi: int, effort: str):
    return (CACHE_DIR / "ocr" / fingerprint /
            f"p{page_index + 1:04d}_{lang.replace('+', '-')}_{dpi}_{effort}_{PIPELINE_VERSION}.json")


def ocr_pdf_pages(doc, indices, fingerprint: str, lang: str, dpi: int = 300, workers: int | None = None,
                  effort: str = "best", review_dir=None, review_stem: str = "sida") -> dict[int, dict]:
    """OCR:a valda sidor (0-baserade). Resultat cachas per sida, så en avbruten
    körning på en 300-sidorsbok fortsätter där den slutade.

    PyMuPDF är inte trådsäkert -> rendering sker i huvudtråden, i omgångar;
    förbehandling + Tesseract (externa processer) körs parallellt.

    review_dir: sidor med låg konfidens sparas som PNG där, så att Claude kan
    läsa sidan visuellt och rätta texten (res['review_image'])."""
    import pymupdf

    lang = _normalize_lang(lang)
    results: dict[int, dict] = {}
    todo = []
    for i in indices:
        cp = _cache_path(fingerprint, i, lang, dpi, effort)
        if cp.exists():
            try:
                results[i] = json.loads(cp.read_text(encoding="utf-8"))
                continue
            except Exception:
                pass
        todo.append(i)

    if todo:
        workers = workers or int(os.environ.get("XT_WORKERS", 0)) or max(1, min(8, (os.cpu_count() or 2) // 2))
        log(f"    OCR: {len(todo)} sidor @ {dpi} dpi, språk {lang}, {workers} trådar, effort={effort}"
            + (f" ({len(results)} från cache)" if results else ""))
        done = 0
        batch = workers + 2  # begränsar minnet: en A4-sida i 300 dpi gråskala ~9 MB + arbetskopior per tråd
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, len(todo), batch):
                chunk = todo[start:start + batch]
                images = {}
                for i in chunk:
                    # Tak på ~4500 px längsta sida: vissa scans har gigantiska
                    # sidmått (2848x4272 pt -> 211 Mpx vid 300 dpi = krasch).
                    r = doc[i].rect
                    eff = min(dpi, int(MAX_RENDER_PX * 72 / max(r.width, r.height, 1)))
                    pix = doc[i].get_pixmap(dpi=max(eff, 10), colorspace=pymupdf.csGRAY)
                    images[i] = Image.open(io.BytesIO(pix.tobytes("png")))
                    del pix
                # PyMuPDF behåller avkodade scanbilder i sin interna cache (~250 MB
                # uppmätt på en 464 MB-scan) – töm efter varje omgång.
                pymupdf.TOOLS.store_shrink(100)
                futs = {i: pool.submit(ocr_robust, images[i], lang, effort) for i in chunk}
                for i, fut in futs.items():
                    try:
                        res = fut.result()
                    except Exception as e:  # en trasig sida ska inte fälla boken
                        log(f"      [!] OCR-fel s. {i + 1}: {type(e).__name__}: {e}")
                        results[i] = {"words": [], "error": f"{type(e).__name__}: {e}", "unread_lines": 99}
                        done += 1
                        continue  # cachas inte – görs om nästa körning
                    res["dpi"] = dpi
                    cp = _cache_path(fingerprint, i, lang, dpi, effort)
                    cp.parent.mkdir(parents=True, exist_ok=True)
                    cp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
                    results[i] = res
                    done += 1
                del images
                log(f"      ...{done}/{len(todo)}")

    if review_dir is not None:
        for i, res in results.items():
            if needs_review(res):
                review_dir.mkdir(parents=True, exist_ok=True)
                fn = review_dir / f"{review_stem}_s{i + 1:03d}.png"
                if not fn.exists():
                    # ~1800 px långsida: läsbart för Claudes Read-verktyg utan att bli för tungt
                    r = doc[i].rect
                    dpi_r = max(60, min(200, int(1800 * 72 / max(r.width, r.height, 1))))
                    doc[i].get_pixmap(dpi=dpi_r).save(str(fn))
                res["review_image"] = str(fn)
                res["review_reason"] = review_reason(res)
    return results


UNREAD_LINES_REVIEW = 4  # så många olästa bläckrader -> troligen handskrift


def review_reason(res: dict) -> str | None:
    """Varför sidan behöver visuell granskning (None = ser pålitlig ut)."""
    if res.get("error"):
        return f"OCR-fel: {res['error'][:80]}"
    if res.get("blank"):
        return None
    ws = res.get("words") or []
    if res.get("unread_lines", 0) >= UNREAD_LINES_REVIEW:
        return f"oläst skrift ({res['unread_lines']} rader) – handskrift?"
    if len(ws) < 5:
        return "nästan ingen text hittad"
    if mean_conf(res) < REVIEW_CONF:
        return f"låg konfidens {mean_conf(res):.0f}"
    if sum(1 for w in ws if w["c"] < 60) / len(ws) > 0.25:
        return "många osäkra ord"
    if res.get("coverage", 1.0) < REVIEW_COVERAGE:
        return f"tappade rader? (täckning {res['coverage']:.2f})"
    return None


def needs_review(res: dict) -> bool:
    return review_reason(res) is not None


# ---------------------------------------------------------------------------
# Ord -> stycken -> markdown
# ---------------------------------------------------------------------------

UNCERTAIN = 55  # ord under denna konfidens markeras med ⟦ ⟧ om mark_uncertain=True
NOISE_SOLID = 0.35  # stycken med lägre andel säkra ord behandlas som bildbrus


def paragraphs_from_ocr(res: dict, mark_uncertain: bool = False) -> list[dict]:
    """Gruppera Tesseract-ord till stycken (block, par) i läsordning."""
    lines: dict[tuple, list] = {}
    for w in res["words"]:
        lines.setdefault((w["b"], w["p"], w["l"]), []).append(w)
    paras: dict[tuple, list] = {}
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda w: w["x"])
        paras.setdefault(key[:2], []).append({
            "text": " ".join((f"⟦{w['t']}⟧" if mark_uncertain and w["c"] < UNCERTAIN else w["t"]) for w in ws),
            "h": sum(w["h"] for w in ws) / len(ws),
            "y": min(w["y"] for w in ws),
            "x": min(w["x"] for w in ws),
            "c": sum(w["c"] for w in ws) / len(ws),
        })
    out = []
    for key in sorted(paras):
        ls = paras[key]
        text = ls[0]["text"]
        for ln in ls[1:]:
            text = dehyphenate_join(text, ln["text"])
        ws = [w for k2, lw in lines.items() if k2[:2] == key for w in lw]
        out.append({
            "text": text, "nlines": len(ls),
            "h": sum(l["h"] for l in ls) / len(ls),
            "y": ls[0]["y"], "x": ls[0]["x"],
            "c": sum(l["c"] for l in ls) / len(ls),
            "n": len(ws), "solid": sum(w["c"] >= 75 for w in ws) / max(1, len(ws)),
        })
    return out


_ROMAN_PAGE_RE = re.compile(r"(x{0,3})(ix|iv|v?i{0,3})", re.I)  # i–xxxix, giltiga former


def _is_junk(t: str, at_edge: bool = True) -> bool:
    """Brus som aldrig är innehåll. Sidnummer (arabiska 1–4 siffror, romerska i–xxxix)
    räknas bara som brus i sidans kanter. "Ja", "120", "mild", "civil" ska vara kvar
    (förra versionen tog "mild/civil/DIM" för romerska tal)."""
    t = t.strip()
    if not t or (len(t) == 1 and not t.isalnum()):
        return True
    if at_edge and (_ROMAN_PAGE_RE.fullmatch(t) or re.fullmatch(r"[-–—\s]*\d{1,4}[-–—\s]*", t)):
        return True  # ensamt sidnummer (OBS: datum/tider/personnummer ska vara kvar)
    ns = re.sub(r"\s", "", t)
    alnum = sum(c.isalnum() for c in ns)
    return len(ns) >= 6 and alnum / len(ns) < 0.3  # dingbats/separatorer


def _merge_dropcaps(paras: list[dict]) -> list[dict]:
    """Anfang (stor ensam versal) + stycke som börjar med gemen -> ett ord.
    Guard mot dubblering ("TTatooine"-buggen i WEG v3)."""
    out: list[dict] = []
    i = 0
    while i < len(paras):
        p = paras[i]
        t = p["text"].strip()
        if (i + 1 < len(paras) and len(t) == 1 and t.isalpha()
                and paras[i + 1]["text"][:1].islower()
                and p["h"] > paras[i + 1]["h"] * 1.35
                and not paras[i + 1]["text"].lower().startswith(t.lower())):
            nxt = dict(paras[i + 1])
            nxt["text"] = t + nxt["text"]
            out.append(nxt)
            i += 2
            continue
        out.append(p)
        i += 1
    return out


def body_height(pages_paras: list[list[dict]], default: float | None = 0.0) -> float | None:
    """Vanligaste radhöjden i brödtext (stycken > 40 tecken). Utan brödtext:
    medianhöjd över allt, annars `default` (None = 'kalibrera på nästa block' –
    ett fast 12 px gav ### på varje rad vid 300 dpi där brödtext är ~30 px)."""
    hs = [round(p["h"] * 2) / 2 for ps in pages_paras for p in ps if len(p["text"]) > 40]
    if hs:
        return Counter(hs).most_common(1)[0][0]
    if default is None:
        return None
    allh = sorted(p["h"] for ps in pages_paras for p in ps)
    return allh[len(allh) // 2] if allh else (default or 12.0)


def _heading_score(p: dict, body_h: float) -> float:
    """Poängmodell från WEG v4. Höjdkvoten är den enda pålitliga primärsignalen;
    'kort rad utan punkt' är det nästan varje PDF-rad, så den väger lite."""
    t = p["text"].strip()
    ratio = p["h"] / body_h if body_h else 1.0
    s = 3.5 if ratio >= 1.6 else 2.2 if ratio >= 1.3 else 1.0 if ratio >= 1.15 else 0.0
    if p["nlines"] == 1:
        s += 0.5
    else:
        s -= 1.0 * (p["nlines"] - 1)
    if t.isupper() and 3 < len(t) <= 80:
        s += 1.2
    if len(t) <= 50:
        s += 0.3
    s += -0.5 if t.endswith((".", ",", ";", ":")) else 0.2
    if p["c"] < 55:
        s -= 2.0
    return s


def _is_image_noise(p: dict) -> bool:
    """Stycke som Tesseract 'läst' ur en illustration: låg konfidens OCH nästan
    inga riktiga ord. Båda krävs – namn/koder i journaler har få kända ord men
    hög konfidens, och ska vara kvar."""
    from .common import known_word_ratio

    if p["c"] >= 70:
        return False
    kr, n = known_word_ratio(p["text"].replace("⟦", "").replace("⟧", ""))
    if p["c"] < 45 and len(p["text"]) < 40:
        return True
    return n >= 1 and kr < 0.34 and p["c"] < 62


def ocr_pages_to_markdown(results: dict[int, dict], mark_uncertain: bool = False,
                          body_h: float | None = None) -> dict[int, str]:
    """{sidindex: ocr-resultat} -> {sidindex: markdown}. Rubrikkalibrering görs
    över sidorna i anropet, eller med given brödtexthöjd (body_h) när en stor
    bok bearbetas i block."""
    pages = {}
    for i, r in results.items():
        ps = _merge_dropcaps(paragraphs_from_ocr(r, mark_uncertain))
        n = len(ps)
        # Ensamma sidnummer tas bara bort i sidans kanter – mitt på sidan kan
        # "120" vara en dos och "vi" ett ord (granskning 2026-10-10).
        pages[i] = [p for k, p in enumerate(ps) if not _is_junk(p["text"], at_edge=k < 2 or k >= n - 2)]
    bh = body_h or body_height(list(pages.values()))
    out = {}
    for i, ps in pages.items():
        parts = []
        hidden: list[str] = []
        for p in ps:
            t = p["text"].strip()
            # Andel säkra ord (konf >= 75) per stycke: bildbrus ~0,1, riktig text
            # >= 0,85 (uppmätt WEG40069 s.30/40). Osäkert material KASTAS ALDRIG:
            # det göms i en kommentar (syns i md, rensas i txt/jsonl) – en blek
            # rad eller en handskriven signatur kan vara det viktigaste på sidan.
            noisy = ((p["n"] >= 3 and p["solid"] < NOISE_SOLID)
                     or (p["n"] < 3 and p["c"] < 80 and len(t) < 25)
                     or _is_image_noise(p))
            if noisy:
                hidden.append(t)
                continue
            sc = _heading_score(p, bh)
            if sc >= 5.5:
                parts.append("## " + t)
            elif sc >= 3.0:
                parts.append("### " + t)
            else:
                parts.append(t)
        if hidden:
            parts.append("<!-- osäker OCR (bild/handskrift?): " + " | ".join(hidden).replace("--", "–") + " -->")
        out[i] = "\n\n".join(parts)
    return out


def mean_conf(res: dict) -> float:
    ws = res.get("words") or []
    return sum(w["c"] for w in ws) / len(ws) if ws else 0.0
