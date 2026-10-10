"""Övriga format: HTML, EPUB, e-post (EML/MSG), bilder (OCR), CSV/TSV,
JSON/XML/YAML och ren text."""
from __future__ import annotations

import csv
import email
import email.policy
import io
import json
import re
from pathlib import Path

from .common import ENCODINGS, bom_encoding, clean_text, md_table, read_text_file, tesseract_lang

MAX_TEXT_BYTES = 200 << 20  # textfiler större än så kopieras inte in i markdown
MAX_IMAGE_PX = 4500          # samma tak som för PDF-rendering (ocr.MAX_RENDER_PX)
MAX_FRAMES = 200             # flersidig TIFF/animerad GIF


# ---------------------------------------------------------------------------
# HTML / EPUB
# ---------------------------------------------------------------------------

def html_to_md(html: str, base_url: str | None = None) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "form", "button"]):
        t.decompose()
    for t in soup.find_all(attrs={"aria-hidden": "true"}):
        t.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    try:
        from markdownify import markdownify

        md = markdownify(str(root), heading_style="ATX", bullets="-")
    except ImportError:
        md = root.get_text("\n")
    md = re.sub(r"\n[ \t]+\n", "\n\n", md)
    return clean_text(md)


def extract_html(path: Path) -> tuple[str, dict]:
    raw, enc = read_text_file(path)
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    return html_to_md(raw), {"title": m.group(1).strip() if m else None, "encoding": enc}


_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def extract_epub(path: Path, figures_dir: Path | None = None) -> tuple[str, dict]:
    import posixpath
    from urllib.parse import unquote

    import ebooklib
    from ebooklib import epub

    from .figures import fig_id, placeholder

    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    parts, figs, saved = [], [], {}
    stem = Path(path).stem

    def save_image(item_dir: str, src: str, alt: str) -> str | None:
        href = posixpath.normpath(posixpath.join(item_dir, unquote(src.split("#")[0])))
        if href in saved:
            return placeholder(saved[href])
        img = book.get_item_with_href(href)
        if img is None:
            return None
        k = len(figs) + 1
        fid = fig_id(None, k)
        figures_dir.mkdir(parents=True, exist_ok=True)
        fn = figures_dir / f"{stem}_{k:03d}{Path(href).suffix or '.png'}"
        fn.write_bytes(img.get_content())
        figs.append({"id": fid, "page": None, "file": str(fn), "caption": alt.strip(), "source": "epub"})
        saved[href] = fid
        return placeholder(fid)

    # spine-ordning = bokens läsordning (get_items() ger godtycklig ordning)
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        md = html_to_md(item.get_content().decode("utf-8", errors="replace"))
        if figures_dir is not None:
            item_dir = posixpath.dirname(item.get_name())
            md = _MD_IMG_RE.sub(lambda m: save_image(item_dir, m.group(2), m.group(1)) or m.group(0), md)
        if md.strip():
            parts.append(md)
    title = book.get_metadata("DC", "title")
    author = book.get_metadata("DC", "creator")
    return "\n\n---\n\n".join(parts), {"title": title[0][0] if title else None,
                                         "author": author[0][0] if author else None, "figures": figs}


# ---------------------------------------------------------------------------
# E-post
# ---------------------------------------------------------------------------

def _mail_md(headers: dict, body: str, attachments: list[str]) -> str:
    head = "\n".join(f"**{k}:** {v}" for k, v in headers.items() if v)
    out = [head, body.strip()]
    if attachments:
        out.append("**Bilagor:**\n" + "\n".join(f"- {a}" for a in attachments))
    return "\n\n".join(p for p in out if p)


def extract_eml(path: Path) -> tuple[str, dict]:
    msg = email.message_from_bytes(Path(path).read_bytes(), policy=email.policy.default)
    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):  # t.ex. charset="unknown-8bit" i gammal e-post
            raw = part.get_payload(decode=True) or b""
            content = raw.decode("utf-8", errors="replace")
        body = html_to_md(content) if part.get_content_type() == "text/html" else content
    atts = [a.get_filename() or "(namnlös)" for a in msg.iter_attachments()]
    h = {"Från": msg["from"], "Till": msg["to"], "Kopia": msg["cc"], "Datum": msg["date"], "Ämne": msg["subject"]}
    return _mail_md(h, body, atts), {"title": msg["subject"]}


def extract_msg(path: Path) -> tuple[str, dict]:
    import extract_msg

    m = extract_msg.Message(str(path))
    try:
        body = m.body or ""
        if not body.strip() and m.htmlBody:
            hb = m.htmlBody
            body = html_to_md(hb.decode("utf-8", "replace") if isinstance(hb, bytes) else hb)
        atts = [a.longFilename or a.shortFilename or "(namnlös)" for a in m.attachments]
        h = {"Från": m.sender, "Till": m.to, "Kopia": m.cc, "Datum": m.date, "Ämne": m.subject}
        return _mail_md(h, body, atts), {"title": m.subject}
    finally:
        m.close()


# ---------------------------------------------------------------------------
# Bilder -> OCR
# ---------------------------------------------------------------------------

def extract_image(path: Path, lang: str = "auto", effort: str = "best",
                  mark_uncertain: bool = False) -> tuple[str, dict]:
    """Foto/scan/skärmdump -> OCR. Multi-sides-TIFF stöds (en sektion per bild)."""
    from PIL import Image, ImageOps

    from . import ocr as ocrmod

    tl = tesseract_lang(None) if lang == "auto" else lang
    results = {}
    with Image.open(path) as img:
        total_frames = getattr(img, "n_frames", 1)
        frames = min(total_frames, MAX_FRAMES)  # animerad GIF med 500 rutor = 500 OCR-körningar
        for f in range(frames):
            img.seek(f)
            fr = img.convert("L")
            if f == 0:  # exif_transpose: mobilfoton lagrar ofta rotationen bara i EXIF
                fr = ImageOps.exif_transpose(img).convert("L") if hasattr(img, "getexif") else fr
            longest = max(fr.size)
            if longest > MAX_IMAGE_PX:  # 100 Mpx-TIFF -> flera GB i OCR-pipelinen
                k = MAX_IMAGE_PX / longest
                fr = fr.resize((int(fr.width * k), int(fr.height * k)), Image.LANCZOS)
            elif longest < 2000:  # skärmdumpar/små foton: uppskalning hjälper Tesseract mycket
                k = 2000 / longest
                fr = fr.resize((int(fr.width * k), int(fr.height * k)), Image.LANCZOS)
            results[f] = ocrmod.ocr_robust(fr, tl, effort)
    frames_note = total_frames > frames
    pages = ocrmod.ocr_pages_to_markdown(results, mark_uncertain)
    confs = [ocrmod.mean_conf(r) for r in results.values() if not r.get("blank")]
    review = {f + 1: round(ocrmod.mean_conf(r), 1) for f, r in results.items() if ocrmod.needs_review(r)}
    md = "\n\n".join((f"<!-- bild {f + 1} -->\n\n" if frames > 1 else "") + pages[f] for f in sorted(pages))
    if frames_note:
        md += f"\n\n*… {total_frames - frames} bildrutor till hoppades över (max {MAX_FRAMES})*"
    return md, {"frames": frames, "ocr_conf": round(sum(confs) / len(confs), 1) if confs else None,
                "method": "ocr", "review": {k: {"conf": v, "image": str(path)} for k, v in review.items()}}


# ---------------------------------------------------------------------------
# Data och text
# ---------------------------------------------------------------------------

def _sniff_encoding(path: Path) -> str:
    """Avgör kodning från de första 2 MB – en CSV på flera GB läses aldrig in hel."""
    with open(path, "rb") as f:
        sample = f.read(2 << 20)
    bom = bom_encoding(sample[:4])
    if bom:
        return bom
    for enc in ENCODINGS:
        try:
            sample.decode(enc) if enc != "utf-8-sig" else sample[:-4].decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def iter_csv_rows(path: Path):
    enc = _sniff_encoding(path)
    with open(path, "r", encoding=enc, errors="replace", newline="") as f:
        sample = f.read(16384)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel_tab if Path(path).suffix.lower() == ".tsv" else csv.excel
        yield from csv.reader(f, dialect)


def read_csv_rows(path: Path) -> list[list[str]]:
    return list(iter_csv_rows(path))


def extract_csv(path: Path, max_rows: int = 2000) -> tuple[str, dict]:
    keep, total = [], 0
    for r in iter_csv_rows(path):
        total += 1
        if len(keep) <= max_rows:
            keep.append(r)
    if not keep:
        return "", {"rows": 0}
    md = md_table(keep)
    if total - 1 > max_rows:
        md += f"\n\n*… {total - 1 - max_rows:,} rader till utelämnade (öka --max-rows)*"
    return md, {"rows": total - 1}


MAX_DATA_BYTES = 50 << 20  # JSON/XML pretty-print tar ~10x filstorleken i minne


def extract_data(path: Path) -> tuple[str, dict]:
    ext = Path(path).suffix.lower()
    size = Path(path).stat().st_size
    if size > MAX_DATA_BYTES:
        raise ValueError(f"Datafilen är {size / 1e6:.0f} MB – för stor att formatera om; "
                         "den är redan text: läs i delar (Read offset/limit) eller med jq/Grep")
    text, enc = read_text_file(path)
    if ext in (".json", ".jsonl", ".ndjson"):
        try:
            if ext == ".json":
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            return f"```json\n{text.strip()}\n```", {"encoding": enc}
        except json.JSONDecodeError:
            return f"```\n{text.strip()}\n```", {"encoding": enc}
    if ext == ".xml":
        try:
            import xml.dom.minidom

            pretty = xml.dom.minidom.parseString(text.encode("utf-8")).toprettyxml(indent="  ")
            pretty = "\n".join(l for l in pretty.splitlines()[1:] if l.strip())
            return f"```xml\n{pretty}\n```", {"encoding": enc}
        except Exception:
            return f"```xml\n{text.strip()}\n```", {"encoding": enc}
    lang = {".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini"}.get(ext, "")
    return f"```{lang}\n{text.strip()}\n```", {"encoding": enc}


def extract_text(path: Path) -> tuple[str, dict]:
    size = Path(path).stat().st_size
    if size > MAX_TEXT_BYTES:
        raise ValueError(f"Textfilen är {size / 1e6:.0f} MB – redan text; läs den i delar "
                         "(Read med offset/limit, eller Grep) i stället för att konvertera")
    text, enc = read_text_file(path)
    return text.strip(), {"encoding": enc}
