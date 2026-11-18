"""Slide surgery on any deck: replace, insert, delete and move slides in one pass.

Slide numbers in a batch always mean the slides as they were BEFORE the batch, so tasks
planned in parallel against the same deck can be applied together without renumbering each
other. The deck is saved through the engine's `save_preserving`, so every slide the batch did
not touch keeps its original bytes - a hand-edited slide stays exactly as its author left it.

Called by: revise.nodes.join.
"""
from __future__ import annotations

from pathlib import Path

from pptx import Presentation

from ..design.render import add_spec_slide, number_page
from ..engine import bootstrap


def apply_changes(src: str | Path, changes: list[dict], P: dict, dst: str | Path) -> dict:
    """Apply slide changes. -> {"position": {old slide number: new number or None},
    "changed": [new numbers of drawn slides], "problems": {new number: [...]},
    "at": [for each change, in order: the new number of the slide it drew, or None]}

    changes: {"op": "replace", "slide": n, "spec": {...}}
             {"op": "insert", "after": n (0 = at the start), "spec": {...}}
             {"op": "delete", "slide": n}
             {"op": "move", "slide": n, "after": m}
    """
    bootstrap()
    from deckops.executor import save_preserving

    prs = Presentation(str(src))
    ids = prs.slides._sldIdLst
    original = list(ids)                                # sldId elements, in the old order
    slides = list(prs.slides)
    drawn: dict[int, object] = {}                       # sldId element -> problems
    problems_by_el: dict = {}
    drawn_for: list = []                                # per change: the sldId it drew, or None

    def unique_name(slide):
        # python-pptx names a new slide part after the slide count, which repeats a name still
        # in use once a slide has been removed - and a zip with two slide18.xml is a broken deck.
        from pptx.opc.packuri import PackURI

        names = [str(p.partname) for p in prs.part.package.iter_parts()]
        if names.count(str(slide.part.partname)) > 1:
            n = 1
            while f"/ppt/slides/slide{n}.xml" in names:
                n += 1
            slide.part.partname = PackURI(f"/ppt/slides/slide{n}.xml")

    def place_after(el, anchor):
        ids.remove(el)
        if anchor is None:
            ids.insert(0, el)
        else:
            ids.insert(list(ids).index(anchor) + 1, el)

    for ch in changes:
        op = ch["op"]
        if op == "replace":
            old = original[ch["slide"] - 1]
            slide, found = add_spec_slide(prs, ch["spec"], P, ch["slide"])
            unique_name(slide)
            if not ch["spec"].get("notes") and slides[ch["slide"] - 1].has_notes_slide:
                kept = slides[ch["slide"] - 1].notes_slide.notes_text_frame.text
                if kept.strip():
                    slide.notes_slide.notes_text_frame.text = kept
            new_el = ids[-1]
            place_after(new_el, old)
            ids.remove(old)
            prs.part.drop_rel(old.rId)
            drawn[id(new_el)] = new_el
            problems_by_el[id(new_el)] = found
            drawn_for.append(new_el)
        elif op == "insert":
            after = ch.get("after", len(original))
            slide, found = add_spec_slide(prs, ch["spec"], P, after + 1)
            unique_name(slide)
            new_el = ids[-1]
            place_after(new_el, original[after - 1] if after >= 1 else None)
            drawn[id(new_el)] = new_el
            problems_by_el[id(new_el)] = found
            drawn_for.append(new_el)
        elif op == "delete":
            drawn_for.append(None)
            el = original[ch["slide"] - 1]
            if el in list(ids):
                ids.remove(el)
                prs.part.drop_rel(el.rId)
        elif op == "move":
            drawn_for.append(None)
            el = original[ch["slide"] - 1]
            after = ch.get("after", 0)
            place_after(el, original[after - 1] if after >= 1 else None)

    order = list(ids)
    position = {n: (order.index(el) + 1 if el in order else None) for n, el in enumerate(original, start=1)}
    changed = sorted(order.index(el) + 1 for el in drawn.values() if el in order)
    problems = {order.index(el) + 1: problems_by_el[k] for k, el in drawn.items()
                if el in order and problems_by_el[k]}
    at = [order.index(el) + 1 if el is not None and el in order else None for el in drawn_for]
    # Drawn slides number themselves "n / total" now that the order and total are final
    # (the slides nobody touched keep theirs: their bytes are not ours to change).
    for n in changed:
        number_page(prs.slides[n - 1], P, n, len(order))
    save_preserving(prs, str(src), str(dst))
    return {"position": position, "changed": changed, "problems": problems, "at": at}
