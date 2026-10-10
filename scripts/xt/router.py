"""Filtyp -> extraktor, och rendering till md / txt / jsonl."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .common import yaml_str

VERSION = "1.0"

PDF_LIKE = {".pdf", ".xps", ".oxps", ".cbz", ".fb2", ".mobi"}  # allt PyMuPDF öppnar som sidor
OFFICE = {".docx", ".docm", ".dotx", ".dotm", ".doc", ".dot", ".rtf", ".odt", ".wpd", ".wps",
          ".xlsx", ".xlsm", ".xltx", ".xls", ".ods",
          ".pptx", ".pptm", ".ppt", ".pps", ".ppsx", ".ppsm", ".potx", ".odp"}
HTML = {".html", ".htm", ".xhtml"}
EPUB = {".epub"}
MAIL = {".eml", ".msg"}
IMAGE = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
CSV = {".csv", ".tsv"}
DATA = {".json", ".jsonl", ".ndjson", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}
TEXT = {".txt", ".md", ".markdown", ".rst", ".log", ".tex", ".srt", ".vtt"}

SUPPORTED = PDF_LIKE | OFFICE | HTML | EPUB | MAIL | IMAGE | CSV | DATA | TEXT


@dataclass
class Doc:
    source: Path
    kind: str
    sections: list[dict]          # [{"page": int|None, "method": str, "text": str}]
    meta: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    outline: list = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(s["text"]) for s in self.sections)


def extract_file(path, *, pages=None, ocr="auto", lang="auto", engine="auto", dpi=300,
                 images_dir: Path | None = None, page_images=False, max_rows=2000,
                 password=None, effort="best", review_dir: Path | None = None,
                 mark_uncertain=False) -> Doc:
    path = Path(path)
    ext = path.suffix.lower()

    if ext in PDF_LIKE:
        from .pdf import extract_pdf

        r = extract_pdf(path, pages=pages, ocr=ocr, lang=lang, engine=engine, dpi=dpi,
                        images_dir=images_dir, page_images=page_images, password=password,
                        effort=effort, review_dir=review_dir, mark_uncertain=mark_uncertain)
        secs = [{"page": p.page, "method": p.method, "text": p.text, **({"ocr_conf": round(p.conf, 1)} if p.conf else {})}
                for p in r.pages]
        meta = dict(r.meta)
        if r.images:
            meta["images"] = [i["file"] for i in r.images]
        return Doc(path, "pdf", secs, meta, r.warnings, r.outline)

    if ext in OFFICE:
        from .office import extract_office

        md, meta = extract_office(path, images_dir=images_dir, max_rows=max_rows, password=password)
        kind = "office"
    elif ext in HTML:
        from .other import extract_html
        md, meta = extract_html(path)
        kind = "html"
    elif ext in EPUB:
        from .other import extract_epub
        try:
            md, meta = extract_epub(path)
        except Exception:  # trasig/ovanlig epub -> PyMuPDF:s sidrendering
            return _epub_via_pymupdf(path, pages, engine)
        kind = "epub"
    elif ext in MAIL:
        from .other import extract_eml, extract_msg
        md, meta = (extract_eml if ext == ".eml" else extract_msg)(path)
        kind = "mail"
    elif ext in IMAGE:
        from .other import extract_image
        md, meta = extract_image(path, lang, effort, mark_uncertain)
        kind = "image"
    elif ext in CSV:
        from .other import extract_csv
        md, meta = extract_csv(path, max_rows)
        kind = "table"
    elif ext in DATA:
        from .other import extract_data
        md, meta = extract_data(path)
        kind = "data"
    elif ext in TEXT:
        from .other import extract_text
        md, meta = extract_text(path)
        kind = "text"
    else:
        raise ValueError(f"Formatet {ext or '(ingen ändelse)'} stöds inte")

    warns = []
    if isinstance(meta, dict) and meta.get("review"):
        warns.append(f"Låg OCR-konfidens ({meta.get('ocr_conf')}) – läs originalbilden visuellt och rätta")
    method = meta.pop("method", kind) if isinstance(meta, dict) else kind
    return Doc(path, kind, [{"page": None, "method": method, "text": md}], meta, warns)


def _epub_via_pymupdf(path, pages, engine) -> Doc:
    from .pdf import extract_pdf

    r = extract_pdf(path, pages=pages, ocr="off", engine=engine)
    return Doc(path, "epub", [{"page": p.page, "method": p.method, "text": p.text} for p in r.pages],
               r.meta, r.warnings + ["epub lästes via PyMuPDF (ebooklib misslyckades)"], r.outline)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _method_summary(doc: Doc) -> dict:
    cnt: dict = {}
    for s in doc.sections:
        cnt[s["method"]] = cnt.get(s["method"], 0) + 1
    return cnt


def render_md(doc: Doc, with_frontmatter=True) -> str:
    m = doc.meta
    title = m.get("title") or doc.source.stem
    out = []
    if with_frontmatter:
        fm = ["---", f"source: {yaml_str(doc.source.name)}", f"path: {yaml_str(doc.source)}",
              f"format: {doc.source.suffix.lower().lstrip('.')}"]
        if m.get("title"):
            fm.append(f"title: {yaml_str(m['title'])}")
        if m.get("author"):
            fm.append(f"author: {yaml_str(m['author'])}")
        if doc.kind == "pdf":
            fm += [f"pages: {m.get('pages_extracted')}/{m.get('pages_total')}",
                   f"text_layer: {m.get('text_layer')}", f"engine: {m.get('engine')}"]
            if m.get("lang"):
                fm.append(f"lang: {m['lang']}")
        ms = _method_summary(doc)
        fm.append("methods: {" + ", ".join(f"{k}: {v}" for k, v in ms.items()) + "}")
        if m.get("converted_from"):
            fm.append(f"converted_from: {m['converted_from']}")
        fm.append(f"chars: {doc.chars}")
        if m.get("review"):
            fm.append("review:  # låg OCR-konfidens – läs bilden och rätta texten")
            for pg, r in sorted(m["review"].items(), key=lambda kv: int(kv[0])):
                fm.append(f"  - {{page: {pg}, conf: {r['conf']}, reason: {yaml_str(r.get('reason') or '')}, "
                          f"image: {yaml_str(r.get('image') or '')}}}")
        fm.append(f"extracted: {dt.datetime.now():%Y-%m-%d %H:%M} (extract-this {VERSION})")
        if doc.warnings:
            fm.append("warnings:")
            fm += [f"  - {yaml_str(w)}" for w in doc.warnings]
        fm.append("---")
        out.append("\n".join(fm))
    out.append(f"# {title}")
    if len(doc.outline) >= 3:
        ol = ["<details><summary>Innehåll (PDF-bokmärken)</summary>", ""]
        for lvl, t, pg in doc.outline[:400]:
            ol.append(f"{'  ' * (lvl - 1)}- {t.strip()} — s. {pg}")
        ol += ["", "</details>"]
        out.append("\n".join(ol))
    review = m.get("review") or {}
    for s in doc.sections:
        if s["page"] is not None:
            bits = [f"s. {s['page']}"]
            if s["method"] not in ("layout", "text"):
                bits.append(s["method"] + (f" {s['ocr_conf']:.0f}%" if s.get("ocr_conf") else ""))
            if s["page"] in review:
                rv = review[s["page"]]
                bits.append(f"GRANSKA ({rv.get('reason') or 'osäker'}): " + (rv.get("image") or "rendera sidan"))
            tag = "<!-- " + " | ".join(bits) + " -->"
            out.append(tag + ("\n\n" + s["text"] if s["text"].strip() else ""))
        elif s["text"].strip():
            out.append(s["text"])
    return "\n\n".join(out).rstrip() + "\n"


_MD_STRIP = [
    (re.compile(r"^#{1,6}\s+", re.M), ""),
    (re.compile(r"\*\*\*(.+?)\*\*\*"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"\1"),
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
    (re.compile(r"<br>"), " / "),
]


_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def strip_comments(md: str) -> str:
    """Gömd osäker OCR ('<!-- osäker OCR … -->') och andra kommentarer bort –
    de hör hemma i markdown för granskning, inte i ren text eller ett sökindex."""
    return re.sub(r"\n{3,}", "\n\n", _COMMENT_RE.sub("", md)).strip()


def md_to_plain(md: str) -> str:
    md = strip_comments(md)
    for rx, rep in _MD_STRIP:
        md = rx.sub(rep, md)
    return md


def render_txt(doc: Doc) -> str:
    out = []
    for s in doc.sections:
        if s["page"] is not None:
            out.append(f"--- s. {s['page']} ---")
        out.append(md_to_plain(s["text"]))
    return "\n\n".join(t for t in out if t.strip()).rstrip() + "\n"


def iter_jsonl(doc: Doc):
    """En post per sida (PDF) eller per ##-avsnitt (övriga) – redo för RAG/sökindex."""
    base = {"source": doc.source.name, "path": str(doc.source)}
    if doc.kind == "pdf" or any(s["page"] is not None for s in doc.sections):
        for s in doc.sections:
            t = strip_comments(s["text"])
            if t:
                yield {**base, "page": s["page"], "method": s["method"], "text": t}
        return
    text = strip_comments("\n\n".join(s["text"] for s in doc.sections))
    parts = re.split(r"(?m)^(?=#{1,2} )", text)
    for n, part in enumerate(p for p in parts if p.strip()):
        head = part.splitlines()[0].lstrip("#").strip() if part.startswith("#") else None
        yield {**base, "section": n + 1, "heading": head, "text": part.strip()}


def render(doc: Doc, fmt: str) -> str:
    if fmt == "md":
        return render_md(doc)
    if fmt == "txt":
        return render_txt(doc)
    if fmt == "jsonl":
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in iter_jsonl(doc))
    raise ValueError(fmt)
