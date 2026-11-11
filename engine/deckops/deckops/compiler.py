"""Compile: DeckPlan + DeckIndex (+ template catalog) -> OpList.

Pure function: no files, no network, no model. Everything the LLM produced is
validated here against the real deck before any op exists.
"""
from __future__ import annotations

from collections import Counter

from .probe import iter_paragraphs
from .refs import TagError, parse_tagged


class CompileError(ValueError):
    pass


# ---- translate --------------------------------------------------------------

def compile_translate(plan: dict, index: dict) -> list[dict]:
    paras = {p["ref"]: p for p in iter_paragraphs(index)}
    ops, errors = [], []
    for item in plan["paragraphs"]:
        src = paras.get(item["ref"])
        if src is None:
            errors.append(f"{item['ref']}: not in deck index")
            continue
        if item["tagged"] == src["tagged"]:
            continue                                   # unchanged - emit nothing
        try:
            toks = parse_tagged(item["tagged"])
        except TagError as e:
            errors.append(f"{item['ref']}: {e}")
            continue
        if toks[0][0] != "plain":
            bad = [t[1] for t in toks if t[0] == "r" and t[1] >= src["runs"]]
            if bad:
                errors.append(f"{item['ref']}: unknown run tags {bad}")
                continue
            # fields (slide numbers etc.) must survive translation
            src_f = Counter(t for t in parse_tagged(src["tagged"]) if t[0] == "f")
            out_f = Counter(t for t in toks if t[0] == "f")
            if src_f != out_f:
                errors.append(f"{item['ref']}: field tokens changed")
                continue
        ops.append({
            "op": "set_paragraph_text",
            "target": item["ref"],
            "expect": {"text_sha": src["sha"]},
            "value": item["tagged"],
        })
    if errors:
        raise CompileError(errors)
    return ops


# ---- create -----------------------------------------------------------------

FAMILIES = {
    "title": ["title", "subtitle"],
    "section": ["title", "body"],
    "title_and_body": ["title", "body"],
    "two_content": ["title", "body", "body"],
    "title_only": ["title"],
}
# Tie-breakers when two layouts have the same role shape. For a real corporate
# template this is config ("loaded, not inferred"), not a heuristic.
NAME_HINTS = {"section": "section", "title_and_body": "content", "title": "title slide"}


def _content_roles(layout):
    return [ph for ph in layout["placeholders"] if ph["role"] != "chrome"]


def pick_layout(family: str, catalog: list[dict], family_map: dict | None = None) -> dict:
    if family_map and family in family_map:
        return next(l for l in catalog if l["id"] == family_map[family])
    want = Counter(FAMILIES[family])
    exact = [l for l in catalog if Counter(p["role"] for p in _content_roles(l)) == want]
    if not exact:
        raise CompileError([f"no layout in template matches family {family!r}"])
    hint = NAME_HINTS.get(family, "")
    exact.sort(key=lambda l: (hint not in l["name"].lower(), l["id"]))
    return exact[0]


def compile_create(plan: dict, catalog: list[dict], family_map: dict | None = None) -> list[dict]:
    ops, errors = [], []
    for n, s in enumerate(plan["slides"]):
        fam = s["layout_family"]
        if fam not in FAMILIES:
            errors.append(f"slide {n}: unknown layout_family {fam!r}")
            continue
        layout = pick_layout(fam, catalog, family_map)
        by_role: dict[str, list] = {}
        for ph in sorted(_content_roles(layout), key=lambda p: (p["x"] or 0, p["y"] or 0)):
            by_role.setdefault(ph["role"], []).append(ph["idx"])
        fill = {}
        for b in s["blocks"]:
            slots = by_role.get(b["role"])
            if not slots:
                errors.append(f"slide {n}: layout {layout['name']!r} has no free {b['role']!r} placeholder")
                continue
            fill[slots.pop(0)] = b["content"]
        ops.append({"op": "add_slide", "layout": layout["id"], "fill": fill, "notes": s.get("notes")})
    if errors:
        raise CompileError(errors)
    return ops


# ---- edit: numbered card grids -----------------------------------------------
# A "card" = circle + number + heading + description, as separate shapes.
# The model says which cards survive / merge and what they say; this code owns
# every coordinate.

def detect_cards(slide: dict) -> list[dict]:
    shapes = [s for s in slide["shapes"] if s.get("geom")]
    text = lambda s: "".join(p["text"] for p in s.get("paras", [])).strip()
    used, cards = set(), []
    for num in sorted((s for s in shapes if text(s).isdigit()), key=lambda s: int(text(s))):
        g = num["geom"]
        circle = next((s for s in shapes if s is not num and s["geom"] == g and not text(s)), None)
        same_row = [s for s in shapes if s["geom"]["y"] == g["y"] and s["geom"]["x"] > g["x"] and text(s)
                    and not text(s).isdigit() and s["ref"] not in used]
        below = [s for s in shapes if s["geom"]["x"] == g["x"] and s["geom"]["y"] > g["y"] and text(s)
                 and not text(s).isdigit() and s["ref"] not in used]
        if not (circle and same_row and below):
            continue
        heading = min(same_row, key=lambda s: s["geom"]["x"])
        body = min(below, key=lambda s: s["geom"]["y"])
        parts = {"circle": circle, "number": num, "heading": heading, "body": body}
        used.update(p["ref"] for p in parts.values())
        cards.append({"n": int(text(num)), **parts})
    return cards


def _set_text(shape: dict, text: str) -> dict:
    p = shape["paras"][0]
    # single-run paragraphs keep their formatting by writing into run 0
    return {"op": "set_paragraph_text", "target": p["ref"], "expect": {"text_sha": p["sha"]},
            "value": f"<r0>{_esc(text)}</r0>" if p["runs"] else _esc(text)}


def _esc(t):
    import html
    return html.escape(t, quote=False)


def compile_card_grid(plan: dict, index: dict) -> list[dict]:
    """plan = {"slide_number": 7 (1-based) or "slide": "slide:262", "columns": 3,
               "cards": [{"from": [1, 2], "heading": "...", "body": "..."}, ...]}"""
    if "slide_number" in plan:
        slide = next((s for s in index["slides"] if s["index"] == plan["slide_number"] - 1), None)
    else:
        slide = next((s for s in index["slides"] if s["ref"] == plan.get("slide")), None)
    if slide is None:
        raise CompileError([f"slide {plan.get('slide_number', plan.get('slide'))} not in deck"])
    cards = {c["n"]: c for c in detect_cards(slide)}
    if not cards:
        raise CompileError(["no numbered card pattern on this slide"])
    claimed = [n for c in plan["cards"] for n in c["from"]]
    errors = [f"card {n} does not exist" for n in claimed if n not in cards]
    errors += [f"card {n} used twice" for n in set(claimed) if claimed.count(n) > 1]
    if errors:
        raise CompileError(errors)

    # Grid box = bounding box of the existing cards; keep original gutters and row positions.
    all_c = list(cards.values())
    left = min(c["circle"]["geom"]["x"] for c in all_c)
    right = max(c["body"]["geom"]["x"] + c["body"]["geom"]["w"] for c in all_c)
    xs = sorted({c["circle"]["geom"]["x"] for c in all_c})
    gutter = (xs[1] - xs[0]) - all_c[0]["body"]["geom"]["w"] if len(xs) > 1 else 182880
    rows_y = sorted({c["circle"]["geom"]["y"] for c in all_c})
    c0 = cards[min(cards)]
    head_dx = c0["heading"]["geom"]["x"] - c0["circle"]["geom"]["x"]
    body_dy = c0["body"]["geom"]["y"] - c0["circle"]["geom"]["y"]
    cols = plan.get("columns", 3)
    n_rows = -(-len(plan["cards"]) // cols)
    if n_rows > len(rows_y):
        raise CompileError([f"{len(plan['cards'])} cards need {n_rows} rows, slide has {len(rows_y)}"])
    card_w = (right - left - gutter * (cols - 1)) // cols

    ops = []
    for i, spec in enumerate(plan["cards"]):
        keep = cards[spec["from"][0]]
        x = left + (i % cols) * (card_w + gutter)
        y = rows_y[i // cols]
        cg, hg, bg = keep["circle"]["geom"], keep["heading"]["geom"], keep["body"]["geom"]
        ops += [
            {"op": "set_geometry", "target": keep["circle"]["ref"], "x": x, "y": y, "w": cg["w"], "h": cg["h"]},
            {"op": "set_geometry", "target": keep["number"]["ref"], "x": x, "y": y, "w": cg["w"], "h": cg["h"]},
            {"op": "set_geometry", "target": keep["heading"]["ref"], "x": x + head_dx, "y": y,
             "w": card_w - head_dx, "h": hg["h"]},
            {"op": "set_geometry", "target": keep["body"]["ref"], "x": x, "y": y + body_dy, "w": card_w, "h": bg["h"]},
            _set_text(keep["number"], str(i + 1)),
            _set_text(keep["heading"], spec["heading"]),
            _set_text(keep["body"], spec["body"]),
        ]
        for n in spec["from"][1:]:                      # merged-away cards
            ops += [{"op": "delete_shape", "target": cards[n][k]["ref"]} for k in ("circle", "number", "heading", "body")]
    for n in set(cards) - set(claimed):                 # dropped cards
        ops += [{"op": "delete_shape", "target": cards[n][k]["ref"]} for k in ("circle", "number", "heading", "body")]
    # don't emit no-op text writes
    return [o for o in ops if not (o["op"] == "set_paragraph_text" and _noop(o, index))]


def _noop(op, index):
    from .probe import iter_paragraphs
    p = next(p for p in iter_paragraphs(index) if p["ref"] == op["target"])
    return p["tagged"] == op["value"]
