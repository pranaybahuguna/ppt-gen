"""The create route as graph nodes: storyline -> approve -> write (one per slide, in parallel) -> build.

    storyline  the deck's argument from the gathered facts: action titles, sections, a visual
               and cited facts per slide (design.plan.storyline); the cover and section
               dividers are framed here, the content slides are left for the writers
    approve    optional: stop and let a person read the storyline first (approve_outline)
    write      one per content slide, all at once (LangGraph Send): the slide's spec, checked
               and revised by the model on its own (design.plan.write_spec)
    build      every spec drawn on the template (design.render), published as a version, and
               handed to the edit route's `review` - which looks at every new slide and sends
               the weak ones back through `work` and `join`, exactly as for an edit

Called by: LangGraph, registered in graph.build_graph.
"""
from __future__ import annotations

from langgraph.types import Send, interrupt

from ..settings import settings
from ..state import CLEAR, DeckState, step


def _template(state: DeckState) -> str | None:
    return state.get("template") or settings().default_template or None


# --------------------------------------------------------------------------- storyline

def storyline(state: DeckState, planner=None, log=print) -> dict:
    """Plan the deck's argument and frame it: cover, section dividers, content slide plans.

    Reads: instruction, content_pack, template (or settings.default_template), slides_wanted.
    Writes: story, outline (the same, for approval), deck_frame, template, slide_specs cleared;
        or route='ask' when there is no template.
    Next: approve_outline.
    """
    from ..nodes.common import workspace
    from .plan import frame_specs
    from .plan import storyline as plan_storyline

    template = _template(state)
    if not template:
        return {"route": "ask", "pending_question": "Which template should the deck follow? Attach the .pptx.",
                "trace": [step("storyline", outcome="no template")]}
    pack = state.get("content_pack") or {"facts": [], "datasets": {}}
    story = plan_storyline(state.get("instruction") or "", pack, planner,
                           state.get("slides_wanted") or "8-12", log)
    frame = [{"spec": spec, "plan": plan} for spec, plan in frame_specs(story)]
    with workspace(state) as ws:
        ws.publish_json(story, "storyline.json")       # a record of the run, not state: tidy may drop it
    return {"story": story, "outline": story, "deck_frame": frame, "template": template,
            "slide_specs": CLEAR,
            "trace": [step("storyline", slides=len(frame), lint=len(story.get("lint") or []))]}


def approve_outline(state: DeckState) -> dict:
    """Optionally stop and let a person read the storyline before a deck is built from it.

    Off by default (settings.approve_outline). Reading a dozen action titles is seconds of a
    person's time; reading a finished deck is minutes, and this is where changing your mind is
    free.
    """
    if not settings().approve_outline or (state.get("approved") or {}).get("outline"):
        return {"trace": [step("approve_outline", outcome="not required")]}
    story = state.get("story") or {}
    verdict = interrupt({"approve": "storyline", "title": story.get("title"),
                         "slides": [s.get("title") for s in story.get("slides") or []],
                         "question": "Shall I build the deck from this storyline?"})
    ok = verdict is True or (isinstance(verdict, str) and verdict.strip().lower() in ("y", "yes", "ok", "go"))
    if isinstance(verdict, dict):
        ok = bool(verdict.get("approved"))
    return {"approved": {**(state.get("approved") or {}), "outline": ok},
            "route": state.get("route") if ok else "decline",
            "route_reason": None if ok else "you did not approve the storyline, so I stopped.",
            "trace": [step("approve_outline", approved=ok)]}


def after_storyline(state: DeckState) -> str:
    return state["route"] if state.get("route") in ("ask", "decline") else "approve_outline"


def after_approval(state: DeckState):
    """Fan out: one `write` per content slide, all at once."""
    if state.get("route") == "decline":
        return "decline"
    sends = [Send("write", {"thread_id": state.get("thread_id"), "run_id": state.get("run_id"),
                            "index": i, "plan": f["plan"], "story": state.get("story"),
                            "pack": state.get("content_pack"), "template": state.get("template")})
             for i, f in enumerate(state.get("deck_frame") or []) if f.get("plan")]
    return sends or "build"


# --------------------------------------------------------------------------- write

def write(payload: dict, planner=None, log=print) -> dict:
    """Write one content slide's spec. Runs once per content slide, in parallel.

    Reads: the Send payload {thread_id, run_id, index, plan, story, pack, template}.
    Writes: slide_specs (+1).
    Next: build, once every slide is written.
    """
    from ..nodes.common import workspace
    from .plan import write_spec
    from .profile import extract_profile

    with workspace(payload) as ws:
        template = ws.fetch(payload["template"], f"write-{payload['index']}-template.pptx")
        spec, problems, tries = write_spec(payload["plan"], payload["story"] or {}, payload["pack"] or {"facts": []},
                                           planner, template, extract_profile(template))
    log(f"  write slide {payload['index'] + 1}: {tries} tr{'y' if tries == 1 else 'ies'}"
        + (f", {len(problems)} note(s)" if problems else ""))
    return {"slide_specs": [{"index": payload["index"], "spec": spec, "problems": problems, "tries": tries}],
            "trace": [step("write", slide=payload["index"] + 1, tries=tries, notes=len(problems))]}


# --------------------------------------------------------------------------- build

def build(state: DeckState, log=print) -> dict:
    """Draw every spec on the template and publish the deck; queue every new slide for review.

    Reads: deck_frame, slide_specs, content_pack, template.
    Writes: deck_versions (+1), previews, changelog, vision_notes, and - for `review` -
        edit_tasks and changed (one entry per content slide), edit_round=0, work_base=None.
    Next: review.
    """
    from ..nodes.common import make_previews, publish_version, workspace
    from .plan import _fallback
    from .profile import extract_profile
    from .render import render_deck

    frame = state.get("deck_frame") or []
    written = {s["index"]: s for s in state.get("slide_specs") or []}
    specs, notes, tasks, changed, changelog = [], [], [], [], []
    pack = state.get("content_pack") or {"facts": []}
    for i, f in enumerate(frame):
        if not f.get("plan"):
            specs.append(f["spec"])
            continue
        w = written.get(i) or {}
        spec = w.get("spec") or _fallback(f["plan"], pack)
        specs.append(spec)
        notes += [f"slide {i + 1}: {p}" for p in w.get("problems") or []]
        plan = f["plan"]
        ask = f"A slide that makes this point: {plan.get('title')}. Its job: {plan.get('job')}"
        tasks.append({"id": f"s{i + 1}", "kind": "redesign", "slide": i + 1, "instruction": ask, "round": 0})
        changed.append({"task": f"s{i + 1}", "kind": "redesign", "instruction": ask, "slide": i + 1, "was": None})
        changelog.append({"slide": i + 1, "change": f"new slide: {spec.get('title')}",
                          "drawn_with": [it.get("piece") for r in spec.get("rows") or [] for it in r.get("items") or []]})
    with workspace(state) as ws:
        template = ws.fetch(state["template"], "template.pptx")
        out = ws.path("deck.pptx")
        drawn = render_deck(template, specs, out, extract_profile(template))
        uri, versions = publish_version(state, ws, "deck.pptx")
        ws.publish_json({"story": state.get("story"), "specs": specs}, "specs.json")
        previews, why = make_previews(state, ws, out)
    notes += [f"slide {n}: {p}" for n, ps in drawn["problems"].items() for p in ps]
    log(f"  build: {len(specs)} slides -> {uri}")
    return {"deck_versions": versions, "previews": previews, "changelog": changelog,
            "vision_notes": notes, "edit_tasks": tasks, "changed": changed, "edit_round": 0,
            "work_base": None, "task_results": CLEAR,
            "trace": [step("build", slides=len(specs), notes=len(notes),
                           previews=len(previews), why_no_previews=why)]}
