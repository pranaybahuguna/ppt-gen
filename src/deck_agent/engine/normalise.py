"""Make a real template usable, without changing how it looks.

Templates bought or exported from the wild carry things PowerPoint tolerates and a program
addressing shapes by id cannot. The one that matters, found on the first real commercial
template this project was given, blocked **fifteen of its twenty slides**:

    slide 6: shapes share ids ['57']: can't be addressed safely

The engine is right to refuse. Its addressing is `slide:<sldId>/shape:<id>`, and two shapes
with the same id on one slide cannot be told apart, so an edit could land on either. What makes
this worth fixing rather than accepting is *where* the duplicates were:

    id 57 appears inside: EMBEDDED_OLE_OBJECT(id=57)

Every single one was an embedded OLE object whose own `p:cNvPr` and whose fallback picture's
`p:cNvPr` carry the same id. Among the **top-level** shapes - the only ones the engine ever
addresses, fills or deletes - there were no duplicates at all. The slide was never ambiguous in
the way the check feared; the duplicate was buried inside an object nothing addresses.

So this pass renumbers those nested duplicates to ids that are free on that slide, and leaves
every top-level shape's id exactly as it was. Nothing moves, nothing changes colour, no text is
touched: the only bytes that differ are `@id` attributes on non-visual property elements that
nothing refers to.

It is deliberately conservative:

* a **top-level** shape never has its id changed, because the kit addresses it and an edit
  planned against one id must not land on another;
* only ids that are genuinely duplicated are touched - a slide that is already clean comes
  through byte-identical;
* new ids are picked above everything already used on that slide, so they cannot collide;
* the result is saved through the engine's part-by-part comparison, so every slide the pass did
  not need to touch keeps its original bytes.

The normalised copy is what the kit is built from and what decks are built with, because the
kit's addressing refers to it. `report` says exactly what was changed, for a person who wants
to know what happened to their template.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from . import bootstrap


def shape_id_problems(path: str | Path) -> dict[int, list[str]]:
    """Which slides have duplicate shape ids, and which ids. -> {slide number: [ids]}"""
    bootstrap()
    from pptx import Presentation
    from pptx.oxml.ns import qn

    out: dict[int, list[str]] = {}
    prs = Presentation(str(path))
    for n, slide in enumerate(prs.slides, start=1):
        counts = Counter(e.get("id") for e in slide.shapes._spTree.iter(qn("p:cNvPr")))
        duplicated = sorted(i for i, c in counts.items() if c > 1 and i is not None)
        if duplicated:
            out[n] = duplicated
    return out


def unique_shape_ids(src: str | Path, dst: str | Path) -> dict:
    """Give every shape on every slide an id of its own. -> a report of what changed.

    Top-level shapes keep the ids they had. Where a duplicate exists, the *nested* occurrence -
    inside an OLE object, a group's fallback, a picture - is the one renumbered.
    """
    bootstrap()
    from pptx import Presentation
    from pptx.oxml.ns import qn

    from deckops.executor import save_preserving

    prs = Presentation(str(src))
    changes: list[dict] = []

    for n, slide in enumerate(prs.slides, start=1):
        tree = slide.shapes._spTree
        every = list(tree.iter(qn("p:cNvPr")))
        counts = Counter(e.get("id") for e in every)
        duplicated = {i for i, c in counts.items() if c > 1 and i is not None}
        if not duplicated:
            continue

        used = {int(e.get("id")) for e in every if (e.get("id") or "").isdigit()}
        nxt = (max(used) if used else 0) + 1

        for dup in sorted(duplicated):
            holders = [e for e in every if e.get("id") == dup]
            # Keep the first top-level occurrence if there is one, otherwise the first of any.
            keep = next((e for e in holders if _is_top_level(e, tree)), holders[0])
            for element in holders:
                if element is keep:
                    continue
                # Recorded rather than assumed. For a nested duplicate - the real-world case -
                # this is always False, and a test holds it to that. It is True only when two
                # top-level shapes collide, which no renumbering can avoid.
                element.set("id", str(nxt))
                changes.append({"slide": n, "was": dup, "now": str(nxt),
                                "name": element.get("name") or "",
                                "renumbered_a_top_level_shape": _is_top_level(element, tree)})
                used.add(nxt)
                nxt += 1

    stats = save_preserving(prs, str(src), str(dst))
    stable_zip(dst)
    touched = sorted({c["slide"] for c in changes})
    unsafe = [c for c in changes if c["renumbered_a_top_level_shape"]]
    return {"slides_touched": touched, "ids_renumbered": len(changes),
            "renumbered_top_level_shapes": len(unsafe),
            "top_level_collisions": [{"slide": c["slide"], "was": c["was"], "now": c["now"]}
                                     for c in unsafe][:12],
            "changes": changes[:40], "parts": stats}


def _is_top_level(cNvPr, tree) -> bool:
    """Is this the non-visual properties element of a shape directly on the slide?

    Top level means: `p:cNvPr` -> `p:nvSpPr`(or nvPicPr/nvGraphicFramePr/nvGrpSpPr/nvCxnSpPr)
    -> the shape -> the slide's shape tree. Anything deeper belongs to something nested.
    """
    holder = cNvPr.getparent()
    shape = holder.getparent() if holder is not None else None
    return shape is not None and shape.getparent() is tree


# --------------------------------------------------------------------------- groups

# Flattening groups was tried here and is NOT kept, because it silently lost content.
#
# The arithmetic is not the hard part and it was verified correct to the EMU: a child's
# position in slide space is
#
#     slide_x = off.x + (child_x - chOff.x) * ext.cx / chExt.cx
#
# and 228 groups across 19 slides flattened with every paragraph preserved, every shape
# accounted for, and rendered output that differed by 0.00-0.17% of pixels.
#
# That last number is why this is not here. Looking at the 0.17% slide showed **four of its six
# icons had disappeared** - white freeform strokes whose geometry was provably correct and
# whose fill and line XML was byte-identical before and after. A change that passes a pixel
# threshold and still drops half the icons off a slide is not a change to keep, and the cause
# was not found. (It may yet be a LibreOffice rendering quirk rather than a broken file -
# opening a flattened deck in PowerPoint would settle it - but "probably fine" is not a basis
# for rewriting somebody's template.)
#
# The need is real: group support is what stands between this project and most commercial
# templates. The right place for it is the engine, in `probe`, emitting slide-space geometry
# for shapes nested in groups - `_shape_entries` already recurses into them and gives each one
# an addressable ref, so only the coordinates are wrong. That leaves the template untouched,
# which is the part this experiment got wrong.


# --------------------------------------------------------------------------- the entry point

def prepare(src: str | Path, dst: str | Path) -> dict:
    """Everything a template needs before a kit can be built from it. -> a report.

    One call, so that ingesting a template is a single documented step rather than a sequence
    somebody has to remember. Today that is only the shape-id pass; anything else that turns
    out to be needed belongs here, where it happens once, before the ids the kit will refer to
    are read.

    **The result is the template from now on.** A kit addresses shapes by id, so a kit built
    from the prepared copy only makes sense against that copy. Callers upload it and use its
    hash as the cache key - which also means a template that needed no preparation keeps its
    original hash, because the bytes come through unchanged.
    """
    problems = shape_id_problems(src)
    if not problems:
        # Nothing to do, so copy the bytes rather than round-tripping through python-pptx.
        # That matters for more than speed: the cache key is the template's content hash, and
        # a rewrite changes the zip container even when every part survives - so an already
        # clean template would get a new hash, and be learned again, every single time.
        import shutil

        shutil.copy(str(src), str(dst))
        return {"slides_touched": [], "ids_renumbered": 0, "renumbered_top_level_shapes": 0,
                "top_level_collisions": [], "changes": [], "parts": {},
                "duplicate_ids_before": {}, "changed": False}
    report = unique_shape_ids(src, dst)
    report["duplicate_ids_before"] = {str(k): v for k, v in problems.items()}
    report["changed"] = report["ids_renumbered"] > 0
    return report


def stable_zip(path: str | Path) -> None:
    """Same input, same bytes: zip entries stamped with a fixed time, not the clock's.

    A prepared template's content hash is its kit's cache key. Preparing the same template at
    10:01 and at 10:02 must give the same hash, or a template that needs preparing gets its
    kit built again for every deck made from it.
    """
    import zipfile

    path = Path(path)
    with zipfile.ZipFile(path) as src:
        entries = [(info, src.read(info.filename)) for info in src.infolist()]
    tmp = path.with_name(path.name + ".stable")
    with zipfile.ZipFile(tmp, "w") as dst:
        for info, data in entries:
            fixed = zipfile.ZipInfo(info.filename, date_time=(2000, 1, 1, 0, 0, 0))
            fixed.compress_type = info.compress_type
            fixed.external_attr = info.external_attr
            dst.writestr(fixed, data)
    tmp.replace(path)
