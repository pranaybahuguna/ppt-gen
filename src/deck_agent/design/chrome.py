"""A drawn template's furniture, learned from its slides.

Many decks have no title boxes at all: one blank layout, and every slide draws its own header
(an accent bar, the title, a subtitle), footer rule, footer text and "3 / 27" page number as
plain shapes. Building on such a deck's layout gives a bare slide, so the look has to be read
from the slides themselves:

* shapes at the same place on at least half the slides (and at least two) are its furniture;
* among them, the varying text near the top in the largest font is the title, the varying text
  just below it the subtitle; a "3 / 27" is the page number; text that never changes is a
  footer; shapes without text are decoration;
* the first slide, when it does not carry that header, is the cover: its biggest text is the
  title, the text right under it the subtitle, the text along the bottom the meta line.

The result is plain JSON (shape XML as strings), so it travels inside the design profile and a
renderer can stamp it onto new slides without the template's slides being there.

Called by: design.profile.extract_profile; design.render (stamping); revise.read (finding titles).
"""
from __future__ import annotations

import copy
import re
from collections import Counter, defaultdict

from lxml import etree
from pptx.oxml import parse_xml
from pptx.util import Pt

EMU = 914400
PAGE = re.compile(r"^\s*(\d{1,3})\s*(/|of)\s*(\d{1,3})\s*$|^\s*(\d{1,3})\s*$", re.I)
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_STOP = {"the", "and", "for", "with", "from", "this", "that", "deck", "slide", "page", "confidential",
         "internal", "only", "draft"}


def _sig(sh) -> tuple | None:
    """Where a shape is, to a twentieth of an inch: how furniture is recognised across slides."""
    if sh.left is None or sh.top is None:
        return None
    return tuple(int(round((v or 0) / EMU * 20)) for v in (sh.left, sh.top, sh.width, sh.height))


def _box(sh) -> list[float]:
    return [round((v or 0) / EMU, 3) for v in (sh.left, sh.top, sh.width, sh.height)]


def _text(sh) -> str:
    return sh.text_frame.text.strip() if sh.has_text_frame else ""


def look(sh) -> dict:
    """The first run's look: size, bold, italic, colour, font. Missing values are None."""
    for p in sh.text_frame.paragraphs:
        for r in p.runs:
            if not r.text.strip():
                continue
            f = r.font
            colour = None
            try:
                colour = str(f.color.rgb) if f.color and f.color.type is not None else None
            except (AttributeError, TypeError, ValueError):
                pass
            return {"pt": f.size.pt if f.size else None, "bold": f.bold, "italic": f.italic,
                    "colour": colour, "font": f.name}
    return {"pt": None, "bold": None, "italic": None, "colour": None, "font": None}


def _portable(sh) -> bool:
    """A shape that can be stamped onto another slide as XML alone: no pictures or links."""
    return not etree.ElementBase.xpath(sh._element, ".//@r:embed | .//@r:link | .//@r:id",
                                       namespaces={"r": _R})


def _xml(sh) -> str:
    return etree.tostring(sh._element).decode()


_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _background(slide) -> str | None:
    """A slide's own background (p:bg), when it has one that needs no linked picture."""
    bg = slide._element.find(f"{_P}cSld/{_P}bg")
    if bg is None or etree.ElementBase.xpath(bg, ".//@r:embed", namespaces={"r": _R}):
        return None
    return etree.tostring(bg).decode()


def set_background(slide, xml: str | None) -> None:
    if not xml:
        return
    cSld = slide._element.find(f"{_P}cSld")
    old = cSld.find(f"{_P}bg")
    if old is not None:
        cSld.remove(old)
    cSld.insert(0, parse_xml(xml))


def _deck_specific(text: str, cover_text: str) -> bool:
    """A footer that names this deck (it shares a word with the cover) rather than the brand."""
    words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", text)} - _STOP
    cover = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", cover_text)}
    return bool(words & cover)


def learn(prs) -> dict | None:
    """The deck's furniture, or None when it has none worth keeping (no recurring title)."""
    slides = list(prs.slides)
    if len(slides) < 2:
        return None
    W, H = prs.slide_width / EMU, prs.slide_height / EMU
    count, texts = Counter(), defaultdict(set)
    for s in slides:
        sigs = set()
        for sh in s.shapes:
            g = _sig(sh)
            if g is not None:
                sigs.add(g)
                texts[g].add(_text(sh))
        count.update(sigs)
    need = max(2, (len(slides) + 1) // 2)
    recurring = {g for g, n in count.items() if n >= need}
    if not recurring:
        return None
    ref = max(slides, key=lambda s: sum(_sig(sh) in recurring for sh in s.shapes))
    mine = [sh for sh in ref.shapes if _sig(sh) in recurring]

    def varying(sh):
        return len(texts[_sig(sh)] - {""}) > 1

    tops = [sh for sh in mine if _text(sh) and varying(sh) and sh.top / EMU < H * 0.3
            and not PAGE.match(_text(sh))]
    if not tops:
        return None
    title = max(tops, key=lambda sh: ((look(sh)["pt"] or 0), -sh.top))
    below = [sh for sh in tops if sh is not title and sh.top >= title.top + title.height * 0.5]
    subtitle = min(below, key=lambda sh: sh.top, default=None)

    cover = slides[0] if not any(_sig(sh) == _sig(title) for sh in slides[0].shapes) else None
    cover_text = " ".join(_text(sh) for sh in cover.shapes) if cover is not None else ""

    content, page_format = [], "{n}"
    for sh in mine:
        t = _text(sh)
        if sh is title:
            role = "title"
        elif sh is subtitle:
            role = "subtitle"
        elif t and PAGE.match(t):
            role = "page"
            m = PAGE.match(t)
            page_format = "{n} " + m.group(2) + " {total}" if m.group(2) else "{n}"
        elif t and varying(sh):
            continue                                     # content that happens to sit in one place
        elif t:
            role = "deck_footer" if _deck_specific(t, cover_text) else "footer"
        else:
            role = "decor"
        if _portable(sh):
            content.append({"role": role, "xml": _xml(sh), "box": _box(sh)})

    header = [c["box"] for c in content if c["box"][1] < H * 0.3]
    footer = [c["box"] for c in content if c["box"][1] > H * 0.75]
    left = min([b[0] for b in header + footer] or [0.5])
    right = max([b[0] + b[2] for b in footer] or [W - left])
    if right - left < W * 0.6:
        right = W - left
    top = max(b[1] + b[3] for b in header) + 0.2
    bottom = (min(b[1] for b in footer) if footer else H - 0.5) - 0.15

    out = {"content": content, "page_format": page_format, "background": _background(ref),
           "title": {"box": _box(title), **look(title)},
           "subtitle": ({"box": _box(subtitle), **look(subtitle)} if subtitle is not None else None),
           "area": [round(left, 3), round(top, 3), round(right - left, 3), round(bottom - top, 3)],
           "cover": _learn_cover(cover, H) if cover is not None else None}
    return out


def _learn_cover(slide, H: float) -> dict | None:
    texts = [sh for sh in slide.shapes if _text(sh) and _portable(sh)]
    if not texts:
        return None
    title = max(texts, key=lambda sh: ((look(sh)["pt"] or 0), -sh.top))
    below = [sh for sh in texts if sh is not title and sh.top >= title.top + title.height * 0.5
             and sh.top / EMU < H * 0.7]
    subtitle = min(below, key=lambda sh: sh.top, default=None)
    bottom = [sh for sh in texts if sh.top / EMU > H * 0.85 and sh is not title]
    meta = max(bottom, key=lambda sh: sh.width, default=None)
    ids = {"title": title.shape_id, "subtitle": getattr(subtitle, "shape_id", None),
           "meta": getattr(meta, "shape_id", None)}
    shapes = []
    for sh in slide.shapes:
        if not _portable(sh):
            continue
        if sh.shape_id == ids["title"]:
            role = "title"
        elif sh.shape_id == ids["subtitle"]:
            role = "subtitle"
        elif sh.shape_id == ids["meta"]:
            role = "meta"
        elif _text(sh):
            continue                                     # that deck's own facts, not the design
        elif (sh.width or 0) / EMU < 1.0 or (sh.height or 0) / EMU < 1.0 or \
                (sh.width or 0) * (sh.height or 0) > 0.5 * slide.part.package.presentation_part.presentation.slide_width * slide.part.package.presentation_part.presentation.slide_height:
            role = "decor"                               # bars, rules, a full-bleed background
        else:
            continue                                     # a card that held one of the dropped facts
        shapes.append({"role": role, "xml": _xml(sh), "box": _box(sh)})
    return {"shapes": shapes, "background": _background(slide), "title": {"box": _box(title), **look(title)}}


# --------------------------------------------------------------------------- stamping

def set_text(shape, value: str, pt: float | None = None) -> None:
    """Replace a shape's text, keeping the look of its first run (font, colour, size, bold).
    Lines of `value` become paragraphs with the first paragraph's formatting."""
    txBody = shape.text_frame._txBody
    paras = txBody.findall("{http://schemas.openxmlformats.org/drawingml/2006/main}p")
    first = paras[0]
    for p in paras[1:]:
        txBody.remove(p)
    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    runs = first.findall(f"{A}r")
    proto = copy.deepcopy(runs[0]) if runs else None
    for child in list(first):
        if child.tag in (f"{A}r", f"{A}br", f"{A}fld"):
            first.remove(child)
    lines = value.split("\n") or [""]
    template_p = copy.deepcopy(first)
    for k, line in enumerate(lines):
        p = first if k == 0 else copy.deepcopy(template_p)
        if k:
            txBody.append(p)
        r = copy.deepcopy(proto) if proto is not None else etree.SubElement(p, f"{A}r")
        if proto is not None:
            end = p.find(f"{A}endParaRPr")
            if end is not None:
                end.addprevious(r)
            else:
                p.append(r)
        t = r.find(f"{A}t")
        if t is None:
            t = etree.SubElement(r, f"{A}t")
        t.text = line
    if pt is not None:
        for p in shape.text_frame.paragraphs:
            for r in p.runs:
                r.font.size = Pt(pt)


def stamp(slide, shapes: list[dict]) -> dict:
    """Copy furniture onto a slide, bottom-most first, each with a fresh id and a name saying its
    role ("deck-title", "deck-page", ...). -> {role: shape} for the text roles."""
    tree = slide.shapes._spTree
    found = {}
    for item in shapes:
        el = parse_xml(item["xml"])
        nv = el.find(".//{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr")
        if nv is not None:
            nv.set("id", str(slide.shapes._next_shape_id))
            nv.set("name", f"deck-{item['role'].replace('_', '-')}")
        tree.append(el)
        if item["role"] != "decor":
            found[item["role"]] = slide.shapes[-1]
    return found


def remove(shape) -> None:
    shape._element.getparent().remove(shape._element)
