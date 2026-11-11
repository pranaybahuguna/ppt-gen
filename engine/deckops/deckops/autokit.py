"""Draft a style kit from a template automatically: rules only, no AI, no person.

Runs in the sandbox (it reads the template file). Output: a draft kit in the same format
as a hand-written one (see deckops/patterns.py), plus notes on what was found. The draft is
then checked against the template (patterns.validate_kit) and tested by building dummy
slides (controlplane/kit_check.py). Patterns that fail are dropped, so a wrong guess here
can only make the menu smaller, never break a deck.

What the rules look for on each slide:
  title      largest text near the top
  subtitle   smaller text directly under the title
  page       small text like "5 / 27" or "5" near the bottom
  footer     other small text near the bottom
  items      shapes that repeat with the same size at regular spacing (cards, columns, tiles);
             shapes that keep the same offset from each copy belong to the same item
  roles      inside an item: digit = number, big short text = value, longest text = body or points,
             text above/below a value = label/caption, short top text = heading
  table      a table shape
  charts     a large rectangle that is empty or says "chart"
  other text named by position: intro (above the content), takeaway (below), side_title/side_text
"""
from __future__ import annotations

import re
from collections import Counter

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.util import Emu

from .refs import to_slide

TOL = 54864                    # 0.06 inch, in EMU
PAGE_RE = re.compile(r"^\s*\d{1,3}\s*(/\s*\d{1,3})?\s*$")
CHART_WORD = re.compile(r"chart|graph", re.I)
NUMBER_LIKE = re.compile(r"\d")            # a headline value has at least one digit: "42%", "3.2x", "EUR 5M"
CHROME = {"title", "subtitle", "page", "footer"}


def leaves(shapes):
    """Every shape that actually draws something, groups opened up.

    A group is a container, not a thing on the slide: what a reader sees is its contents. The
    rules below reason about position and size - which boxes repeat, what sits beside what -
    so they need the shapes themselves, with slide coordinates (`S` converts those).

    Without this, a template whose cards are "icon + heading + body, grouped" - which is how
    most bought templates are built - presents every card as one opaque box with no text, and
    no pattern can be found.
    """
    for sh in shapes:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from leaves(sh.shapes)
        else:
            yield sh


class S:
    """The facts about one shape that the rules need."""

    def __init__(self, sh):
        self.id = sh.shape_id
        # Slide coordinates, always: a shape inside a group states its position in that
        # group's own space, which cannot be compared with anything else on the slide.
        self.x, self.y, self.w, self.h = to_slide(
            sh._element, int(sh.left or 0), int(sh.top or 0), int(sh.width or 0), int(sh.height or 0))
        self.is_table = bool(getattr(sh, "has_table", False) and sh.has_table)
        self.is_chart = bool(getattr(sh, "has_chart", False) and sh.has_chart)
        self.is_group = sh.shape_type == MSO_SHAPE_TYPE.GROUP
        self.is_picture = sh.shape_type == MSO_SHAPE_TYPE.PICTURE
        self.can_text = sh.has_text_frame
        self.text = sh.text_frame.text.strip() if sh.has_text_frame else ""
        self.paras = [p for p in sh.text_frame.paragraphs if p.text.strip()] if sh.has_text_frame else []
        sizes, bold = [], False
        if sh.has_text_frame:
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    if r.font.size:
                        sizes.append(r.font.size.pt)
                    bold = bold or bool(r.font.bold)
        self.size = max(sizes) if sizes else None
        self.bold = bold
        kind = "table" if self.is_table else "chart" if self.is_chart else "group" if self.is_group else \
            "picture" if self.is_picture else ("text" if self.text else "box")
        self.kind = kind
        self.sig = (kind, round(self.w / 45720), round(self.h / 45720))     # size in 0.05-inch steps

    @property
    def lines(self):
        """How many lines of text the box holds at its own font size."""
        return (self.h / 12700) / ((self.size or 12) * 1.2)

    @property
    def area(self):
        return self.w * self.h

    @property
    def bottom(self):
        return self.y + self.h


def _fill_colour(sh):
    try:
        if sh.fill.type == 1 and sh.fill.fore_color.type is not None:
            return str(sh.fill.fore_color.rgb)
    except Exception:
        pass
    return None


def _is_grey(hexc):
    r, g, b = int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)
    return max(r, g, b) - min(r, g, b) < 24          # white, black and greys aren't brand colours


def chart_style(prs):
    """Palette = the most used non-grey fill colours in the template, then the theme accents."""
    counts = Counter()
    fonts = Counter()
    for s in prs.slides:
        for sh in s.shapes:
            c = _fill_colour(sh)
            if c and not _is_grey(c):
                counts[c] += 1
            if sh.has_text_frame:
                for p in sh.text_frame.paragraphs:
                    for r in p.runs:
                        if r.font.name:
                            fonts[r.font.name] += 1
    palette = [c for c, _ in counts.most_common(6)]
    try:
        theme = etree.fromstring(prs.slide_master.part.part_related_by(RT.THEME).blob)
        ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
        for i in range(1, 7):
            for v in theme.xpath(f"//a:accent{i}/a:srgbClr/@val", namespaces=ns):
                if v.upper() not in palette and len(palette) < 6:
                    palette.append(v.upper())
        minor = theme.xpath("//a:minorFont/a:latin/@typeface", namespaces=ns)
    except Exception:
        minor = []
    font = fonts.most_common(1)[0][0] if fonts else (minor[0] if minor else "Calibri")
    return {"palette": palette or ["4472C4"], "font": font, "text_colour": "333333", "grid_colour": "E5E7EB"}


# ---- slide furniture: title, subtitle, page number, footer --------------------------------

def find_chrome(shapes, W, H):
    texts = [s for s in shapes if s.kind == "text"]
    chrome = {}
    big = max((s.size or 0) for s in texts) if texts else 0
    top_limit = 0.4 * H if big >= 36 else 0.25 * H
    cands = [s for s in texts if s.y < top_limit]
    if cands:
        title = max(cands, key=lambda s: ((s.size or 18), -s.y))
        chrome["title"] = title
        subs = [s for s in texts if s is not title and title.y < s.y <= title.bottom + 0.6 * 914400
                and (s.size or 12) < (title.size or 18) and s.x < title.x + title.w and s.x + s.w > title.x]
        if subs:
            chrome["subtitle"] = min(subs, key=lambda s: s.y)
    low = [s for s in texts if s.y > 0.85 * H and s not in chrome.values()]
    pages = [s for s in low if PAGE_RE.match(s.text)]
    if pages:
        chrome["page"] = max(pages, key=lambda s: s.x)
    feet = [s for s in low if s not in chrome.values() and (s.size or 10) <= 12]
    if feet:
        chrome["footer"] = min(feet, key=lambda s: s.x)
    return chrome


# ---- repeated items -----------------------------------------------------------------------

def _row_major(insts):
    return sorted(insts, key=lambda s: (round(s.y / 182880), s.x))     # rows in 0.2-inch bands


def find_items(shapes, exclude):
    """Find the repeated block with the most member shapes. Returns groups (one list per item).

    An anchor is a shape size that occurs k times (2-12). Another shape belongs to the item if
    EVERY anchor copy has a shape of the same size at the same offset from it. Matching by
    offset (not only by size) keeps apart shapes that happen to share a size, like a tile's
    label and caption.
    """
    pool = [s for s in shapes if s not in exclude and s.kind in ("text", "box", "picture")]
    by_sig = {}
    for s in pool:
        by_sig.setdefault(s.sig, []).append(s)

    def at(sig, x, y, taken):
        for t in by_sig.get(sig, []):
            if t not in taken and abs(t.x - x) <= TOL and abs(t.y - y) <= TOL:
                return t
        return None

    best = None
    for anchor_sig, lst in by_sig.items():
        k = len(lst)
        if not 2 <= k <= 12:
            continue
        anchors = _row_major(lst)
        a0 = anchors[0]
        members, taken = [anchors], set(anchors)
        for cand in sorted(pool, key=lambda s: (s.y, s.x)):
            if cand in taken:
                continue
            dx, dy = cand.x - a0.x, cand.y - a0.y
            row = []
            for a in anchors:
                t = at(cand.sig, a.x + dx, a.y + dy, taken | set(row))
                if t is None:
                    break
                row.append(t)
            if len(row) == k:
                members.append(row)
                taken.update(row)
        if len(members) < 2 or not any(m[0].kind == "text" for m in members):
            continue                              # an item needs 2+ shapes, at least one with text
        score = (len(members), k, a0.area)
        if best is None or score > best[0]:
            best = (score, members)
    if not best:
        return None
    members = best[1]
    k = len(members[0])
    order = sorted(range(len(members)), key=lambda j: (members[j][0].y - members[0][0].y,
                                                       members[j][0].x - members[0][0].x))
    return [[members[j][i] for j in order] for i in range(k)]


def item_roles(groups):
    """Name the text shapes inside an item. Returns {field: position in group}.

    A shape is the item's NUMBER only if its texts across all items run 1, 2, 3 ...
    (so headline values like "42" or "99" are not mistaken for step numbers).
    """
    group = groups[0]
    texts = [(i, s) for i, s in enumerate(group) if s.kind == "text"]
    roles = {}
    rest = list(texts)
    for i, s in list(rest):
        seq = [g[i].text for g in groups]
        if all(t.isdigit() for t in seq) and [int(t) for t in seq] == list(range(1, len(groups) + 1)):
            roles["number"] = i
            rest.remove((i, s))
            break
    def is_value(i, s):                  # big, short, and clearly bigger than the item's other text
        others = [t.size or 12 for j, t in rest if j != i]
        return (len(s.text) <= 10 and NUMBER_LIKE.search(s.text) and others
                and (s.size or 12) >= 1.5 * min(others))
    value = next(((i, s) for i, s in rest if is_value(i, s)), None)
    if value:
        roles["value"] = value[0]
        rest.remove(value)
        for i, s in rest:
            name = "label" if s.y < value[1].y else "caption"
            roles[name if name not in roles else f"{name}_{i}"] = i
        return roles
    roomy = [t for t in rest if t[1].lines >= 1.8]
    if roomy:
        body = max(roomy, key=lambda t: len(t[1].text))
        roles["points" if len(body[1].paras) > 1 else "body"] = body[0]
        rest.remove(body)
    for n, (i, s) in enumerate(sorted(rest, key=lambda t: t[1].y)):
        roles["heading" if n == 0 else f"detail_{n}"] = i
    return roles


# ---- one slide → one pattern ---------------------------------------------------------------

def slide_pattern(slide, n, total, W, H):
    shapes = [S(sh) for sh in leaves(slide.shapes)]
    notes = []
    ids = Counter(e.get("id") for e in slide.shapes._spTree.iter("{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr"))
    if any(c > 1 for c in ids.values()):
        notes.append(f"shapes share ids {sorted(i for i, c in ids.items() if c > 1)}: can't be addressed safely")
        return None, notes
    if any(s.is_chart for s in shapes):
        notes.append("holds a chart: can't be used as a pattern yet")
        return None, notes
    chrome = find_chrome(shapes, W, H)
    used = set(chrome.values())
    p = {"slide": n, "fields": {k: s.id for k, s in chrome.items()}}

    tables = [s for s in shapes if s.is_table]
    if tables:
        t = tables[0]
        used.add(t)
        p["table"] = {"shape": t.id, "max_rows": 9}

    content_area = 0.15 * W * H

    def holds_something(box):            # a background panel has other shapes sitting on it
        return any(o is not box and box.x <= o.x + o.w / 2 <= box.x + box.w and box.y <= o.y + o.h / 2 <= box.bottom
                   for o in shapes)
    charts = sorted([s for s in shapes if s not in used and s.kind in ("box", "text") and s.area > content_area
                     and (not s.text or CHART_WORD.search(s.text)) and not holds_something(s)], key=lambda s: s.x)
    if charts:
        used.update(charts)
        names = ["main"] if len(charts) == 1 else ["left", "right"] if len(charts) == 2 else \
            [f"chart_{i + 1}" for i in range(len(charts))]
        p["charts"] = {nm: s.id for nm, s in zip(names, charts)}

    groups = find_items(shapes, used)
    if groups:
        roles = item_roles(groups)
        if roles:
            used.update(s for g in groups for s in g)
            xs = {round(g[0].x / 91440) for g in groups}
            lo = 2 if any(r in roles for r in ("value", "points")) else 1
            p["items"] = {"groups": [[s.id for s in g] for g in groups], "fields": roles,
                          "min": min(lo, len(groups)), "max": len(groups), "max_columns": len(xs)}

    # every other text shape becomes a field named by where it sits
    body = [s for s in used if s not in chrome.values()]
    top = min((s.y for s in body), default=None)
    bottom = max((s.bottom for s in body), default=None)
    counts = Counter()
    for s in sorted([s for s in shapes if s not in used and s.kind == "text"], key=lambda s: (s.y, s.x)):
        roomy = s.lines >= 1.8               # long-text names only for boxes that hold 2+ lines
        if top is None:
            name = "body" if roomy else "heading"
        elif s.bottom <= top + TOL:
            name = "intro" if roomy else "heading"
        elif s.y >= bottom - TOL:
            name = "takeaway"
        else:
            name = "side_text" if roomy else "side_title"
        counts[name] += 1
        p["fields"][name if counts[name] == 1 else f"{name}_{counts[name]}"] = s.id
        used.add(s)

    big = max((s.size or 0) for s in shapes if s.kind == "text") if any(s.kind == "text" for s in shapes) else 0
    if n not in (1, total) and set(p["fields"]) <= CHROME and not any(k in p for k in ("items", "charts", "table")):
        notes.append("only a title and footer are fillable (the rest is a picture or fixed drawing); not offered")
        return None, notes
    p["big_text"] = big >= 36
    name = kind_of(p, n, total)
    p["use_for"] = USE_FOR[name](p)
    return (name, p), notes


def kind_of(p, n, total):
    """Name a pattern from what it holds. Used by the rules, and again after vision relabels boxes."""
    roles = set(p.get("items", {}).get("fields", {}))
    if n == 1 and p.get("big_text"):
        return "cover"
    if n == total and p.get("big_text"):
        return "closing"
    if "table" in p:
        return "table"
    if "charts" in p:
        return "two_charts" if len(p["charts"]) == 2 else "chart"
    if "items" in p:
        return "steps" if "number" in roles else "stats" if "value" in roles else \
            "panels" if p["items"]["max_columns"] == 1 else "columns" if "points" in roles else "cards"
    return "text"


def consistent(kind, p, n, total):
    """Can a pattern with this content honestly be called `kind`? (vision's kind is accepted only if so)"""
    roles = set(p.get("items", {}).get("fields", {}))
    need = {"steps": "number" in roles, "stats": "value" in roles, "table": "table" in p,
            "chart": len(p.get("charts", {})) == 1, "two_charts": len(p.get("charts", {})) == 2,
            "panels": "items" in p, "columns": "items" in p, "cards": "items" in p,
            "cover": n == 1, "closing": n == total, "text": True}
    return need.get(kind, False)


USE_FOR = {
    "cover": lambda p: "opening slide with a title" + (", subtitle" if "subtitle" in p["fields"] else "")
                       + (f" and up to {p['items']['max']} headline items" if "items" in p else ""),
    "closing": lambda p: "final slide",
    "table": lambda p: "a table with a header row",
    "chart": lambda p: "one chart with supporting text",
    "two_charts": lambda p: "two charts side by side",
    "steps": lambda p: f"a numbered sequence of {p['items']['min']}-{p['items']['max']} steps",
    "stats": lambda p: f"{p['items']['min']}-{p['items']['max']} headline numbers, each with a short label",
    "panels": lambda p: f"{p['items']['min']}-{p['items']['max']} stacked labelled paragraphs",
    "columns": lambda p: f"{p['items']['min']}-{p['items']['max']} parallel themes side by side, each with bullet points",
    "cards": lambda p: f"{p['items']['min']}-{p['items']['max']} cards of short text",
    "text": lambda p: "a slide of text",
}


#: A picture thinner than this in either direction is an icon, a logo or a rule - decoration,
#: whatever else is true of it. 0.8 inch: the logos and icons on real templates measure
#: 0.34-0.77 inch, and the content photographs 3-6 inch.
ICON_EMU = 731520


def _decorative(sh, sha, often, W, H):
    """Is this picture part of the template's look, rather than content on an example slide?

    The question matters because a pattern copies its example slide: decoration has to come
    with it, and content must not. Getting it wrong in one direction leaves somebody's
    architecture diagram in every new deck; in the other it strips the logo, the icons and the
    cover photograph and the slide stops looking like the template at all.

    Frequency alone - "on at least half the slides" - was the original test, and it is right
    about a logo repeated throughout. It is wrong about everything that appears once by design.
    On a real commercial template it stripped the cover photograph, the logo, an icon inside a
    circle and a row of tick marks, and the acceptance test's vision check caught each one by
    name. So two more signals, both about what the picture *is* rather than how often it turns
    up:

    * **small** - an icon or a logo. Nothing that reads as content is a third of an inch tall.
    * **bleeds** - it runs off the edge of the slide. Content sits inside the page; artwork
      that runs past the trim is there to be looked through, not read.

    What is still removed: a picture of real size, sitting inside the page, that appears once.
    That is the screenshot or the diagram the rule was written for.
    """
    if sha in often:
        return True
    try:
        x, y, w, h = to_slide(sh._element, int(sh.left or 0), int(sh.top or 0),
                              int(sh.width or 0), int(sh.height or 0))
    except Exception:
        return False
    if min(w, h) < ICON_EMU:
        return True
    return x < 0 or y < 0 or x + w > W or y + h > H


def _pic_sha(sh):
    import hashlib
    try:
        return hashlib.sha256(sh.image.blob).hexdigest()[:16]
    except Exception:
        return None


def draft_kit(path: str, template_name: str, dedupe: bool = True) -> dict:
    prs = Presentation(path)
    # a picture that appears on at least half the slides is decoration (e.g. a logo); others are content
    seen = Counter()
    for slide in prs.slides:
        seen.update({_pic_sha(sh) for sh in leaves(slide.shapes)
                     if sh.shape_type == MSO_SHAPE_TYPE.PICTURE} - {None})
    decoration = {h for h, c in seen.items() if c >= max(2, len(prs.slides) / 2)}
    W, H = prs.slide_width, prs.slide_height
    total = len(prs.slides)
    patterns, notes, pages = {}, {}, []
    for n, slide in enumerate(prs.slides, start=1):
        res, why = slide_pattern(slide, n, total, W, H)
        if why:
            notes[n] = why
        if not res:
            continue
        name, p = res
        # `leaves`, not `slide.shapes`: once slides with groups can be patterns, the icons are
        # inside the groups, and a picture that never reaches this list can never be kept.
        keep = [sh.shape_id for sh in leaves(slide.shapes)
                if sh.shape_type == MSO_SHAPE_TYPE.PICTURE
                and _decorative(sh, _pic_sha(sh), decoration, W, H)]
        if keep:
            p["keep"] = keep
        if "page" in p["fields"]:
            pg = next(sh for sh in slide.shapes if sh.shape_id == p["fields"]["page"]).text_frame.text
            pages.append(pg)
        key = name if name not in patterns else (None if dedupe else f"{name}@{n}")
        if key is None:
            notes.setdefault(n, []).append(f"same kind as an earlier slide ({name}); not offered")
            continue
        patterns[key] = p
    page_format = "{n} / {total}" if any("/" in t for t in pages) else "{n}"
    return {"name": f"auto kit for {template_name}", "template": template_name, "page_format": page_format,
            "chart_style": chart_style(prs), "patterns": patterns, "notes": {str(k): v for k, v in notes.items()},
            "slides_total": total, "draft": True}
