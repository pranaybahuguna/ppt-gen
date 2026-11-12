"""Acceptance test for a style kit: replaces human review of automatically drafted kits.

For every pattern, build two test slides with placeholder content (fewest items + short text,
and most items + text as long as the example's), run the normal checks, and optionally ask a
vision model whether each test slide keeps the example's design. A pattern that fails anything
is removed from the kit, so a wrong automatic label shrinks the menu instead of breaking decks.
"""
from __future__ import annotations

from deckops.patterns import _shapes_by_id

WORDS = ("sample text used to test how this layout holds content of a realistic length "
         "without overflowing or overlapping other parts of the slide").split()
TEST_DATA = {"test_data": {"source": "acceptance test", "columns": ["Category", "Value"],
                           "rows": [["A", 3], ["B", 5], ["C", 2], ["D", 4]]}}


def dummy(n_chars: int) -> str:
    """Placeholder text of *at most* `n_chars`, as close to it as whole words allow.

    "At most" is the whole point. The acceptance test asks one question - does this pattern
    hold content like its example's? - and then answers it with text of the example's length.
    Overshooting makes the test ask something harder than it claims to, and the error is worst
    where it matters most: a 12-character title came back as 16, a third longer, and the layout
    was dropped because that tipped a one-line heading box onto a second line. Short strings
    are exactly where templates are tightest.

    The capacity measurement still asks for more on purpose, by passing `stretch`; that stays
    deliberate rather than accidental.
    """
    out: list[str] = []
    for i in range(400):
        word = WORDS[i % len(WORDS)]
        if out and len(" ".join([*out, word])) > n_chars:
            break
        out.append(word)
    return (" ".join(out)[:max(1, n_chars)]).capitalize()


def _example_text(shape: dict) -> list[str]:
    """The example slide's non-empty paragraphs for a shape (a 12-character stand-in if none)."""
    return [p["text"] for p in shape.get("paras", []) if p["text"].strip()] or ["x" * 12]


# Two separate questions:
#  1. does the pattern hold content like its example?  -> test text as long as the example's text
#  2. does each label make sense for its box?          -> a field meant for longer text (body, points,
#     text, commentary, intro) must sit in a box that holds at least two lines at its font size.
# (2) is physical, so it catches a field pointed at the wrong box even when (1) can't.
LONG_ROLES = {"body", "points", "text", "commentary", "intro"}


def _value(shape, full, field="", stretch=1.0):
    """Placeholder text for a test slide: 'Sample' for the minimal variant, otherwise text as long
    as the example's (times `stretch`).
    """
    paras = _example_text(shape)
    if not full:
        return "Sample"
    vals = [dummy(max(4, int(len(p) * stretch))) for p in paras]
    return vals if len(vals) > 1 else vals[0]


def box_lines(shape) -> float:
    """How many lines of text fit in the box at its own font size."""
    size = shape.get("font_pt") or 12
    return (shape["geom"]["h"] / 12700) / (size * 1.2)


def role_problems(pat: dict, shapes: dict) -> list[str]:
    """Labels that cannot be right physically: a field meant for longer text whose box holds
    fewer than two lines at its font size.

    Called by: the agent's engine.kitbuild (accept, light_kit), before a pattern is test-built.
    """
    out = []
    named = list(pat["fields"].items())
    if "items" in pat:
        g0 = pat["items"]["groups"][0]
        named += [(f, g0[pos]) for f, pos in pat["items"]["fields"].items()]
    for field, sid in named:
        if field.split("_")[-1] in LONG_ROLES and sid in shapes and "paras" in shapes[sid]:
            lines = box_lines(shapes[sid])
            if lines < 1.8:
                out.append(f"label: field {field!r} is meant for longer text but its box holds {lines:.1f} line(s)")
    return out


def test_slides(name: str, pat: dict, index: dict, counts=None, stretch=1.0) -> list[dict]:
    """Test slides for one pattern. Default: a minimal and a full variant. With counts=[n, ...]:
    one full-text slide per item count (used to measure how many items really fit)."""
    shapes = _shapes_by_id(index["slides"][pat["slide"] - 1])
    slides = []
    for full, count in ([(False, None), (True, None)] if counts is None else [(True, c) for c in counts]):
        s = {"pattern": name, "fields": {f: _value(shapes[sid], full, stretch=stretch) for f, sid in pat["fields"].items()
                                         if f not in ("page", "footer")}}
        if "items" in pat:
            it = pat["items"]
            n = count or (len(it["groups"]) if full else max(1, it.get("min", 1)))
            g0 = it["groups"][0]
            s["items"] = [{f: _value(shapes[g0[pos]], full, stretch=stretch) for f, pos in it["fields"].items()
                           if f != "number"}
                          for _ in range(n)]
        if "charts" in pat:
            s["charts"] = {slot: {"type": "column", "dataset": "test_data", "category": "Category", "series": ["Value"]}
                           for slot in pat["charts"]}
        if "table" in pat:
            ncols = 1 + max(c["rc"][1] for c in shapes[pat["table"]["shape"]]["cells"])
            rows = pat["table"].get("max_rows", 9) if full else 1
            s["table"] = {"header": [f"Column {i + 1}" for i in range(ncols)],
                          "rows": [[dummy(30) if full else "Sample"] * ncols for _ in range(rows)]}
        slides.append(s)
    return slides


CAPACITY_CAP = 16          # never offer more than this many items on one slide
HEADROOM = 1.15            # measure with text 15% longer than the example's


