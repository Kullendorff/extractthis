"""Figurer: platshållare i texten, rendering (markdown / Obsidian / text),
korsreferenser ("se Figur 3", "se sidan 61") och Obsidian-bildnoter.

Extraktorerna lägger ⟪FIG:id⟫ där bilden hör hemma och registrerar figuren
({id, page, file, caption, source, w, h}). Först vid rendering bestäms hur
den syns – samma extraktion kan alltså skrivas som md, Obsidian eller text."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote

FIG_RE = re.compile(r"⟪FIG:([\w\-]+)⟫")


def fig_id(page: int | None, k: int) -> str:
    return f"s{page:03d}-{k:02d}" if page else f"f{k:03d}"


def placeholder(fid: str) -> str:
    return f"⟪FIG:{fid}⟫"


def _rel(path: str, base: Path | None) -> str:
    """Relativ, URL-kodad sökväg (mellanslag/å/ä/ö fungerar i både GitHub och Obsidian)."""
    p = Path(path)
    if base is not None:
        try:
            p = Path(os.path.relpath(p, base))
        except ValueError:  # annan enhet på Windows
            pass
    return quote(p.as_posix(), safe="/:")


def _label(f: dict) -> str:
    return f"Bild s. {f['page']}" if f.get("page") else "Bild"


def render_figure(f: dict, style: str, base: Path | None) -> str:
    cap = (f.get("caption") or "").strip()
    alt = (cap or _label(f)).replace("[", "(").replace("]", ")")[:200]
    if style == "obsidian":
        line = f"![[{Path(f['file']).name}]]"
        return line + "\n" + (f"*{cap}* ^fig-{f['id']}" if cap else f"^fig-{f['id']}")
    if style == "txt":
        return f"[{_label(f)}" + (f": {cap}" if cap else "") + "]"
    return (f'<a id="fig-{f["id"]}"></a>\n![{alt}]({_rel(f["file"], base)})'
            + (f"\n*{cap}*" if cap else ""))


def render_figures(text: str, figs: dict[str, dict], style: str = "md", base: Path | None = None) -> str:
    def rep(m: re.Match) -> str:
        f = figs.get(m.group(1))
        return render_figure(f, style, base) if f else ""

    return FIG_RE.sub(rep, text)


# ---------------------------------------------------------------------------
# Korsreferenser
# ---------------------------------------------------------------------------

_KIND = {
    "fig": "fig", "fig.": "fig", "figur": "fig", "figure": "fig", "figuren": "fig",
    "bild": "bild", "bilden": "bild", "illustration": "bild", "illustrationen": "bild", "ill.": "bild",
    "karta": "karta", "kartan": "karta", "map": "karta",
    "diagram": "diagram", "diagrammet": "diagram",
    "tabell": "tabell", "tabellen": "tabell", "table": "tabell", "tab.": "tabell",
}
_REF_RE = re.compile(r"\b(Fig(?:ur(?:en)?|ure)?\.?|Bild(?:en)?|Illustration(?:en)?|Ill\.|Kart(?:a|an)|Map|"
                     r"Diagram(?:met)?|Tabell(?:en)?|Table|Tab\.)\s?(\d{1,3}[a-z]?)\b", re.I)
_PAGE_RE = re.compile(r"\b(sidan|sida|sid\.|s\.|page|p\.|pp\.)\s?(\d{1,4})\b", re.I)


def _kind(word: str) -> str:
    return _KIND.get(word.lower(), _KIND.get(word.lower().rstrip("."), "fig"))


def figure_labels(figs: list[dict]) -> dict[tuple, str]:
    """(typ, nummer) -> figur-id, från bildtexter som "Figur 3: Däcksplan"."""
    out = {}
    for f in figs:
        m = _REF_RE.search(f.get("caption") or "")
        if m:
            out.setdefault((_kind(m.group(1)), m.group(2).lower()), f["id"])
    return out


def link_crossrefs(text: str, figs: list[dict], style: str = "md",
                   page_to_pdf=None, pages_present: set[int] | None = None) -> str:
    """Länka "se Figur 3" till figuren och "se sidan 61" till sidan.

    page_to_pdf: tryckt sidnummer -> PDF-sidnummer (de skiljer oftast: omslag,
    förord). Sidlänkar bara i md-stil (Obsidian saknar sidankare)."""
    labels = figure_labels(figs)
    if not labels and page_to_pdf is None:
        return text

    out_lines = []
    for line in text.splitlines():
        s = line.lstrip()
        # rör inte bildrader, bildtexter, kommentarer, rubriker med ankare eller kod
        if s.startswith(("![", "<a id=", "<!--", "*", "```", "^fig", "#", "⟪FIG")):
            out_lines.append(line)
            continue
        if labels:
            def rf(m: re.Match) -> str:
                fid = labels.get((_kind(m.group(1)), m.group(2).lower()))
                if not fid:
                    return m.group(0)
                return f"[[#^fig-{fid}|{m.group(0)}]]" if style == "obsidian" else f"[{m.group(0)}](#fig-{fid})"
            line = _sub_outside_links(_REF_RE, rf, line)
        if page_to_pdf is not None and style == "md":
            def rp(m: re.Match) -> str:
                target = page_to_pdf(int(m.group(2)))
                if target is None or (pages_present is not None and target not in pages_present):
                    return m.group(0)
                return f"[{m.group(0)}](#s{target})"
            line = _sub_outside_links(_PAGE_RE, rp, line)
        out_lines.append(line)
    return "\n".join(out_lines)


_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)|\[\[[^\]]*\]\]|`[^`]*`|<[^>]+>")


def _sub_outside_links(rx: re.Pattern, fn, line: str) -> str:
    """Ersätt bara utanför befintliga länkar/kod/HTML-taggar."""
    parts, last = [], 0
    for m in _LINK_RE.finditer(line):
        parts.append(rx.sub(fn, line[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(rx.sub(fn, line[last:]))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Obsidian-bildnoter
# ---------------------------------------------------------------------------

def _yaml(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_obsidian_notes(figs: list[dict], notes_dir: Path, md_name: str, page_texts: dict[int, str],
                         source_name: str, force: bool = False) -> int:
    """En not per figur (WEG v4-stil): frontmatter, inbäddad bild, länk tillbaka
    till dokumentet och kontext från sidan. Skriver inte över utan force."""
    notes_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in figs:
        note = notes_dir / (Path(f["file"]).stem + ".md")
        if note.exists() and not force:
            continue
        cap = (f.get("caption") or "").strip()
        ctx = re.sub(r"\s+", " ", FIG_RE.sub("", page_texts.get(f.get("page"), "")))
        ctx = re.sub(r"<!--.*?-->", "", ctx).strip()[:400]
        lines = ["---", "type: bild", f"källa: {_yaml('[[' + md_name + ']]')}", f"källfil: {_yaml(source_name)}"]
        if f.get("page"):
            lines.append(f"sida: {f['page']}")
        if cap:
            lines.append(f"bildtext: {_yaml(cap)}")
        lines += [f"storlek: \"{f.get('w', '?')}x{f.get('h', '?')}\"", "tags:", "  - bild", "---", "",
                  f"![[{Path(f['file']).name}]]", ""]
        if cap:
            lines += [f"*{cap}*", ""]
        lines.append(f"**Källa:** [[{md_name}#^fig-{f['id']}|{md_name}]]" + (f", s. {f['page']}" if f.get("page") else ""))
        if ctx:
            lines += ["", "## Kontext", "", "> " + ctx]
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        n += 1
    return n


# ---------------------------------------------------------------------------
# Dekor/ikon-filter
# ---------------------------------------------------------------------------

def drop_decorations(figs: list[dict], min_pages: int = 3) -> set[str]:
    """Identiska bildfiler på >= min_pages sidor = logotyp/ornament/sidram ->
    returnerar id:n att ta bort (filerna raderas)."""
    import hashlib

    by_hash: dict[str, list[dict]] = {}
    for f in figs:
        try:
            h = hashlib.md5(Path(f["file"]).read_bytes()).hexdigest()
        except OSError:
            continue
        by_hash.setdefault(h, []).append(f)
    drop = set()
    for group in by_hash.values():
        if len({g.get("page") for g in group}) >= min_pages:
            for g in group:
                drop.add(g["id"])
                try:
                    Path(g["file"]).unlink()
                except OSError:
                    pass
    return drop
