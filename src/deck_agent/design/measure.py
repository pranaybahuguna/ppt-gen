"""How much room text needs, measured with the fonts the renderer will use.

Every piece sizes its text here before drawing it, so text that would overflow is made smaller
or wrapped *before* it reaches the slide - overflow is prevented, not detected afterwards.

The fonts are the metric-compatible ones the sandbox renders with (Liberation Sans has Arial's
widths, Carlito has Calibri's), so a measurement here matches what LibreOffice draws, and what
PowerPoint draws with the real fonts.

Called by: design.pieces (every piece with text), design.render (titles).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from ..settings import settings

#: Which metric-compatible file stands in for which font.
STAND_INS = {
    "arial": "LiberationSans", "helvetica": "LiberationSans", "liberation sans": "LiberationSans",
    "calibri": "Carlito", "cambria": "Caladea", "times new roman": "LiberationSerif",
    "courier new": "LiberationMono", "arial narrow": "LiberationSansNarrow",
}
LINE = 1.17          # line height as a multiple of the font size (Liberation Sans, single spacing)
#: Lines are measured against this fraction of the real width. Renderers break lines a little
#: differently (kerning, rounding, how a box's edge is treated); a line measured to fit exactly
#: can wrap when drawn, and then runs into whatever sits below it.
SLACK = 0.96


@lru_cache(maxsize=64)
def _font(family: str, bold: bool, italic: bool) -> ImageFont.FreeTypeFont:
    """The TrueType file for a family and style, loaded at 1000 units per em."""
    stem = STAND_INS.get(family.lower(), "LiberationSans")
    style = ("Bold" if bold else "") + ("Italic" if italic else "")
    path = Path(settings().font_path) / f"{stem}-{style or 'Regular'}.ttf"
    return ImageFont.truetype(str(path), 1000)


def width_in(text: str, pt: float, family: str = "Arial", bold: bool = False,
             italic: bool = False) -> float:
    """Width of one line of `text` at `pt` points, in inches."""
    return _font(family, bold, italic).getlength(text) / 1000 * pt / 72


def wrap(text: str, pt: float, width: float, family: str = "Arial", bold: bool = False,
         italic: bool = False) -> list[str]:
    """`text` broken into lines no wider than `width` inches, at word boundaries.

    A single word wider than the line stays on a line of its own (it will be too wide, and the
    caller's fit check sees that through `fits`).
    """
    lines: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if line and width_in(trial, pt, family, bold, italic) > width * SLACK:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


def height_in(lines: int, pt: float, spacing: float = 1.0) -> float:
    """Height of `lines` lines at `pt` points, in inches."""
    return lines * pt * LINE * spacing / 72


def fits(text: str, pt: float, w: float, h: float, family: str = "Arial", bold: bool = False,
         italic: bool = False, max_lines: int | None = None, spacing: float = 1.0) -> bool:
    """Whether `text` at `pt` fits a w x h inch box, wrapped, in at most `max_lines` lines."""
    lines = wrap(text, pt, w, family, bold, italic)
    if max_lines is not None and len(lines) > max_lines:
        return False
    if any(width_in(l, pt, family, bold, italic) > w * SLACK for l in lines):
        return False
    return height_in(len(lines), pt, spacing) <= h


def fit(text: str, w: float, h: float, max_pt: float, min_pt: float, family: str = "Arial",
        bold: bool = False, italic: bool = False, max_lines: int | None = None,
        spacing: float = 1.0) -> float | None:
    """The largest size between max_pt and min_pt (half-point steps) at which `text` fits.

    None when it does not fit even at min_pt - the caller must shorten the text or give it
    more room; it never gets drawn too small to read.
    """
    pt = max_pt
    while pt >= min_pt:
        if fits(text, pt, w, h, family, bold, italic, max_lines, spacing):
            return pt
        pt -= 0.5
    return None


def words_that_fit(w: float, h: float, pt: float, family: str = "Arial") -> int:
    """Roughly how many words of ordinary prose fit a w x h box at `pt` - a budget to give the
    writer when its text did not fit, instead of a vague 'too long'."""
    sample = "the quick brown fox jumps over the lazy dog while a cat sleeps nearby"
    per_char = width_in(sample, pt, family) / len(sample)
    lines = max(1, int(h / (pt * LINE / 72)))
    return max(1, int((w / per_char) * lines / 6.2 * 0.85))


def fit_all(texts: list[str], w: float, h: float, max_pt: float, min_pt: float,
            **kw) -> float | None:
    """One size for several texts that must match (every KPI value, every card heading)."""
    sizes = [fit(t, w, h, max_pt, min_pt, **kw) for t in texts if t]
    return None if any(s is None for s in sizes) else min(sizes, default=max_pt)
