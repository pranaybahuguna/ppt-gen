"""Planning a designed deck: context -> facts -> storyline -> one spec per slide -> a deck.

    gather      the facts the deck may state, each checked against the source (nodes.gather)
    storyline   the argument: an action title, a visual and cited facts per slide (one model call),
                linted, and sent back once with the problems if it has any
    specs       one model call per content slide, in parallel; each spec is validated, its numbers
                checked against the facts, and drawn once in memory to catch text that will not
                fit - any problem goes back to the model as feedback (up to `attempts` tries)
    render      cover and section dividers are built from the storyline; everything is drawn on
                the template (design.render)

Called by: cli.cmd_design; later the graph's create route.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pptx import Presentation

from ..settings import settings
from .profile import extract_profile
from .render import _clear_slides, _no_full_stop, add_spec_slide, render_deck
from .spec import META, tone_names, unsourced, validate

VISUALS = {"numbers", "chart", "big_number", "comparison", "sequence", "timeline", "table", "cards",
           "matrix", "statement", "points"}


# --------------------------------------------------------------------------- facts

def gather_facts(request: str, sources: list[dict], planner, log=print) -> dict:
    """The facts and datasets the deck may use, each supported by a quote from the sources.

    sources: [{"name": ..., "text": ...}]. Reuses the graph's gather step: same prompt, same
    chunking, same quote check.
    Called by: plan_deck.
    """
    from ..nodes.gather import check, chunks

    parts = [c for s in sources for c in chunks(s["name"], s["text"])] or [{"name": "request", "text": request}]
    with ThreadPoolExecutor(max_workers=max(1, settings().render_workers)) as pool:
        answers = list(pool.map(lambda src: planner.gather(request, [src]) or {}, parts))
    merged = {"facts": [f for a in answers for f in a.get("facts") or []],
              "datasets": {k: v for a in answers for k, v in (a.get("datasets") or {}).items()},
              "gaps": [g for a in answers for g in a.get("gaps") or []]}
    pack, report = check(merged, parts + [{"name": "request", "text": request}])
    log(f"  facts: {report['kept']} kept, {report['datasets']} datasets, {len(report['dropped'])} dropped")
    return pack


# --------------------------------------------------------------------------- storyline

def lint_story(story: dict, pack: dict) -> list[str]:
    """Problems with a storyline that a reader would notice. [] when it is fine.

    Called by: storyline.
    """
    out = []
    ids = {f["id"] for f in pack.get("facts") or []}
    slides = story.get("slides") or []
    if len(slides) < 3:
        out.append(f"only {len(slides)} content slides; the facts support more")
    for i, s in enumerate(slides, start=1):
        t = str(s.get("title") or "")
        if META.search(t) or len(t.split()) < 4:
            out.append(f"slide {i}: {t!r} is a label, not a takeaway; write it as a sentence that states the point")
        if len(t) > 85:
            out.append(f"slide {i}: the title is {len(t)} characters; keep it under 80")
        if s.get("visual") not in VISUALS:
            out.append(f"slide {i}: visual {s.get('visual')!r} is not one of {sorted(VISUALS)}")
        bad = [f for f in s.get("facts") or [] if f not in ids]
        if bad:
            out.append(f"slide {i}: cites facts that do not exist: {bad}")
        if not s.get("facts") and s.get("visual") != "statement":
            out.append(f"slide {i}: cites no facts; every slide must be backed by facts")
        if i > 1 and s.get("visual") == slides[i - 2].get("visual"):
            out.append(f"slides {i - 1} and {i}: both use '{s.get('visual')}'; vary the visuals")
    pts = sum(1 for s in slides if s.get("visual") == "points")
    if slides and pts > len(slides) / 3:
        out.append(f"{pts} of {len(slides)} slides are 'points'; use a stronger visual for some of them")
    return out


def storyline(request: str, pack: dict, planner, slides_wanted: str = "8-12", log=print,
              house_rules: list[str] | None = None) -> dict:
    """The deck's argument, linted, and revised once if the lint finds problems.

    Called by: plan_deck.
    """
    payload = {"request": request,
               "facts": [{"id": f["id"], "text": f["text"]} for f in pack["facts"]],
               "datasets": pack.get("datasets") or {}, "slides_wanted": slides_wanted}
    if house_rules:                     # the cover and section titles are written here
        payload["house_rules"] = house_rules
    story = planner.storyline(payload)
    problems = lint_story(story, pack)
    if problems:
        log(f"  storyline: {len(problems)} problem(s), revising: {problems[:3]}")
        story = planner.storyline({**payload, "previous": story, "feedback": problems})
        problems = lint_story(story, pack)
    story["lint"] = problems
    log(f"  storyline: {len(story.get('slides') or [])} content slides"
        + (f"; still {len(problems)} problem(s)" if problems else ""))
    return story


# --------------------------------------------------------------------------- specs

def frame_specs(story: dict) -> list[tuple[dict, dict | None]]:
    """The deck in order: (spec, plan) pairs. Cover and section dividers come complete (plan
    None); content slides carry their plan and an empty spec for the writer to fill.

    Called by: plan_deck.
    """
    sections = story.get("sections") or []
    out: list[tuple[dict, dict | None]] = [({"type": "cover", "title": story.get("title") or "",
                                             "subtitle": story.get("subtitle") or "",
                                             "agenda": sections[:6]}, None)]
    slides = story.get("slides") or []
    use_sections = len(slides) >= 8 and len(sections) >= 2
    current = None
    for s in slides:
        if use_sections and s.get("section") in sections and s.get("section") != current:
            current = s["section"]
            out.append(({"type": "section", "number": sections.index(current) + 1, "title": current,
                         "agenda": [_no_full_stop(x.get("title") or "") for x in slides
                                    if x.get("section") == current]}, None))
        out.append(({}, s))
    return out


def dry_run(spec: dict, template: str | Path, P: dict) -> list[str]:
    """Draw a spec on a throwaway slide, in memory, and return what would not fit.

    Called by: check_spec.
    """
    prs = Presentation(str(template))
    _clear_slides(prs)
    try:
        slide, problems = add_spec_slide(prs, json.loads(json.dumps(spec)), P, 1)
    except Exception as e:                           # a malformed spec that validate missed
        return [f"the spec could not be drawn: {e.__class__.__name__}: {e}"]
    return problems or _shrunk(slide, P)


def _shrunk(slide, P: dict) -> list[str]:
    """Body text the pieces had to shrink below a comfortable size: the slide says too much.

    Footnotes (italic) and table cells are small by design and not counted.
    Called by: dry_run.
    """
    floor = P["body_pt"] - 1
    small = set()
    for sh in slide.shapes:
        if not sh.has_text_frame or sh.is_placeholder or sh.name.startswith("deck-"):
            continue                                 # the template's own header and footer
        for p in sh.text_frame.paragraphs:
            for r in p.runs:
                if r.font.size and r.font.size.pt < floor and not r.font.italic and r.text.strip():
                    small.add(r.font.size.pt)
    if not small:
        return []
    return [f"the text had to shrink to {min(small):g}pt to fit (comfortable is {floor:g}pt or more): "
            "the slide says too much. Use fewer items or shorter texts, or move detail to the notes"]


def check_spec(spec: dict, source_text: str, template, P: dict) -> list[str]:
    """Every problem with one content spec: shape, invented numbers, text that will not fit, and
    what breaks the template's own style guide (design.guide)."""
    from .guide import checks_of, house_problems, spec_texts

    problems = validate(spec)
    if not problems:
        problems += [f"the number {n} is not in the facts; copy numbers exactly from the facts, or drop it"
                     for n in unsourced(spec, source_text)]
        problems += dry_run(spec, template, P)
        problems += house_problems(spec_texts(spec), checks_of(P))
    return problems


def write_spec(plan: dict, story: dict, pack: dict, planner, template, P: dict,
               attempts: int = 3) -> tuple[dict, list[str], int]:
    """One content slide's spec, checked and revised. -> (spec, problems left, tries used)

    Called by: plan_deck, in parallel for every content slide.
    """
    from .guide import rules_of

    by_id = {f["id"]: f for f in pack["facts"]}
    cited = [by_id[f] for f in plan.get("facts") or [] if f in by_id]
    data = (pack.get("datasets") or {}).get(plan.get("dataset") or "")
    source_text = " ".join(f"{f['text']} {f.get('quote', '')}" for f in pack["facts"]) + " " + \
        json.dumps(pack.get("datasets") or {})
    payload = {"deck": {"title": story.get("title"), "sections": story.get("sections"),
                        "titles": [s.get("title") for s in story.get("slides") or []]},
               "slide": {k: plan.get(k) for k in ("title", "job", "visual", "section")},
               "facts": [{"id": f["id"], "text": f["text"]} for f in cited],
               "dataset": data, "tones": tone_names(P),
               "palette": P.get("palette", "brand"), **rules_of(P)}
    tried: list[tuple[dict, list[str]]] = []
    for _ in range(attempts):
        # A model call that fails is not a bad slide: it raises, so the run stops with the
        # endpoint's reason instead of quietly shipping a deck of half-made slides.
        spec = planner.slide_spec(payload)
        if not isinstance(spec, dict) or not spec.get("title"):
            tried.append(({}, ["the answer was not a slide spec with a title"]))
            continue
        spec["type"] = "content"
        problems = check_spec(spec, source_text, template, P)
        tried.append((spec, problems))
        if not problems:
            break
        payload = {**payload, "previous": spec, "feedback": problems}
    # Checks are advice: the attempt with the fewest problems is kept, and what is left of them
    # travels with it as warnings.
    spec, problems = min(reversed(tried), key=lambda t: len(t[1]))
    return spec, problems, len(tried)


def _fallback(plan: dict, pack: dict) -> dict:
    """A plain but correct slide for a plan the writer could not get right: its facts as points."""
    by_id = {f["id"]: f for f in pack["facts"]}
    items = [{"text": by_id[f]["text"][:140]} for f in (plan.get("facts") or []) if f in by_id][:5] \
        or [{"text": plan.get("job") or ""}]
    if len(items) == 1:
        items.append({"text": " "})
    return {"type": "content", "title": str(plan.get("title") or "")[:90],
            "rows": [{"weight": 1, "items": [{"span": 12, "piece": "points", "items": items}]}]}


# --------------------------------------------------------------------------- the whole deck

def plan_deck(request: str, sources: list[dict], template: str | Path, out: str | Path, planner,
              slides_wanted: str = "8-12", log=print) -> dict:
    """Context in, a designed deck out. -> {"out", "specs", "report"}

    Called by: cli.cmd_design.
    """
    t0 = time.time()
    P = extract_profile(template)
    pack = gather_facts(request, sources, planner, log)
    if len(pack["facts"]) < 3:
        raise ValueError("too little to build a deck from: fewer than 3 facts could be taken from the context")
    story = storyline(request, pack, planner, slides_wanted, log)
    frame = frame_specs(story)
    todo = [(i, plan) for i, (_, plan) in enumerate(frame) if plan is not None]
    with ThreadPoolExecutor(max_workers=max(1, settings().render_workers)) as pool:
        written = list(pool.map(lambda ip: write_spec(ip[1], story, pack, planner, template, P), todo))
    specs = [spec for spec, _ in frame]
    report = {"facts": len(pack["facts"]), "storyline_lint": story.get("lint"), "slides": []}
    for (i, plan), (spec, problems, tries) in zip(todo, written):
        if not spec:                                  # nothing usable came back at all
            log(f"  slide {i + 1}: no usable spec after {tries} tries; its facts as points")
            spec = _fallback(plan, pack)
        elif problems:
            log(f"  slide {i + 1}: kept with {len(problems)} note(s): {problems[:2]}")
        specs[i] = spec
        report["slides"].append({"slide": i + 1, "title": spec.get("title"), "tries": tries,
                                 "pieces": [it.get("piece") for r in spec.get("rows") or [] for it in r["items"]],
                                 "problems": problems})
    drawn = render_deck(template, specs, out, P)
    report["render_problems"] = drawn["problems"]
    report["seconds"] = round(time.time() - t0, 1)
    Path(out).with_suffix(".specs.json").write_text(json.dumps(
        {"story": story, "specs": specs, "facts": pack["facts"]}, indent=1, ensure_ascii=False))
    log(f"  deck: {len(specs)} slides in {report['seconds']}s -> {out}")
    return {"out": str(out), "specs": specs, "report": report}

