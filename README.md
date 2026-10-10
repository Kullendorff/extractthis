# extract-this

**Text ur vilka dokument som helst – även de riktigt dåliga.** En Claude Code-skill
(och fristående CLI) som gör PDF:er, scans, Office-filer, e-böcker, e-post och bilder
till ren markdown, text eller jsonl.

*English summary: document → markdown/text/jsonl extractor and Claude Code skill.
Hybrid PDF engine (pymupdf4llm layout for digital pages, a robust Tesseract pipeline for
scans), with per-page reliability checks that catch the failures OCR normally makes
silently – dropped lines, unread handwriting, corrupt hidden OCR layers – and flag those
pages for visual review. Swedish + English OCR out of the box. Code comments are in Swedish.*

## Vad den gör bra

- **Hybrid per sida:** digitala sidor via `pymupdf4llm` (läsordning, rubriker, tabeller),
  inskannade sidor via egen OCR-pipeline – i samma dokument.
- **Dåliga scans:** automatisk rotation (med verifiering), uträtning, belysnings-
  utjämning, upp till 6 OCR-varianter per svår sida; bästa väljs.
- **Fångar tysta fel:** täckningsmått (tappade rader syns inte i konfidensen),
  detektor för oläst skrift (handskrift på formulär), korrupta gamla OCR-lager.
- **Granskning:** osäkra sidor sparas som bilder och listas i frontmatter, så att
  en människa – eller Claude – kan läsa sidan visuellt och rätta.
- **Stora filer:** sidblock och strömmande läsning; tusentals sidor och miljontals
  kalkylbladsrader med konstant minne. OCR-cache per sida → avbrutna körningar återupptas.
- **Format:** PDF, XPS, CBZ, EPUB, MOBI, DOCX/DOC/RTF/ODT, XLSX/XLS/ODS/CSV,
  PPTX/PPT/ODP, HTML, EML/MSG, PNG/JPG/TIFF m.fl.
- **Strukturerad data:** verktyg för att plocka statblock/besvärjelser/tabeller ur
  regelböcker till JSON (`scripts/xt/structured.py`).

## Installation

```bash
pip install -r requirements.txt
```

Systemprogram:
- **Tesseract OCR** med språken `eng`, `swe`, `osd`
  (Windows: [UB Mannheim-installern](https://github.com/UB-Mannheim/tesseract/wiki);
  `swe.traineddata` från [tessdata_best](https://github.com/tesseract-ocr/tessdata_best)).
- **LibreOffice** (för .doc/.ppt/.odt/.rtf m.fl.). På Windows används Word/PowerPoint-COM som reserv.

Hittas inte programmen automatiskt: sätt `XT_TESSERACT` / `XT_SOFFICE` till sökvägen.
Cache: `XT_CACHE` (default `%LOCALAPPDATA%/extract-this/cache` resp. `~/.cache/extract-this`).

Som Claude Code-skill: klona till `~/.claude/skills/extract-this/`.

## Användning

```bash
python scripts/probe.py bok.pdf                 # diagnos först – skriver inget
python scripts/extract.py bok.pdf               # -> extracted/bok.md
python scripts/extract.py mapp -r --combine kunskap.md
python scripts/extract.py scan.pdf --mark-uncertain --lang swe+eng
python scripts/extract.py --check               # beroenden
```

Se [SKILL.md](SKILL.md) för beslutsträd, alla flaggor och granskningsflödet, och
[references/lessons.md](references/lessons.md) för varför trösklarna är som de är.

## Tester

```bash
python -m pytest tests -q          # bygger syntetiska fixtures (journaler, fax, Office …)
python tests/bench_ocr.py          # teckenfelfrekvens mot facit, fast vs best
```

## Licens

MIT
