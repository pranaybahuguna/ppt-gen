"""The create route: a new deck in a template's look, built in two stages.

    outline          one model call: what each slide SAYS. No layouts, no field names.
    render_slides    one model call per slide: the content, plus only the layouts that can
                     physically hold it. The model picks one and fills it.
    assemble         compile the plan to ops, apply them, check the result
    repair           send the slides that did not fit back, with a character budget

The two stages are the engine author's design and the reason long decks stay sharp: a single
call that plans twelve slides at once drifts, while a call that places one slide into a short
menu of layouts that are already known to fit does not have room to.

Two things this layer adds on top:

* **per-slide failure is classified.** The engine's `plan_slides` raises if any slide fails
  after its retries. Here each slide is rendered on its own so we can tell the two failures
  apart: "the model keeps producing an invalid plan" (a real failure) and "no layout in this
  template can hold this content" - which is not the model's fault and is exactly what the
  escape hatch exists for.
* **a slide that nothing can hold is remembered, not dropped**, with the position it should
  occupy, so the code path can put it back in the right place.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from langgraph.types import interrupt

from ..engine import bootstrap
from ..engine.build import build_from_kit, baseline_for_kit, repair_feedback
from ..engine.deck import probe
from ..engine.fallback import relaxed_options
from ..engine.variety import describe, repetitive, spread, without
from ..errors import PlanInvalid
from ..infra.storage import store
from ..settings import settings
from ..state import DeckState, step
from .common import make_previews, publish_version, warnings_of, workspace

NO_FIT = "no layout in this template can hold this slide's content"


# --------------------------------------------------------------------------- stage 1

def outline(state: DeckState, planner=None, log=print) -> dict:
    """What the deck says, slide by slide. One call, no layouts."""
    bootstrap()
    from controlplane import slide_flow

    if state.get("outline"):                       # supplied by the caller: skip the call
        return {"trace": [step("outline", outcome="supplied by the caller")]}

    kit = store().get_json(state["kit"])
    content = state.get("content_pack") or {"facts": [], "datasets": {}}
    try:
        result = planner.outline(state.get("instruction") or "", content, slide_flow.max_points(kit))
    except NotImplementedError:
        return {"errors": [{"kind": "no_model", "message":
                            "writing a deck's content needs a model. Configure llm.json, or "
                            "pass a ready-made outline."}],
                "route": "decline",
                "route_reason": "I need either a model or an outline to write a deck's content.",
                "trace": [step("outline", outcome="no model")]}
    log(f"  outline: {len(result.get('slides', []))} slides")
    with workspace(state) as ws:
        ws.publish_json(result, "outline.json")
    return {"outline": result,
            "trace": [step("outline", slides=len(result.get("slides", [])))]}


def approve_outline(state: DeckState) -> dict:
    """Optionally stop and let a person read the outline before a deck is built from it.

    Off by default. It exists because reviewing eight lines of content is seconds of a person's
    time, while reviewing a finished deck is minutes - and because this is the one point in the
    run where changing your mind is free.
    """
    if not settings().approve_outline or (state.get("approved") or {}).get("outline"):
        return {"trace": [step("approve_outline", outcome="not required")]}
    verdict = interrupt({"approve": "outline", "outline": state.get("outline"),
                         "question": "Shall I build the deck from this outline?"})
    ok = verdict is True or (isinstance(verdict, str) and verdict.strip().lower() in ("y", "yes", "ok", "go"))
    if isinstance(verdict, dict):
        ok = bool(verdict.get("approved"))
    return {"approved": {**(state.get("approved") or {}), "outline": ok},
            "route": state.get("route") if ok else "decline",
            "route_reason": None if ok else "you did not approve the outline, so I stopped.",
            "trace": [step("approve_outline", approved=ok)]}


def after_approval(state: DeckState) -> str:
    return "render_slides" if (state.get("approved") or {}).get("outline", True) else "decline"


# --------------------------------------------------------------------------- stage 2

def render_slides(state: DeckState, planner=None, log=print) -> dict:
    """One call per slide: content plus the layouts that fit. Failures are classified."""
    bootstrap()
    from controlplane import slide_flow

    s = settings()
    kit = store().get_json(state["kit"])
    content = state.get("content_pack") or {"facts": [], "datasets": {}}
    outline_doc = state["outline"]

    with workspace(state) as ws:
        template = ws.fetch(state["template"], "template.pptx")
        index = probe(template)
        jobs = slide_flow.expand(outline_doc, kit)      # splits over-full slides, attaches options
        total = len(jobs)

        # A slide the strict rules found no layout for gets one more, named, deterministic
        # attempt before anything is drawn by generated code. See engine/fallback.py for why
        # this is not a rare case and what each relaxation gives up.
        relaxations: dict[int, str] = {}
        for i, job in enumerate(jobs):
            if job["options"]:
                continue
            options, relaxed = relaxed_options(job["content"], kit, i + 1, total)
            if options:
                job["options"] = options
                relaxations[i] = relaxed or "relaxed"
                log(f"  slide {i + 1}: no layout fits it strictly; {relaxed}")

        def one(i: int):
            ctx = {"deck_title": outline_doc.get("title", ""), "slide": i + 1, "of": total}
            return slide_flow.render_one(planner, jobs[i], ctx, index, kit, content,
                                         s.render_retries, log=log)

        # Before anything is placed: reorder each slide's menu so the deck does not converge
        # on one layout. Stage 2 runs each slide in parallel and in isolation, so no call can
        # know what another chose; this is the only place that sees all of them at once, and it
        # costs nothing. The model still picks - only the order it is offered changes.
        jobs, spread_changes = spread(jobs)
        if spread_changes:
            log("  variety: narrowed the menu on slides "
                + ", ".join(f"{c['slide']} (no {'/'.join(c['removed'])})"
                            for c in spread_changes))

        with ThreadPoolExecutor(max_workers=s.render_workers) as pool:
            results = list(pool.map(one, range(total)))

        placed: list[dict | None] = [None] * total
        pending, failed, attempts = [], [], []
        for i, (slide, log_entries) in enumerate(results):
            attempts.append(log_entries)
            if slide is not None:
                placed[i] = slide
                continue
            reasons = (log_entries[-1].get("errors") or log_entries[-1].get("error") or [])
            if any(NO_FIT in str(r) for r in reasons):
                pending.append({"position": i, "content": jobs[i]["content"],
                                "why": "no layout in the template fits this slide"})
            else:
                failed.append({"slide": i + 1, "reasons": reasons})

        # Variety pass. Stage 2 places each slide knowing nothing about the others, and in
        # parallel, so it converges on one layout - four slides of six on a real deck. Look at
        # the finished set, decide which slides are the repetitive ones, and ask for those
        # again with the over-used layout taken off the menu. See engine/variety.py.
        varied = []
        chosen = [p["pattern"] if p else None for p in placed]
        for i, avoid in repetitive(chosen):
            narrowed = without(jobs[i]["options"], avoid)
            if not narrowed:
                continue            # nothing else fits: a repetitive slide beats a broken one
            ctx = {"deck_title": outline_doc.get("title", ""), "slide": i + 1, "of": total}
            again, tries = slide_flow.render_one(
                planner, {**jobs[i], "options": narrowed}, ctx, index, kit, content,
                s.render_retries, log=log)
            attempts[i] = attempts[i] + tries
            if again is not None:
                varied.append({"slide": i + 1, "from": avoid, "to": again["pattern"]})
                placed[i] = again
        if varied:
            log(f"  variety: {describe(varied)}")

        slides = [p for p in placed if p is not None]
        ws.publish_json(attempts, "render_attempts.json")

    plan = {"title": outline_doc.get("title", ""), "footer": outline_doc.get("footer", ""),
            "slides": slides}
    retried = sum(len(a) - 1 for a in attempts)
    log(f"  stage 2: {len(slides)} slides placed, {retried} retried, "
        f"{len(pending)} need a custom slide, {len(failed)} failed")

    out: dict = {"plan": plan, "pending_code": pending,
                 "trace": [step("render_slides", placed=len(slides), retried=retried,
                                no_fit=len(pending), failed=len(failed),
                                varied=varied or None,
                                spread={str(c["slide"]): c["removed"]
                                        for c in spread_changes} or None,
                                relaxed={str(k + 1): v for k, v in relaxations.items()} or None)]}
    if failed:
        out["errors"] = [{"kind": "plan_invalid", "message":
                          f"slide {f['slide']} could not be planned after "
                          f"{s.render_retries + 1} attempts", "detail": f["reasons"]} for f in failed]
    if not slides and not pending:
        out["route"] = "decline"
        out["route_reason"] = "none of the slides could be placed in this template."
    return out


def after_render(state: DeckState) -> str:
    if state.get("route") == "decline":
        return "decline"
    return "assemble" if (state.get("plan") or {}).get("slides") else "custom_code"


# --------------------------------------------------------------------------- build

def assemble(state: DeckState, log=print) -> dict:
    """Compile the plan, build the deck, check it. The deterministic heart of the create route."""
    with workspace(state) as ws:
        template = ws.fetch(state["template"], "template.pptx")
        kit = store().get_json(state["kit"])
        index = probe(template)
        plan = state["plan"]
        content = state.get("content_pack") or {}
        try:
            result = build_from_kit(template, index, kit, plan, content,
                                    ws.path("out.pptx"), baseline_for_kit(template, kit))
        except PlanInvalid as e:
            return {"errors": [{"kind": e.kind, "message": str(e), "detail": e.detail}],
                    "route": "decline",
                    "route_reason": "the plan for this deck did not fit the template.",
                    "trace": [step("assemble", outcome="plan invalid")]}

        plan_uri = ws.publish_json(plan, "plan.json")
        ws.publish_json(result.ops, "ops.json")
        uri, versions = publish_version(state, ws, "out.pptx")
        previews, why_not = make_previews(state, ws, result.out_path)
        log(f"  assemble: {result.summary()}")
        return {
            "deck_versions": versions, "deck_plan": plan_uri,
            "findings": result.findings, "structural": result.structural, "leaked": result.leaked,
            "previews": previews,
            "changelog": [{"change": f"built {len(plan['slides'])} slides in the template's look",
                           "pattern_counts": _counts(plan)}],
            "trace": [step("assemble", **result.summary(), previews=len(previews),
                           previews_skipped=why_not)],
        }


def _counts(plan: dict) -> dict:
    out: dict[str, int] = {}
    for s in plan.get("slides", []):
        out[s.get("pattern", "?")] = out.get(s.get("pattern", "?"), 0) + 1
    return out


# --------------------------------------------------------------------------- repair

def repair(state: DeckState, planner=None, log=print) -> dict:
    """Send the slides that did not fit back to the model, with a length it can aim at.

    Only the offending slides are re-rendered, and only they change in the plan. The previous
    answer and the exact complaint go with the request, which is what turns a vague "too long"
    into a fix.
    """
    bootstrap()
    from controlplane import slide_flow

    s = settings()
    kit = store().get_json(state["kit"])
    content = state.get("content_pack") or {"facts": [], "datasets": {}}
    plan = {**state["plan"], "slides": list(state["plan"]["slides"])}
    round_ = state.get("repair_round", 0) + 1

    with workspace(state) as ws:
        template = ws.fetch(state["template"], "template.pptx")
        index = probe(template)
        deck = ws.fetch(state["deck_versions"][-1], "current.pptx")
        out_index = probe(deck)
        by_slide = repair_feedback(state.get("findings") or [], out_index)
        jobs = slide_flow.expand(state["outline"], kit)
        log(f"  repair round {round_}: slides {[i + 1 for i in by_slide]} sent back")

        fixed = 0
        for i, messages in by_slide.items():
            if i >= len(plan["slides"]) or i >= len(jobs):
                continue
            ctx = {"deck_title": plan.get("title", ""), "slide": i + 1, "of": len(plan["slides"])}
            slide, _ = slide_flow.render_one(planner, jobs[i], ctx, index, kit, content,
                                             s.render_retries, feedback=messages,
                                             previous=plan["slides"][i], log=log)
            if slide is not None:
                plan["slides"][i] = slide
                fixed += 1
        ws.publish_json({"round": round_, "feedback": {str(k): v for k, v in by_slide.items()},
                         "fixed": fixed}, f"repair-{round_}.json")

    return {"plan": plan, "repair_round": round_,
            "trace": [step("repair", round=round_, slides=sorted(i + 1 for i in by_slide),
                           rewritten=fixed)]}


def after_assemble(state: DeckState) -> str:
    """Clean -> hand it over. Problems -> repair, then the escape hatch, then say so honestly."""
    s = settings()
    if state.get("pending_code") and state.get("code_attempt", 0) == 0:
        return "custom_code"                      # slides nothing could hold are still owed
    if not warnings_of(state):
        return "deliver"
    if state.get("repair_round", 0) < s.max_repair_rounds and state.get("findings"):
        return "repair"
    if s.allow_custom_code and state.get("findings") and \
            state.get("code_attempt", 0) < s.code_attempts:
        return "custom_code"
    return "deliver"
