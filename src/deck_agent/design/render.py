"""Slide specs + template -> a deck. Deterministic: the same specs always give the same deck.

A slide spec says what a slide shows; nothing in it is a coordinate:

    {"title": "One worker caps us at 20 scans/hr",          # the slide's point, as a sentence
     "subtitle": "Throughput against the onboarding target", # optional, one line
     "rows": [                                               # top to bottom
        {"weight": 1, "items": [{"span": 12, "piece": "kpis", "items": [...]}]},
        {"weight": 1.7, "items": [{"span": 8, "piece": "chart", ...},
                                  {"span": 4, "piece": "..."}]},
        {"height": 0.8, "items": [{"span": 12, "piece": "callout", ...}]}],
     "footnote": "Assumes ~3 min per scan ...",              # optional
     "notes": "What the presenter says"}                     # optional, speaker notes

Rows share the height left after the fixed ones by weight; items share a row on a 12-column
grid by span. Every piece draws itself inside its cell (design.pieces).

Each slide is added on the template's own content layout, so the title box, footer, page number
and decoration are the template's, untouched. The template's example slides are removed.

Called by: the design spike script; later the graph's render node.
"""
from __future__ import annotations

import copy
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.enum.text import MSO_ANCHOR
from pptx.util import Pt

from . import measure
from .pieces import PIECES, Box, _rgb
from .profile import extract_profile

ROW_GAP = 0.32
GUTTER = 0.28
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _clear_slides(prs) -> None:
    """Remove every slide the template came with (their parts go when nothing refers to them)."""
    ids = prs.slides._sldIdLst
    for sld in list(ids):
        prs.part.drop_rel(sld.rId)
        ids.remove(sld)


def _page_number(slide, number: int) -> None:
    """Give the slide its layout's page-number box, holding a live slide-number field.

    python-pptx's add_slide copies a layout's title and body boxes but not its slide number.
    """
    src = next((ph for ph in slide.slide_layout.placeholders
                if ph.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER), None)
    if src is None:
        return
    slide.shapes.clone_placeholder(src)
    ph = next(p for p in slide.placeholders
              if p.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER)
    p = ph.text_frame.paragraphs[0]._p
    for r in list(p):
        if r.tag in (f"{{{_A}}}r", f"{{{_A}}}fld"):
            p.remove(r)
    fld = etree.SubElement(p, f"{{{_A}}}fld", id="{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}",
                           type="slidenum")
    etree.SubElement(fld, f"{{{_A}}}rPr", lang="en-US")
    etree.SubElement(fld, f"{{{_A}}}t").text = str(number)
    end = p.find(f"{{{_A}}}endParaRPr")
    if end is not None:
        p.remove(end)
        p.append(end)


def _title(slide, spec: dict, P: dict) -> list[str]:
    """Write the title into the layout's own title box, sized to fit in at most two lines."""
    t = P["title"]
    ph = slide.shapes.title
    x, y, w, h = t["box"]
    if ph is None:                     # a deck whose layout has no title box: draw one where it would be
        from pptx.util import Emu

        ph = slide.shapes.add_textbox(Emu(int(x * 914400)), Emu(int(y * 914400)),
                                      Emu(int(w * 914400)), Emu(int(h * 914400)))
        ph.text_frame.word_wrap = True
    pt = measure.fit(spec["title"], w, h, t["max_pt"], t["min_pt"], family=t["font"],
                     bold=True, max_lines=2)
    problems = []
    if pt is None:
        problems.append("title: too long for two lines; make the point in about 10 words")
        pt = t["min_pt"]
    tf = ph.text_frame
    tf.text = spec["title"]
    # Always from the top of the box: one line or two, subtitle or not, the first line of every
    # title sits at the same distance from the top of the slide.
    tf.vertical_anchor = MSO_ANCHOR.TOP
    for r in tf.paragraphs[0].runs:
        r.font.size, r.font.bold, r.font.name = Pt(pt), True, t["font"]
        r.font.color.rgb = _rgb(t["colour"])
    return problems


#: The rule under a content slide's title: this far below the title box, and content starts
#: this far below the rule. Fixed, so every content slide matches.
RULE_GAP, AFTER_RULE = 0.06, 0.2
RULE_NAME = "title-rule"


def title_rule_y(P: dict) -> float:
    """Where the title rule goes on every content slide, in inches from the top."""
    _, y, _, h = P["title"]["box"]
    return y + h + RULE_GAP


def draw_title_rule(slide, P: dict):
    """The thin accent line that separates a content slide's title from its content.

    Called by: render_slide; revise.house.align_titles.
    """
    from .pieces import line

    area = Box(*P["area"])
    y = title_rule_y(P)
    c = line(slide, area.x, y, area.right, y, P["colours"]["accent"], 1.0)
    c.name = RULE_NAME
    return c


def content_top(P: dict) -> float:
    """Where content starts on a content slide: below the title rule when there is one."""
    return title_rule_y(P) + AFTER_RULE if P.get("title_rule", True) else P["area"][1] + 0.15


def _cells(row_box: Box, items: list[dict]) -> list[Box]:
    """Where each item of a row goes, from its span on the 12-column grid."""
    col = (row_box.w - 11 * GUTTER) / 12
    out, x = [], row_box.x
    for it in items:
        span = it.get("span", 12 // len(items))
        w = span * col + (span - 1) * GUTTER
        out.append(Box(x, row_box.y, w, row_box.h))
        x += w + GUTTER
    return out


def render_slide(slide, spec: dict, P: dict) -> list[str]:
    """Draw one spec onto a slide that already has its layout. -> problems found while drawing.

    Called by: render_deck.
    """
    problems = _title(slide, spec, P)
    area = Box(*P["area"])
    if P.get("title_rule", True):
        draw_title_rule(slide, P)
    top = content_top(P)
    area = Box(area.x, top, area.w, area.bottom - top)
    if spec.get("subtitle"):
        spt = P["body_pt"] + 3
        sub, area = area.take_top(measure.height_in(1, spt) + 0.02, gap=0.25)
        _subtitle(slide, sub, spec["subtitle"], spt, P)
    if spec.get("footnote"):
        fpt = P["small_pt"] - 1
        lines = len(measure.wrap(spec["footnote"], fpt, area.w, P["font"], italic=True))
        fbox, area = area.take_bottom(measure.height_in(lines, fpt) + 0.02, gap=0.22)
        problems += PIECES["footnote"](slide, fbox, {"text": spec["footnote"]}, P)
    rows = spec.get("rows", [])
    fixed = sum(r["height"] for r in rows if "height" in r)
    weights = sum(r.get("weight", 1) for r in rows if "height" not in r) or 1
    free = area.h - fixed - ROW_GAP * (len(rows) - 1)
    y = area.y
    for r in rows:
        h = r["height"] if "height" in r else free * r.get("weight", 1) / weights
        row_box = Box(area.x, y, area.w, h)
        for cell, item in zip(_cells(row_box, r["items"]), r["items"]):
            piece = PIECES.get(item["piece"])
            if piece is None:
                problems.append(f"unknown piece {item['piece']!r}")
                continue
            problems += piece(slide, cell, item, P)
        y += h + ROW_GAP
    if spec.get("notes"):
        slide.notes_slide.notes_text_frame.text = spec["notes"]
    return problems


def render_cover(slide, spec: dict, P: dict) -> list[str]:
    """The opening slide: the deck's title on the left, a brand panel on the right that lists
    what the deck covers (spec["agenda"], optional).

    spec: {"type": "cover", "title", "subtitle", "meta": "date / author", "agenda": [...]}
    Called by: render_deck.
    """
    from .pieces import rect, text

    W, H = P["slide"]["w"], P["slide"]["h"]
    c = P["colours"]
    split = W * 0.62
    rect(slide, Box(split, 0, W - split, H), c["primary_dark"])
    rect(slide, Box(split, 0, 0.14, H), c["accent"])
    x, w = 0.8, split - 1.5
    problems = []
    pt = measure.fit(spec["title"], w, 2.4, 44, 28, family=P["font"], bold=True, max_lines=3)
    if pt is None:
        problems.append("cover: the title is too long; at most ~12 words")
        pt = 28
    t_h = measure.height_in(len(measure.wrap(spec["title"], pt, w, P["font"], True)), pt)
    t_bottom = H * 0.56
    rect(slide, Box(x, t_bottom - t_h - 0.4, 1.0, 0.09), c["accent"])
    text(slide, Box(x, t_bottom - t_h, w, t_h + 0.04), spec["title"], pt, c["text"], P, bold=True,
         anchor="bottom")
    if spec.get("subtitle"):
        spt = P["body_pt"] + 5
        text(slide, Box(x, t_bottom + 0.25, w, measure.height_in(3, spt)), spec["subtitle"], spt,
             c["muted"], P)
    if spec.get("meta"):
        text(slide, Box(x, H - 0.95, w, 0.4), spec["meta"], P["small_pt"], c["muted"], P, anchor="bottom")
    agenda = spec.get("agenda") or []
    if agenda:
        ax, aw = split + 0.75, W - split - 1.3
        apt = measure.fit_all(agenda, aw - 0.6, measure.height_in(2, P["body_pt"] + 2), P["body_pt"] + 2,
                              P["small_pt"], family=P["font"], max_lines=2) or P["small_pt"]
        rows = [measure.height_in(len(measure.wrap(a, apt, aw - 0.6, P["font"])), apt) for a in agenda]
        total = sum(rows) + 0.3 * (len(rows) - 1) + 0.6
        y = (H - total) / 2
        text(slide, Box(ax, y, aw, 0.35), "IN THIS DECK", P["small_pt"], c["accent_light"], P, bold=True)
        y += 0.6
        for k, (a, h) in enumerate(zip(agenda, rows)):
            text(slide, Box(ax, y, 0.5, h), f"{k + 1:02d}", apt, c["accent"], P, bold=True)
            text(slide, Box(ax + 0.6, y, aw - 0.6, h + 0.02), a, apt, "FFFFFF", P)
            y += h + 0.3
    if spec.get("notes"):
        slide.notes_slide.notes_text_frame.text = spec["notes"]
    return problems


def render_section(slide, spec: dict, P: dict) -> list[str]:
    """A section divider: brand background, a large section number, the section's title.

    spec: {"type": "section", "number": 2, "title", "subtitle"}
    Called by: render_deck.
    """
    from .pieces import line, rect, text

    W, H = P["slide"]["w"], P["slide"]["h"]
    c = P["colours"]
    rect(slide, Box(0, 0, W, H), c["primary_dark"])
    rect(slide, Box(0, H - 0.14, W, 0.14), c["accent"])
    x, w = 0.9, W * 0.7
    if spec.get("number") is not None:
        text(slide, Box(x, H * 0.18, 3, 1.4), f"{int(spec['number']):02d}", 80, c["accent"], P,
             bold=True, anchor="bottom")
    pt = measure.fit(spec["title"], w, 1.8, 40, 26, family=P["font"], bold=True, max_lines=2) or 26
    text(slide, Box(x, H * 0.18 + 1.6, w, 1.8), spec["title"], pt, "FFFFFF", P, bold=True)
    y = H * 0.18 + 1.6 + measure.height_in(len(measure.wrap(spec["title"], pt, w, P["font"], True)), pt) + 0.25
    if spec.get("subtitle"):
        text(slide, Box(x, y, w, 0.5), spec["subtitle"], P["body_pt"] + 4, c["accent_light"], P)
        y += 0.6
    # What the section argues: the titles of its slides, so the divider is a summary, not a pause.
    points = spec.get("agenda") or []
    if points:
        apt = P["body_pt"] + 1
        y += 0.2
        line(slide, x, y, x + 1.2, y, c["accent"], 2.0)
        y += 0.3
        for p in points[:5]:
            h = measure.height_in(len(measure.wrap(p, apt, w, P["font"])), apt)
            if y + h > H - 0.5:
                break
            text(slide, Box(x, y, w, h + 0.02), p, apt, "FFFFFF", P)
            y += h + 0.16
    if spec.get("notes"):
        slide.notes_slide.notes_text_frame.text = spec["notes"]
    return []


def _subtitle(slide, box: Box, value: str, pt: float, P: dict) -> None:
    """The subtitle line under the title."""
    from .pieces import text

    text(slide, box, value, pt, P["colours"]["muted"], P)


def _no_full_stop(title: str) -> str:
    """Slide titles carry no closing full stop, however the writer ended them."""
    t = title.strip()
    return t[:-1].rstrip() if t.endswith(".") and not t.endswith("..") else t


def render_deck(template: str | Path, specs: list[dict], out: str | Path,
                profile: dict | None = None) -> dict:
    """Build a deck of `specs` on `template`. -> {"out", "problems": {slide number: [...]}}

    Called by: the design spike script; later the graph's render node.
    """
    P = profile or extract_profile(template)
    prs = Presentation(str(template))
    _clear_slides(prs)
    problems: dict[int, list[str]] = {}
    for n, spec in enumerate(specs, start=1):
        _, found = add_spec_slide(prs, spec, P, n)
        if found:
            problems[n] = found
    prs.save(str(out))
    return {"out": str(out), "problems": problems}


def add_spec_slide(prs, spec: dict, P: dict, number: int):
    """Append one slide drawn from `spec` to an open presentation. -> (slide, problems)

    Covers and section dividers go on the profile's blank layout, content slides on its content
    layout with a page number. The slide is appended; a caller that wants it elsewhere moves it.
    Called by: render_deck; revise.surgery (redesigned and added slides in an existing deck).
    """
    layouts = {l.name: l for l in prs.slide_layouts}
    layout = layouts.get(P["layout"]) or prs.slide_layouts[0]
    blank = layouts.get(P.get("blank_layout") or "", layout)
    kind = spec.get("type", "content")
    spec = {**copy.deepcopy(spec), "title": _no_full_stop(spec.get("title") or "")}
    if kind in ("cover", "section"):
        slide = prs.slides.add_slide(blank)
        for ph in list(slide.placeholders):          # a fallback layout's title box
            ph._element.getparent().remove(ph._element)
        draw = render_cover if kind == "cover" else render_section
        return slide, draw(slide, spec, P)
    slide = prs.slides.add_slide(layout)
    _page_number(slide, number)
    return slide, render_slide(slide, spec, P)
