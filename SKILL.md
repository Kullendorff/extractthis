---
name: extract-this
description: 'Extrahera text och material ur VILKA dokument som helst – PDF (digital, inskannad, korrupt OCR-lager), dåliga scans (journaler, fax, fotokopior, mobilfoton, handskrift), Word/DOC/RTF/ODT, Excel/XLS/ODS/CSV, PowerPoint/PPT, EPUB, HTML, e-post (EML/MSG), bilder – till markdown, text eller jsonl. Använd när Johan vill extrahera, OCR:a, "läsa in", konvertera till markdown/text, bygga kunskapsbas eller sökindex av dokument, plocka ut bilder ur PDF, eller strukturerad data (statblock, besvärjelser, tabeller) ur regelböcker. Triggers: "extrahera", "OCR", "läs in pdf:en", "gör om till markdown", "scannad", "journal", "kunskapsbas", "extract text", "/extract-this".'
---

# extract-this

Ett verktyg för all textextraktion. Byggt 2026-10-10 av det bästa ur ~20 egna skript
(convert_to_markdown, extract_weg v4, dungeoncrawl/pdfio) och kalibrerat mot riktiga
dåliga scans: 28 WEG/Darkstryder/EON-böcker, CIA-memon och handskrivna brev/dagböcker.

**Skript:** `C:/Users/kulle/.claude/skills/extract-this/scripts/` (förkortas `$X` nedan)

## Beslutsträd

| Situation | Gör så här |
|---|---|
| Liten digital PDF (≤ ~20 s.) som bara ska *läsas* | Claudes `Read` direkt – inga skript |
| Okänd/stor/inskannad PDF, eller en mapp | `python $X/probe.py FIL` först, sedan `extract.py` |
| Vilket dokument som helst → markdown | `python $X/extract.py FIL` |
| Många filer → en kunskapsbas | `python $X/extract.py MAPP -r --combine kunskap.md` |
| RAG/sökindex | `--format jsonl` (en post per sida resp. ##-avsnitt) |
| Statblock/besvärjelser/tabeller → JSON | `xt.structured` – se `references/structured-recipes.md` |
| Fylla/slå ihop/dela PDF, formulär | `anthropic-skills:pdf` (inte denna) |
| Redigera Word | `anthropic-skills:docx` (inte denna) |

## Kommandon

```bash
python $X/extract.py bok.pdf                     # -> <bokens mapp>/extracted/bok.md
python $X/extract.py bok.pdf --stdout --pages 1-5
python $X/extract.py MAPP -r -o D:/ut            # speglar undermappar, återupptagbart
python $X/extract.py scan.pdf --lang swe+eng     # tvinga språk (default auto)
python $X/extract.py journal.pdf --mark-uncertain   # osäkra OCR-ord som ⟦ord⟧
python $X/extract.py bok.pdf --images            # inbäddade bilder -> extracted/bok_bilder/
python $X/extract.py karta.pdf --page-images     # hela sidor som JPEG (kartor, deckplans)
python $X/extract.py fil.docx --password hemligt
python $X/extract.py --check                     # beroenden + Tesseract-språk
```

Övriga flaggor: `--ocr auto|force|off`, `--effort best|fast`, `--engine layout|plain`,
`--dpi 300`, `--format md|txt|jsonl`, `--max-rows 2000`, `--force`, `--no-review`.
Befintliga utfiler hoppas över (nyare än källan) – en avbruten batch fortsätter där den
slutade. OCR cachas per sida i `%LOCALAPPDATA%/extract-this/cache/`.

## Stora filer

Minnet hålls konstant: PDF bearbetas i block om 40 sidor (bara färdig text sparas),
kalkylblad och CSV läses strömmande (bara de första `--max-rows` raderna behålls, resten
räknas), och `--combine`/`--stdout` skrivs löpande. Uppmätt (8 trådar):

| Fil | Tid | Toppminne |
|---|---|---|
| Inskannad WEG-bok, 96 s. | ~2 min | – |
| Inskannad PDF 464 MB, 240 s. | ~7,5 min (~1,8 s/sida) | ~0,7 GB (+ Tesseract ~0,4 GB) |
| Digital PDF 2000 s., layoutmotorn | ~5,5 min | ~430 MB (samma som vid 300 s.) |
| Digital PDF 2000 s., `--engine plain` | ~1 min | ~230 MB |
| XLSX 300 000 rader | ~37 s | ~85 MB |
| CSV 2 miljoner rader (57 MB) | ~4 s | ~30 MB |

OCR-minnet styrs av antalet trådar (default min(8, kärnor/2), ~60–80 MB per tråd):
sätt `XT_WORKERS=2` på en svag maskin.

- **Kör jobb över ~100 sidor i bakgrunden** (`run_in_background`) och läs loggen.
- **Jätteböcker:** prova `--pages 1-20` först för att se kvaliteten innan hela körningen.
- **Bara söka/citera i en enorm digital PDF?** `--engine plain` är 5–10× snabbare än layout.
- Utfilen för en 2000-sidorsbok kan bli 5–10 MB markdown – läs den med `Read` offset/limit
  eller `Grep` på `<!-- s. N -->`-markörerna, aldrig hela på en gång.
- Textfiler > 200 MB konverteras inte (de är redan text) – läs dem i delar.

## Så fungerar PDF-motorn (per sida)

1. **Frisk textlagersida** → `pymupdf4llm` med GNN-layout: läsordning, rubriker,
   tabeller, sidhuvud/-fot bort.
2. **Ingen text / inskannad sida** → egen Tesseract-pipeline: OSD-rotation,
   uträtning, belysningsutjämning, upp till 6 varianter (binarisering, PSM 3/4/6/11,
   uppskalning) – bästa väljs på konfidens × täckning.
3. **Textlager som ligger *bakom* en scan** (osynlig text, renderingsläge 3) litas bara på
   om ≥ 96 % av orden är riktiga ord; annars OCR:as sidan om (vår OCR var lika bra eller
   bättre på varje testad sida). Korrupt gammal OCR (WEG40141) upptäcks och görs om.

Varje sida i utdata får en markör: `<!-- s. 12 -->`, `<!-- s. 13 | ocr 91% -->`.
Frontmatter visar `text_layer`, `methods` (antal sidor per metod), `warnings` och `review`.

## ⚠ Granskning – det som gör det tillförlitligt

OCR misslyckas ofta **tyst**: Tesseract läser förtryckt formulärtext med 90 % konfidens
och hoppar helt över handskriften; den kan tappa hela rader utan att konfidensen sjunker.
Därför mäts per sida även **täckning** (andel textrader på sidan som blev ord) och
**oläst skrift** (bläckrader som inte lästs – handskrift). Osäkra sidor:

- listas under `review:` i frontmatter (sida, konfidens, orsak, bild) och i `_extract_report.json`
- markeras `<!-- s. 7 | ocr 51% | GRANSKA (orsak): ...\bok_granska\bok_s007.png -->`
- får sin sida sparad som PNG i `extracted/<namn>_granska/`
- osäker brustext (bild/handskrift/blek text) göms i `<!-- osäker OCR ...: ... -->` i
  markdown i stället för att slängas – **läs dessa kommentarer vid granskningen**; i
  `txt`/`jsonl` rensas de bort (så att sökindex inte fylls med bildbrus)

**När extraktionen är klar och `review` inte är tom – gör alltid detta:**
1. `Read` varje granskningsbild (de är ~1800 px, läsbara).
2. Jämför med texten för sidan. Rätta fel; **transkribera handskrift** själv från bilden.
3. Skriv in rättelsen i .md-filen under sidmarkören, och ändra markören till
   `<!-- s. 7 | granskad visuellt -->`. Är sidan bara en bild utan text: skriv
   `*[Helsidesillustration: kort beskrivning]*`.
4. Rapportera till Johan hur många sidor som granskades och om något var oläsligt.
Vid > ~25 granskningssidor: fråga Johan om alla ska tas, eller bara de med orsak
"handskrift"/"tappade rader" (illustrationssidor med "låg konfidens" är ofta ofarliga).

## Format som stöds

PDF, XPS, CBZ, FB2, MOBI · DOCX/DOCM, DOC, RTF, ODT, WPD · XLSX/XLSM, XLS, ODS, CSV/TSV ·
PPTX, PPT, ODP · EPUB · HTML · EML, MSG · PNG/JPG/TIFF (flersidig)/BMP/GIF/WEBP (OCR) ·
JSON/XML/YAML/TOML/INI · TXT/MD/RST/LOG/SRT/VTT.
Äldre format konverteras via LibreOffice headless (egen profil, så den hänger inte om
LibreOffice är öppet), med Word/PowerPoint/Excel-COM som reserv.

## Från Python

```python
import sys; sys.path.insert(0, r"C:/Users/kulle/.claude/skills/extract-this/scripts")
from xt.router import extract_file, render_md
doc = extract_file(r"D:/fil.pdf", pages="1-10")        # Doc(sections, meta, warnings, outline)
md = render_md(doc); doc.meta["review"]                   # {sida: {conf, reason, image}}
```

## Lärdomar och fallgropar

Se `references/lessons.md` – läs den innan du ändrar trösklar eller lägger till format.
Kort: Tesseract ligger INTE i PATH; stoppord duger inte som skräpdetektor (stat-tabeller);
PDF:er använder tre olika minustecken; PyMuPDF är inte trådsäkert; pymupdf4llm kräver
exakt samma version som PyMuPDF.

## Underhåll

- Tester: `python -m pytest tests -q` (bygger fixtures själv; ~1 min).
- OCR-mätning mot facit: `python tests/bench_ocr.py`.
- Ändrar du `ocr_robust`/förbehandling: bumpa `PIPELINE_VERSION` i `xt/ocr.py` (ogiltigförklarar cachen).
- Beroenden: `pip install -r requirements.txt` + Tesseract (språk eng+swe+osd) + LibreOffice.
  Hittas de inte: miljövariablerna `XT_TESSERACT`, `XT_SOFFICE`; cache: `XT_CACHE`.
- Repo: https://github.com/Kullendorff/extractthis
