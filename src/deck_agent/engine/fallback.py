"""What to offer when no layout fits - relaxed in a named order, never by guessing.

The engine's `options_for` is strict on purpose: it offers a model only the layouts that can
physically hold a slide's content, which is why a deck built from a kit does not overflow. The
cost of that strictness is that it sometimes offers *nothing*, and then the slide fails.

A worked example from the bundled sample template, which is not a contrived one:

    a cover slide with a title and a subtitle and no bullet points fits nothing.

Every pattern in that kit either needs items (`cover` wants 2-4 value/caption pairs, `stats` 2,
`steps` 1, `columns` 2, `panels` 1), or is a table or chart layout, or is `closing` - which is
excluded because `closing` may only be used on the last slide. So a perfectly ordinary title
slide has no home, and without a fallback it either fails or goes straight to generated code.

Generated code should not be the first answer to that. This module relaxes the rules one step
at a time, in an order chosen so that each step gives up something small and says what it gave
up, and so that the result is still a layout from the template rather than something invented:

    1. patterns with no repeated items at all, whose long-text fields can hold the points.
       Gives up: the repeated-item look. A title slide gets a title layout.
    2. position restrictions dropped. Gives up: the convention that a closing layout appears
       last. A `closing` layout used as a cover is a title-and-subtitle slide, which is exactly
       what was wanted.
    3. nothing - and *then* the escape hatch, with the honest note that no layout fitted.

What this never does is pad items with invented text to satisfy a minimum, or truncate content
to satisfy a maximum. Both would change what the deck says in order to make the layout work,
which is the wrong way round.
"""
from __future__ import annotations

from . import bootstrap

#: A field that can hold a sentence rather than a label. Mirrors the engine's own list.
LONG_FIELDS = ("body", "points", "text", "commentary", "side_text", "intro")


def _long_capacity(pattern: dict) -> int:
    """How many points a pattern can hold in its prose fields, with no repeated items.

    Called by: relaxed_options.
    """
    return sum(1 for f in pattern["fields"]
               if f.split("_")[0] in LONG_FIELDS or f.rsplit("_", 1)[0] in LONG_FIELDS)


def relaxed_options(slide: dict, kit: dict, position: int, total: int) -> tuple[list[dict], str | None]:
    """Options for a slide the strict rules rejected. -> (options, what was relaxed)

    Returns `([], None)` when even the relaxed rules find nothing, which is the honest signal
    that this slide needs drawing rather than filling.

    Called by: nodes.create.render_slides, nodes.edit._change_layouts,
        nodes.edit._rebuild_with_layouts; tests/validate_real_model.py.
    """
    bootstrap()
    from controlplane.slide_flow import option

    points = len(slide.get("points") or [])
    rows = len((slide.get("table") or {}).get("rows") or [])
    charts = len(slide.get("charts") or ({"main": slide["chart"]} if slide.get("chart") else {}))

    # A table or a chart is content a prose layout cannot hold, so these relaxations do not
    # apply: pretending otherwise would silently drop the data.
    if rows or charts:
        return [], None

    # Step 1: layouts with no repeated items, if the points fit their prose fields.
    step1 = [option(name, p) for name, p in kit["patterns"].items()
             if "items" not in p and "table" not in p and "charts" not in p
             and points <= _long_capacity(p)
             and not (name.split("@")[0] == "cover" and position != 1)
             and not (name.split("@")[0] == "closing" and position != total)]
    if step1:
        return _sorted(step1, slide), "offered layouts without repeated items"

    # Step 2: the same, with the first/last-slide conventions dropped.
    step2 = [option(name, p) for name, p in kit["patterns"].items()
             if "items" not in p and "table" not in p and "charts" not in p
             and points <= _long_capacity(p)]
    if step2:
        return _sorted(step2, slide), ("offered a layout out of its usual position "
                                       "(a closing or cover layout used elsewhere)")

    return [], None


def _sorted(options: list[dict], slide: dict) -> list[dict]:
    """Fewest unfilled fields first: the closest layout, by the one measure that is objective.

    Called by: relaxed_options.
    """
    given = {k for k, v in slide.items() if v} | {"title", "subtitle"}
    return sorted(options, key=lambda o: len([f for f in o["fields"] if f not in given]))
