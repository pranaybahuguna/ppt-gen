"""Two tidying passes over a plan and its ops, for things the checks cannot see.

Both of these were found by looking at rendered slides, which the engine's own working notes
insist on for exactly this reason: overflow and overlap checks answer "does the text fit", and
neither of these is a fitting problem.

**Orphaned decoration.** A template's example slide often has a decorative panel sitting behind
a group of text boxes - a tinted rectangle behind a commentary column, say. The compiler fills
the fields it was given and deletes the text boxes it was not, which is right. But the panel
holds no text of its own, so the leak guard leaves it, and the finished slide has an empty
grey box where the commentary would have been. The rule here is geometric and conservative: a
shape with no text, not kept by the kit, is dropped only when it encloses at least one of the
pattern's field boxes and *every* field box it encloses was left empty. A global decoration -
an accent bar down the side, a rule above the footer - encloses no field box, so it is never
touched.

**Citation markers in visible text.** Asked to use only the facts in a content pack and to cite
them, a model will sometimes write the citation into the words on the slide: "The bot answered
38% of tickets. [F2]". The fact id belongs in the plan's `fact` field, where it can be checked;
on a slide it is a mistake that no amount of layout work will catch. Only bracketed forms are
removed, so ordinary prose is never touched.
"""
from __future__ import annotations

import re

from . import bootstrap

#: `[F2]`, `(F3, F5)` - a citation in brackets after the words.
CITATION = re.compile(r"\s*[\[\(]\s*(F\d+(?:\s*[,;/]\s*F\d+)*)\s*[\]\)]")

#: `Source: F14`, `Sources: F1, F2` - a whole line whose content is only fact ids. Deliberately
#: narrow: the value must be *nothing but* ids, so "Source: pilot report" is untouched. A deck
#: put "Source: F14" on a slide as a bullet of its own, which is how this one was found.
CITATION_LINE = re.compile(
    r"(?:^|[\n\r]|(?<=[.;,]\s))\s*(?:source|sources|ref|refs|reference|references|fact|facts)"
    r"\s*[:\-\u2013]\s*F\d+(?:\s*[,;/& ]+\s*F\d+)*\s*\.?\s*$",
    re.IGNORECASE | re.MULTILINE)


# --------------------------------------------------------------------------- citations

def strip_citations(value):
    """Remove `[F2]`, `(F3, F5)` and `Source: F14` from any strings in a nested structure.

    Called by: clean_plan, without_citations.
    """
    if isinstance(value, str):
        return CITATION_LINE.sub("", CITATION.sub("", value)).strip()
    if isinstance(value, list):
        return [strip_citations(v) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in ("fact", "sources", "pattern") else strip_citations(v))
                for k, v in value.items()}
    return value


#: Keys that hold a citation rather than words. The plan keeps them - that is where a fact id
#: belongs, and where it can be checked. Anything whose job is to *draw* the content must not
#: see them, because it has no way to use one correctly and will put it on the slide.
CITATION_KEYS = ("fact", "facts", "sources", "source", "citation", "citations", "ref", "refs")


def without_citations(value):
    """Content with every citation field removed, for a consumer that would only print them.

    The code path needed this: `strip_citations` removes `[F2]` from visible text but keeps the
    plan's own `fact: "F2"` field, which is correct for the plan and wrong for a model asked to
    draw a slide - it rendered "F2" as a line of text under each heading.

    Called by: nodes.custom_code.custom_code.
    """
    if isinstance(value, dict):
        return {k: without_citations(v) for k, v in value.items() if k not in CITATION_KEYS}
    if isinstance(value, list):
        return [without_citations(v) for v in value]
    return strip_citations(value) if isinstance(value, str) else value


def clean_plan(plan: dict) -> tuple[dict, int]:
    """-> (the plan with citation markers taken out of the visible text, how many were removed)

    Called by: engine.build.build_from_kit.
    """
    found = 0
    for slide in plan.get("slides", []):
        for text in _strings(slide):
            found += len(CITATION.findall(text)) + len(CITATION_LINE.findall(text))
    return strip_citations(plan), found


def _strings(value):
    """Every string in a plan value, skipping the keys that are not slide text (fact, sources,
    pattern).

    Called by: clean_plan, to strip citation markers.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in ("fact", "sources", "pattern"):
                yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


# --------------------------------------------------------------------------- orphaned decoration

def _encloses(outer: dict, inner: dict, slack: int = 45720) -> bool:
    """Is `inner` inside `outer`? Slack is half a centimetre in EMU, for rounding in the template.


    Called by: orphan_steps.
    """
    a, b = outer.get("geom"), inner.get("geom")
    if not a or not b:
        return False
    return (b["x"] >= a["x"] - slack and b["y"] >= a["y"] - slack
            and b["x"] + b["w"] <= a["x"] + a["w"] + slack
            and b["y"] + b["h"] <= a["y"] + a["h"] + slack
            and (a["w"] * a["h"]) > (b["w"] * b["h"]))


def _bbox(geoms):
    """The box around a set of shape geometries, as (x0, y0, x1, y1).

    Called by: unused_item_steps, to find an unused item's area.
    """
    xs = [g["x"] for g in geoms]
    ys = [g["y"] for g in geoms]
    return (min(xs), min(ys),
            max(g["x"] + g["w"] for g in geoms), max(g["y"] + g["h"] for g in geoms))


def _inside(shape_geom, box, slack: int = 45720) -> bool:
    """Whether a shape sits inside a box, with a little slack for rounding.

    Called by: unused_item_steps.
    """
    x0, y0, x1, y1 = box
    g = shape_geom
    return (g["x"] >= x0 - slack and g["y"] >= y0 - slack
            and g["x"] + g["w"] <= x1 + slack and g["y"] + g["h"] <= y1 + slack)


def unused_item_steps(pattern: dict, example_slide: dict, used_items: int,
                      claimed: set[int]) -> list[dict]:
    """Delete decoration that belonged to item slots this slide did not fill.

    A pattern's example slide shows some number of items, and a slide using fewer leaves the
    rest behind. The compiler removes the *item groups* it does not need, but a template's
    decoration is rarely all inside those groups: a tick mark under each card, a rule beneath
    each column, a connector between them. Those are separate shapes that sit in the item's
    area, and they survive - so a slide with three of four items shows three cards and four
    tick marks.

    It is obvious to look at and invisible to every other check, which is how it was found: the
    vision review said "four checkmark markers and underline rules remain with no accompanying
    content". The rule is the same conservative shape as the one above - a shape with no text,
    claimed by nothing, lying inside the area an unused item would have occupied.

    Called by: remove_orphans.
    """
    if "items" not in pattern:
        return []
    groups = pattern["items"]["groups"]
    if used_items >= len(groups):
        return []

    shapes = {int(s["ref"].rsplit("shape:", 1)[1]): s for s in example_slide["shapes"]}
    dropped = [sid for group in groups[used_items:] for sid in group]
    geoms = [shapes[sid]["geom"] for sid in dropped if sid in shapes and shapes[sid].get("geom")]
    if not geoms:
        return []
    box = _bbox(geoms)
    kept_item_ids = {sid for group in groups[:used_items] for sid in group}

    steps = []
    for sid, shape in shapes.items():
        if sid in claimed or sid in kept_item_ids or sid in dropped:
            continue
        if shape.get("kind") == "group" or not shape.get("geom"):
            continue
        if any(p["text"].strip() for p in shape.get("paras", [])):
            continue                       # it has words; the leak guard decides about those
        if _inside(shape["geom"], box):
            steps.append({"op": "delete_shape", "target": f"shape:{sid}"})
    return steps


def orphan_steps(pattern: dict, example_slide: dict, filled: set[int]) -> list[dict]:
    """Extra `delete_shape` steps for decoration whose every field was left empty.

    `filled` is the set of shape ids the plan actually put something in.

    Called by: remove_orphans.
    """
    shapes = {int(s["ref"].rsplit("shape:", 1)[1]): s for s in example_slide["shapes"]}
    field_ids = set(pattern["fields"].values())
    if "items" in pattern:
        field_ids |= {sid for group in pattern["items"]["groups"] for sid in group}
    field_ids |= set(pattern.get("charts", {}).values())
    if "table" in pattern:
        field_ids.add(pattern["table"].get("shape"))
    keep = set(pattern.get("keep", []))

    steps = []
    for sid, shape in shapes.items():
        if sid in field_ids or sid in keep or shape.get("kind") in ("picture", "group"):
            continue
        if any(p["text"].strip() for p in shape.get("paras", [])):
            continue                                  # it has words: the leak guard owns it
        enclosed = [f for f in field_ids if f in shapes and _encloses(shape, shapes[f])]
        if enclosed and not any(f in filled for f in enclosed):
            steps.append({"op": "delete_shape", "target": f"shape:{sid}"})
    return steps


EMU_PER_IN = 914400
COMPANION_MAX = int(0.8 * EMU_PER_IN)   # an icon or a rule, not a panel (same bound as the kit's)
COMPANION_GAP = int(0.6 * EMU_PER_IN)   # how close it must sit to the field it belongs to


def _all_shapes(example_slide: dict) -> dict[int, dict]:
    """Every shape on the example slide by id, groups opened up.

    Called by: companion_steps, nodes.common.design_frame.
    """
    out: dict[int, dict] = {}

    def walk(shapes):
        """Add every shape, and the children of every group, to the result."""
        for sh in shapes:
            try:
                out[int(sh["ref"].rsplit("shape:", 1)[1])] = sh
            except (KeyError, ValueError, IndexError):
                pass
            walk(sh.get("children") or [])

    walk(example_slide["shapes"])
    return out


def _gap(a: dict, b: dict) -> float:
    """Shortest distance between two boxes; 0 when they touch or overlap.

    Called by: companion_steps.
    """
    import math

    dx = max(0, a["x"] - (b["x"] + b["w"]), b["x"] - (a["x"] + a["w"]))
    dy = max(0, a["y"] - (b["y"] + b["h"]), b["y"] - (a["y"] + a["h"]))
    return math.hypot(dx, dy)


def companion_steps(pattern: dict, example_slide: dict, filled: set[int]) -> list[dict]:
    """Delete the icons and rules that belong to a field this slide leaves empty.

    A layout's text boxes come with small furniture: a tick above each point, a rule under it,
    an arrow beside a label. `orphan_steps` only removes decoration that *encloses* an empty
    field, and leaves pictures and kept decoration alone - so a slide that used one of four
    points showed three ticks over nothing. Here a small shape with no words is matched to the
    field it sits nearest, and goes when that field is left empty. Big shapes (panels,
    backgrounds, photos) are never touched, and nor is anything nearer a field that is filled.

    Called by: remove_orphans.
    """
    shapes = _all_shapes(example_slide)
    named = {sid for name, sid in pattern["fields"].items() if name not in ("page", "footer")}
    empty = {sid for sid in named if sid not in filled}
    if not empty:
        return []
    fields = set(pattern["fields"].values())
    if "items" in pattern:
        fields |= {sid for group in pattern["items"]["groups"] for sid in group}
    # The page number is furniture itself; a rule that happens to sit near the slide's foot
    # belongs to the text above it, not to the page number beside it.
    furniture = {sid for name, sid in pattern["fields"].items() if name in ("page", "footer")}
    boxes = {sid: shapes[sid]["geom"] for sid in fields - furniture
             if sid in shapes and shapes[sid].get("geom")}

    steps = []
    for sid, shape in shapes.items():
        geom = shape.get("geom")
        if sid in fields or not geom or shape.get("kind") == "placeholder":
            continue
        if any(p["text"].strip() for p in shape.get("paras", [])):
            continue
        if max(geom["w"], geom["h"]) > COMPANION_MAX and min(geom["w"], geom["h"]) > EMU_PER_IN // 20:
            continue                     # neither small nor a thin rule: part of the design
        nearest = min(boxes, key=lambda f: _gap(geom, boxes[f]), default=None)
        if nearest in empty and _gap(geom, boxes[nearest]) <= COMPANION_GAP:
            steps.append({"op": "delete_shape", "target": f"shape:{sid}"})
    return steps


def remove_orphans(ops: list[dict], plan: dict, index: dict, kit: dict) -> tuple[list[dict], int]:
    """Add the orphan deletions to a compiled op list. -> (ops, how many were added)

    Called by: engine.build.build_from_kit, nodes.edit._change_layouts,
        nodes.edit._insert_slides.
    """
    added = 0
    slides = plan.get("slides", [])
    slide_ops = [o for o in ops if o.get("op") == "add_slide_from_pattern"]
    for plan_slide, op in zip(slides, slide_ops):
        pattern = kit["patterns"].get(plan_slide.get("pattern"))
        if not pattern:
            continue
        example = index["slides"][pattern["slide"] - 1]
        filled = _filled_ids(plan_slide, pattern)
        existing = {s.get("target") for s in op.get("steps", []) if s.get("op") == "delete_shape"}
        claimed = _claimed_ids(pattern)
        extra = (orphan_steps(pattern, example, filled)
                 + companion_steps(pattern, example, filled)
                 + unused_item_steps(pattern, example, len(plan_slide.get("items") or []), claimed))
        for step in extra:
            if step["target"] not in existing:
                existing.add(step["target"])
                op.setdefault("steps", []).append(step)
                added += 1
    return ops, added


def _claimed_ids(pattern: dict) -> set[int]:
    """Every shape id the pattern names for something, whether or not this slide fills it.

    Called by: remove_orphans.
    """
    out = set(pattern["fields"].values()) | set(pattern.get("keep", []))
    out |= set(pattern.get("charts", {}).values())
    if "table" in pattern:
        out.add(pattern["table"].get("shape"))
    return out


def _filled_ids(plan_slide: dict, pattern: dict) -> set[int]:
    """Which of the pattern's shape ids this slide actually puts something into.

    Called by: remove_orphans.
    """
    filled = {sid for name, sid in pattern["fields"].items()
              if plan_slide.get("fields", {}).get(name) not in (None, "", [])
              or name in ("page", "footer")}
    items = plan_slide.get("items") or []
    if "items" in pattern and items:
        groups = pattern["items"]["groups"]
        for i, item in enumerate(items):
            if i >= len(groups):
                break
            for name, position in pattern["items"]["fields"].items():
                if item.get(name) or name == "number":
                    filled.add(groups[i][position])
    for slot, sid in pattern.get("charts", {}).items():
        if (plan_slide.get("charts") or {}).get(slot):
            filled.add(sid)
    if "table" in pattern and plan_slide.get("table"):
        filled.add(pattern["table"].get("shape"))
    return filled


# --------------------------------------------------------------------------- page numbers

PAGE_TEXT = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")


def renumber_pages(src, dst, fmt: str = "{n} / {total}") -> dict:
    """Rewrite every "n / total" footer so it matches the deck as it now stands.

    The compiler fills page numbers while it builds, from the plan - which is right until
    something adds a slide afterwards. When the escape hatch drew a slide, the finished deck
    had seven slides and every footer still read "n / 6". Nobody's checks look at a footer, and
    it is the first thing a reader notices.

    A page box is found by what it says, not by which field the kit called it: a shape whose
    entire text is "<number> / <number>". That is template-agnostic, and it means a slide drawn
    from scratch - which has no page box at all - is simply reported rather than guessed at.

    -> {"renumbered": n, "without_page_number": [slide numbers], "parts": {...}}

    Called by: nodes.create.assemble, nodes.custom_code.custom_code, nodes.edit.edit_apply.
    """
    bootstrap()
    from pptx import Presentation

    from deckops.executor import save_preserving

    prs = Presentation(str(src))
    slides = list(prs.slides)
    total = len(slides)
    renumbered, missing = 0, []
    for position, slide in enumerate(slides, start=1):
        found = False
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            text = shape.text_frame.text or ""
            if PAGE_TEXT.match(text):
                wanted = fmt.format(n=position, total=total)
            elif _is_slide_number(shape) and BARE_NUMBER.match(text):
                # A template that numbers its slides with a bare figure in the slide-number
                # box: the compiler wrote the plan's number into it, which is wrong as soon as
                # a designed slide is inserted in front of it.
                wanted = str(position)
            else:
                continue
            paragraph = shape.text_frame.paragraphs[0]
            if paragraph.runs:
                # Write into the first run and clear the rest, so the run's own formatting -
                # the small grey type a template uses for page numbers - is kept.
                paragraph.runs[0].text = wanted
                for extra in paragraph.runs[1:]:
                    extra.text = ""
            else:
                shape.text_frame.text = wanted
            renumbered += 1
            found = True
            break
        if not found:
            missing.append(position)
    stats = save_preserving(prs, str(src), str(dst))
    return {"renumbered": renumbered, "without_page_number": missing, "parts": stats}


BARE_NUMBER = re.compile(r"^\s*\d+\s*$")


def _is_slide_number(shape) -> bool:
    """Whether a shape is a slide-number placeholder.

    Called by: renumber_pages, add_slide_number.
    """
    try:
        return shape.is_placeholder and int(shape.placeholder_format.type) == 13   # SLIDE_NUMBER
    except (AttributeError, ValueError):
        return False


def add_slide_number(src, position: int, dst) -> bool:
    """Give a drawn slide the page number its layout has a box for. -> whether one was added.

    python-pptx's `add_slide` copies a layout's title and body placeholders but not its date,
    footer or slide-number boxes, so a slide drawn by code arrives with no page number while
    every slide around it has one. The layout says where it goes and how it looks; this puts it
    there, holding the slide's position, which `renumber_pages` keeps right from then on.

    Called by: nodes.custom_code.custom_code.
    """
    bootstrap()
    from pptx import Presentation

    from deckops.executor import save_preserving

    prs = Presentation(str(src))
    slide = prs.slides[position]
    if any(_is_slide_number(sh) for sh in slide.placeholders):
        return False
    source = next((ph for ph in slide.slide_layout.placeholders if _is_slide_number(ph)), None)
    if source is None:
        return False
    slide.shapes.clone_placeholder(source)
    added = next(sh for sh in slide.placeholders if _is_slide_number(sh))
    added.text_frame.text = str(position + 1)
    save_preserving(prs, str(src), str(dst))
    return True
