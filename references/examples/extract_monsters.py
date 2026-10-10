"""Extraherar rules/monsters.json ur Bestiary 1 (Bestiary 2/3 + GMG NPC-
galleri lamnas till en senare korning nar B1-kvaliteten ar verifierad --
samma "CREATURE N"-monster ska funka rakt av, men stickprovas separat).

Statblock-layouten ar valstrukturerad med kanda faltetiketter (Perception/
Languages/Skills/Str/Items/AC/HP/Speed) i FAST ordning, men mellan dem
ligger friliggande formage-stycken UTAN egen etikett (t.ex. "Truespeech A
kolyarut kan tala...") -- de kan bara identifieras genom att de INTE
matchar nagon kand etikett. parse_statblock_fields() skannar hela kroppen
en gang och samlar sadant "fritext" mellan kanda falt i ordning, istallet
for att stanna vid forsta okanda rad (som extract_spells._extract_fields
gor -- den enklare varianten racker inte har eftersom flera formage-
stycken kan ligga i rad mellan Str-raden och Items/AC).

Verifierat mot Arbiter (CREATURE 1, s.9), Axiomite (CREATURE 8, s.10) och
Kolyarut (CREATURE 12, s.11) i chattloggen 2026-08-31 -- inklusive sidopanelen
"AEON DIVINITIES" som later in i Axiomitens actions-sektion om den inte
trimmas bort explicit (se _trim_sidebar, lagre versalrads-troskel an
pdfio.trim_chunk_body eftersom monster-sidopaneler ofta ar EN ensam rad,
inte tva i rad).
"""
import json
import re
from pathlib import Path

from pdfio import book_lines, book_source, chunk_by_pattern, toc_range

OUT_PATH = Path(__file__).resolve().parent.parent / "rules" / "monsters.json"

# En-dash (U+2013), inte ASCII-bindestreck, for negativa creature-nivaer
# (t.ex. "CREATURE –1", Animated Broom, B1 s.21) -- samma artefakt som i
# formagevardena (se _SIGN nedan). Missad markor har tidigare svalt in
# HELA nasta kreaturs statblock i det foregaende (Astral Deva fick Animated
# Brooms bristles/dust-text) eftersom chunk_by_pattern bara delar vid
# matchande rader -- chattlogg 2026-08-31.
CREATURE_MARKER = re.compile(r"^CREATURE ([-−–]?\d+)$")
TRAIT_LINE_RE = re.compile(r"^[A-Z][A-Z '\-]*$")

ALIGNMENTS = {"LG", "NG", "CG", "LN", "N", "CN", "LE", "NE", "CE"}
SIZES = {"TINY", "SMALL", "MEDIUM", "LARGE", "HUGE", "GARGANTUAN"}

FIELD_LABELS = ["Perception", "Languages", "Skills", "Str", "Items", "AC", "HP", "Speed"]
FIELD_RE = re.compile(r"^(" + "|".join(FIELD_LABELS) + r")\b(.*)$")

_DIGIT_ONLY_RE = re.compile(r"^\d+$")
_CHROME_CAP_RE = re.compile(r"^[A-Z][A-Z '&\-]*$")
_HP_CLAUSE_RE = re.compile(r"^(Immunities|Weaknesses|Resistances)\b")
# Str-raden radbryter alltid mellan Int och Wis (fast ordning Str/Dex/Con/
# Int/Wis/Cha) -- en fortsattning borjar darfor alltid med precis "Wis"
# eller "Cha" (Zelekhut, B2 s.10). AC:s status-bonusklausul kan borja med
# ett tecken/tal ("+1 status to all saves vs. magic") som inte fangas av
# gemene-fallbacken langre ner.
_STR_CONT_RE = re.compile(r"^(Wis|Cha)\b")
_AC_CONT_RE = re.compile(r"^[+–-]?\d")

# "Str" star inte kvar i fields["Str"] -- FIELD_RE konsumerar etikettordet,
# sa vardet borjar direkt med Str-siffran ("+5, Dex +4, Con +3, ...").
# PDF:en anvander olika Unicode-minustecken for negativa modifierare
# beroende pa bok/font -- EN-DASH U+2013 ("Cha –2", Skum B1 s.13) och
# MINUS SIGN U+2212 ("Int −2", Behir B2 s.39) har bada setts, inte bara
# ASCII-bindestreck. Chattlogg 2026-08-31.
_SIGN = "[+−–-]"
ABILITY_RE = re.compile(
    rf"^({_SIGN}\d+), Dex ({_SIGN}\d+), Con ({_SIGN}\d+), Int ({_SIGN}\d+), Wis ({_SIGN}\d+), Cha ({_SIGN}\d+)"
)
# Leading-nummer-match, INTE fullstrangsmatch -- "AC 17 (13 when broken);
# construct armor; Fort +10, ..." och "HP 215, regeneration 15 (deactivated
# by chaotic); Immunities ..." har godtycklig text mellan varde och resten
# (se Animated Armor/Kolyarut, B1 s.11/21, chattlogg 2026-08-31), sa Fort/
# Ref/Will och Immunities/Weaknesses/Resistances soks fristaende i HELA
# resten av faltet istallet for att krava en exakt strukturerad svans.
LEAD_NUM_RE = re.compile(r"^(\d+)")
SAVE_RE = re.compile(rf"Fort ({_SIGN}\d+),?\s*Ref ({_SIGN}\d+),?\s*Will ({_SIGN}\d+)")
# Immunities/Weaknesses/Resistances separeras av bocker OFTA med ';', men
# ibland bara med ',' nar raden radbryter (Axiomite, B1 s.10: "Weaknesses
# chaotic 10, Resistances electricity 10, mental 10" -- inget semikolon
# mellan Weaknesses och Resistances). Stoppa darfor vid BADE ';' OCH nasta
# kanda etikett-ord, inte bara ';'.
_STOP = r"(?:;|(?=\bWeaknesses\b)|(?=\bResistances\b)|$)"
IMM_RE = re.compile(r"Immunities (.*?)" + _STOP)
WEAK_RE = re.compile(r"Weaknesses (.*?)" + _STOP)
RESIST_RE = re.compile(r"Resistances (.*?)" + _STOP)


def _int(s):
    return int(s.replace("–", "-").replace("−", "-").replace("+", ""))


def _split_traits(lines):
    traits = []
    i = 0
    while i < len(lines) and TRAIT_LINE_RE.match(lines[i][1].strip()) and len(lines[i][1].strip()) <= 30:
        traits.append(lines[i][1].strip())
        i += 1
    return traits, lines[i:]


def parse_statblock_fields(lines):
    """Skanna hela kroppen, plocka ut kanda falt var de an ligger, samla
    allt annat som fritext-spans i ordning (se moduldocstring)."""
    fields = {}
    free_spans = []
    current = None
    buf = []
    free_buf = []
    last_label = None

    def flush_field():
        if current:
            fields[current] = " ".join(buf).strip()

    def flush_free():
        if free_buf:
            free_spans.append((last_label, list(free_buf)))
            free_buf.clear()

    for p, l in lines:
        s = l.strip()
        if not s or _DIGIT_ONLY_RE.match(s):
            continue
        m = FIELD_RE.match(s)
        if m:
            flush_field()
            flush_free()
            current = m.group(1)
            buf = [m.group(2).strip()] if m.group(2).strip() else []
            last_label = current
        elif current in ("Skills", "Languages"):
            # Dessa faltens flerradsfortsattningar borjar ofta med en versal
            # -- skill-/spraknamn ("Religion +24" efter "Skills Acrobatics
            # +24, ..."). Bada foljs alltid direkt av nasta kanda falt
            # (Str), sa det ar sakert att alltid fortsatta har.
            buf.append(s)
        elif current == "Str" and _STR_CONT_RE.match(s):
            # Str-raden radbryter ibland mitt i sig sjalv ("Str +6, Dex +5,
            # Con +4, Int +0," / "Wis +3, Cha +3" -- Zelekhut, B2 s.10).
            # Till skillnad fran Skills/Languages far INTE Str alltid
            # fortsatta: en friststaende formaga (t.ex. "Truespeech A
            # zelekhut kan...") kan ligga direkt efter och far INTE svaljas
            # in har -- kravs darfor att raden faktiskt ser ut som resten
            # av formagevardena (borjar med "Wis"/"Cha", fast ordning).
            buf.append(s)
        elif current in ("AC", "HP") and (_HP_CLAUSE_RE.match(s) or _AC_CONT_RE.match(s)):
            # HP:s Weaknesses/Resistances-underklausuler radbryter ibland
            # utan semikolon emellan (Axiomite, B1 s.10: "Weaknesses
            # chaotic 10," / "Resistances electricity 10, mental 10"), och
            # AC:s status-bonusklausul kan borja med tecken/tal ("+1 status
            # to all saves vs. magic", fangas inte av gemene-fallbacken).
            # Men till skillnad fran Skills/Languages/Str foljs INTE AC/HP
            # alltid av nasta kanda falt: en friststaende formaga (t.ex.
            # "Attack of Opportunity [reaction]") kan ligga mellan HP och
            # Speed (Zelekhut, B2 s.10) och far INTE svaljas in i HP-faltet.
            # Fortsatt darfor bara om raden faktiskt ser ut som en ny
            # Immunities/Weaknesses/Resistances-klausul eller AC-bonusrad.
            buf.append(s)
        elif current is not None and s[0].islower():
            buf.append(s)
        else:
            if current is not None:
                flush_field()
                current = None
            free_buf.append((p, l))
    flush_field()
    flush_free()
    return fields, free_spans


def _trim_sidebar(lines, max_lines=150):
    """Som pdfio.trim_chunk_body, men versalrads-troskeln ar 1 (inte 2):
    monster-sidopaneler i Bestiary ar ofta EN ensam kort versalrad (t.ex.
    "AEON DIVINITIES") omgiven av lopande text, till skillnad fran feats/
    spells dar kapitelbrus kom i par. Ability-namn i actions-sektionen ar
    alltid Title Case (t.ex. "Crystalline Dust Form"), aldrig rena versaler,
    sa detta ar sakert."""
    out = []
    seen_prose = False
    truncated = False
    for p, l in lines:
        s = l.strip()
        if not s:
            continue
        if _DIGIT_ONLY_RE.match(s):
            if seen_prose:
                break
            continue
        is_chrome = bool(_CHROME_CAP_RE.match(s)) and len(s) <= 30
        if is_chrome and seen_prose:
            break
        out.append((p, l))
        if not is_chrome and any(c.islower() for c in s):
            seen_prose = True
        if len(out) >= max_lines:
            truncated = True
            break
    return out, truncated


def parse_creature_chunk(chunk, next_name, book):
    name = (chunk["name"] or "").strip()
    level = _int(chunk["match"].group(1))

    body = chunk["lines"][1:]
    if body and body[-1][1].strip() == next_name:
        body = body[:-1]

    raw_traits, body = _split_traits(body)
    alignment = next((t for t in raw_traits if t in ALIGNMENTS), None)
    size = next((t for t in raw_traits if t in SIZES), None)
    traits = [t for t in raw_traits if t not in ALIGNMENTS and t not in SIZES]

    fields, free_spans = parse_statblock_fields(body)

    # Fristaende formage-stycken kan dyka upp mellan VILKET kant falt som
    # helst (inte bara efter Str) -- t.ex. "Attack of Opportunity
    # [reaction]" mellan HP och Speed (Zelekhut, B2 s.10). "Speed" ar alltid
    # SISTA kanda faltet (se FIELD_LABELS), sa dess span ar actions-
    # sektionen; allt annat fritext-span (oavsett vilket falt det foljer
    # pa) racknas som passiva specialformagor.
    special_spans = []
    actions_lines = []
    truncated = False
    for label, span_lines in free_spans:
        if label == "Speed":
            actions_lines = span_lines
        else:
            special_spans.append(span_lines)
    special_abilities = " ".join(l for span in special_spans for _p, l in span).strip()
    if actions_lines:
        trimmed, truncated = _trim_sidebar(actions_lines)
        actions_text = " ".join(l for _p, l in trimmed).strip()
    else:
        actions_text = ""

    abilities = None
    m = ABILITY_RE.search(fields.get("Str", ""))
    if m:
        abilities = {
            k: _int(v) for k, v in zip(("str", "dex", "con", "int", "wis", "cha"), m.groups())
        }

    ac_field = fields.get("AC", "")
    ac = None
    saves = None
    m = LEAD_NUM_RE.match(ac_field)
    if m:
        ac = int(m.group(1))
        sm = SAVE_RE.search(ac_field)
        if sm:
            saves = {"fort": _int(sm.group(1)), "ref": _int(sm.group(2)), "will": _int(sm.group(3))}

    hp_field = fields.get("HP", "")
    hp = None
    immunities = weaknesses = resistances = None
    m = LEAD_NUM_RE.match(hp_field)
    if m:
        hp = int(m.group(1))
        im = IMM_RE.search(hp_field)
        immunities = [x.strip() for x in im.group(1).rstrip(", ").split(",")] if im else None
        wk = WEAK_RE.search(hp_field)
        weaknesses = wk.group(1).strip().rstrip(",").strip() if wk else None
        rs = RESIST_RE.search(hp_field)
        resistances = rs.group(1).strip().rstrip(",").strip() if rs else None

    creature = {
        "name": name.title() if name else name,
        "level": level,
        "alignment": alignment,
        "size": size,
        "traits": traits,
        "perception": fields.get("Perception") or None,
        "languages": fields.get("Languages") or None,
        "skills": fields.get("Skills") or None,
        "abilities": abilities,
        "special_abilities": special_abilities or None,
        "items": fields.get("Items") or None,
        "ac": ac,
        "saves": saves,
        "hp": hp,
        "immunities": immunities,
        "weaknesses": weaknesses,
        "resistances": resistances,
        "speed": fields.get("Speed") or None,
        "actions_text": actions_text,
        "source": book_source(book, chunk["page"]),
    }
    if truncated:
        creature["_truncated"] = True
    return creature


def extract_book(book, start=None, end=None):
    lines = book_lines(book, start, end)
    chunks = chunk_by_pattern(lines, CREATURE_MARKER, name_offset=-1)
    creatures = []
    for k, c in enumerate(chunks):
        name = c["name"]
        if not name or not name.isupper() or len(name) > 60:
            continue
        next_name = chunks[k + 1]["name"] if k + 1 < len(chunks) else None
        creatures.append(parse_creature_chunk(c, next_name, book))
    return creatures


# (bokkod, TOC-titel, TOC-niva) for varje boks monster-/NPC-kapitel. B1 och
# GMG har egna "Monsters"/"Chapter 5 - NPC Gallery"-rubriker; B2 heter
# "Bestiary 2" i sin egen TOC, B3 bara "Bestiary" (inte "Bestiary 3") --
# verifierat 2026-08-31.
BOOK_SECTIONS = [
    ("B1", "Monsters", 1),
    ("B2", "Bestiary 2", 1),
    ("B3", "Bestiary", 1),
    ("GMG", "Chapter 5 - NPC Gallery", 1),
]


def main():
    all_creatures = []
    for book, title, level in BOOK_SECTIONS:
        start, end = toc_range(book, title, level=level)
        creatures = extract_book(book, start, end)
        all_creatures.extend(creatures)

        missing_ac = sum(1 for c in creatures if c["ac"] is None)
        missing_hp = sum(1 for c in creatures if c["hp"] is None)
        missing_abilities = sum(1 for c in creatures if c["abilities"] is None)
        missing_perception = sum(1 for c in creatures if c["perception"] is None)
        truncated = sum(1 for c in creatures if c.get("_truncated"))
        print(
            f"{book:4} {len(creatures):4} monster  "
            f"saknar AC:{missing_ac} HP:{missing_hp} abilities:{missing_abilities} "
            f"perception:{missing_perception}  truncated:{truncated}"
        )

    OUT_PATH.write_text(
        json.dumps({"monsters": all_creatures}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nTotalt {len(all_creatures)} monster -> {OUT_PATH}")


if __name__ == "__main__":
    main()
