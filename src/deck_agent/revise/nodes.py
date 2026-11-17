"""The edit route as graph nodes: split -> work (one per task, in parallel) -> join -> review.

    split    one model call turns the request into independent per-slide tasks
    work     a node instance per task, run in parallel by LangGraph (Send): redesign/add write a
             spec, tweak writes code; each tries, checks and retries on its own slide
    join     applies every result to the deck in one pass (slide surgery, then the tweaks' code
             in the sandbox), publishes the new version and previews the changed slides
    review   a vision model looks at the changed slides next to what was asked; slides it is
             unhappy with go back to `work` once with its notes, everything else is delivered

Called by: LangGraph, registered in graph.build_graph.
"""
from __future__ import annotations

from pathlib import Path

from langgraph.types import Send
from pptx import Presentation

from ..settings import settings
from ..state import CLEAR, DeckState, current_deck, step
from .read import deck_outline
from .work import visible_copy

KINDS = {"redesign", "draw", "tweak", "add", "delete", "move"}
MODEL_KINDS = {"redesign", "draw", "tweak", "add"}
CODE_KINDS = {"tweak", "draw"}                 # done by model-written code in the sandbox


def _deck_uri(state: DeckState) -> str | None:
    return current_deck(state) or state.get("source_deck")


# --------------------------------------------------------------------------- split

def split(state: DeckState, planner=None, log=print) -> dict:
    """Turn the request into per-slide tasks.

    Graph node. Runs: first on the edit route.
    Reads: instruction, the current deck (or the attached one).
    Writes: edit_tasks, work_base, edit_round=0, task_results cleared; or route='ask'/'decline'.
    Next: after_split -> work (one per model task) | join | ask | decline.
    """
    from ..nodes.common import workspace

    uri = _deck_uri(state)
    if not uri:
        return {"route": "ask", "pending_question": "Which deck should I change? Attach it.",
                "trace": [step("split", outcome="no deck")]}
    with workspace(state) as ws:
        prs = Presentation(str(ws.fetch(uri, "base.pptx")))
        outline = deck_outline(prs)
    answer = planner.split_edit({"request": state.get("instruction") or "", "deck": outline})
    if answer.get("question"):
        return {"route": "ask", "pending_question": answer["question"],
                "trace": [step("split", outcome="question")]}
    n = len(outline)
    tasks, dropped = [], []
    for t in answer.get("tasks") or []:
        kind, slide = t.get("kind"), t.get("slide")
        ok_slide = isinstance(slide, int) and (0 <= slide <= n if kind == "add" else 1 <= slide <= n)
        if kind not in KINDS or not ok_slide:
            dropped.append(t)
            continue
        tasks.append({"id": f"t{len(tasks) + 1}", "kind": kind, "slide": slide,
                      "after": t.get("after"), "instruction": str(t.get("instruction") or ""), "round": 0})
    if not tasks:
        return {"route": "decline",
                "route_reason": "I could not turn that into a change to any slide of this deck"
                                + (f" ({len(dropped)} task(s) named slides that do not exist)" if dropped else ""),
                "trace": [step("split", outcome="nothing to do")]}
    log(f"  split: {len(tasks)} task(s): " + ", ".join(f"{t['kind']} {t['slide']}" for t in tasks))
    return {"edit_tasks": tasks, "work_base": uri, "edit_round": 0, "task_results": CLEAR,
            "changed": [], "edit_summary": answer.get("summary"),
            "trace": [step("split", tasks=len(tasks), dropped=len(dropped))]}


def after_split(state: DeckState):
    """Fan out: one `work` per model task, all at once. Deck-only tasks go straight to join."""
    if state.get("route") in ("ask", "decline"):
        return state["route"]
    sends = [Send("work", _payload(state, t, state["work_base"])) for t in state.get("edit_tasks") or []
             if t["kind"] in MODEL_KINDS]
    return sends or "join"


def _payload(state: DeckState, task: dict, deck: str, feedback: list[str] | None = None) -> dict:
    return {"thread_id": state.get("thread_id"), "run_id": state.get("run_id"), "task": task,
            "deck": deck, "feedback": feedback}


# --------------------------------------------------------------------------- work

def work(payload: dict, planner=None, log=print) -> dict:
    """Do one task. Runs as many times at once as there are tasks (LangGraph Send).

    Reads: the Send payload {thread_id, run_id, task, deck, feedback}.
    Writes: task_results (+1).
    Next: join, once every task of the round has finished.
    """
    from ..design.profile import extract_profile
    from ..nodes.common import workspace
    from . import work as w

    task = payload["task"]
    with workspace(payload) as ws:
        deck = ws.fetch(payload["deck"], f"work-{task['id']}-r{task.get('round', 0)}.pptx")
        P = extract_profile(deck)
        scratch = Path(ws.dir(f"work-{task['id']}-r{task.get('round', 0)}"))
        do = w.tweak if task["kind"] in CODE_KINDS else w.redesign
        result = do(task, deck, P, planner, scratch, feedback=payload.get("feedback"))
    log(f"  work {task['id']} ({task['kind']} slide {task['slide']}): "
        + ("done" if result["ok"] else "failed") + (f", {len(result['problems'])} note(s)" if result.get("problems") else ""))
    return {"task_results": [result], "trace": [step("work", task=task["id"], kind=task["kind"],
                                                     slide=task["slide"], ok=result["ok"])]}


# --------------------------------------------------------------------------- join

def join(state: DeckState, log=print) -> dict:
    """Apply this round's results to the deck in one pass and publish a new version.

    Round 0 starts from the deck the request was about; a review round starts from the version
    round 0 produced, and its slide numbers are the changed slides' new positions.
    Reads: edit_tasks, task_results, edit_round, work_base, changed.
    Writes: deck_versions (+1), previews, changelog, changed, errors.
    Next: review.
    """
    from ..design.profile import extract_profile
    from ..engine.deck import reseal
    from ..nodes.common import make_previews, publish_version, workspace
    from ..sandbox.code import run_builder
    from .surgery import apply_changes

    rnd = state.get("edit_round") or 0
    results = [r for r in state.get("task_results") or [] if r.get("round", 0) == rnd]
    tasks = state.get("edit_tasks") or []
    base_uri = state["work_base"] if rnd == 0 else current_deck(state)
    changes, codes, changelog, errors = [], [], [], []
    for r in results:
        if not r["ok"]:
            errors.append({"kind": "task", "message": f"slide {r['slide']} ({r['kind']}): not changed: "
                                                      + "; ".join(r["problems"][:2])})
            continue
        if r["kind"] == "redesign":
            changes.append({"op": "replace", "slide": r["slide"], "spec": r["spec"], "task": r})
        elif r["kind"] == "add":
            changes.append({"op": "insert", "after": r["slide"], "spec": r["spec"], "task": r})
        elif r["kind"] in CODE_KINDS:
            codes.append(r)
    if rnd == 0:
        for t in tasks:
            if t["kind"] == "delete":
                changes.append({"op": "delete", "slide": t["slide"]})
            elif t["kind"] == "move":
                changes.append({"op": "move", "slide": t["slide"], "after": t.get("after") or 0})

    with workspace(state) as ws:
        deck = ws.fetch(base_uri, f"join-base-r{rnd}.pptx")
        P = extract_profile(deck)
        out = ws.path(f"join-r{rnd}.pptx")
        done = apply_changes(deck, [{k: v for k, v in c.items() if k != "task"} for c in changes], P, out)
        position = done["position"]
        changed = []
        for c, at in zip(changes, done["at"]):
            if c["op"] in ("replace", "insert"):
                r = c["task"]
                was = r.get("was", r["slide"]) if c["op"] == "replace" else None
                changed.append({"task": r["id"], "kind": r["kind"], "instruction": r["instruction"],
                                "slide": at, "was": was})
                pieces = [it.get("piece") for row in r["spec"].get("rows") or [] for it in row.get("items") or []]
                changelog.append({"slide": at, "change": r.get("changes") or r["instruction"],
                                  "drawn_with": pieces or [r["spec"].get("type", "content")]})
        # Tweaks: replay each task's working code on the deck as it is now, at the slide's new
        # position, and reseal so every other slide keeps its bytes.
        current = out
        for k, r in enumerate(codes):
            pos = position.get(r["slide"]) if rnd == 0 else r["slide"]
            if pos is None:
                continue
            res = run_builder(r["code"], {**r["ctx"], "slide_index": pos - 1}, current)
            if not res.ok:
                errors.append({"kind": "task", "message": f"slide {r['slide']} ({r['kind']}): the change did not "
                                                          f"apply to the merged deck: {res.error}"})
                continue
            nxt = ws.path(f"join-r{rnd}-t{k}.pptx")
            reseal(current, res.deck, nxt)
            current = nxt
            changed.append({"task": r["id"], "kind": r["kind"], "instruction": r["instruction"], "slide": pos,
                            "was": r.get("was", r["slide"])})
            changelog.append({"slide": pos, "change": r.get("changes") or r["instruction"]})
        ws.path("result.pptx").write_bytes(Path(current).read_bytes())
        uri, versions = publish_version(state, ws, "result.pptx")
        pages = sorted({c["slide"] - 1 for c in changed if c.get("slide")})
        visible = visible_copy(ws.path("result.pptx"), ws.path("result-visible.pptx"))
        previews, why = make_previews(state, ws, visible, pages or None)
    notes = [f"slide {r['slide']}: {p}" for r in results if r["ok"] for p in r.get("problems") or []]
    # A task that could not be done is a warning while others were done; only an edit that
    # changed nothing at all is an error.
    if changed or rnd > 0:
        notes += [e["message"] for e in errors]
        errors = []
    log(f"  join: {len(changes)} slide change(s), {len(codes)} tweak(s) -> {uri}")
    if rnd:                                  # a second pass updates the first pass's account
        redone = {c["slide"] for c in changelog}
        changelog = [c for c in state.get("changelog") or [] if c["slide"] not in redone] + changelog
    return {"deck_versions": versions, "previews": previews, "changelog": changelog,
            "changed": changed if rnd == 0 else _merge_changed(state.get("changed") or [], changed),
            "errors": errors, "vision_notes": (state.get("vision_notes") or []) + notes if rnd else notes,
            "trace": [step("join", round=rnd, changes=len(changes), tweaks=len(codes),
                           errors=len(errors), previews=len(previews), why_no_previews=why)]}


def _merge_changed(before: list[dict], now: list[dict]) -> list[dict]:
    by_task = {c["task"]: c for c in before}
    for c in now:
        by_task[c["task"]] = {**by_task.get(c["task"], {}), **c}
    return list(by_task.values())


# --------------------------------------------------------------------------- review

def review(state: DeckState, planner=None, log=print) -> dict:
    """Look at the changed slides next to what was asked; queue a second pass for any that miss.

    Reads: changed, previews, edit_round, edit_tasks.
    Writes: revise (the tasks to redo, with the reviewer's notes), vision_notes.
    Next: after_review -> work (once more for those slides) | deliver.
    """
    from ..infra.storage import store

    s = settings()
    changed = [c for c in state.get("changed") or [] if c.get("slide")]
    if not (s.review_changes and getattr(planner, "vision", False) and changed and state.get("previews")):
        return {"revise": [], "trace": [step("review", outcome="skipped")]}
    by_page = {}
    for uri in state.get("previews") or []:
        name = uri.rsplit("/", 1)[-1]
        try:
            by_page[int(name.split("-")[1].split(".")[0])] = uri
        except (IndexError, ValueError):
            continue
    shown = [c for c in changed if c["slide"] in by_page]
    if not shown:
        return {"revise": [], "trace": [step("review", outcome="no previews")]}
    before = _before_pictures(state, [c.get("was") for c in shown])

    def look_at(batch: list[dict]) -> list[dict]:
        pngs, listed = [], []
        for c in batch:
            entry = {"slide": c["slide"], "asked": c["instruction"],
                     "made_with": "code" if c["kind"] in CODE_KINDS else "pieces"}
            if before.get(c.get("was")):
                pngs.append(before[c["was"]])
                entry["before_picture"] = len(pngs)
            pngs.append(store().get_bytes(by_page[c["slide"]]))
            entry["after_picture"] = len(pngs)
            listed.append(entry)
        answer = planner.review_changes(
            {"slides": listed, "note": "pictures are numbered from 1 in the order attached"}, pngs)
        return answer.get("slides") or []

    # A few slides per look, several looks at once: a reviewer shown a dozen pictures together
    # skims them, and passed a slide whose "big number" was the word "twelve".
    from concurrent.futures import ThreadPoolExecutor

    batches = [shown[i:i + 4] for i in range(0, len(shown), 4)]
    with ThreadPoolExecutor(max_workers=max(1, min(len(batches), s.render_workers))) as pool:
        verdicts = [v for part in pool.map(look_at, batches) for v in part]
    bad = {v.get("slide"): v.get("problems") or [] for v in verdicts if not v.get("ok") and v.get("problems")}
    # What the checks already found on a slide goes back with it too, whatever the reviewer saw.
    for c in shown:
        found = [n.split(": ", 1)[1] for n in state.get("vision_notes") or []
                 if n.startswith(f"slide {c['slide']}: ")]
        if found:
            bad[c["slide"]] = bad.get(c["slide"], []) + found
    escalate = {v.get("slide") for v in verdicts if v.get("redo_with") == "custom_drawing"}
    rnd = state.get("edit_round") or 0
    tasks = {t["id"]: t for t in state.get("edit_tasks") or []}
    redo = []
    if rnd < s.revision_rounds:
        for c in shown:
            if c["slide"] in bad and c["task"] in tasks:
                t = tasks[c["task"]]
                kind = "redesign" if t["kind"] == "add" else t["kind"]
                if kind == "redesign" and c["slide"] in escalate:
                    kind = "draw"          # the building blocks were not enough: draw it freely
                redo.append({"task": {**t, "kind": kind, "slide": c["slide"], "round": rnd + 1,
                                      "was": c.get("was")},
                             "feedback": bad[c["slide"]]})
    leftover = [f"slide {k}: {p}" for k, ps in bad.items() for p in ps] if not redo else []
    log(f"  review: {len(bad)} of {len(shown)} changed slide(s) need another look"
        + (f"; revising {len(redo)}" if redo else ""))
    return {"revise": redo, "edit_round": rnd + 1 if redo else rnd,
            "vision_notes": _notes_after_review(state.get("vision_notes") or [], leftover,
                                                {r["task"]["slide"] for r in redo}),
            "trace": [step("review", looked=len(shown), unhappy=len(bad), revising=len(redo))]}


def _before_pictures(state: DeckState, numbers: list) -> dict:
    """{original slide number: PNG} of the deck the request started from, for comparison."""
    from ..nodes.common import workspace
    from ..sandbox.render import render

    wanted = sorted({n for n in numbers if isinstance(n, int)})
    if not wanted or not state.get("work_base"):
        return {}
    with workspace(state) as ws:
        base = ws.fetch(state["work_base"], "review-base.pptx")
        visible = visible_copy(base, ws.path("review-base-visible.pptx"))
        result = render(visible, ws.dir("review-before"), [n - 1 for n in wanted])
        if not result.ok:
            return {}
        return {int(p.stem.split("-")[1]): p.read_bytes() for p in result.previews}


def _notes_after_review(notes: list[str], leftover: list[str], redone: set[int]) -> list[str]:
    """Notes about slides going round again are dropped (the second pass writes its own); the
    reviewer's leftovers are added once."""
    kept = [n for n in notes if not any(n.startswith(f"slide {k}: ") for k in redone)]
    return kept + [n for n in leftover if n not in kept]


def after_review(state: DeckState):
    """A second pass for the slides the reviewer was unhappy with, or delivery."""
    redo = state.get("revise") or []
    if not redo:
        return "deliver"
    deck = current_deck(state)
    return [Send("work", _payload(state, r["task"], deck, r["feedback"])) for r in redo]
