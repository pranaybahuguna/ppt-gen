"""Turn an edit instruction's actions into the engine's ops.

The model produces a small, typed list of actions - `set_text`, `delete_slide`,
`delete_shape`, `relayout_cards`, `insert_slide` - and this module compiles them. It is pure:
it reads the probed index and returns ops; it never opens a file and never calls a model.

Two of the five need more than a compile:

* `insert_slide` cannot be expressed as an op at all, because a slide copied from an example
  slide is always appended. It is returned separately, for the edit node to build in a second
  pass and then move into place.
* `relayout_cards` is handed to the engine's own `compile_card_grid`, which knows how to merge
  numbered cards and re-lay out the grid while keeping the template's gutters and rows.

Every text change carries the fingerprint of the text it replaces, so if the deck moved under
us between planning and applying, the whole batch is refused rather than half-applied.
"""
from __future__ import annotations

from ..errors import PlanInvalid
from . import bootstrap
from .deck import EXPECT_FIELD

ACTIONS = {"set_text", "delete_slide", "delete_shape", "relayout_cards", "insert_slide",
           "set_style", "set_theme_colour", "change_layout"}


def deck_view(index: dict, slide_scope: set[int] | None = None) -> list[dict]:
    """The deck as the edit model sees it: refs and text, no geometry, no ids, no XML.

    Slides that carry a numbered card grid are flagged, because that is the one structural
    edit the engine can do today and the model needs to know where it is available.

    Called by: nodes.edit.edit_plan; tests/validate_real_model.py.
    """
    bootstrap()
    from deckops.compiler import detect_cards

    out = []
    for i, slide in enumerate(index["slides"]):
        if slide_scope is not None and i not in slide_scope:
            continue
        boxes = []
        for shape in slide["shapes"]:
            paras = [{"ref": p["ref"], "text": p["text"]}
                     for p in shape.get("paras", []) if p["text"].strip()]
            if paras:
                boxes.append({"ref": shape["ref"], "paragraphs": paras})
        entry = {"slide": i + 1, "boxes": boxes}
        cards = detect_cards(slide)
        if cards:
            entry["cards"] = [{"n": c["n"],
                               "heading": "".join(p["text"] for p in c["heading"].get("paras", [])),
                               "body": "".join(p["text"] for p in c["body"].get("paras", []))}
                              for c in cards]
        out.append(entry)
    return out


def compile_edit(actions: list[dict], index: dict) -> tuple[list[dict], list[dict], dict, list[str]]:
    """-> (ops, slides to insert afterwards, visual changes, problems worth telling the model)

    Three kinds of result, because the engine takes them through three different doors: typed
    ops go to the executor, inserted slides need a second pass that copies and moves a slide,
    and visual changes have no op at all (see `engine/style.py`) so they are a deterministic
    pass of their own.

    Called by: nodes.edit.edit_apply; tests/validate_real_model.py.
    """
    bootstrap()
    from deckops.compiler import CompileError, compile_card_grid

    shas = {p["ref"]: p["sha"] for s in index["slides"]
            for sh in s["shapes"] for p in sh.get("paras", [])}
    tagged = {p["ref"]: p["tagged"] for s in index["slides"]
              for sh in s["shapes"] for p in sh.get("paras", [])}
    shape_refs = {sh["ref"] for s in index["slides"] for sh in s["shapes"]}
    slide_refs = {i + 1: s["ref"] for i, s in enumerate(index["slides"])}

    ops: list[dict] = []
    inserts: list[dict] = []
    styles: list = []
    theme: dict = {}
    relayouts: list[dict] = []
    problems: list[str] = []

    for a in actions:
        kind = a.get("action")
        if kind not in ACTIONS:
            problems.append(f"{kind!r} is not an action I can do; use one of {sorted(ACTIONS)}")
            continue

        if kind == "set_text":
            ref, text = a.get("ref"), a.get("text")
            if ref not in shas:
                problems.append(f"there is no text at {ref!r} in this deck")
                continue
            if text is None:
                problems.append(f"set_text at {ref!r} has no text")
                continue
            value = f"<r0>{_escape(text)}</r0>" if "<r0>" in (tagged.get(ref) or "") else _escape(text)
            if value == tagged.get(ref):
                continue                              # nothing to do; do not spend an op on it
            ops.append({"op": "set_paragraph_text", "target": ref,
                        "expect": {EXPECT_FIELD: shas[ref]}, "value": value})

        elif kind == "delete_slide":
            n = a.get("slide")
            if n not in slide_refs:
                problems.append(f"this deck has no slide {n}")
                continue
            ops.append({"op": "delete_slide", "target": slide_refs[n]})

        elif kind == "delete_shape":
            ref = a.get("ref")
            if ref not in shape_refs:
                problems.append(f"there is no shape at {ref!r} in this deck")
                continue
            ops.append({"op": "delete_shape", "target": ref})

        elif kind == "relayout_cards":
            try:
                ops += compile_card_grid({"slide_number": a.get("slide"),
                                          "columns": a.get("columns", 3),
                                          "cards": a.get("cards", [])}, index)
            except CompileError as e:
                problems += list(e.args[0])
            except KeyError as e:
                problems.append(f"relayout_cards on slide {a.get('slide')}: missing {e}")

        elif kind == "change_layout":
            n = a.get("slide")
            if n not in slide_refs:
                problems.append(f"this deck has no slide {n}")
                continue
            relayouts.append({"slide": n, "why": a.get("why")})

        elif kind == "set_style":
            from .style import StyleChange, normalise_colour

            scope = a.get("scope") or ("shape" if a.get("ref") else "slide" if a.get("slide") else "deck")
            if scope == "shape" and a.get("ref") not in shape_refs and a.get("ref") not in shas:
                problems.append(f"there is nothing at {a.get('ref')!r} to restyle")
                continue
            if scope == "slide" and a.get("slide") not in slide_refs:
                problems.append(f"this deck has no slide {a.get('slide')}")
                continue
            bad = [c for c in (a.get("text_colour"), a.get("fill_colour"))
                   if c and not normalise_colour(c)]
            if bad:
                problems.append(f"{bad[0]!r} is not a colour I can use; give RRGGBB or a common name")
                continue
            change = StyleChange(
                scope=scope, ref=a.get("ref"), slide=a.get("slide"),
                text_colour=normalise_colour(a.get("text_colour")),
                fill_colour=normalise_colour(a.get("fill_colour")),
                bold=a.get("bold"), italic=a.get("italic"),
                font_size_pt=a.get("font_size_pt"))
            if change.empty:
                problems.append("a set_style action has to say what to change")
                continue
            styles.append(change)

        elif kind == "set_theme_colour":
            from .style import THEME_SLOTS, normalise_colour

            slot, colour = a.get("slot"), normalise_colour(a.get("colour"))
            if slot not in THEME_SLOTS:
                problems.append(f"{slot!r} is not a theme colour; use one of {list(THEME_SLOTS)}")
            elif not colour:
                problems.append(f"{a.get('colour')!r} is not a colour I can use")
            else:
                theme[slot] = colour

        elif kind == "insert_slide":
            content = a.get("content") or {}
            if not content.get("title") and not content.get("points"):
                problems.append("insert_slide needs content with at least a title or some points")
                continue
            # `or` is wrong here: "after": 0 means *before slide 1*, and `0 or 7` is 7, which
            # sent a title slide to the end of the deck. Only a missing value means "at the end".
            after = a.get("after")
            after = len(index["slides"]) if after is None else int(after)
            inserts.append({"after": max(0, min(after, len(index["slides"]))), "content": content})

    if problems and not ops and not inserts and not styles and not theme and not relayouts:
        raise PlanInvalid("none of these edits could be applied", problems)
    return ops, inserts, {"styles": styles, "theme": theme, "relayouts": relayouts}, problems


def _escape(text: str) -> str:
    """Escape text for the engine's tagged-run text format (<, >, &).

    Called by: compile_edit, for every set_text action.
    """
    import html

    return html.escape(text, quote=False)


# --------------------------------------------------------------------------- changing a layout

#: How a *plan* slide's named fields map back to the *outline* shape that `options_for` and
#: stage 2 expect. Going backwards like this is what lets a finished slide be re-placed into a
#: different layout without touching a word of it.
ITEM_HEADING = ("heading", "label", "number")
ITEM_TEXT = ("body", "text", "points", "caption", "detail_1")


def content_from_plan_slide(plan_slide: dict, kind: str | None = None) -> dict:
    """A built slide's plan entry -> the content shape stage 2 takes.

    Nothing is invented and nothing is dropped: the same words come back out, arranged the way
    the layout chooser understands them. A field the new layout has no room for is simply not
    offered by `options_for`, which is how "change the layout" stays honest - it can only move
    the slide to a layout that genuinely holds what is already there.

    Called by: nodes.edit._change_layouts, nodes.edit._rebuild_with_layouts.
    """
    fields = plan_slide.get("fields", {}) or {}
    content: dict = {"kind": kind or _guess_kind(plan_slide)}
    for name in ("title", "subtitle", "intro", "takeaway"):
        if fields.get(name):
            content[name] = fields[name]

    points = []
    for item in plan_slide.get("items") or []:
        point: dict = {}
        for key in ITEM_HEADING:
            if item.get(key) and "heading" not in point:
                point["heading"] = item[key]
        for key in ITEM_TEXT:
            if item.get(key) and "text" not in point:
                value = item[key]
                point["text"] = " ".join(value) if isinstance(value, list) else value
        if item.get("value"):
            point["value"] = item["value"]
        if point:
            points.append(point)

    # A layout with no repeated items keeps its prose in long fields instead. Those are content
    # too, so they come back as points rather than being lost on the way out.
    if not points:
        for name, value in fields.items():
            root = name.split("_")[0]
            if root in ("body", "points", "text", "commentary", "side") and value:
                points.append({"text": " ".join(value) if isinstance(value, list) else value})
    if points:
        content["points"] = points
    if plan_slide.get("table"):
        content["table"] = plan_slide["table"]
    if plan_slide.get("charts"):
        content["charts"] = plan_slide["charts"]
    return content


def _guess_kind(plan_slide: dict) -> str:
    """A hint for sorting the options, taken from what the slide actually holds.

    Called by: content_from_plan_slide.
    """
    if plan_slide.get("table"):
        return "table"
    if plan_slide.get("charts"):
        return "chart"
    items = plan_slide.get("items") or []
    if items and all(i.get("value") for i in items):
        return "numbers"
    if len(items) >= 4:
        return "sequence"
    if items:
        return "list"
    return "text"


def match_plan_slide(plan: dict, title: str) -> tuple[int, dict] | tuple[None, None]:
    """Which plan entry built this slide, matched on its title.

    Position cannot be used: a slide drawn by generated code, or one inserted later, shifts
    everything after it. The title is what a person would match on, and it is what is matched
    on here.

    Called by: nodes.edit._change_layouts.
    """
    wanted = _flatten(title)
    if not wanted:
        return None, None
    for i, slide in enumerate(plan.get("slides") or []):
        if _flatten((slide.get("fields") or {}).get("title", "")) == wanted:
            return i, slide
    return None, None


def _flatten(text) -> str:
    """Text compared as words only: a list joined, spacing collapsed, lower case.

    Called by: match_plan_slide.
    """
    if isinstance(text, list):
        text = " ".join(text)
    return " ".join(str(text or "").split()).strip().lower()
