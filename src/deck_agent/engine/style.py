"""Visual changes: colour, weight, size - deterministically, and byte-preservingly.

The engine's op language is closed, and deliberately so: `set_paragraph_text`, `set_font_size`,
`set_geometry`, `delete_shape`, `add_slide_from_pattern` and a few more. There is no operation
for "make this red", so "change the colour of the title" cannot be expressed as ops at all.

This module adds it the same way `deck.move_slide` adds reordering: a small deterministic pass
on the host, using python-pptx, saved through the engine's `save_preserving` so that every part
it did not touch keeps its original bytes. The model never writes a colour value into XML; it
names a target and a colour, and this code does the rest. That keeps the engine's first rule
intact - the model picks from a menu, code does anything spatial or visual.

Three scopes, because they are genuinely different jobs:

* **a shape or a paragraph** - "make the title on slide 1 dark red"
* **a slide, or the whole deck** - "make all the headings grey"
* **a theme colour** - "use a green accent instead of blue". This one rewrites the theme's
  colour scheme, so everything that *inherits* that colour follows: charts, table banding,
  shape fills, the lot. It is the only honest way to answer a request about a deck's accent
  colour, and it is also the one with a caveat worth stating: a shape whose colour was typed
  in by hand as an explicit RGB value does not inherit from the theme, and will not change.
  `apply_theme_colours` reports how many such shapes it found, so the caller can say so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import PlanInvalid
from . import bootstrap

#: Names a person actually uses, so "dark red" does not have to come back as a hex string.
#: Anything else must arrive as RRGGBB - the model is told so.
NAMED_COLOURS = {
    "black": "000000", "white": "FFFFFF", "red": "C00000", "dark red": "8B0000",
    "bright red": "FF0000", "green": "107C41", "dark green": "0B5A2F", "bright green": "00B050",
    "blue": "1F4E79", "dark blue": "15375C", "light blue": "2E9BD6", "navy": "1F3864",
    "orange": "ED7D31", "amber": "D97706", "yellow": "FFC000", "purple": "7030A0",
    "violet": "6B46C1", "pink": "DB2777", "teal": "0F766E", "grey": "6B7280",
    "gray": "6B7280", "dark grey": "374151", "dark gray": "374151",
    "light grey": "D1D5DB", "light gray": "D1D5DB", "silver": "A6A6A6",
}

HEX = re.compile(r"^#?([0-9A-Fa-f]{6})$")

#: The theme slots a request can name. `accent1` is what most templates use as "the brand colour".
THEME_SLOTS = ("accent1", "accent2", "accent3", "accent4", "accent5", "accent6",
               "dk1", "dk2", "lt1", "lt2", "hlink", "folHlink")


def normalise_colour(value: str | None) -> str | None:
    """"dark red" or "#8B0000" or "8b0000" -> "8B0000". Anything else -> None."""
    if not value:
        return None
    text = str(value).strip()
    match = HEX.match(text)
    if match:
        return match.group(1).upper()
    return NAMED_COLOURS.get(text.lower())


@dataclass
class StyleChange:
    """One visual change, already resolved to a target. Produced by the edit compiler."""

    scope: str                       # "shape" | "slide" | "deck"
    ref: str | None = None           # a shape or paragraph ref, when scope is "shape"
    slide: int | None = None         # 1-based, when scope is "slide"
    text_colour: str | None = None
    fill_colour: str | None = None
    bold: bool | None = None
    italic: bool | None = None
    font_size_pt: float | None = None

    def describe(self) -> str:
        where = (f"shape {self.ref}" if self.scope == "shape"
                 else f"slide {self.slide}" if self.scope == "slide" else "the whole deck")
        what = []
        if self.text_colour:
            what.append(f"text {self.text_colour}")
        if self.fill_colour:
            what.append(f"fill {self.fill_colour}")
        if self.bold is not None:
            what.append("bold" if self.bold else "not bold")
        if self.italic is not None:
            what.append("italic" if self.italic else "not italic")
        if self.font_size_pt:
            what.append(f"{self.font_size_pt}pt")
        return f"{where}: {', '.join(what) or 'nothing'}"

    @property
    def empty(self) -> bool:
        return not any((self.text_colour, self.fill_colour, self.font_size_pt,
                        self.bold is not None, self.italic is not None))


# --------------------------------------------------------------------------- shapes and text

def _target_shapes(prs, change: StyleChange):
    """The shapes one change applies to, with the paragraph index when the ref names one."""
    want_slide_id = want_shape_id = want_para = None
    if change.scope == "shape":
        if not change.ref:
            raise PlanInvalid("a style change on a shape needs a ref")
        parts = change.ref.split("/")
        for part in parts:
            if part.startswith("slide:"):
                want_slide_id = int(part.split(":", 1)[1])
            elif part.startswith("shape:"):
                want_shape_id = int(part.split(":", 1)[1])
            elif part.startswith("para:"):
                want_para = int(part.split(":", 1)[1])

    for position, slide in enumerate(prs.slides, start=1):
        if change.scope == "slide" and position != change.slide:
            continue
        if change.scope == "shape" and want_slide_id is not None and slide.slide_id != want_slide_id:
            continue
        for shape in slide.shapes:
            if change.scope == "shape" and want_shape_id is not None and shape.shape_id != want_shape_id:
                continue
            yield shape, want_para


def _recolour_shape(shape, change: StyleChange, para_index: int | None) -> int:
    """Apply a change to one shape. -> how many runs (or fills) were touched."""
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    touched = 0
    if change.fill_colour and hasattr(shape, "fill"):
        try:
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor.from_string(change.fill_colour)
            touched += 1
        except (AttributeError, ValueError, TypeError):
            pass                               # not a shape that can take a solid fill

    if not getattr(shape, "has_text_frame", False):
        return touched
    paragraphs = shape.text_frame.paragraphs
    if para_index is not None:
        paragraphs = paragraphs[para_index:para_index + 1] if para_index < len(paragraphs) else []
    for paragraph in paragraphs:
        # A paragraph with no runs still has formatting, but nothing to colour; the engine's
        # own text writer always produces runs, so this only skips genuinely empty lines.
        for run in paragraph.runs:
            if change.text_colour:
                run.font.color.rgb = RGBColor.from_string(change.text_colour)
            if change.bold is not None:
                run.font.bold = change.bold
            if change.italic is not None:
                run.font.italic = change.italic
            if change.font_size_pt:
                run.font.size = Pt(change.font_size_pt)
            touched += 1
    return touched


def apply_styles(src: str | Path, changes: list[StyleChange], dst: str | Path) -> dict:
    """Apply visual changes and save byte-preservingly. -> a report for the changelog."""
    bootstrap()
    from pptx import Presentation

    from deckops.executor import save_preserving

    real = [c for c in changes if not c.empty]
    if not real:
        raise PlanInvalid("none of those style changes said what to change")

    prs = Presentation(str(src))
    applied = []
    for change in real:
        touched = 0
        for shape, para_index in _target_shapes(prs, change):
            touched += _recolour_shape(shape, change, para_index)
        applied.append({"change": change.describe(), "runs_touched": touched})
        if touched == 0:
            applied[-1]["note"] = "nothing on the deck matched this"
    stats = save_preserving(prs, str(src), str(dst))
    return {"applied": applied, "parts": stats}


# --------------------------------------------------------------------------- theme colours

def apply_theme_colours(src: str | Path, slots: dict[str, str], dst: str | Path) -> dict:
    """Rewrite the theme's colour scheme, so everything that inherits those colours follows.

    This is what "use a green accent instead of blue" has to mean. A shape whose colour was
    typed in as an explicit RGB value does not inherit, so it will not move; the count of those
    is returned, because a caller that says "done" when half the deck did not change is lying.
    """
    bootstrap()
    from lxml import etree
    from pptx import Presentation
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    from deckops.executor import save_preserving

    wanted = {}
    for slot, value in slots.items():
        if slot not in THEME_SLOTS:
            raise PlanInvalid(f"{slot!r} is not a theme colour; use one of {list(THEME_SLOTS)}")
        colour = normalise_colour(value)
        if not colour:
            raise PlanInvalid(f"{value!r} is not a colour I can use; give RRGGBB or a common name")
        wanted[slot] = colour

    prs = Presentation(str(src))
    theme_part = prs.slide_master.part.part_related_by(RT.THEME)
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    root = etree.fromstring(theme_part.blob)
    scheme = root.find(".//a:clrScheme", ns)
    if scheme is None:
        raise PlanInvalid("this deck's theme has no colour scheme to change")

    changed, before = {}, {}
    for slot, colour in wanted.items():
        node = scheme.find(f"a:{slot}", ns)
        if node is None:
            continue
        srgb = node.find("a:srgbClr", ns)
        if srgb is None:                       # a system colour: replace it with an explicit one
            sysclr = node.find("a:sysClr", ns)
            if sysclr is not None:
                node.remove(sysclr)
            srgb = etree.SubElement(node, "{%s}srgbClr" % ns["a"])
        before[slot] = srgb.get("val")
        srgb.set("val", colour)
        changed[slot] = colour

    if not changed:
        raise PlanInvalid("none of those theme colours exist in this deck")
    theme_part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                                      standalone=True)

    # How much of the deck will actually follow? Count shapes pinned to an explicit RGB.
    hardcoded = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            xml = shape._element.xml
            for old in before.values():
                if old and f'srgbClr val="{old}"' in xml:
                    hardcoded += 1
                    break

    stats = save_preserving(prs, str(src), str(dst))
    return {"theme": changed, "replaced": before, "shapes_pinned_to_the_old_colour": hardcoded,
            "parts": stats}
