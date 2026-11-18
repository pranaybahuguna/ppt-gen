"""A template's own guidelines: its colour palette, its writing rules, its graphical elements.

Corporate templates often carry slides that are not examples but instructions - "Colour palette"
with the official swatches, a "style guide" with spelling, number and capital-letter rules,
"Graphical elements" with the approved icons and shapes. Read once, when the template is
ingested, they say what the profile would otherwise guess:

* the palette, by plain code: every swatch's fill, checked against the "R 000 G 145 B 090" (or
  "#00915A") written on it, grouped by its row's label (main / highlight / grey);
* the writing rules, by one model call over the style slides' text: short rules for every
  writer, and the ones code can check (spelling, number and currency formats, title capitals,
  bullet punctuation) as settings for `house_problems`;
* the graphical elements, by plain code: what each named element is and where it sits.

The guide is stored next to the template (`<template>.guide.json`) and applied to every profile
of a deck made on it (`designed`), so the renderer draws in the official colours and every
writer, check and the deck check follow the same rules. Guideline slides themselves are never
counted as the brand's content (design.profile) and never left in a deck (render_deck starts
from no slides).

Called by: templates.ingest (build_guide); design.nodes, revise.nodes (designed);
design.plan.check_spec, revise.work.redesign and revise.audit (house_problems).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from pptx.enum.shapes import MSO_SHAPE_TYPE

from .profile import _hsl, _mix

EMU = 914400

#: Titles that mark a slide as guidance about the template rather than an example of it.
GUIDE_TITLE = re.compile(
    r"\b(colou?r (palette|scheme|guide|codes?)|(universal |brand |house |writing )?style ?guide|"
    r"graphic(al)? elements|icon(ography|s)|typography|brand (guide|guidelines|rules)|"
    r"guidelines|how to use (this|the) template|template (guide|instructions))\b", re.I)
_RGB = re.compile(r"\bR\s*[:=]?\s*(\d{1,3})\W{0,4}G\s*[:=]?\s*(\d{1,3})\W{0,4}B\s*[:=]?\s*(\d{1,3})\b",
                  re.I | re.S)
_HEX = re.compile(r"#\s?([0-9A-F]{6})\b", re.I)
_HIGHLIGHT = re.compile(r"highlight|additional|accent|secondary|complementary|alert", re.I)
_GREY = re.compile(r"gr[ae]y|neutral|black|white|mono", re.I)


def _leaves(shapes):
    for sh in shapes:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _leaves(sh.shapes)
        else:
            yield sh


def _text(sh) -> str:
    if sh.has_text_frame:
        return sh.text_frame.text.strip()
    if getattr(sh, "has_table", False) and sh.has_table:
        return "\n".join(" | ".join(c.text.strip() for c in row.cells) for row in sh.table.rows)
    return ""


def _title(slide) -> str:
    t = slide.shapes.title
    if t is not None and t.text_frame.text.strip():
        return t.text_frame.text.strip()
    texts = [(sh.top or 0, _text(sh)) for sh in _leaves(slide.shapes) if sh.has_text_frame and _text(sh)]
    return min(texts)[1] if texts else ""


def _fill(sh) -> str | None:
    try:
        if sh.fill.type == 1:
            return str(sh.fill.fore_color.rgb).upper()
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _swatches(slide) -> list[dict]:
    out = []
    for sh in _leaves(slide.shapes):
        t = _text(sh) if sh.has_text_frame else ""
        m, h = _RGB.search(t), _HEX.search(t)
        if not (m or h):
            continue
        if m:
            vals = [int(v) for v in m.groups()]
            if max(vals) > 255:
                continue
            colour = "".join(f"{v:02X}" for v in vals)
        else:
            colour = h.group(1).upper()
        fill = _fill(sh)
        out.append({"colour": colour, "fill": fill, "x": (sh.left or 0) / EMU, "y": (sh.top or 0) / EMU,
                    "h": (sh.height or 0) / EMU})
    return out


def guideline_slides(prs) -> dict[int, str]:
    """{slide number: "palette" | "style" | "graphics" | "guide"} for the slides that are guidance.
    A slide with six or more labelled colour swatches is a palette whatever its title says."""
    out = {}
    for n, slide in enumerate(prs.slides, start=1):
        title = _title(slide)
        if len(_swatches(slide)) >= 6 or re.search(r"colou?r", title, re.I) and GUIDE_TITLE.search(title):
            out[n] = "palette"
        elif GUIDE_TITLE.search(title):
            low = title.lower()
            out[n] = ("graphics" if re.search(r"graphic|icon", low) else
                      "style" if re.search(r"style|typograph|writing", low) else "guide")
    return out


def read_palette(slide) -> dict:
    """The swatches of a palette slide, by row: {"main", "highlight", "grey", "rows", "mismatches"}.
    The colour written on a swatch wins over its fill (the fill may have been nudged by a theme);
    a swatch whose fill disagrees is listed under "mismatches"."""
    sw = sorted(_swatches(slide), key=lambda s: (s["y"], s["x"]))
    labels = [(sh, _text(sh)) for sh in _leaves(slide.shapes)
              if sh.has_text_frame and _text(sh) and not _RGB.search(_text(sh)) and not _HEX.search(_text(sh))]
    rows: list[list[dict]] = []
    for s in sw:
        if rows and abs(rows[-1][0]["y"] - s["y"]) < 0.35:
            rows[-1].append(s)
        else:
            rows.append([s])
    out = {"main": [], "highlight": [], "grey": [], "rows": [], "mismatches": []}
    for row in rows:
        mid = row[0]["y"] + row[0]["h"] / 2
        left = min(s["x"] for s in row)
        near = [(left - (sh.left or 0) / EMU, t) for sh, t in labels
                if (sh.top or 0) / EMU - 0.3 <= mid <= ((sh.top or 0) + (sh.height or 0)) / EMU + 0.3
                and (sh.left or 0) / EMU < left]
        label = min(near)[1] if near else ""
        colours = [s["colour"] for s in sorted(row, key=lambda s: s["x"])]
        neutral = all(_hsl(c)[2] < 0.12 for c in colours)
        group = "highlight" if _HIGHLIGHT.search(label) else "grey" if _GREY.search(label) or neutral else "main"
        out[group] += [c for c in colours if c not in out[group]]
        out["rows"].append({"label": " ".join(label.split()), "group": group, "colours": colours})
        out["mismatches"] += [{"written": s["colour"], "fill": s["fill"]} for s in row
                              if s["fill"] and s["fill"] != s["colour"]]
    return out


def slide_text(slide) -> str:
    """Everything a slide says, tables included, in reading order."""
    items = sorted(_leaves(slide.shapes), key=lambda sh: ((sh.top or 0), (sh.left or 0)))
    return "\n".join(t for t in (_text(sh) for sh in items) if t)


def graphics(slide) -> list[dict]:
    """The named elements on a graphical-elements slide: name, kind, box in inches."""
    out = []
    for sh in slide.shapes:
        if sh.is_placeholder or (sh.has_text_frame and re.match(r"^\[.*\]$", _text(sh))):
            continue
        kind = str(sh.shape_type).split(".")[-1].split(" ")[0].lower() if sh.shape_type else "shape"
        out.append({"name": sh.name, "kind": kind,
                    "box": [round((v or 0) / EMU, 2) for v in (sh.left, sh.top, sh.width, sh.height)]})
    return out


def build_guide(path, planner=None, log=print) -> dict | None:
    """Read a template's guideline slides. -> the guide, or None when it has none.

    The palette and the graphics are read by code; the writing rules take one model call
    (planner.house_rules) when there is a style slide and a planner that can make it.
    Called by: templates.ingest.
    """
    from pptx import Presentation

    prs = Presentation(str(path))
    found = guideline_slides(prs)
    if not found:
        return None
    guide: dict = {"slides": {str(n): k for n, k in found.items()}, "palette": None, "rules": [],
                   "checks": {}, "graphics": []}
    for n, kind in found.items():
        slide = prs.slides[n - 1]
        if kind == "palette":
            pal = read_palette(slide)
            if pal["main"] or pal["highlight"]:
                guide["palette"] = pal
        elif kind == "graphics":
            guide["graphics"] += graphics(slide)
    style = [{"title": _title(prs.slides[n - 1]), "text": slide_text(prs.slides[n - 1])[:12000]}
             for n, k in found.items() if k in ("style", "guide")]
    if style and planner is not None and hasattr(planner, "house_rules"):
        answer = planner.house_rules({"slides": style}) or {}
        guide["rules"] = [str(r).strip() for r in answer.get("rules") or [] if str(r).strip()][:40]
        guide["checks"] = {k: v for k, v in (answer.get("checks") or {}).items() if v not in (None, "", [])}
    log(f"  guide: {len(found)} guideline slide(s); palette "
        f"{len((guide['palette'] or {}).get('main') or [])}+{len((guide['palette'] or {}).get('highlight') or [])}"
        f" colours; {len(guide['rules'])} rule(s); {len(guide['graphics'])} graphical element(s)")
    return guide


# --------------------------------------------------------------------------- using it

def _dist(a: str, b: str) -> float:
    r1, g1, b1 = (int(a[i:i + 2], 16) for i in (0, 2, 4))
    r2, g2, b2 = (int(b[i:i + 2], 16) for i in (0, 2, 4))
    rm = (r1 + r2) / 2
    return ((2 + rm / 256) * (r1 - r2) ** 2 + 4 * (g1 - g2) ** 2 + (2 + (255 - rm) / 256) * (b1 - b2) ** 2) ** 0.5


def guide_colours(P: dict) -> list[str]:
    """Every colour the guide allows (main, highlight, greys), or [] without a guide."""
    pal = (P.get("guide") or {}).get("palette") or {}
    return [c for g in ("main", "highlight", "grey") for c in pal.get(g) or []]


def apply_guide(P: dict, guide: dict | None) -> dict:
    """The profile in the guide's colours, carrying its rules. Without a palette the colours stay.

    Roles: the first main colour is the primary (the brand colour named first); the darkest main
    colour its dark shade; the accent the main colour that stands furthest from it (and is not
    too pale to carry text); the first highlight colour marks what must stand out (alert); the
    greys give the muted text, lines and panels.
    """
    if not guide:
        return P
    out = {**P, "guide": {k: guide.get(k) for k in ("rules", "checks", "palette")}}
    pal = guide.get("palette") or {}
    main = pal.get("main") or []
    if not main:
        return out
    c = dict(P["colours"])
    primary = main[0]
    darker = sorted(main, key=lambda x: _hsl(x)[1])
    dark = darker[0] if _hsl(darker[0])[1] < _hsl(primary)[1] - 0.06 else _mix(primary, "000000", 0.3)
    others = [x for x in main[1:] if _hsl(x)[1] < 0.75] or main[1:] or [_mix(primary, "000000", 0.35)]
    accent = max(others, key=lambda x: _dist(x, primary))
    c.update({"primary": primary, "primary_dark": dark, "primary_light": _mix(primary, "FFFFFF", 0.85),
              "accent": accent, "accent_dark": _mix(accent, "000000", 0.3), "accent_light": _mix(accent, "FFFFFF", 0.8)})
    if pal.get("highlight"):
        c["alert"] = pal["highlight"][0]
    greys = sorted(pal.get("grey") or [], key=lambda x: _hsl(x)[1])
    if greys:
        c["muted"] = greys[0] if _hsl(greys[0])[1] < 0.45 else c["muted"]
        c["panel"] = greys[-1] if _hsl(greys[-1])[1] > 0.9 else c["panel"]
        lines = [g for g in greys if 0.75 <= _hsl(g)[1] <= 0.9]
        c["line"] = lines[0] if lines else c["line"]
    out["colours"] = c
    return out


def extended_from_guide(P: dict, names) -> dict | None:
    """The extended palette's named hues (teal, blue, amber, plum) taken from the guide's own
    colours - each the unused guide colour nearest that hue - so "more colour" stays on-brand.
    None without a guide palette."""
    pal = (P.get("guide") or {}).get("palette") or {}
    pool = [x for x in (pal.get("main") or [])[1:] + (pal.get("highlight") or [])
            if x not in (P["colours"]["primary"], P["colours"]["accent"])]
    if not pool:
        return None
    out, used = {}, set()
    for name, hue in names.items():
        free = [x for x in pool if x not in used] or pool
        pick = min(free, key=lambda x: min(abs(_hsl(x)[0] - hue), 360 - abs(_hsl(x)[0] - hue)))
        used.add(pick)
        out[name], out[f"{name}_light"] = pick, _mix(pick, "FFFFFF", 0.82)
    return out


@lru_cache(maxsize=32)
def _guide_bytes(uri: str) -> bytes | None:
    from ..infra.storage import store

    try:
        return store().get_bytes(uri + ".guide.json") if store().exists(uri + ".guide.json") else None
    except Exception:
        return None


def load_guide(template_uri: str | None) -> dict | None:
    """The guide stored with an ingested template, or None."""
    if not template_uri:
        return None
    raw = _guide_bytes(template_uri)
    return json.loads(raw) if raw else None


def designed(path, template_uri: str | None = None, palette: str | None = None) -> dict:
    """The design profile a deck's slides are drawn and checked with: the file's own profile, in
    its template's guide (when the template was ingested with one), in the palette asked for.

    Called by: design.nodes.write/build; revise.nodes.work/join/audit.
    """
    from .profile import extract_profile, with_palette

    return with_palette(apply_guide(extract_profile(path), load_guide(template_uri)), palette)


def rules_of(P: dict) -> dict:
    """{"house_rules": [...]} for a model payload, or {} when the template has none."""
    rules = (P.get("guide") or {}).get("rules")
    return {"house_rules": rules} if rules else {}


def checks_of(P: dict) -> dict:
    """The guide's code-checkable rules (see house_problems), or {}."""
    return (P.get("guide") or {}).get("checks") or {}


# --------------------------------------------------------------------------- checking text

_US_UK = {
    "color": "colour", "colors": "colours", "colored": "coloured", "favor": "favour", "favorable": "favourable",
    "behavior": "behaviour", "behaviors": "behaviours", "labor": "labour", "honor": "honour",
    "neighbor": "neighbour", "organization": "organisation", "organizations": "organisations",
    "organize": "organise", "organized": "organised", "analyze": "analyse", "analyzed": "analysed",
    "optimize": "optimise", "optimized": "optimised", "optimization": "optimisation",
    "prioritize": "prioritise", "prioritized": "prioritised", "realize": "realise", "realized": "realised",
    "utilize": "utilise", "recognize": "recognise", "recognized": "recognised", "standardize": "standardise",
    "monetization": "monetisation", "modeling": "modelling", "modeled": "modelled", "traveled": "travelled",
    "canceled": "cancelled", "labeled": "labelled", "center": "centre", "centers": "centres",
    "catalog": "catalogue", "defense": "defence", "offense": "offence", "fulfill": "fulfil",
    "enrollment": "enrolment", "judgment": "judgement",
}
_SMALL = {"and", "the", "of", "for", "in", "to", "with", "on", "by", "at", "from", "a", "an", "or", "as",
          "into", "its", "our", "vs", "per", "than"}
_MONTH = r"(January|February|March|April|May|June|July|August|September|October|November|December)"


def _title_case(title: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", re.split(r"[:—–]", title)[0])
    rest = words[1:]
    if any(w[0].isupper() and w.lower() in _SMALL for w in rest):
        return True
    long = [w for w in rest if len(w) >= 4 and not w.isupper()]
    return len(long) >= 4 and sum(w[0].isupper() for w in long) / len(long) >= 0.75


def house_problems(texts: list[tuple[str, str]], checks: dict | None) -> list[str]:
    """What breaks the template's checkable writing rules, as instructions. `texts` are
    (kind, text) pairs, kind "title", "bullet" or "body". [] without checks.

    Called by: design.plan.check_spec, revise.work.redesign, revise.audit.
    """
    if not checks:
        return []
    out: list[str] = []

    def add(msg):
        if msg not in out:
            out.append(msg)

    scale = checks.get("scale") or {}
    for kind, t in texts:
        if not t:
            continue
        if checks.get("spelling") == "UK":
            for w in re.findall(r"[A-Za-z]+", t):
                if w.lower() in _US_UK:
                    add(f"house style is UK spelling: '{w}' should be '{_US_UK[w.lower()]}'")
        if kind == "title" and checks.get("title_case") == "sentence" and _title_case(t):
            add(f"house style: titles take a capital letter at the start only (and for names): '{t[:60]}'")
        if kind == "bullet" and checks.get("bullet_full_stop") is False and re.search(r"[.;:]\s*$", t) \
                and not re.search(r"\b(etc|approx|incl|c|e\.g|i\.e)\.\s*$", t):
            add(f"house style: no full stop at the end of a bullet: '{t[-40:]}'")
        if checks.get("currency_symbol") == "before":
            for m in re.finditer(r"\d[\d.,]*\s?(?:[kKmM]|bn)?\s?([€$£¥])", t):
                add(f"house style: currency symbol before the figure ({m.group(1)}5m, not 5m{m.group(1)}): '{m.group(0)}'")
            if checks.get("space_before_unit") is False:
                for m in re.finditer(r"([€$£¥])\s+\d[\d.,]*(?:[kKmM]|bn)?", t):
                    add(f"house style: no space between the currency symbol and the figure: '{m.group(0)}' -> "
                        f"'{m.group(0).replace(' ', '')}'")
        if scale:
            words = {"thousand": scale.get("thousand"), "million": scale.get("million"), "billion": scale.get("billion")}
            for m in re.finditer(r"(\d[\d.,]*)\s?(thousand|million|billion|mn|[MBK]|Bn|BN)\b(?!\w)", t):
                word = {"mn": "million", "M": "million", "B": "billion", "Bn": "billion", "BN": "billion",
                        "K": "thousand"}.get(m.group(2), m.group(2))
                want = words.get(word)
                if want and m.group(2) != want:
                    add(f"house style: write {word}s as '{m.group(1)}{want}': '{m.group(0)}'")
        if checks.get("space_before_unit") is False:
            for m in re.finditer(r"\d[\d.,]*\s(%|mm|cm|km|kg)(?!\w)", t):
                add(f"house style: no space before the unit: '{m.group(0)}' -> '{m.group(0).replace(' ', '')}'")
        if checks.get("thousands_separator") == ",":
            for m in re.finditer(r"(?<![\d.,])\d{5,}(?![\d.,])", t):
                add(f"house style: a comma for thousands: '{m.group(0)}' -> '{int(m.group(0)):,}'")
            for m in re.finditer(r"\b\d{1,3}(?:\.\d{3})+,\d+\b", t):
                add(f"house style: comma for thousands, full stop for decimals: '{m.group(0)}'")
        if checks.get("date_format") == "day month year":
            for m in re.finditer(_MONTH + r"\s+\d{1,2},\s*\d{4}", t):
                add(f"house style: dates as day month year ('2 September 2019'): '{m.group(0)}'")
    return out[:8]


def spec_texts(spec: dict) -> list[tuple[str, str]]:
    """A slide spec's words as (kind, text): its title, the items of point lists as bullets,
    everything else as body text."""
    out = [("title", str(spec.get("title") or "")), ("body", str(spec.get("subtitle") or "")),
           ("body", str(spec.get("footnote") or ""))]

    def walk(node, kind):
        if isinstance(node, dict):
            sub = "bullet" if node.get("piece") == "points" else kind
            for k, v in node.items():
                if k in ("piece", "kind", "tone", "type", "span", "weight", "height", "highlight"):
                    continue
                if isinstance(v, str):
                    out.append((sub if k == "text" else "body", v))
                else:
                    walk(v, sub)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, str):
                    out.append((kind, v))
                else:
                    walk(v, kind)

    walk(spec.get("rows") or [], "body")
    return out
