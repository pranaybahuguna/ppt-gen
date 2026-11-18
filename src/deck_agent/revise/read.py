"""What is on a slide, read from the file - whoever made it, however it was edited.

Two views, for two kinds of work:

* `slide_content`  what the slide SAYS: title, texts in reading order, tables, chart data,
                   pictures, notes. A redesign rebuilds the slide from this.
* `slide_shapes`   what the slide IS: every shape with its id, kind, position, text, size and
                   fill. A tweak writes code against this, so it can find "the right-hand box".

And `deck_outline`, one line per slide, for deciding which slides a request is about.

Called by: revise.work, revise.nodes.split.
"""
from __future__ import annotations

import re

from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.oxml.ns import qn

EMU = 914400
_SKIP_PH = {PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.DATE}


def _leaves(shapes, group=None):
    """Every shape, groups opened, with the id of the group it sits in."""
    for sh in shapes:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _leaves(sh.shapes, sh.shape_id)
        else:
            yield sh, group


def _box(sh) -> list[float]:
    return [round((v or 0) / EMU, 2) for v in (sh.left, sh.top, sh.width, sh.height)]


def _is_chrome(sh) -> bool:
    """Page numbers, footers and dates: the deck's furniture, not the slide's content."""
    return sh.is_placeholder and sh.placeholder_format.type in _SKIP_PH


def _font_pt(sh) -> float | None:
    for p in sh.text_frame.paragraphs:
        for r in p.runs:
            if r.font.size:
                return r.font.size.pt
    return None


def _fill(sh) -> str | None:
    try:
        if sh.fill.type == 1:                          # solid
            return str(sh.fill.fore_color.rgb)
    except Exception:
        pass
    return None


def _line(sh) -> dict:
    """A connector's ends (inches) and its arrowheads: "end" (at the to-point), "start", "both"
    or "none". Arrowheads live in a:ln as a:tailEnd (end) / a:headEnd (start); type="none" or
    no element means no head."""
    def has(tag):
        ln = sh._element.spPr.find(qn("a:ln"))
        el = ln.find(qn(tag)) if ln is not None else None
        return el is not None and el.get("type", "none") != "none"
    start, end = has("a:headEnd"), has("a:tailEnd")
    arrow = "both" if start and end else "end" if end else "start" if start else "none"
    pt = lambda x, y: [round(x / EMU, 2), round(y / EMU, 2)]
    return {"from_in": pt(sh.begin_x, sh.begin_y), "to_in": pt(sh.end_x, sh.end_y), "arrow": arrow}


def _chart(sh) -> dict:
    ch = sh.chart
    plot = ch.plots[0]
    return {"kind": str(ch.chart_type).split(".")[-1].split(" ")[0].lower(),
            "categories": [str(c) for c in plot.categories],
            "series": [{"name": s.name, "values": [v for v in s.values]} for s in plot.series]}


def _chart_format(ch) -> dict:
    """How a chart is drawn - what a person sees and complains about ("the numbers on the bars are
    unreadable"): value labels, legend, axes, bar spacing. Read defensively: a chart made
    elsewhere may lack any of it.
    """
    def rgb(font):
        try:
            return str(font.color.rgb) if font.color and font.color.type is not None else None
        except (AttributeError, TypeError, ValueError):
            return None

    plot = ch.plots[0]
    out: dict = {}
    try:
        if plot.has_data_labels:
            dl = plot.data_labels
            pos = None
            try:
                pos = str(dl.position).split(".")[-1].split(" ")[0].lower() if dl.position is not None else None
            except (ValueError, KeyError):
                pass
            out["value_labels"] = {"shown": True, "font_pt": dl.font.size.pt if dl.font.size else None,
                                   "colour": rgb(dl.font), "bold": dl.font.bold, "position": pos,
                                   "number_format": dl.number_format}
        else:
            out["value_labels"] = {"shown": False}
    except (AttributeError, ValueError):
        pass
    try:
        out["series_colours"] = [str(srs.format.fill.fore_color.rgb) if srs.format.fill.type == 1 else None
                                 for srs in plot.series]
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        out["legend"] = str(ch.legend.position).split(".")[-1].split(" ")[0].lower() if ch.has_legend else None
    except (AttributeError, ValueError):
        pass
    for name in ("value_axis", "category_axis"):
        try:
            ax = getattr(ch, name)
            out[name] = {"visible": ax.visible,
                         "font_pt": ax.tick_labels.font.size.pt if ax.tick_labels.font.size else None}
        except (AttributeError, ValueError):
            pass
    try:
        out["gap_width"] = plot.gap_width
    except AttributeError:
        pass
    return out


def slide_content(slide) -> dict:
    """What a slide says, for rebuilding it: no positions, no styling.

    Called by: revise.work.redesign.
    """
    title = slide.shapes.title.text_frame.text.strip() if slide.shapes.title is not None else ""
    texts, tables, charts, pictures = [], [], [], 0
    items = sorted(_leaves(slide.shapes), key=lambda p: ((p[0].top or 0), (p[0].left or 0)))
    for sh, _ in items:
        if _is_chrome(sh) or (slide.shapes.title is not None and sh.shape_id == slide.shapes.title.shape_id):
            continue
        if sh.has_chart:
            charts.append(_chart(sh))
        elif sh.has_table:
            tables.append([[c.text.strip() for c in row.cells] for row in sh.table.rows])
        elif sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
            pictures += 1
        elif sh.has_text_frame and sh.text_frame.text.strip():
            texts.append({"text": sh.text_frame.text.strip(), "pt": _font_pt(sh),
                          "bold": any(r.font.bold for p in sh.text_frame.paragraphs for r in p.runs)})
    notes = slide.notes_slide.notes_text_frame.text.strip() if slide.has_notes_slide else ""
    return {"title": title, "texts": texts, "tables": tables, "charts": charts,
            "pictures": pictures, "notes": notes}


def slide_shapes(slide) -> list[dict]:
    """Every shape on a slide, for code that edits it in place: ids, kinds, boxes in inches, text.

    Called by: revise.work.tweak.
    """
    out = []
    for sh, group in _leaves(slide.shapes):
        e = {"id": sh.shape_id, "name": sh.name, "kind": str(sh.shape_type).split(".")[-1].split(" ")[0].lower()
             if sh.shape_type else "shape", "box_in": _box(sh)}
        if group is not None:
            e["in_group"] = group
        if sh.is_placeholder:
            e["placeholder"] = str(sh.placeholder_format.type).split(".")[-1].split(" ")[0].lower()
        if sh.has_text_frame and sh.text_frame.text.strip():
            e["text"] = sh.text_frame.text.strip()[:300]
            e["font_pt"] = _font_pt(sh)
        if sh.has_chart:
            e["chart"] = {**_chart(sh), "format": _chart_format(sh.chart)}
        if sh.has_table:
            e["table"] = [[c.text.strip()[:60] for c in row.cells] for row in sh.table.rows][:10]
        if sh.shape_type == MSO_SHAPE_TYPE.LINE and hasattr(sh, "begin_x"):
            e.update(_line(sh))
        fill = _fill(sh)
        if fill:
            e["fill"] = fill
        out.append(e)
    return out


#: Roles a slide can have. Deck-wide rules ("align every title") apply to "content" slides only,
#: unless a request names the others: a cover, a section divider or an agenda is designed
#: differently on purpose.
ROLES = ("cover", "section", "agenda", "content", "closing")
_AGENDA = re.compile(r"^\s*(agenda|contents|table of contents|in this deck|overview|outline)\b", re.I)
_NUMBER = re.compile(r"^\s*\d{1,2}\s*$")


def slide_role(slide, number: int, total: int, content_layout: str) -> str:
    """What a slide is for, from how it is built: cover, section, agenda, content or closing.

    The deck's main layout (the one most of its slides use) marks content slides; a slide on
    another layout, or one that is mostly a full-bleed panel with a big number, is a cover,
    divider or closing. Works on any deck, ours or hand-made.
    Called by: deck_outline; revise.house.
    """
    layout = slide.slide_layout.name.lower()
    title = slide.shapes.title
    title_text = title.text_frame.text.strip() if title is not None else ""
    texts = [sh.text_frame.text.strip() for sh, _ in _leaves(slide.shapes)
             if sh.has_text_frame and sh.text_frame.text.strip() and not _is_chrome(sh)]
    W, H = slide.part.package.presentation_part.presentation.slide_width, \
        slide.part.package.presentation_part.presentation.slide_height
    big_panel = any((sh.width or 0) * (sh.height or 0) > 0.6 * W * H
                    for sh, _ in _leaves(slide.shapes) if not sh.has_text_frame or not sh.text_frame.text.strip())
    if "section" in layout:
        return "section"
    if slide.slide_layout.name == content_layout and title_text:
        return "agenda" if _AGENDA.match(title_text) else "content"
    if number == 1 or "title slide" in layout:
        return "cover"
    if any(_NUMBER.match(t) for t in texts) and (big_panel or title is None):
        return "section"
    if number == total and len(" ".join(texts).split()) < 25:
        return "closing"
    if big_panel:
        return "section"
    return "content" if title_text else "section"


def deck_outline(prs) -> list[dict]:
    """One line per slide: number, role, title, a taste of the text, what visuals it carries.

    Called by: revise.nodes.split.
    """
    from ..design.profile import _content_layout

    main = _content_layout(prs).name
    total = len(prs.slides)
    out = []
    for n, slide in enumerate(prs.slides, start=1):
        c = slide_content(slide)
        words = " ".join(t["text"] for t in c["texts"]).split()
        out.append({"slide": n, "role": slide_role(slide, n, total, main), "title": c["title"][:120],
                    "text": " ".join(words[:40]), "charts": len(c["charts"]),
                    "tables": len(c["tables"]), "pictures": c["pictures"]})
    return out
