"""A template's design profile: everything a renderer needs to draw slides that belong to it.

No model, no rendering - this reads the file and takes a few seconds. It answers:

* which of the template's own layouts to build slides on (the one its slides use most), so the
  title box, footer, page number and decoration come from the template itself;
* where the title sits and how it looks, and the safe area below it for content;
* the brand colours. A template's *theme* is often the Office default while the real colours
  are painted onto its slides (this one: the theme says blue, the slides are navy and green),
  so they are counted from the slides, grouped by hue, and given roles;
* the fonts and text sizes the slides actually use.

Called by: design.render.render_deck; the design spike script.
"""
from __future__ import annotations

import colorsys
import re
from collections import Counter
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER

EMU = 914400
_FILL = re.compile(r'<a:solidFill><a:srgbClr val="([0-9A-Fa-f]{6})"')
_ANY = re.compile(r'srgbClr val="([0-9A-Fa-f]{6})"')


def _hsl(hex6: str) -> tuple[float, float, float]:
    """(hue 0-360, lightness 0-1, saturation 0-1) of an RRGGBB colour."""
    r, g, b = (int(hex6[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, l, s


def _mix(hex6: str, other: str, t: float) -> str:
    """`hex6` moved a fraction `t` of the way towards `other`."""
    a = [int(hex6[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(other[i:i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


def brand_colours(prs) -> dict[str, str]:
    """The template's colours by role, counted from what its slides actually paint.

    Saturated colours are grouped into hue families; the family used most for fills is one
    brand colour, the next a second. The darker family becomes `primary` (it carries text and
    labels), the other `accent`. Each gets a dark and a light shade from its own family when the
    template has one, mixed otherwise.

    Called by: extract_profile.
    """
    fills, every = Counter(), Counter()
    for slide in prs.slides:
        xml = etree.tostring(slide._element).decode()
        fills.update(c.upper() for c in _FILL.findall(xml))
        every.update(c.upper() for c in _ANY.findall(xml))
    families: dict[int, Counter] = {}
    for c, n in (fills + every).items():
        h, l, s = _hsl(c)
        if s < 0.18 or l < 0.08 or l > 0.92:
            continue                                   # greys, black, white: not brand colours
        families.setdefault(int(h // 30) % 12, Counter())[c] += n
    ranked = sorted(families.values(), key=lambda f: -sum(f.values()))[:2]
    if not ranked:
        ranked = [Counter({"1F4E79": 1}), Counter({"2E8B57": 1})]
    if len(ranked) == 1:
        ranked.append(Counter({_mix(ranked[0].most_common(1)[0][0], "000000", 0.35): 1}))

    def shades(fam: Counter) -> tuple[str, str, str]:
        base = fam.most_common(1)[0][0]
        by_l = sorted(fam, key=lambda c: _hsl(c)[1])
        dark = by_l[0] if _hsl(by_l[0])[1] < _hsl(base)[1] - 0.06 else _mix(base, "000000", 0.3)
        light = by_l[-1] if _hsl(by_l[-1])[1] > _hsl(base)[1] + 0.1 else _mix(base, "FFFFFF", 0.6)
        return base, dark, light

    a, b = (shades(f) for f in ranked)
    primary, accent = (a, b) if _hsl(a[0])[1] <= _hsl(b[0])[1] else (b, a)
    return {
        "primary": primary[0], "primary_dark": primary[1], "primary_light": _mix(primary[0], "FFFFFF", 0.85),
        "accent": accent[0], "accent_dark": accent[1], "accent_light": _mix(accent[0], "FFFFFF", 0.8),
        "text": "262626", "muted": "595959", "line": "D9D9D9", "panel": "F2F2F2", "card": "FFFFFF",
        "alert": "C8463D",
    }


def _content_layout(prs):
    """The layout most of the template's slides are built on, if it has a title box."""
    used = Counter(s.slide_layout.name for s in prs.slides)
    layouts = {l.name: l for l in prs.slide_layouts}
    for name, _ in used.most_common():
        lay = layouts.get(name)
        if lay is not None and any(ph.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
                                   for ph in lay.placeholders):
            return lay
    return next(l for l in prs.slide_layouts
                if any(ph.placeholder_format.type == PP_PLACEHOLDER.TITLE for ph in l.placeholders))


def _fonts_and_sizes(prs) -> tuple[str, float, float]:
    """(the font most text uses, the commonest body size, the next smaller one), by characters."""
    fonts, sizes = Counter(), Counter()
    for slide in prs.slides:
        for sh in slide.shapes:
            if not sh.has_text_frame or sh.is_placeholder:
                continue
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    n = len(r.text.strip())
                    if r.font.name:
                        fonts[r.font.name] += n
                    if r.font.size:
                        sizes[r.font.size.pt] += n
    font = fonts.most_common(1)[0][0] if fonts else "Arial"
    body = sizes.most_common(1)[0][0] if sizes else 14.0
    smaller = [s for s, _ in sizes.most_common() if s < body]
    return font, body, (smaller[0] if smaller else body - 2)


def extract_profile(template: str | Path) -> dict:
    """Template in, design profile out (a JSON-able dict, all lengths in inches).

    Called by: design.render.render_deck; the design spike script.
    """
    prs = Presentation(str(template))
    W, H = prs.slide_width / EMU, prs.slide_height / EMU
    layout = _content_layout(prs)
    title = next(ph for ph in layout.placeholders
                 if ph.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE))
    tx, ty, tw, th = (v / EMU for v in (title.left, title.top, title.width, title.height))

    # The safe area: under the title, above whatever the layout puts along the bottom (footer,
    # page number, rules), with the title's left margin on both sides.
    bottoms = [sh.top / EMU for sh in layout.shapes
               if sh.top / EMU > H * 0.8 and sh.height / EMU < H * 0.2]
    floor = min(bottoms) if bottoms else H - 0.6
    font, body_pt, small_pt = _fonts_and_sizes(prs)
    # For full-bleed slides (cover, section): a layout with no title box and the least on it.
    untitled = [l for l in prs.slide_layouts
                if not any(ph.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
                           for ph in l.placeholders)]
    blank = min(untitled, key=lambda l: (len([s for s in l.shapes if not s.is_placeholder]),
                                         "blank" not in l.name.lower()), default=None)
    return {
        "template": Path(template).name,
        "slide": {"w": round(W, 3), "h": round(H, 3)},
        "layout": layout.name,
        "blank_layout": blank.name if blank is not None else None,
        "title": {"box": [round(tx, 3), round(ty, 3), round(tw, 3), round(th, 3)],
                  "font": font, "bold": True, "colour": "262626", "max_pt": 30, "min_pt": 22},
        "area": [round(tx, 3), round(ty + th + 0.08, 3), round(W - 2 * tx, 3),
                 round(floor - 0.14 - (ty + th + 0.08), 3)],
        "font": font, "body_pt": body_pt, "small_pt": small_pt,
        "colours": brand_colours(prs),
    }
