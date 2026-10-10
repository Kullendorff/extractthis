# Lärdomar – varför koden ser ut som den gör

Varje punkt är en bugg eller ett mätresultat. Ändra inte motsvarande kod utan att
läsa punkten och köra `pytest` + `bench_ocr.py` efteråt.

## Textlager och klassificering

- **Stoppord duger inte som skräpdetektor.** *D6 Starships Stats* (336 s. stat-tabeller)
  har stoppordsandel 0,02 men ett perfekt textlager – den gamla regeln (< 0,093) hade
  OCR:at 335 felfria sidor. Ersatt av **andel kända ord** (wordfreq en+sv, ord ≥ 3 tecken):
  frisk text 0,91–0,99 (även EON på svenska), korrupt WEG40141 0,695 → gräns 0,85.
- **Bakgrundsbild ≠ scan.** WEG40120 har en bild över 76 % av sidan med *synlig* text
  ovanpå. En scan känns igen på **osynlig text** (`get_texttrace()` typ 3) under en
  helsidesbild. Att bara titta på bildtäckning gav falska scans.
- **Dolt OCR-lager bakom scan** är någon annans OCR. Mätt: vår OCR ≥ lagret på varje
  testad sida (0,79→0,88; 0,857→0,934 kända ord; lagret tappade 25 % av orden).
  Därför krävs ≥ 0,96 kända ord för att lita på lagret.
- **Korrupt OCR syns inte på fontstorlekar** (WEG40141 har perfekt diskreta storlekar).
- pymupdf4llm:s `force_ocr` gör INTE om OCR när ett textlager finns (testat) – därför egen pipeline.
- pymupdf4llm kräver exakt samma version som PyMuPDF (import kastar annars). Uppgradera båda samtidigt.

## OCR

- **Tesseract ligger inte i PATH**: `C:\Program Files\Tesseract-OCR\tesseract.exe` (sätts i `common.py`).
  Språk installerade: eng, swe (tessdata_best), osd.
- **Sortera på Tesseracts (block, par, line)**, aldrig på y-position – annars blandas
  spalterna i tvåspaltiga böcker (PSM 3 är kolumnmedveten).
- **Uträtning:** projektionsprofil på belysningsnormaliserad, kantbeskuren, tecken-filtrerad
  bild. Utan det gav en grå rotationskant + gradient −6,5° i stället för −3,2° och sidan
  blev snedare.
- **Konfidens ser inte tappade rader.** Fyra hela rader försvann med 95 % konfidens på
  resten. Därför **täckning**: andel tecken-blobbar i riktiga textrader (≥ 4 tecken, lik
  höjd/baslinje) som hamnar i en ordruta. Stjärnfält/skraffering i illustrationer räknas
  inte (de klarar inte radkravet).
- **Handskrift på formulär** (krigsdagbok 1915): Tesseract läser den tryckta blanketten
  med 87 % konfidens och hoppar tyst över all blyerts. `unread_lines()` räknar radband av
  oläst streckbläck (adaptiv tröskel fångar blyerts). Handskriftens över/underhäng flyter
  ihop raderna → räkna band i projektionsprofilen, inte komponenter. Illustrationer maskas:
  stora+täta komponenter (fyllnad > 4,5 %; formulärramar är glesare) och texturytor
  (lokal bläcktäthet > 20 %).
- **Låg konfidens ≠ fel ord.** Text över en blek serieruta (WEG40122 s.61) gav "captured",
  "reward" under 30 % konfidens men rätt. Ord < 30 behålls om de är kända ord.
- **Bildbrus:** andel säkra ord (konf ≥ 75) per stycke: brus ~0,1, riktig text ≥ 0,85.
  Brus bredvid bra text slängs; är hela sidan osäker göms texten i en HTML-kommentar och
  sidan flaggas – inget kastas tyst.
- **Ordlistekontroll fungerar inte på bildbrus** – nästan alla 3-bokstavskombinationer
  ("uly", "oak", "mld") finns i wordfreq.
- **Datum/personnummer är inte skräp.** Gamla filtret (`^[\d\s.\-]+$`, < 30 % bokstäver)
  slängde "2026-03-14 10:32" och "19121212-1212". Nu tas bara 1–4-siffriga sidnummer bort.
- **Återkommande sidhuvud:** första förekomsten behålls – i journaler bär det identiteten.
- **Jättesidor:** vissa scans har sidmått 2848×4272 pt → 211 Mpx vid 300 dpi
  (PIL DecompressionBombError). Rendering har tak 4500 px.
- **Rena scans utan textlager**: språket detekteras på 2 snabb-OCR:ade sidor (eng+swe)
  innan resten körs med rätt modell.
- PyMuPDF är inte trådsäkert: rendera i huvudtråden, kör Tesseract i trådpool.
- Anfang (drop cap): slå ihop före skräpfiltrering, med guard mot "TTatooine".

## Från den oberoende kodgranskningen (2026-10-10) – tyst dataförlust

- **Sidkrom-filtret får aldrig siffer-normalisera fritt.** "Datum: 2024-03-04" och
  "Blodtryck: 123/80" blev samma nyckel på varje sida och raderades från alla journal-
  sidor utom första. Nu: sidnummerrad = EXAKT ett tal som följer sidindex med konstant
  offset; allt annat måste vara exakt identiskt.
- **Skannad sida + påförd stämpel** ("Inkom … Dnr … sida 3 av 12", synlig text) gav
  "textsida" → bara stämpeln extraherades. Helsidesbild + < 300 tecken synlig text = scan.
- **Osäkert OCR-material kastas aldrig** – det göms i `<!-- osäker OCR … -->` (md) och
  rensas bort först i txt/jsonl. En blek rad eller handskriven signatur kan vara det
  viktigaste på sidan.
- **Romerska sidnummer:** bara giltiga i–xxxix och bara i sidkanten ("mild", "civil",
  "DIM" matchade det gamla `[ivxlcdm]{1,6}`). Ensamma tal mitt på sidan är doser/värden.
- **Anfangsfixen** kräver att versal+ord är mycket vanligare än ordet ensamt (zipf) och
  att ordet versalen satt på är känt utan den – annars "in … PCs" → "Pin … Cs".
- **Word:** textrutor (`w:txbxContent`, dubbletter under `mc:Fallback` hoppas över),
  fot-/slutnoter, `w:moveTo`. .docm/.dotx/.pptm/.ppsx: skriv om innehållstypen i minnet.
- **Kalkylblad:** formaterade tomrader räknas inte (gav falskt "990 rader utelämnade").
- **LibreOffice:** temp-mapp med profil raderas alltid; vid timeout dödas hela process-
  trädet (soffice.exe är bara en startare för soffice.bin). Word/Excel-COM får dummy-
  lösenord så att skyddade filer kastar i stället för att hänga på en dialog.
- **Fel isoleras:** en OCR-sida som kraschar, eller en layoutomgång som kraschar, fäller
  inte boken (sidan flaggas / omgången tas som råtext).
- **OSD-rotation verifieras** med snabb-OCR av båda orienteringarna (EON s.14 vändes 180°).
- Minne: texturanalysen görs i 1/4-upplösning; färre förrenderade sidor per omgång.
- UTF-16/32 känns igen på BOM; e-post med `charset="unknown-8bit"` avkodas som utf-8 (replace).

## Office m.m.

- `<w:b w:val="0"/>` betyder INTE fetstil (gamla skriptet tolkade det som fet).
- Text i hyperlänkar ligger i `w:hyperlink`, inte direkt under `w:p` – gamla skriptet tappade den.
- Numrering ärvs ofta från stilen ("List Number") – slå upp numPr i stildefinitionen.
- Svensk Word: Rubrik = Title, "Rubrik 1" = Heading 1.
- openpyxl `data_only=True` ger cachade formelvärden – en fil skapad av Python (aldrig
  öppnad i Excel) har inga cachade värden (formelcell blir tom).
- EPUB: läs i `spine`-ordning, inte `get_items()`.
- Textfiler: prova utf-8-sig → cp1252 → latin-1 (latin-1 lyckas alltid, så den sist).
- LibreOffice headless måste få en egen `-env:UserInstallation`, annars hänger den om
  Johan har LibreOffice öppet. .doc→docx tar ~15–20 s (uppstart).

## PDF-text (layoutmotorn)

- pymupdf4llm slår ihop radbrutna rader med mellanslag: "par- ticularly" och mjukt
  bindestreck "par\u00ad ticularly". Ihopslagning undantar "och/eller/and/or/to/till"
  ("för- och nackdelar").
- PDF:er använder tre minustecken: `-`, EN DASH `–` (U+2013) och MINUS `−` (U+2212).
  Använd `structured.SIGN` i alla tal-regexar och `to_int()`.
