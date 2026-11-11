"""Marry the rule-based draft with the vision model's reading of the same slide.

Who owns what:
  structure (which boxes exist, which repeat, how items are grouped)   -> the rules. Measured, exact.
  meaning   (what a box is for, what kind of slide it is, use_for)      -> the vision model.
  physics   (can this box actually hold that?)                          -> a veto on both.

So vision can rename a box ("box 8 is a header, not a number"), mark a box as fixed decoration,
say what kind of slide it is, or say a slide isn't worth offering. It can't invent boxes, move
boxes between items, or give a one-line box a paragraph role. Every decision is written down
(agree / vision / vetoed), and the acceptance test still runs on the merged result.

Input from the vision model, per slide (all optional; silence means "no opinion"):
  {"kind": "columns", "use_for": "...", "offer": true,
   "boxes": {"9": "heading", "10": "body", "6": "fixed"}}
"""
from __future__ import annotations

import copy
import re
from collections import Counter

from deckops.autokit import CHROME, NUMBER_LIKE, PAGE_RE, USE_FOR, consistent, kind_of

from .kit_check import box_lines

ROLES = {"title", "subtitle", "page", "footer", "heading", "body", "points", "number", "value", "label",
         "caption", "intro", "takeaway", "note", "side_title", "side_text", "commentary", "fixed"}
LONG = {"body", "points", "intro", "side_text", "commentary"}
KINDS = set(USE_FOR)


def _shapes(slide):
    return {int(s["ref"].rsplit("shape:", 1)[1]): s for s in slide["shapes"]}


def _text(shp):
    return " ".join(p["text"] for p in shp.get("paras", [])).strip()


def boxes_for_vision(pat: dict, slide: dict) -> list[dict]:
    """What the vision model is told about each labelled box (it also sees them numbered in the picture)."""
    shapes = _shapes(slide)
    out = []
    for field, sid in pat["fields"].items():
        out.append({"id": sid, "rules_say": field, "text": _text(shapes[sid])[:80],
                    "lines": round(box_lines(shapes[sid]), 1)})
    if "items" in pat:
        it = pat["items"]
        for field, pos in it["fields"].items():
            sid = it["groups"][0][pos]
            out.append({"id": sid, "rules_say": field, "in_item": f"item 1 of {len(it['groups'])}",
                        "text": _text(shapes[sid])[:80], "lines": round(box_lines(shapes[sid]), 1)})
    return out


def _veto(role, sid, shapes, column_texts=None):
    """Why this box can't take this role, or None."""
    if role not in ROLES:
        return f"unknown role {role!r}"
    if role in LONG and box_lines(shapes[sid]) < 1.8:
        return f"box holds {box_lines(shapes[sid]):.1f} line(s), too small for {role}"
    if role == "fixed" and len(_text(shapes[sid])) >= 12:
        return "too much text to keep as decoration (it would carry template content into new decks)"
    if role == "page" and not PAGE_RE.match(_text(shapes[sid])):
        return "text doesn't look like a page number"
    if role == "value" and not (NUMBER_LIKE.search(_text(shapes[sid])) and len(_text(shapes[sid])) <= 12):
        return "not a short figure like '42%' or '3.2x', so it isn't a headline number"
    if role == "number":
        texts = column_texts or [_text(shapes[sid])]
        if not all(t.isdigit() for t in texts) or [int(t) for t in texts] != list(range(1, len(texts) + 1)):
            return "texts don't count 1, 2, 3 ..."
    return None


def _name(role, taken):
    taken[role] += 1
    return role if taken[role] == 1 else f"{role}_{taken[role]}"


def merge(rule_name: str, pat: dict, slide: dict, vision: dict | None, n: int, total: int):
    """-> (name or None if dropped, merged pattern, decisions)"""
    decisions = {"agree": [], "vision": [], "vetoed": [], "kind": None, "dropped": None}
    if not vision:
        return rule_name.split("@")[0], pat, decisions
    if vision.get("offer") is False:
        decisions["dropped"] = "vision: not worth offering as a reusable slide"
        return None, pat, decisions
    shapes = _shapes(slide)
    said = {int(k): v for k, v in (vision.get("boxes") or {}).items() if str(k).isdigit()}
    p = copy.deepcopy(pat)

    def decide(field, sid, column_texts=None):
        base = re.sub(r"_\d+$", "", field)
        v = said.get(sid)
        if v is None or v == base:
            if v == base:
                decisions["agree"].append(f"box {sid}: {base}")
            return base
        why = _veto(v, sid, shapes, column_texts)
        if why:
            decisions["vetoed"].append(f"box {sid}: vision said {v}, kept {base} ({why})")
            return base
        decisions["vision"].append(f"box {sid}: {base} -> {v}")
        return v

    # loose boxes
    taken, fields, keep = Counter(), {}, list(p.get("keep", []))
    for field, sid in pat["fields"].items():
        role = decide(field, sid)
        if role == "fixed":
            keep.append(sid)
        else:
            fields[_name(role, taken)] = sid
    p["fields"] = fields
    if keep:
        p["keep"] = sorted(set(keep))

    # boxes inside repeated items: decide on item 1, applies to every item
    if "items" in p:
        it = p["items"]
        taken_i, roles = Counter(), {}
        for field, pos in pat["items"]["fields"].items():
            sid = it["groups"][0][pos]
            column = [_text(shapes[g[pos]]) for g in it["groups"]]
            role = decide(field, sid, column)
            if role != "fixed":
                roles[_name(role, taken_i)] = pos
        if not roles:
            decisions["dropped"] = "nothing fillable left inside the repeated items"
            return None, p, decisions
        it["fields"] = roles
        if not any(r in roles for r in ("value", "points")):
            it["min"] = 1

    # what kind of slide: vision's word if the content supports it, else re-derived from the content
    rules_kind = rule_name.split("@")[0]
    vk = vision.get("kind")
    if vk in KINDS and consistent(vk, p, n, total):
        kind = vk
    else:
        kind = rules_kind if consistent(rules_kind, p, n, total) else kind_of(p, n, total)
        if vk and vk != kind:
            decisions["vetoed"].append(f"kind: vision said {vk}, kept {kind} (content doesn't fit {vk})")
    if kind != rules_kind:
        decisions["kind"] = f"{rules_kind} -> {kind}"
    use = vision.get("use_for")
    p["use_for"] = use.strip()[:160] if isinstance(use, str) and use.strip() and kind == vk else USE_FOR[kind](p)
    if set(p["fields"]) <= CHROME and not any(k in p for k in ("items", "charts", "table")) and n not in (1, total):
        decisions["dropped"] = "only title and footer left to fill"
        return None, p, decisions
    return kind, p, decisions
