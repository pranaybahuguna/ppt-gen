"""One task of an edit, done by a model: a redesign or a new slide writes a spec, a tweak writes
code. Many of these run at once, one per slide, each against the same starting deck.

Checks here are advice, not gates. Whatever the checks find goes back to the model as feedback;
after the last try the best attempt is kept and its remaining problems travel with it as
warnings. Nothing is replaced by a plainer slide behind the person's back, and the only thing
that stops a task is a model call that fails outright.

Called by: revise.nodes.work.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from pptx import Presentation

from ..design.guide import checks_of, house_problems, rules_of, spec_texts
from ..design.plan import dry_run
from ..design.spec import tone_names, validate
from .read import deck_outline, slide_content, slide_shapes

#: A request that asks for numbers to be made up: then they may be, and they are labelled.
INVENT = re.compile(r"\b(arbitrary|made[- ]up|make up|dummy|example|illustrative|sample|fake|placeholder)\b"
                    r".{0,40}\b(numbers?|figures?|data|values?)\b|\b(numbers?|figures?|data|values?)\b"
                    r".{0,30}\b(arbitrary|made[- ]up|dummy|illustrative|fake)\b", re.I)


def visible_copy(deck: str | Path, dst: str | Path) -> Path:
    """A copy of the deck with every slide visible, for rendering.

    A renderer skips hidden slides, so every page after one would be the wrong slide.
    Called by: slide_picture, revise.nodes.join.
    """
    prs = Presentation(str(deck))
    if any(s._element.get("show") == "0" for s in prs.slides):
        for s in prs.slides:
            s._element.attrib.pop("show", None)
        prs.save(str(dst))
    else:
        shutil.copy(deck, dst)
    return Path(dst)


def slide_picture(deck: str | Path, index: int, workdir: Path, dpi: int = 110) -> bytes | None:
    """A PNG of one slide (0-based), or None when rendering is unavailable."""
    from ..sandbox.render import render

    workdir.mkdir(parents=True, exist_ok=True)
    copy = visible_copy(deck, workdir / "visible.pptx")
    result = render(copy, workdir / f"pic-{index}", [index], dpi=dpi)
    return result.previews[0].read_bytes() if result.ok and result.previews else None


def _best(attempts: list[tuple[dict, list[str]]]) -> tuple[dict, list[str]]:
    """The attempt with the fewest problems; the latest one on a tie."""
    return min(reversed(attempts), key=lambda a: len(a[1]))


def _label_invented(spec: dict, instruction: str) -> dict:
    """Numbers the person asked to be made up are said to be illustrative, on the slide."""
    if INVENT.search(instruction or "") and "illustrative" not in (spec.get("footnote") or "").lower():
        spec["footnote"] = ((spec.get("footnote") or "") + " Illustrative figures, not measured data.").strip()
    return spec


def redesign(task: dict, deck: Path, P: dict, planner, workdir: Path, attempts: int = 3,
             feedback: list[str] | None = None) -> dict:
    """Rebuild one slide (or write a new one, for kind 'add') as a spec. -> a task result.

    Called by: revise.nodes.work.
    """
    prs = Presentation(str(deck))
    n = task["slide"]
    if task["kind"] == "add":
        content, png = None, None
        payload = {**_asked(task), "slide": None,
                   "deck": [{"slide": o["slide"], "title": o["title"]} for o in deck_outline(prs)],
                   "goes_after": n}
    else:
        content = slide_content(prs.slides[n - 1])
        png = slide_picture(deck, n - 1, workdir / f"s{n}")
        payload = {**_asked(task), "slide_number": n, "slide": content,
                   "deck_title": slide_content(prs.slides[0])["title"] if len(prs.slides) else ""}
    payload["tones"], payload["palette"] = tone_names(P), P.get("palette", "brand")
    payload.update(rules_of(P))
    if feedback:
        payload["feedback"] = feedback
        if task.get("previous"):
            payload["previous"] = task["previous"]
    tried: list[tuple[dict, list[str]]] = []
    for _ in range(attempts):
        spec = planner.redesign_slide(payload, png)
        if not isinstance(spec, dict) or not spec.get("title"):
            tried.append(({}, ["the answer was not a slide spec with a title"]))
            continue
        spec.setdefault("type", "content")
        spec = _label_invented(spec, task["instruction"])
        problems = (validate(spec) or dry_run(spec, deck, P)) + house_problems(spec_texts(spec), checks_of(P))
        tried.append((spec, problems))
        if not problems:
            break
        payload = {**payload, "previous": spec, "feedback": problems}
    spec, problems = _best(tried)
    if not spec:
        return {**_base(task), "ok": False, "problems": problems}
    return {**_base(task), "ok": True, "spec": spec, "changes": spec.pop("changes", None),
            "problems": problems, "tries": len(tried)}


def tweak(task: dict, deck: Path, P: dict, planner, workdir: Path, attempts: int = 3,
          feedback: list[str] | None = None) -> dict:
    """Change one slide in place with model-written code, tried in the sandbox. -> a task result.

    Two modes, by task kind: "tweak" makes a small direct change; "draw" redraws the slide's
    content area as a custom visual (shapes, arrows, icons made of shapes).

    The code is run here against the starting deck so a failure can go back to the model; the
    join replays the working code on the deck as it is by then.
    Called by: revise.nodes.work.
    """
    from ..sandbox.code import run_builder

    prs = Presentation(str(deck))
    n = task["slide"]
    W, H = prs.slide_width / 914400, prs.slide_height / 914400
    ctx = {"slide_index": n - 1, "slide_w_in": round(W, 3), "slide_h_in": round(H, 3)}
    draw = task["kind"] == "draw"
    if draw:                      # a free redraw of the content area, in the deck's own colours
        ctx.update({"body_in": P["area"], "colours": P["colours"], "font": P["font"]})
    payload = {**_asked(task), "slide_number": n, **ctx,
               "shapes": slide_shapes(prs.slides[n - 1]), **rules_of(P)}
    if draw:
        payload["content"] = slide_content(prs.slides[n - 1])
    ask = planner.draw_slide if draw else planner.tweak_slide
    if feedback:
        payload["feedback"] = feedback
        if task.get("previous"):
            payload["previous_code"] = task["previous"]
    png = slide_picture(deck, n - 1, workdir / f"s{n}")
    last = ["no code was written"]
    tried: list[str] = []
    for _ in range(attempts):
        answer = ask(payload, png)
        code = (answer or {}).get("code") or ""
        result = run_builder(code, ctx, deck)
        if result.ok and result.slides_after == result.slides_before:
            # Code gets the same geometry check as the pieces: text that the change made overflow
            # or collide goes back to the model before anyone looks at the slide.
            clashes = _new_geometry_problems(deck, result.deck, n - 1)
            if not clashes or len(tried) == attempts - 1:
                return {**_base(task), "ok": True, "code": code, "ctx": ctx,
                        "changes": (answer or {}).get("explanation"), "problems": clashes}
            last = clashes
        else:
            last = result.feedback() if not result.ok else ["the code added or removed slides; change only this slide"]
        tried.append(code)
        payload = {**payload, "previous_code": code, "feedback": last}
    return {**_base(task), "ok": False, "problems": last}


def deck_change(task: dict, deck: Path, P: dict, planner, workdir: Path, attempts: int = 3,
                feedback: list[str] | None = None) -> dict:
    """One piece of code for many slides (a consistency request), tried in the sandbox.

    The model sees a few sample slides, not all of them, and writes code that loops over
    ctx["slides"]. The same geometry check as a tweak runs on every slide it touched.
    Called by: revise.nodes.work.
    """
    from ..sandbox.code import run_builder

    prs = Presentation(str(deck))
    slides = [n for n in task["slides"] if 1 <= n <= len(prs.slides)]
    sample = sorted({slides[0], slides[len(slides) // 2], slides[-1]})
    ctx = {"slides": [n - 1 for n in slides], "slide_w_in": round(prs.slide_width / 914400, 3),
           "slide_h_in": round(prs.slide_height / 914400, 3), "colours": P["colours"], "font": P["font"]}
    payload = {**_asked(task), "applies_to_slides": slides,
               "samples": [{"slide": n, "shapes": slide_shapes(prs.slides[n - 1])} for n in sample],
               "slide_w_in": ctx["slide_w_in"], "slide_h_in": ctx["slide_h_in"],
               "colours": P["colours"], "font": P["font"], **rules_of(P)}
    if feedback:
        payload["feedback"] = feedback
    pngs = [p for p in (slide_picture(deck, n - 1, workdir / f"s{n}") for n in sample[:2]) if p]
    last = ["no code was written"]
    for k in range(attempts):
        answer = planner.deck_code(payload, pngs)
        code = (answer or {}).get("code") or ""
        result = run_builder(code, ctx, deck)
        if result.ok and result.slides_after == result.slides_before:
            clashes = [f"slide {n}: {p}" for n in slides
                       for p in _new_geometry_problems(deck, result.deck, n - 1)][:8]
            if not clashes or k == attempts - 1:
                return {**_base(task), "ok": True, "code": code, "ctx": ctx, "slides": slides,
                        "changes": (answer or {}).get("explanation"), "problems": clashes}
            last = clashes
        else:
            last = result.feedback() if not result.ok else ["the code added or removed slides"]
        payload = {**payload, "previous_code": code, "feedback": last}
    return {**_base(task), "ok": False, "slides": slides, "problems": last}


def _new_geometry_problems(before: Path, after: Path, index: int) -> list[str]:
    """Overflow and overlapping text on one slide that were not there before the code ran,
    phrased for the model. Problems the slide already had (a hand-made deck may have some) are
    not the code's to fix.
    """
    from ..engine.deck import Baseline, check

    try:
        found = Baseline.of(before, {index}).filter(check(after, {index}))
    except Exception as e:                   # a slide the checker cannot read: say so, do not block
        return [f"the slide could not be checked for overlaps ({e.__class__.__name__})"]
    out = []
    for f in found["findings"]:
        if f["kind"] == "overlap":
            out.append(f"text boxes overlap: shapes {f.get('shapes')} collide - move or resize them "
                       "so there is clear space between them")
        else:
            out.append(f"text overflows its box ({f.get('ref')}): it needs {f.get('ratio', 0):.0%} of the "
                       "space it has - make the box bigger or the text shorter")
    return out[:6]


def _asked(task: dict) -> dict:
    """The task as split wrote it, and the person's own words beside it - a worker that sees only
    split's version can be sent after the wrong problem (it once "fixed spelling" on a chart
    whose labels were unreadable)."""
    out = {"request": task["instruction"]}
    if task.get("person_asked") and task["person_asked"] != task["instruction"]:
        out["person_asked"] = task["person_asked"]
    return out


def _base(task: dict) -> dict:
    return {"id": task["id"], "kind": task["kind"], "slide": task["slide"],
            "instruction": task["instruction"], "round": task.get("round", 0),
            "was": task.get("was", task["slide"] if task["kind"] != "add" else None)}
