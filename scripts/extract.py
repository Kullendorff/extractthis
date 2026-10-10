#!/usr/bin/env python3
"""extract-this – vilket dokument som helst -> markdown / text / jsonl.

Exempel:
  python extract.py bok.pdf                      # -> bok.pdf:s mapp/extracted/bok.md
  python extract.py bok.pdf --stdout --pages 1-5 # skriv till stdout
  python extract.py "D:/mapp" -r                 # hela mappträdet, återupptagbart
  python extract.py "D:/mapp" --combine kunskap.md   # allt i EN fil (kunskapsbas)
  python extract.py scan.pdf --ocr force --lang swe+eng
  python extract.py bok.pdf --images             # bilder -> extracted/bok_bilder/
  python extract.py data.xlsx --format jsonl
  python extract.py --check                      # visa beroenden/Tesseract-språk
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xt.common import log, safe_filename  # noqa: E402
from xt.router import SUPPORTED, VERSION, extract_file, iter_jsonl, render, render_md, render_txt  # noqa: E402

EXT = {"md": ".md", "txt": ".txt", "jsonl": ".jsonl"}
SKIP_DIRS = {"extracted", ".git", "node_modules", "__pycache__", ".obsidian", ".venv", "venv"}


def find_inputs(paths: list[Path], recursive: bool, out_dir: Path | None) -> list[tuple[Path, Path]]:
    """-> [(fil, basmapp)] – basmappen används för att spegla undermappar."""
    found = []
    for p in paths:
        if p.is_file():
            found.append((p, p.parent))
            continue
        if not p.is_dir():
            log(f"[!] Finns inte: {p}")
            continue
        it = p.rglob("*") if recursive else p.iterdir()
        for f in it:
            if not f.is_file() or f.suffix.lower() not in SUPPORTED:
                continue
            rel_parts = f.relative_to(p).parts[:-1]
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel_parts):
                continue
            if f.name.startswith(("~$", ".")):  # Office-låsfiler
                continue
            if out_dir and out_dir.resolve() in f.resolve().parents:
                continue
            found.append((f, p))
    return sorted(set(found))


def check() -> None:
    import importlib.util as u

    from xt import ocr
    from xt.common import SOFFICE_EXE, TESSERACT_EXE

    mods = [("pymupdf", "PDF-kärna"), ("pymupdf4llm", "layout-markdown för PDF"), ("pytesseract", "OCR"),
            ("PIL", "bilder"), ("docx", "DOCX"), ("openpyxl", "XLSX"), ("xlrd", "XLS"), ("pptx", "PPTX"),
            ("msoffcrypto", "krypterade Office-filer"), ("bs4", "HTML"), ("markdownify", "HTML->md"),
            ("ebooklib", "EPUB"), ("extract_msg", "Outlook .msg"), ("win32com", "Office-COM (reserv)")]
    print(f"extract-this {VERSION}\n")
    for m, what in mods:
        ok = u.find_spec(m) is not None
        print(f"  [{'OK' if ok else '--'}] {m:14} {what}")
    try:
        import pymupdf
        import pymupdf4llm  # noqa: F401
        print(f"\n  PyMuPDF {pymupdf.__version__}, pymupdf4llm {pymupdf4llm.version} (måste vara samma version)")
    except Exception as e:
        print(f"\n  [!] pymupdf4llm går inte att importera: {e}")
    print(f"  Tesseract: {TESSERACT_EXE or 'SAKNAS (sätt XT_TESSERACT)'}  språk: {sorted(ocr.available_langs())}")
    print(f"  LibreOffice: {SOFFICE_EXE or 'SAKNAS (sätt XT_SOFFICE)'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Dokument -> markdown/text/jsonl (PDF med OCR, Office, HTML, EPUB, e-post, bilder …)",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("inputs", nargs="*", type=Path, help="filer och/eller mappar")
    ap.add_argument("-o", "--out", type=Path, help="utmapp (default: <indatamapp>/extracted)")
    ap.add_argument("-r", "--recursive", action="store_true", help="sök undermappar")
    ap.add_argument("-f", "--format", choices=["md", "txt", "jsonl"], default="md")
    ap.add_argument("--combine", type=Path, metavar="FIL", help="skriv ALLT till en fil (kunskapsbas)")
    ap.add_argument("--stdout", action="store_true", help="skriv till stdout i stället för fil")
    ap.add_argument("--pages", help="sidurval för PDF, t.ex. 1-10,15,20-")
    ap.add_argument("--ocr", choices=["auto", "force", "off"], default="auto",
                    help="auto = OCR bara på sidor utan (eller med korrupt) textlager")
    ap.add_argument("--lang", default="auto", help="Tesseract-språk: auto | eng | swe | swe+eng …")
    ap.add_argument("--dpi", type=int, default=300, help="OCR-upplösning (default 300)")
    ap.add_argument("--effort", choices=["best", "fast"], default="best",
                    help="best = förbehandling + upp till 6 OCR-varianter per svår sida; fast = ett försök")
    ap.add_argument("--mark-uncertain", action="store_true", help="markera osäkra OCR-ord som ⟦ord⟧")
    ap.add_argument("--no-review", action="store_true", help="spara inte granskningsbilder för osäkra sidor")
    ap.add_argument("--engine", choices=["auto", "layout", "plain"], default="auto",
                    help="PDF-textlager: layout (pymupdf4llm, struktur) eller plain (snabb råtext)")
    ap.add_argument("--images", action="store_true", help="spara inbäddade bilder i <utmapp>/<namn>_bilder/")
    ap.add_argument("--page-images", action="store_true", help="rendera HELA sidor som JPEG (kartor, deckplans)")
    ap.add_argument("--max-rows", type=int, default=2000, help="maxrader per kalkylblad/CSV i markdown")
    ap.add_argument("--password", help="lösenord för krypterad PDF/Office-fil")
    ap.add_argument("--force", action="store_true", help="skriv över befintliga utfiler")
    ap.add_argument("--check", action="store_true", help="visa beroenden och avsluta")
    ap.add_argument("--debug", action="store_true", help="visa full traceback vid fel")
    a = ap.parse_args()

    if a.check:
        check()
        return 0
    if not a.inputs:
        ap.error("ange minst en fil eller mapp (eller --check)")

    files = find_inputs(a.inputs, a.recursive, a.out)
    if not files:
        log("Inga filer i format som stöds hittades.")
        return 1

    if a.combine:  # den samlade utfilen får aldrig läsas in i sig själv
        files = [(f, b) for f, b in files if f.resolve() != a.combine.resolve()]

    # Unika utfiler: samma stam i samma utmapp (notes.pdf + notes.docx, eller två
    # indatamappar med varsin readme.md, eller a.pdf + a.PDF på Windows) får
    # filändelse och vid behov löpnummer – annars skrev den ena över/hoppade den andra.
    targets: dict[tuple, Path] = {}
    used: set[str] = set()
    for f, base in files:
        out_root = a.out or (base / "extracted")
        rel_dir = f.parent.relative_to(base) if f.parent != base else Path()
        folder = out_root / rel_dir
        name = safe_filename(f.stem)
        cand = folder / (name + EXT[a.format])
        if str(cand).lower() in used:
            cand = folder / (safe_filename(f"{f.stem}_{f.suffix.lstrip('.').lower()}") + EXT[a.format])
        n = 2
        while str(cand).lower() in used:
            cand = folder / (safe_filename(f"{f.stem}_{f.suffix.lstrip('.').lower()}_{n}") + EXT[a.format])
            n += 1
        used.add(str(cand).lower())
        targets[(f, base)] = cand

    # Strömmande utdata: --combine/--stdout skrivs löpande, så att en kunskapsbas
    # av tusentals dokument aldrig ligger i minnet på en gång.
    sep = "\n\n---\n\n" if a.format == "md" else ("\n\n" if a.format == "txt" else "")
    sink, first = None, True
    if a.combine:
        if a.combine.exists() and not a.force:
            log(f"[!] {a.combine} finns redan – kör med --force för att skriva över")
            return 1
        a.combine.parent.mkdir(parents=True, exist_ok=True)
        sink = open(a.combine, "w", encoding="utf-8")
    elif a.stdout:
        sink = sys.stdout
    report, t0 = [], time.time()
    n_ok = n_skip = n_fail = 0
    log(f"extract-this {VERSION}: {len(files)} fil(er)")

    for f, base in files:
        target = targets[(f, base)]
        name = target.stem
        out_root, rel_dir = target.parent, Path()
        to_file = not a.stdout and not a.combine

        if to_file and target.exists() and not a.force and target.stat().st_mtime >= f.stat().st_mtime:
            n_skip += 1
            log(f"  [SKIP] {f.name} (finns redan: {target}; --force för att göra om)")
            continue

        log(f"  -> {f.name}")
        t1 = time.time()
        try:
            img_dir = (out_root / rel_dir / f"{safe_filename(name)}_bilder") if (a.images or a.page_images) else None
            rev_dir = None if a.no_review else (out_root / rel_dir / f"{safe_filename(name)}_granska")
            doc = extract_file(f, pages=a.pages, ocr=a.ocr, lang=a.lang, engine=a.engine, dpi=a.dpi,
                               images_dir=img_dir, page_images=a.page_images, max_rows=a.max_rows,
                               password=a.password, effort=a.effort, review_dir=rev_dir,
                               mark_uncertain=a.mark_uncertain)
        except Exception as e:
            n_fail += 1
            log(f"     FEL: {type(e).__name__}: {e}")
            if a.debug:
                traceback.print_exc()
            report.append({"file": str(f), "ok": False, "error": f"{type(e).__name__}: {e}"})
            continue

        for w in doc.warnings:
            log(f"     [!] {w}")
        if doc.chars < 20:
            log("     [!] nästan ingen text extraherad")

        if sink is not None:
            if a.format == "md":
                piece = (render_md(doc) if (a.stdout and len(files) == 1)
                         else f"<!-- källa: {f} -->\n" + render_md(doc, with_frontmatter=False))
            elif a.format == "txt":
                piece = (f"===== {f.name} =====\n\n" if len(files) > 1 else "") + render_txt(doc)
            else:
                piece = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in iter_jsonl(doc))
            if not first:
                sink.write(sep)
            sink.write(piece)
            sink.flush()
            first = False
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render(doc, a.format), encoding="utf-8")

        n_ok += 1
        methods = {}
        for s in doc.sections:
            methods[s["method"]] = methods.get(s["method"], 0) + 1
        rec = {"file": str(f), "ok": True, "out": None if (a.stdout or a.combine) else str(target),
               "chars": doc.chars, "methods": methods, "warnings": doc.warnings,
               "review": doc.meta.get("review") or {}, "seconds": round(time.time() - t1, 1)}
        report.append(rec)
        log(f"     {doc.chars:,} tecken, {methods}, {rec['seconds']}s" + (f" -> {target}" if rec["out"] else ""))

    if a.combine and sink is not None:
        sink.close()
        log(f"Samlad fil: {a.combine}")

    if not a.stdout and not a.combine and n_ok:
        rep_dir = a.out or (files[0][1] / "extracted")
        rep_dir.mkdir(parents=True, exist_ok=True)
        (rep_dir / "_extract_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    n_rev = sum(len(r.get("review") or {}) for r in report if r.get("ok"))
    log(f"Klart på {time.time() - t0:.0f}s: {n_ok} ok, {n_skip} hoppade över, {n_fail} fel"
        + (f" | {n_rev} sidor att granska visuellt (se 'review' i frontmatter/_extract_report.json)" if n_rev else ""))
    return 0 if not n_fail else 2


if __name__ == "__main__":
    sys.exit(main())
