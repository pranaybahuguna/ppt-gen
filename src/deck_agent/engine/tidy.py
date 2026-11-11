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
    """Remove `[F2]`, `(F3, F5)` and `Source: F14` from any strings in a nested structure."""
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
    """
    if isinstance(value, dict):
        return {k: without_citations(v) for k, v in value.items() if k not in CITATION_KEYS}
    if isinstance(value, list):
        return [without_citations(v) for v in value]
    return strip_citations(value) if isinstance(value, str) else value


def clean_plan(plan: dict) -> tuple[dict, int]:
    """-> (the plan with citation markers taken out of the visible text, how many were removed)"""
    found = 0
    for slide in plan.get("slides", []):
        for text in _strings(slide):
            found += len(CITATION.findall(text)) + len(CITATION_LINE.findall(text))
    return strip_citations(plan), found


def _strings(value):
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
    """Is `inner` inside `outer`? Slack is half a centimetre in EMU, for rounding in the template."""
    a, b = outer.get("geom"), inner.get("geom")
    if not a or not b:
        return False
    return (b["x"] >= a["x"] - slack and b["y"] >= a["y"] - slack
            and b["x"] + b["w"] <= a["x"] + a["w"] + slack
            and b["y"] + b["h"] <= a["y"] + a["h"] + slack
            and (a["w"] * a["h"]) > (b["w"] * b["h"]))


def orphan_steps(pattern: dict, example_slide: dict, filled: set[int]) -> list[dict]:
    """Extra `delete_shape` steps for decoration whose every field was left empty.

    `filled` is the set of shape ids the plan actually put something in.
    """
    from .deck import bootstrap  # noqa: F401  (kept local; this module is imported early)

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


def remove_orphans(ops: list[dict], plan: dict, index: dict, kit: dict) -> tuple[list[dict], int]:
    """Add the orphan deletions to a compiled op list. -> (ops, how many were added)"""
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
        for step in orphan_steps(pattern, example, filled):
            if step["target"] not in existing:
                op.setdefault("steps", []).append(step)
                added += 1
    return ops, added


def _filled_ids(plan_slide: dict, pattern: dict) -> set[int]:
    """Which of the pattern's shape ids this slide actually puts something into."""
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
            if not PAGE_TEXT.match(shape.text_frame.text or ""):
                continue
            wanted = fmt.format(n=position, total=total)
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
