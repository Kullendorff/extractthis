#!/usr/bin/env python3
"""Snabb diagnos INNAN extraktion – skriver inget. Kör alltid detta först på
okända/stora PDF:er: visar textlagerklass, hur många sidor som kräver OCR,
bokmärken, formulär och en uppskattad körtid.

  python probe.py bok.pdf
  python probe.py "D:/mapp" -r          # alla filer i mappen
  python probe.py bok.pdf --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xt.common import log  # noqa: E402
from xt.router import IMAGE, OFFICE, PDF_LIKE, SUPPORTED  # noqa: E402


def probe_pdf(path: Path) -> dict:
    from xt.pdf import classify_doc, open_pdf, page_plan

    try:
        doc = open_pdf(path)
    except PermissionError as e:
        return {"file": str(path), "error": str(e), "encrypted": True}
    try:
        return _probe_open(doc, path)
    finally:
        doc.close()


def _probe_open(doc, path: Path) -> dict:
    from xt.pdf import classify_doc, page_plan

    info = classify_doc(doc)
    plan = page_plan(doc, range(doc.page_count), "auto", info)
    counts = {k: sum(1 for v in plan.values() if v == k) for k in ("text", "ocr", "blank")}
    n_img = sum(len(doc[i].get_images()) for i in range(doc.page_count))
    toc = doc.get_toc()
    workers = max(1, min(8, (os.cpu_count() or 2) // 2))
    # Uppmätt 2026-10-10: layout ~0,2 s/sida; OCR ~15 s/sida/tråd (svåra sidor kör fler varianter)
    est = counts["text"] * 0.25 + counts["ocr"] * 15.0 / workers
    ocr_pages = [i + 1 for i, v in plan.items() if v == "ocr"]
    r = {
        "file": str(path), "pages": doc.page_count, "mb": round(path.stat().st_size / 1e6, 1),
        "text_layer": info["class"], "chars_per_page": info["chars_per_page"],
        "known_words": info["known_words"], "hidden_text": info["hidden_text"], "font_size_concentration": info["size_concentration"],
        "lang": info.get("lang"), "garbage_layer": info.get("garbage_layer"),
        "plan": counts, "ocr_pages_sample": ocr_pages[:15],
        "images": n_img, "toc_entries": len(toc), "toc_top": [t for l, t, p in toc if l == 1][:12],
        "is_form": bool(doc.is_form_pdf), "est_seconds": round(est),
    }
    return r


def probe_other(path: Path) -> dict:
    r = {"file": str(path), "mb": round(path.stat().st_size / 1e6, 2), "type": path.suffix.lower()}
    if path.suffix.lower() in OFFICE:
        try:
            import msoffcrypto
            with open(path, "rb") as f:
                r["encrypted"] = msoffcrypto.OfficeFile(f).is_encrypted()
        except Exception:
            r["encrypted"] = False
    if path.suffix.lower() in IMAGE:
        from PIL import Image
        with Image.open(path) as im:
            r["size_px"] = im.size
            r["frames"] = getattr(im, "n_frames", 1)
        r["note"] = "bild -> OCR"
    return r


def recommend(r: dict) -> str:
    f = r["file"]
    if r.get("error"):
        return f'python extract.py "{f}" --password …'
    if "pages" not in r:
        return f'python extract.py "{f}"'
    tips = []
    if r.get("is_form"):
        tips.append("FORMULÄR: fältvärden -> pymupdf4llm.get_key_values() / anthropic-skills:pdf")
    if r["text_layer"] == "none" and r.get("lang") is None:
        tips.append("ren scan – språk okänt, överväg --lang swe+eng eller eng")
    if r["pages"] <= 20 and r["plan"]["ocr"] == 0:
        tips.append("liten digital PDF – Claudes Read-verktyg räcker om du bara ska läsa den")
    cmd = f'python extract.py "{f}"'
    if r["pages"] > 150:
        cmd += "  (stor – kör i bakgrunden; OCR cachas per sida, avbrott går att återuppta)"
    return cmd + ("\n      " + "\n      ".join(tips) if tips else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("-r", "--recursive", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    files = []
    for p in a.inputs:
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            it = p.rglob("*") if a.recursive else p.iterdir()
            files += sorted(f for f in it if f.is_file() and f.suffix.lower() in SUPPORTED
                            and "extracted" not in f.parts)
    results = []
    for f in files:
        try:
            results.append(probe_pdf(f) if f.suffix.lower() in PDF_LIKE else probe_other(f))
        except Exception as e:
            results.append({"file": str(f), "error": f"{type(e).__name__}: {e}"})

    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for r in results:
        print(f"\n{Path(r['file']).name}")
        if "pages" in r:
            print(f"  {r['pages']} sidor, {r['mb']} MB | textlager: {r['text_layer']}"
                  f" ({r['chars_per_page']} tecken/sida, kända ord {r['known_words']}, dolt lager {r['hidden_text']}, font-konc. {r['font_size_concentration']})"
                  f" | språk: {r['lang'] or '?'}")
            p = r["plan"]
            print(f"  plan: {p['text']} textsidor, {p['ocr']} OCR-sidor, {p['blank']} tomma"
                  + (f"  (OCR t.ex. s. {r['ocr_pages_sample']})" if p["ocr"] else ""))
            if r["garbage_layer"]:
                print("  ⚠ textlagret är korrupt gammal OCR – kommer att OCR:as om")
            print(f"  bilder: {r['images']} | bokmärken: {r['toc_entries']} | formulär: {'ja' if r['is_form'] else 'nej'}"
                  f" | uppskattad tid: ~{r['est_seconds']}s")
            if r["toc_top"]:
                print(f"  kapitel: {', '.join(r['toc_top'])}")
        else:
            print("  " + ", ".join(f"{k}: {v}" for k, v in r.items() if k != "file"))
        print("  -> " + recommend(r))
    return 0


if __name__ == "__main__":
    log_ = log  # noqa: F841
    sys.exit(main())
