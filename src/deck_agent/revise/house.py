"""House rules: deck-wide changes done by plain code, the same on every slide, with no model.

When a request is about consistency ("every title at the same height", "a line under each
title"), sending each slide to its own model gives each slide its own interpretation - which is
exactly what a consistency request is trying to remove. These operations are written once and
applied identically, in seconds, to the content slides (revise.read.slide_role) unless the
request named others.

Called by: revise.nodes.join (task kind "align_titles").
"""
from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR
from pptx.util import Emu

from ..design.render import RULE_NAME, content_top, draw_title_rule, title_rule_y
from ..engine import bootstrap

EMU = 914400


def _old_rules(slide, title, below: float) -> list:
    """Thin full-width lines between the title and `below` inches: earlier title rules, whoever
    drew them. Anything with text, and anything thicker than a hairline, is left alone."""
    area_w = (slide.part.package.presentation_part.presentation.slide_width / EMU) * 0.4
    top = (title.top or 0) / EMU
    out = []
    for sh in slide.shapes:
        if sh.name == RULE_NAME:
            out.append(sh)
            continue
        if sh.shape_id == title.shape_id or (sh.has_text_frame and sh.text_frame.text.strip()):
            continue
        thin = (sh.height or 0) / EMU < 0.03
        wide = (sh.width or 0) / EMU > area_w
        y = (sh.top or 0) / EMU
        is_line = sh.shape_type in (MSO_SHAPE_TYPE.LINE, MSO_SHAPE_TYPE.AUTO_SHAPE) or "connector" in sh.name.lower()
        if thin and wide and is_line and top < y < below:
            out.append(sh)
    return out


def align_titles(src: str | Path, dst: str | Path, P: dict, slides: list[int]) -> dict:
    """Every title of `slides` (1-based) in the template's title box, text from the top, and one
    title rule at the standard height. Other slides keep their bytes. -> {"changed", "notes"}
    """
    bootstrap()
    from deckops.executor import save_preserving

    prs = Presentation(str(src))
    x, y, w, h = P["title"]["box"]
    rule_y = title_rule_y(P)
    changed, notes = [], []
    for n in slides:
        if not 1 <= n <= len(prs.slides):
            continue
        slide = prs.slides[n - 1]
        title = slide.shapes.title
        if title is None:
            notes.append(f"slide {n}: has no title box; left as it is")
            continue
        for old in _old_rules(slide, title, rule_y + 0.45):
            old._element.getparent().remove(old._element)
        title.left, title.top = Emu(int(x * EMU)), Emu(int(y * EMU))
        title.width, title.height = Emu(int(w * EMU)), Emu(int(h * EMU))
        title.text_frame.vertical_anchor = MSO_ANCHOR.TOP
        if P.get("title_rule", True):
            draw_title_rule(slide, P)
        # Content that reaches up past the rule would now be cut by it: say so rather than move it.
        for sh in slide.shapes:
            if sh.shape_id == title.shape_id or sh.name == RULE_NAME or sh.is_placeholder:
                continue
            top, bottom = (sh.top or 0) / EMU, ((sh.top or 0) + (sh.height or 0)) / EMU
            if top < rule_y < bottom and sh.has_text_frame and sh.text_frame.text.strip():
                notes.append(f"slide {n}: '{sh.text_frame.text.strip()[:40]}' crosses the title rule; "
                             f"content should start below {content_top(P):.2f} in")
                break
        changed.append(n)
    save_preserving(prs, str(src), str(dst))
    return {"changed": changed, "notes": notes}
