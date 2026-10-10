# Strukturerad extraktion (regelbok → JSON)

För när Johan vill ha *data* ur en bok – monster, besvärjelser, utrustning, skepp – inte
bara löptext. Verktygen finns i `scripts/xt/structured.py` (generaliserad från
`dungeoncrawl/tools/pdfio.py`, validerad mot PF2 Bestiary 1: 409/411 statblock rena).

## Arbetsgång

1. **Kalibrera för hand först.** Dumpa 2–3 sidor med `Book(...).lines(s, s)` och läs
   radstrukturen innan du skriver regex. Varje bok har sina egenheter.
2. **Avgränsa med innehållsförteckningen:** `start, end = b.toc_range("Spells", level=1)`.
   Dubbletter skiljs med `level=` eller `after_page=`.
3. **Chunka på en markörrad:** `b.chunk_by_pattern(lines, re.compile(rf"^CREATURE ({SIGN}?\d+)$"))`.
   Namnet står ofta på raden *före* markören (`name_offset=-1`).
4. **Kapa svansen:** `trim_chunk_body(body)` stoppar vid sidnummer/kapitelbrus; sista posten
   i ett kapitel suger annars in nästa kapitels sidopaneler (feats.json svällde till 5 MB).
   `caps_needed=1` om sidopaneler börjar med EN versalrad.
5. **Fält:** `fields_in_order(lines, ["Perception", "Languages", "Skills", "Str", "AC", "HP"])`
   ger kända fält + fritextspann (förmågor utan etikett).
6. **Källa på varje post:** `rec["source"] = b.source(chunk["page"])` → `{"book", "page"}`.
7. **Verifiera antal:** jämför antal chunkar med förväntat (index, TOC). En missad markör
   (fel minustecken!) gör att nästa post sväljs in i föregående – syns bara som för få poster.

## Exempel

```python
import re, sys, json
sys.path.insert(0, r"C:/Users/kulle/.claude/skills/extract-this/scripts")
from xt.structured import Book, SIGN, to_int, trim_chunk_body, fields_in_order

b = Book(r"D:/.../PF 2E - Bestiary.pdf", code="B1")
start, end = b.toc_range("Monsters A-Z", level=1)           # anpassa till bokens TOC
mons = []
for ch in b.chunk_by_pattern(b.lines(start, end), re.compile(rf"^CREATURE ({SIGN}?\d+)$")):
    body, cut = trim_chunk_body(ch["lines"][1:], max_lines=80, caps_needed=1)
    fields, free = fields_in_order(body, ["Perception", "Languages", "Skills", "Str", "Items", "AC", "HP", "Speed"])
    mons.append({"name": ch["name"], "level": to_int(ch["match"].group(1)), **fields,
                 "source": b.source(ch["page"]), "truncated": cut})
print(len(mons))   # jämför mot förväntat!
json.dump(mons, open("monsters.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
```

## Inskannade böcker

`Book` läser textlagret. För inskannade böcker: kör först `extract.py bok.pdf` (OCR,
cachas), och arbeta sedan mot markdownfilen: dela på `<!-- s. N -->`-markörerna för
sidnummer och använd samma chunk-/regexidé på raderna.

## Referensimplementationer

`references/examples/` innehåller de ursprungliga, fungerande extraktorerna som recept:
- `extract_monsters.py` – PF2 statblock (svåraste fallet: fält i fast ordning + fritext)
- `extract_spells.py` – besvärjelser med varierande fältrader
- `extract_weg.py` – WEG-bok → Obsidian-noter med bildnoter, taggar, index (SW D6-vaulten)
De importerar `pdfio`, som motsvaras av `xt.structured` här.
