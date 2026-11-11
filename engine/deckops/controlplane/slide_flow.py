"""Two-stage deck generation: content first, then layout, one slide at a time.

Stage 1  outline   (one model call)    what each slide says: purpose, title, points, figures, sources.
                                        No layouts, no field names. Plain content.
Stage 2  render    (one call per slide) the slide's content + only the layouts that can hold it.
                                        The model picks one and fills its fields.

Between and after:
  options_for   which layouts can physically hold this slide (item counts measured per template)
  split         a slide with more points than any layout holds becomes "(1/2)", "(2/2)"
  check         every rendered slide is compiled on its own; an error goes back to the model for
                that slide only, up to `retries` times
The finished plan is the same plan.json the compiler always takes, so nothing downstream changes.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

from deckops.compiler import CompileError
from deckops.patterns import AUTO_FIELDS, compile_from_kit

LONG_FIELDS = ("body", "points", "text", "commentary", "side_text", "intro")
KIND_HINTS = {"numbers": ["stats"], "sequence": ["steps"], "comparison": ["columns", "panels"],
              "list": ["cards", "columns", "panels", "text"], "table": ["table"], "chart": ["chart", "two_charts"],
              "text": ["text", "panels"], "cover": ["cover"], "closing": ["closing"]}


def _text_capacity(p):
    return sum(1 for f in p["fields"] if f.split("_")[0] in LONG_FIELDS or f.rsplit("_", 1)[0] in LONG_FIELDS)


def option(name, p):
    """How one layout is described to the model in stage 2 (no ids, no geometry)."""
    o = {"pattern": name, "use_for": p.get("use_for", ""),
         "fields": [f for f in p["fields"] if f not in AUTO_FIELDS]}
    if "items" in p:
        it = p["items"]
        o["items"] = {"min": it.get("min", 1), "max": it.get("max", len(it["groups"])),
                      "each": [f for f in it["fields"] if f != "number"]}
    if "table" in p:
        o["table"] = {"max_rows": p["table"].get("max_rows", 9)}
    if "charts" in p:
        o["charts"] = list(p["charts"])
    return o


def options_for(slide: dict, kit: dict, position: int, total: int) -> list[dict]:
    """Layouts that can hold this slide's content."""
    n = len(slide.get("points") or [])
    rows = len((slide.get("table") or {}).get("rows") or [])
    charts = len(slide.get("charts") or ({"main": slide["chart"]} if slide.get("chart") else {}))
    out = []
    for name, p in kit["patterns"].items():
        kind = name.split("@")[0]
        if kind == "cover" and position != 1 or kind == "closing" and position != total:
            continue
        if rows and ("table" not in p or rows > p["table"].get("max_rows", 9)):
            continue
        if not rows and "table" in p:
            continue
        if charts and len(p.get("charts", {})) != charts:
            continue
        if not charts and "charts" in p:
            continue
        if "items" in p:
            it = p["items"]
            if not it.get("min", 1) <= n <= it.get("max", len(it["groups"])):
                continue
        elif n > _text_capacity(p):
            continue
        out.append(option(name, p))
    hint = KIND_HINTS.get(slide.get("kind"), [])
    out.sort(key=lambda o: (o["pattern"].split("@")[0] not in hint,
                            hint.index(o["pattern"].split("@")[0]) if o["pattern"].split("@")[0] in hint else 0))
    return out


def max_points(kit: dict) -> int:
    caps = [p["items"].get("max", len(p["items"]["groups"])) for p in kit["patterns"].values() if "items" in p]
    return max(caps + [_text_capacity(p) for p in kit["patterns"].values() if "items" not in p] + [1])


def split(slide: dict, kit: dict) -> list[dict]:
    """Too many points for any layout: spread them over several slides of the same kind."""
    pts = slide.get("points") or []
    cap = max(2, max_points(kit))
    parts = [pts[i:i + cap] for i in range(0, len(pts), cap)]
    out = []
    for k, chunk in enumerate(parts, start=1):
        s = copy.deepcopy(slide)
        s["points"] = chunk
        s["title"] = f"{slide.get('title', '')} ({k}/{len(parts)})".strip()
        if k > 1:
            s.pop("intro", None)
        if k < len(parts):
            s.pop("takeaway", None)
        out.append(s)
    return out


def expand(outline: dict, kit: dict) -> list[dict]:
    """Stage-1 slides, with over-full ones split, each with the layouts that can hold it."""
    slides = []
    for s in outline["slides"]:
        slides += split(s, kit) if len(s.get("points") or []) > max_points(kit) else [s]
    total = len(slides)
    return [{"content": s, "options": options_for(s, kit, i, total)} for i, s in enumerate(slides, start=1)]


def check_slide(plan_slide: dict, index: dict, kit: dict, content: dict) -> list[str]:
    try:
        compile_from_kit({"slides": [plan_slide]}, index, kit, content)
        return []
    except CompileError as e:
        return [m.replace("slide 1 ", "this slide ", 1) for m in e.args[0]]


def render_one(planner, job, context, index, kit, content, retries=2, feedback=None, previous=None, log=print):
    """Ask the model for one slide; on a compile error, send the error back. -> (plan_slide, attempts)"""
    attempts = []
    if not job["options"]:
        return None, [{"error": ["no layout in this template can hold this slide's content"]}]
    for attempt in range(retries + 1):
        answer = planner.render_slide(job["content"], job["options"], context, feedback=feedback, previous=previous)
        errors = check_slide(answer, index, kit, content)
        attempts.append({"answer": answer, "errors": errors, "feedback": feedback})
        if not errors:
            return answer, attempts
        log(f"  slide {context['slide']}: attempt {attempt + 1} rejected: {errors[0][:120]}")
        feedback, previous = errors, answer
    return None, attempts


def plan_slides(outline, kit, index, content, planner, retries=2, log=print, workers=4):
    """Stage 2 for every slide (in parallel). -> (plan, jobs, attempt log)"""
    jobs = expand(outline, kit)
    total = len(jobs)

    def work(i):
        ctx = {"deck_title": outline.get("title", ""), "slide": i + 1, "of": total}
        return render_one(planner, jobs[i], ctx, index, kit, content, retries, log=log)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, range(total)))
    failed = [i + 1 for i, (s, _) in enumerate(results) if s is None]
    if failed:
        raise CompileError([f"slide {i}: still invalid after {retries + 1} attempts: "
                            f"{results[i - 1][1][-1].get('errors')}" for i in failed])
    plan = {"title": outline.get("title", ""), "footer": outline.get("footer", ""),
            "slides": [s for s, _ in results]}
    return plan, jobs, [a for _, a in results]
