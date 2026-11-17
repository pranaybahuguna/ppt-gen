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

from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

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


def _chart(sh) -> dict:
    ch = sh.chart
    plot = ch.plots[0]
    return {"kind": str(ch.chart_type).split(".")[-1].split(" ")[0].lower(),
            "categories": [str(c) for c in plot.categories],
            "series": [{"name": s.name, "values": [v for v in s.values]} for s in plot.series]}


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
            e["chart"] = _chart(sh)
        if sh.has_table:
            e["table"] = [[c.text.strip()[:60] for c in row.cells] for row in sh.table.rows][:10]
        fill = _fill(sh)
        if fill:
            e["fill"] = fill
        out.append(e)
    return out


def deck_outline(prs) -> list[dict]:
    """One line per slide: number, title, a taste of the text, what visuals it carries.

    Called by: revise.nodes.split.
    """
    out = []
    for n, slide in enumerate(prs.slides, start=1):
        c = slide_content(slide)
        words = " ".join(t["text"] for t in c["texts"]).split()
        out.append({"slide": n, "title": c["title"][:120], "text": " ".join(words[:40]),
                    "charts": len(c["charts"]), "tables": len(c["tables"]), "pictures": c["pictures"]})
    return out
