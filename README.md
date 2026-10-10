# extract-this

**Text och bilder ur vilka dokument som helst – även de riktigt dåliga.** En Claude Code-skill
(och fristående CLI) som gör PDF:er, inskannade böcker och journaler, Office-filer, e-böcker,
e-post och bilder till ren markdown, text eller jsonl – med illustrationer, bildtexter och
korsreferenser på rätt plats.

*English summary: document → markdown/text/jsonl extractor and Claude Code skill. Hybrid
PDF engine (pymupdf4llm layout for digital pages, a robust Tesseract pipeline for scans),
per-page reliability checks that catch the failures OCR normally makes silently (dropped
lines, unread handwriting, corrupt hidden OCR layers) and flag those pages for visual
review. Extracts figures – including illustrations cropped out of scanned pages and vector
graphics – places them in the text with captions, and links cross-references ("see Figure 3",
"see page 61"). Obsidian mode with image notes. Swedish + English OCR. Comments in Swedish.*

## Vad den klarar

**Text**
- **Hybrid per sida:** digitala sidor via `pymupdf4llm` (läsordning, rubriker, tabeller),
  inskannade sidor via egen OCR – i samma dokument.
- **Dåliga scans:** rotation (verifierad), uträtning, belysningsutjämning, upp till 6
  OCR-varianter per svår sida. Medelfel per tecken på skadade testscans: **0,5 %**.
- **Fångar tysta fel:** tappade rader (täckningsmått), oläst handskrift på formulär,
  korrupta gamla OCR-lager. Osäkra sidor sparas som bilder och listas för granskning.
- **Korrupta textlager** upptäcks via andel riktiga ord (en + sv) och OCR:as om.

**Bilder** (`--images`, `--obsidian`, `--images-raw`)
- **Digitala PDF:er:** inbäddade bilder *och vektorgrafik* (kartor, diagram) med bildtext.
- **Inskannade böcker:** illustrationer klipps ut ur sidan i färg – textspalter, handskrift,
  formulärramar och statistikrutor med bakgrundskonst blir *inte* figurer.
- **Word / PowerPoint / EPUB:** bilder på sin plats, bildtext från formatmall/alt-text.
- Logotyper och dekor som upprepas på varje sida filtreras bort.
- **Korsreferenser:** "se Figur 3" → figuren, "se sidan 61" → rätt PDF-sida (tryckt
  sidnumrering detekteras automatiskt), bokmärken → sidor.
- **Obsidian:** `![[bild]]`, blockreferenser och en bildnot per figur (källa, sida, bildtext, kontext).

**Stora filer** – minnet växer inte med filstorleken:

| Fil | Tid | Toppminne |
|---|---|---|
| Inskannad PDF 464 MB, 240 sidor | ~7,5 min | ~0,7 GB |
| Digital PDF 2000 sidor (layoutmotorn) | ~5,5 min | ~430 MB |
| XLSX 300 000 rader | ~37 s | ~85 MB |
| CSV 2 miljoner rader | ~4 s | ~30 MB |

**Format:** PDF, XPS, CBZ, EPUB, MOBI · DOCX/DOCM/DOC/RTF/ODT · XLSX/XLS/ODS/CSV ·
PPTX/PPT/ODP · HTML · EML/MSG · PNG/JPG/TIFF (flersidig) m.fl. · JSON/XML/YAML · text.

## Installation

```bash
pip install -r requirements.txt
```

Systemprogram:
- **Tesseract OCR** med språken `eng`, `swe`, `osd`
  (Windows: [UB Mannheim-installern](https://github.com/UB-Mannheim/tesseract/wiki);
  `swe.traineddata` från [tessdata_best](https://github.com/tesseract-ocr/tessdata_best)).
- **LibreOffice** för .doc/.ppt/.odt/.rtf m.fl. (på Windows används Office-COM som reserv).

Miljövariabler vid behov: `XT_TESSERACT`, `XT_SOFFICE` (sökvägar), `XT_CACHE` (OCR-cache),
`XT_WORKERS` (antal OCR-trådar, styr minnet).

Som Claude Code-skill: klona till `~/.claude/skills/extract-this/`.

## Användning

```bash
python scripts/probe.py bok.pdf                    # diagnos först – skriver inget
python scripts/extract.py bok.pdf                  # -> extracted/bok.md
python scripts/extract.py bok.pdf --images         # + figurer i texten -> extracted/bok_bilder/
python scripts/extract.py bok.pdf --obsidian       # Obsidian-stil + bildnoter
python scripts/extract.py mapp -r --combine kunskap.md
python scripts/extract.py scan.pdf --mark-uncertain --lang swe+eng
python scripts/extract.py --check                  # beroenden
```

| Flagga | Betydelse |
|---|---|
| `-o DIR`, `-r` | utmapp, rekursivt (speglar undermappar, återupptagbart) |
| `-f md\|txt\|jsonl` | utformat; `--combine FIL` allt i en fil; `--stdout` |
| `--pages 1-10,15` | sidurval |
| `--ocr auto\|force\|off`, `--lang`, `--dpi`, `--effort best\|fast` | OCR-styrning |
| `--engine layout\|plain` | PDF-textlager: struktur eller snabb råtext |
| `--images`, `--obsidian`, `--image-dpi` | figurer i texten |
| `--images-raw`, `--page-images` | inbäddade originalbilder / hela sidor som JPEG |
| `--mark-uncertain`, `--no-review` | osäkra OCR-ord som ⟦ord⟧ / inga granskningsbilder |
| `--password`, `--max-rows`, `--force` | krypterade filer, kalkylbladsrader, skriv över |

Se [SKILL.md](SKILL.md) för beslutsträd och granskningsflödet, och
[references/lessons.md](references/lessons.md) för varför trösklarna är som de är –
varje regel där är en uppmätt bugg eller kalibrering.

## Tester

```bash
python -m pytest tests -q          # bygger syntetiska fixtures (journaler, fax, figurer, Office …)
python tests/bench_ocr.py          # teckenfelfrekvens mot facit, fast vs best
```

Kalibrerad mot riktiga dokument (ingår inte i repot): WEG Star Wars D6- och
Darkstryder-böcker, EON-regelböcker, avhemligade CIA-memon och handskrivna brev/dagböcker
från Internet Archive.

## Licens

MIT
