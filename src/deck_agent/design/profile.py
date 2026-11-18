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
        # The base is a colour that can carry text and fills: not a pale tint used for rules
        # and backgrounds (a slate family's E2E8F0), when the family has anything deeper.
        deep = Counter({c: n for c, n in fam.items() if _hsl(c)[1] <= 0.8})
        base = (deep or fam).most_common(1)[0][0]
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


#: The extra hues of the extended palette, and what each is for in a spec ("tone": "teal").
EXTRA_HUES = {"teal": 176, "blue": 208, "amber": 38, "plum": 312}


def _from_hsl(h: float, l: float, s: float) -> str:
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "".join(f"{round(v * 255):02X}" for v in (r, g, b))


def extended_palette(colours: dict) -> dict:
    """More colour that still belongs to the brand: four extra hues at the brand's own depth.

    The brand colours set the depth: the new hues take the average lightness of the primary and
    the accent, and a saturation a little above theirs (muted brands stay muted, just less
    monotone). Amber is the exception - a status colour has to read as amber - so it gets the
    saturation it needs. Each new colour comes with a light tint for panels and fills.
    Called by: with_palette.
    """
    _, lp, sp = _hsl(colours["primary"])
    _, la, sa = _hsl(colours["accent"])
    light = min(0.5, max(0.38, (lp + la) / 2))
    sat = min(0.62, max(0.42, (sp + sa) / 2 + 0.12))
    out = {}
    for name, hue in EXTRA_HUES.items():
        out[name] = _from_hsl(hue, 0.5 if name == "amber" else light, 0.78 if name == "amber" else sat)
        out[f"{name}_light"] = _mix(out[name], "FFFFFF", 0.82)
    return out


def with_palette(P: dict, palette: str | None) -> dict:
    """The profile in one of its two palettes. "brand" (the default): the template's own colours
    only. "extended": the brand plus teal, blue, amber and plum, and the pieces' filled styles.

    Called by: design.nodes (create), revise.nodes (edit), design.plan.
    """
    if palette != "extended":
        return {**P, "palette": "brand"}
    return {**P, "palette": "extended", "colours": {**P["colours"], **extended_palette(P["colours"])}}


_MORE_COLOUR = re.compile(r"\b(more colou?r\w*|colou?rful|bolder|vibrant|bright\w*|"
                          r"(bet+e?r|betrer|bettr|richer|nicer|stronger|more) colou?rs?|"
                          r"extend\w* (the )?colou?r\w*|colou?r palette|more pop)\b", re.I)
_BRAND_ONLY = re.compile(r"\b(template'?s? colou?rs|brand colou?rs only|less colou?r\w*|plain(er)? colou?rs?|"
                         r"back to the (template|brand)\w*)\b", re.I)


def palette_asked(request: str, current: str | None = None) -> str:
    """Which palette a request asks for: "extended" for more colour, "brand" to go back to the
    template's own colours, otherwise whatever the deck already uses (default "brand").
    Called by: design.nodes.storyline, revise.nodes.split.
    """
    if _BRAND_ONLY.search(request or ""):
        return "brand"
    if _MORE_COLOUR.search(request or ""):
        return "extended"
    return current or "brand"


def _content_layout(prs):
    """The layout most of the template's slides are built on, if it has a title box."""
    used = Counter(s.slide_layout.name for s in prs.slides)
    layouts = {l.name: l for l in prs.slide_layouts}
    for name, _ in used.most_common():
        lay = layouts.get(name)
        if lay is not None and any(ph.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
                                   for ph in lay.placeholders):
            return lay
    titled = next((l for l in prs.slide_layouts
                   if any(ph.placeholder_format.type == PP_PLACEHOLDER.TITLE for ph in l.placeholders)), None)
    if titled is not None:
        return titled
    # No title box anywhere: a deck that draws everything. Its slides' own layout.
    return layouts.get(used.most_common(1)[0][0]) if used else prs.slide_layouts[0]


def _title_placeholder_look(prs) -> dict:
    """How the template's own slides style their titles (colour, font, size), most common first;
    an empty dict when no slide has a title box to learn from."""
    from .chrome import look

    looks = [look(s.shapes.title) for s in prs.slides
             if s.shapes.title is not None and s.shapes.title.text_frame.text.strip()]
    out = {}
    for key in ("colour", "font", "pt"):
        vals = Counter(lk[key] for lk in looks if lk[key])
        if vals:
            out[key] = vals.most_common(1)[0][0]
    return out


def _cover_layout(prs) -> str | None:
    """The template's own cover layout (a centred title box): the first slide's, if it has one."""
    def is_cover(l):
        return any(ph.placeholder_format.type == PP_PLACEHOLDER.CENTER_TITLE for ph in l.placeholders)
    if len(prs.slides) and is_cover(prs.slides[0].slide_layout):
        return prs.slides[0].slide_layout.name
    return next((l.name for l in prs.slide_layouts if is_cover(l)), None)


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
    from . import chrome

    prs = Presentation(str(template))
    W, H = prs.slide_width / EMU, prs.slide_height / EMU
    # A deck whose slides mostly have no title box draws its header itself: learn it from them.
    boxed = sum(1 for s in prs.slides if s.shapes.title is not None)
    drawn = chrome.learn(prs) if boxed * 2 < len(prs.slides) else None
    if drawn is not None:
        return _drawn_profile(template, prs, drawn)
    layout = _content_layout(prs)
    title = next((ph for ph in layout.placeholders
                  if ph.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)), None)
    if title is not None:
        tx, ty, tw, th = (v / EMU for v in (title.left, title.top, title.width, title.height))
    else:                                    # nothing to learn from: a standard header position
        tx, ty, tw, th = 0.5, 0.35, W - 1.0, 0.9
    tlook = _title_placeholder_look(prs)

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
        "cover_layout": _cover_layout(prs),
        # The template's own title look when its slides show one; otherwise the title keeps
        # whatever its layout gives it (colour None: not overridden).
        "title": {"box": [round(tx, 3), round(ty, 3), round(tw, 3), round(th, 3)],
                  "font": tlook.get("font") or font, "bold": True, "colour": tlook.get("colour"),
                  "max_pt": min(tlook.get("pt") or 30, 36), "min_pt": 22},
        "area": [round(tx, 3), round(ty + th + 0.08, 3), round(W - 2 * tx, 3),
                 round(floor - 0.14 - (ty + th + 0.08), 3)],
        "font": font, "body_pt": body_pt, "small_pt": small_pt,
        "title_rule": True,                  # a thin accent line under every content slide's title
        "colours": brand_colours(prs),
    }


def _drawn_profile(template, prs, drawn: dict) -> dict:
    """The profile of a deck that draws its own header, footer and cover (see design.chrome)."""
    W, H = prs.slide_width / EMU, prs.slide_height / EMU
    used = Counter(s.slide_layout.name for s in prs.slides).most_common(1)[0][0]
    layout = next(l for l in prs.slide_layouts if l.name == used)   # the one its slides are drawn on
    font, body_pt, small_pt = _fonts_and_sizes(prs)
    t = drawn["title"]
    pt = t.get("pt") or 28
    return {
        "template": Path(template).name,
        "slide": {"w": round(W, 3), "h": round(H, 3)},
        "layout": layout.name,
        "blank_layout": layout.name,
        "cover_layout": None,
        "title": {"box": t["box"], "font": t.get("font") or font, "bold": t.get("bold") is not False,
                  "colour": t.get("colour"), "max_pt": pt, "min_pt": max(18, pt - 6)},
        "area": drawn["area"],
        "font": font, "body_pt": body_pt, "small_pt": small_pt,
        "title_rule": False,                 # the deck has its own header decoration
        "colours": brand_colours(prs),
        "chrome": drawn,
    }
