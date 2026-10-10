"""Extraherar rules/spells.json -- spells, cantrips och focus spells ur
CRB (Spell Descriptions s.317-386, Focus Spells s.387-408) och APG
(Spell Descriptions s.215-228, Focus Spells s.229-240).

Samma "NAMN"-pa-egen-rad + "SPELL N"/"CANTRIP N"/"FOCUS N"-markor-monster
som feats (se extract_feats.py) -- ateranvander pdfio.trim_chunk_body av
samma anledning (varierande faltrader -- Traditions/Cast/Range/Area/
Targets/Duration/Saving Throw/Trigger/Requirements/Cost/Frequency -- fore
brodtexten, med risk for kapitelbrus om en chunk skulle lopa langt).
Verifierat mot Magic Missile, Fireball och Heal (CRB) i chattloggen
2026-08-31.

Traits star ibland flera pa SAMMA rad, mellanslagsseparerade (t.ex.
"COMPOSITION EMOTION ENCHANTMENT MENTAL" for bard-kompositionsspells i
Focus Spells-sektionen) istallet for en per rad som i Feats-kapitlet --
delar darfor varje traitrad pa whitespace istallet for att ta hela raden
som en enda trait.
"""
import json
import re
from pathlib import Path

from pdfio import book_lines, book_source, chunk_by_pattern, get_toc, toc_range, trim_chunk_body

OUT_PATH = Path(__file__).resolve().parent.parent / "rules" / "spells.json"

SPELL_MARKER = re.compile(r"^(SPELL|CANTRIP|FOCUS) (\d+)$")
TRAIT_LINE_RE = re.compile(r"^[A-Z][A-Z '\-]*$")

FIELD_RE = re.compile(
    r"^(Traditions|Cast|Range|Area|Targets|Duration|Saving Throw|Trigger|"
    r"Requirements|Cost|Frequency)\b(.*)$"
)


def _split_traits(lines):
    """Konsumera versalrader fran borjan av body -- varje rad kan innehalla
    flera mellanslagsseparerade traits (PF2e-traits ar alltid enkla ord)."""
    traits = []
    i = 0
    while i < len(lines) and TRAIT_LINE_RE.match(lines[i][1].strip()) and len(lines[i][1].strip()) <= 40:
        traits.extend(lines[i][1].strip().split())
        i += 1
    return traits, lines[i:]


_DIGIT_ONLY_RE = re.compile(r"^\d+$")


def _extract_fields(lines):
    """Plocka ut kanda faltrader (Traditions/Cast/Range/.../Frequency) som
    ligger i sviit direkt efter traits -- varje falt kan sla over flera
    rader tills nasta kanda faltetikett eller brodtext (som inte matchar
    FIELD_RE) dyker upp.

    En ensam sifferrad (sidnummer) hoppas over utan att avsluta faltblocket
    -- PyMuPDF interfolierar ibland ett sidnummer mitt i faltraderna nar en
    post spanner en sidbrytning (Bind Soul, CRB s.321-322, chattlogg
    2026-08-31)."""
    fields = {}
    i = 0
    current = None
    buf = []

    def flush():
        if current:
            fields[current] = " ".join(buf).strip()

    while i < len(lines):
        raw = lines[i][1].strip()
        if _DIGIT_ONLY_RE.match(raw):
            i += 1
            continue
        m = FIELD_RE.match(raw)
        if m:
            flush()
            current = m.group(1).lower().replace(" ", "_")
            buf = [m.group(2).strip()] if m.group(2).strip() else []
            i += 1
        elif current is not None and raw and raw[0].islower():
            # Faltet radbryter over tva fysiska rader utan att upprepa
            # etiketten (t.ex. "Cast ...; Requirements black sapphire with a"
            # / "gp value of..." -- Bind Soul, CRB s.321). En fortsattning
            # mitt i mening borjar alltid med gemen -- riktig brodtext
            # borjar alltid med versal ("You wrench...", "A roaring...").
            buf.append(raw)
            i += 1
        elif current is not None:
            break
        else:
            break
    flush()
    return fields, lines[i:]


def _class_ranges(book, page):
    """Nivå-3 TOC-poster under "Focus Spells" ger exakta sidintervall per
    klass (se chattlogg 2026-08-31) -- returnerar {class_name: (start,end)}."""
    toc = get_toc(book)
    idx = next(i for i, (lvl, ttl, p) in enumerate(toc) if ttl == "Focus Spells" and p == page)
    end_idx = idx + 1
    while end_idx < len(toc) and toc[end_idx][0] > 2:
        end_idx += 1
    entries = [(ttl, p) for lvl, ttl, p in toc[idx:end_idx] if lvl == 3]
    ranges = {}
    for k, (name, p) in enumerate(entries):
        end = entries[k + 1][1] - 1 if k + 1 < len(entries) else None
        ranges[name] = (p, end)
    return ranges


def _class_for_page(ranges, page):
    for name, (start, end) in ranges.items():
        if page >= start and (end is None or page <= end):
            return name
    return None


def parse_spell_chunk(chunk, next_name, book, class_ranges):
    name = (chunk["name"] or "").strip()
    category = {"SPELL": "spell", "CANTRIP": "cantrip", "FOCUS": "focus"}[chunk["match"].group(1)]
    level = int(chunk["match"].group(2))

    body = chunk["lines"][1:]
    if body and body[-1][1].strip() == next_name:
        body = body[:-1]

    # Ordningen spelar roll: traits/falt maste tolkas FORE trim_chunk_body,
    # annars kan seen_prose trigga for tidigt inuti sjalva faltblocket
    # (t.ex. "Traditions divine, occult" innehaller redan gemener) och en
    # sidbrytnings-artefakt strax dar efter klipper bort resten av posten.
    # trim_chunk_body kors darfor bara pa det som blir kvar efter traits
    # och kanda falt -- ren brodtext.
    traits, body = _split_traits(body)
    fields, body = _extract_fields(body)
    # max_lines hojd fran default (40): battle-form-spells (Aerial/Dragon/
    # Elemental/Plant/Monstrosity Form, Nature Incarnate) har legitimt
    # 43-52 rader brodtext -- verifierat 2026-08-31.
    body, truncated = trim_chunk_body(body, max_lines=80)
    text = " ".join(l for _p, l in body).strip()

    display_name = name.title() if name else name

    spell = {
        "name": display_name,
        "category": category,
        "level": level,
        "traits": traits,
        "traditions": [t.strip() for t in fields.get("traditions", "").split(",") if t.strip()] or None,
        "cast": fields.get("cast") or None,
        "text": text,
        "class": _class_for_page(class_ranges, chunk["page"]) if category == "focus" else None,
        "source": book_source(book, chunk["page"]),
    }
    if truncated:
        spell["_truncated"] = True
    return spell


def extract_range(book, start, end, class_ranges=None):
    lines = book_lines(book, start, end)
    chunks = chunk_by_pattern(lines, SPELL_MARKER, name_offset=-1)
    spells = []
    for k, c in enumerate(chunks):
        name = c["name"]
        if not name or not name.isupper() or len(name) > 60:
            continue  # falsk traff -- markorordet forekommer aven i lopande text
        next_name = chunks[k + 1]["name"] if k + 1 < len(chunks) else None
        spells.append(parse_spell_chunk(c, next_name, book, class_ranges or {}))
    return spells


def main():
    all_spells = []

    start, end = toc_range("CRB", "Spell Descriptions", level=2)
    all_spells.extend(extract_range("CRB", start, end))

    fstart, fend = toc_range("CRB", "Focus Spells", level=2, after_page=310)
    all_spells.extend(extract_range("CRB", fstart, fend, _class_ranges("CRB", fstart)))

    start, end = toc_range("APG", "Spell Descriptions", level=2)
    all_spells.extend(extract_range("APG", start, end))

    fstart, fend = toc_range("APG", "Focus Spells", level=2, after_page=200)
    all_spells.extend(extract_range("APG", fstart, fend, _class_ranges("APG", fstart)))

    empty_text = sum(1 for s in all_spells if not s["text"])
    truncated = sum(1 for s in all_spells if s.get("_truncated"))
    by_cat = {}
    for s in all_spells:
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1

    OUT_PATH.write_text(
        json.dumps({"spells": all_spells}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Totalt {len(all_spells)} spells -> {OUT_PATH}")
    print(f"per kategori: {by_cat}")
    print(f"tom text: {empty_text}, truncated: {truncated}")


if __name__ == "__main__":
    main()
