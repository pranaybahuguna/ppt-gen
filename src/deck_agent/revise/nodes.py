"""The edit route as graph nodes: split -> work (one per task, in parallel) -> join -> review.

    split    one model call turns the request into independent per-slide tasks
    work     a node instance per task, run in parallel by LangGraph (Send): redesign/add write a
             spec, tweak writes code; each tries, checks and retries on its own slide
    join     applies every result to the deck in one pass (slide surgery, then the tweaks' code
             in the sandbox), publishes the new version and previews the changed slides
    review   a vision model looks at the changed slides next to what was asked; slides it is
             unhappy with go back to `work` with its notes (up to revision_rounds times)
    audit    the whole deck once more, across slides: code fixes chart formats, chart colours and
             title sizes; a model checks units and numbers against the facts; slides with
             findings go back to `work` once, the rest are warnings. Then deliver.

Called by: LangGraph, registered in graph.build_graph.
"""
from __future__ import annotations

import re
from pathlib import Path

from langgraph.types import Send
from pptx import Presentation

from ..settings import settings
from ..state import CLEAR, DeckState, current_deck, step
from .read import deck_outline
from .work import visible_copy

KINDS = {"redesign", "draw", "tweak", "add", "delete", "move", "align_titles", "deck"}
MODEL_KINDS = {"redesign", "draw", "tweak", "add", "deck"}
CODE_KINDS = {"tweak", "draw"}                 # one slide, model-written code in the sandbox
DECK_KINDS = {"align_titles", "deck"}          # one rule for many slides (content slides by default)


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
    content = [o["slide"] for o in outline if o["role"] == "content"]
    tasks, dropped = [], []
    for t in answer.get("tasks") or []:
        kind, slide = t.get("kind"), t.get("slide")
        if kind in DECK_KINDS:
            # A deck-wide rule: the slides named, or else every content slide - never the cover,
            # the section dividers or an agenda unless the request asked for them.
            slides = sorted({s for s in t.get("slides") or [] if isinstance(s, int) and 1 <= s <= n}) or content
            if not slides:
                dropped.append(t)
                continue
            tasks.append({"id": f"t{len(tasks) + 1}", "kind": kind, "slide": slides[0], "slides": slides,
                          "instruction": str(t.get("instruction") or state.get("instruction") or ""),
                          "round": 0, "left_alone": [o["slide"] for o in outline if o["slide"] not in slides]})
            continue
        ok_slide = isinstance(slide, int) and (0 <= slide <= n if kind == "add" else 1 <= slide <= n)
        if kind not in KINDS or not ok_slide:
            dropped.append(t)
            continue
        tasks.append({"id": f"t{len(tasks) + 1}", "kind": kind, "slide": slide,
                      "after": t.get("after"), "instruction": str(t.get("instruction") or ""), "round": 0})
    for t in tasks:                           # the person's own words travel with every task
        t["person_asked"] = state.get("instruction") or ""
    if not tasks:
        return {"route": "decline",
                "route_reason": "I could not turn that into a change to any slide of this deck"
                                + (f" ({len(dropped)} task(s) named slides that do not exist)" if dropped else ""),
                "trace": [step("split", outcome="nothing to do")]}
    log(f"  split: {len(tasks)} task(s): " + ", ".join(
        f"{t['kind']} {t['slides'] if t.get('slides') else t['slide']}" for t in tasks))
    from ..design.profile import palette_asked

    palette = answer.get("palette") if answer.get("palette") in ("brand", "extended") else \
        palette_asked(state.get("instruction") or "", state.get("palette"))
    return {"edit_tasks": tasks, "work_base": uri, "edit_round": 0, "task_results": CLEAR,
            "palette": palette,
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
            "deck": deck, "feedback": feedback, "palette": state.get("palette")}


# --------------------------------------------------------------------------- work

def work(payload: dict, planner=None, log=print) -> dict:
    """Do one task. Runs as many times at once as there are tasks (LangGraph Send).

    Reads: the Send payload {thread_id, run_id, task, deck, feedback}.
    Writes: task_results (+1).
    Next: join, once every task of the round has finished.
    """
    from ..design.profile import extract_profile, with_palette
    from ..nodes.common import workspace
    from . import work as w

    task = payload["task"]
    with workspace(payload) as ws:
        deck = ws.fetch(payload["deck"], f"work-{task['id']}-r{task.get('round', 0)}.pptx")
        P = with_palette(extract_profile(deck), payload.get("palette"))
        scratch = Path(ws.dir(f"work-{task['id']}-r{task.get('round', 0)}"))
        do = w.tweak if task["kind"] in CODE_KINDS else w.deck_change if task["kind"] == "deck" else w.redesign
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
    from ..design.profile import extract_profile, with_palette
    from ..engine.deck import reseal
    from ..nodes.common import make_previews, publish_version, workspace
    from ..sandbox.code import run_builder
    from .surgery import apply_changes

    rnd = state.get("edit_round") or 0
    results = [r for r in state.get("task_results") or [] if r.get("round", 0) == rnd]
    tasks = state.get("edit_tasks") or []
    base_uri = state["work_base"] if rnd == 0 else current_deck(state)
    changes, codes, changelog, errors, house_notes = [], [], [], [], []
    for r in results:
        if not r["ok"]:
            errors.append({"kind": "task", "message": f"slide {r['slide']} ({r['kind']}): not changed: "
                                                      + "; ".join(r["problems"][:2])})
            continue
        if r["kind"] == "redesign":
            changes.append({"op": "replace", "slide": r["slide"], "spec": r["spec"], "task": r})
        elif r["kind"] == "add":
            changes.append({"op": "insert", "after": r["slide"], "spec": r["spec"], "task": r})
        elif r["kind"] in CODE_KINDS or r["kind"] == "deck":
            codes.append(r)
    if rnd == 0:
        for t in tasks:
            if t["kind"] == "delete":
                changes.append({"op": "delete", "slide": t["slide"]})
            elif t["kind"] == "move":
                changes.append({"op": "move", "slide": t["slide"], "after": t.get("after") or 0})

    with workspace(state) as ws:
        deck = ws.fetch(base_uri, f"join-base-r{rnd}.pptx")
        P = with_palette(extract_profile(deck), state.get("palette"))
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
        current = out
        # House rules first (no model): the same fixed code on every slide they apply to.
        for t in (tasks if rnd == 0 else []):
            if t["kind"] != "align_titles":
                continue
            from .house import align_titles

            targets = [position[s] for s in t["slides"] if position.get(s)]
            nxt = ws.path(f"join-r{rnd}-{t['id']}.pptx")
            house = align_titles(current, nxt, P, targets)
            current = nxt
            house_notes.extend(house["notes"])
            left = [position[s] for s in t.get("left_alone") or [] if position.get(s)]
            changelog.append({"slide": None, "change": f"titles aligned and ruled on slides {house['changed']}"
                              + (f"; left alone: {left}" if left else "")})
        # Model-written code: replay each task's working code on the deck as it is now, at the
        # slides' new positions, and reseal so every other slide keeps its bytes.
        for k, r in enumerate(codes):
            if r["kind"] == "deck":
                targets = [position[s] for s in r["slides"] if position.get(s)] if rnd == 0 else r["slides"]
                if not targets:
                    continue
                res = run_builder(r["code"], {**r["ctx"], "slides": [t - 1 for t in targets]}, current)
                if not res.ok:
                    errors.append({"kind": "task", "message": f"slides {targets} (deck): the change did not "
                                                              f"apply to the merged deck: {res.error}"})
                    continue
                nxt = ws.path(f"join-r{rnd}-t{k}.pptx")
                reseal(current, res.deck, nxt)
                current = nxt
                # The review looks at a sample of the slides, not all of them.
                sample = sorted({targets[0], targets[len(targets) // 2], targets[-1]})
                origin = dict(zip(targets, r["slides"])) if rnd == 0 else {}
                for sl in sample:
                    changed.append({"task": r["id"], "kind": "deck", "instruction": r["instruction"],
                                    "slide": sl, "was": origin.get(sl), "all_slides": targets})
                changelog.append({"slide": None, "change": f"slides {targets}: " + (r.get("changes") or r["instruction"])})
                continue
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
    notes = [f"slide {r['slide']}: {p}" for r in results if r["ok"] for p in r.get("problems") or []] + house_notes
    # A task that could not be done is a warning while others were done; only an edit that
    # changed nothing at all is an error.
    if changed or changelog or rnd > 0:
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
            entry = {"slide": c["slide"], "person_asked": state.get("instruction") or "",
                     "task": c["instruction"],
                     "made_with": "code" if c["kind"] in CODE_KINDS | {"deck"} else "pieces"}
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
    # A change nobody can see is not done, whatever the reviewer thought: plain code, no model.
    for c in shown:
        if before.get(c.get("was")) and barely_changed(before[c["was"]], store().get_bytes(by_page[c["slide"]])):
            bad[c["slide"]] = bad.get(c["slide"], []) + [UNCHANGED]
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
                if t["kind"] == "deck":
                    # One task for many slides goes round once, with every sample's notes,
                    # applied to the slides at their positions now.
                    same = next((r for r in redo if r["task"]["id"] == t["id"]), None)
                    if same:
                        same["feedback"] += [f"slide {c['slide']}: {p}" for p in bad[c["slide"]]]
                    else:
                        redo.append({"task": {**t, "slides": c["all_slides"], "slide": c["all_slides"][0],
                                              "round": rnd + 1},
                                     "feedback": [f"slide {c['slide']}: {p}" for p in bad[c["slide"]]]})
                    continue
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
                                                {sl for r in redo for sl in (r["task"].get("slides") or [r["task"]["slide"]])}),
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


UNCHANGED = ("nothing visible changed on this slide - the change did not do what was asked. Look at "
             "the picture and change what the person is pointing at")


def barely_changed(before: bytes, after: bytes, pixels: int = 50) -> bool:
    """Whether two renders of a slide are the same picture (fewer than `pixels` pixels differ).

    Renders of an unchanged slide are identical, while even a one-word edit changes hundreds of
    pixels, so this catches "the code ran but nothing shows" without flagging small real edits.
    Called by: review.
    """
    import io

    from PIL import Image, ImageChops

    a = Image.open(io.BytesIO(before)).convert("L")
    b = Image.open(io.BytesIO(after)).convert("L")
    if a.size != b.size:
        return False
    hist = ImageChops.difference(a, b).histogram()
    return sum(hist[24:]) < pixels


def _notes_after_review(notes: list[str], leftover: list[str], redone: set[int]) -> list[str]:
    """Notes about slides going round again are dropped (the second pass writes its own); the
    reviewer's leftovers are added once."""
    kept = [n for n in notes if not any(n.startswith(f"slide {k}: ") for k in redone)]
    return kept + [n for n in leftover if n not in kept]


def after_review(state: DeckState):
    """Another pass for the slides the reviewer was unhappy with, or the deck check."""
    redo = state.get("revise") or []
    if not redo:
        return "audit"
    deck = current_deck(state)
    return [Send("work", _payload(state, r["task"], deck, r["feedback"])) for r in redo]


# --------------------------------------------------------------------------- audit

def _facts(state: DeckState, limit: int = 20000) -> list:
    """The source facts the deck was built from, as many as fit `limit` characters."""
    import json

    out, size = [], 0
    for f in (state.get("content_pack") or {}).get("facts") or []:
        size += len(json.dumps(f, ensure_ascii=False))
        if size > limit:
            break
        out.append(f)
    return out


def audit(state: DeckState, planner=None, log=print) -> dict:
    """The deck check: the whole deck once more, across slides, after review is done with it.

    Plain code fixes what has one right answer (revise.audit.fix_deck: percent formats, chart
    colours, title sizes) and publishes the result. Plain code and - once per request - one
    text-only model call find the rest (small text; units, axis titles, numbers against the
    facts). Each slide with findings goes back through `work` as a tweak, once per request,
    while revision rounds are left; otherwise the findings are warnings.

    Reads: deck_versions, palette, content_pack, instruction, edit_round, edit_tasks, audited.
    Writes: deck_versions (+1 when it fixed something), changelog, previews, vision_notes;
            revise, edit_tasks, edit_round when slides go back.
    Next: after_audit -> work | deliver.
    """
    from ..design.profile import extract_profile, with_palette
    from ..engine import bootstrap
    from ..nodes.common import make_previews, publish_version, workspace
    from . import audit as A

    s = settings()
    uri = current_deck(state)
    if not s.audit_deck or not uri:
        return {"revise": [], "trace": [step("audit", outcome="skipped")]}
    bootstrap()
    from deckops.executor import save_preserving

    rnd = state.get("edit_round") or 0
    model_pass = not state.get("audited") and hasattr(planner, "audit_deck")
    notes = []
    out: dict = {}
    with workspace(state) as ws:
        deck = ws.fetch(uri, f"audit-r{rnd}.pptx")
        P = with_palette(extract_profile(deck), state.get("palette"))
        prs = Presentation(str(deck))
        roles = {o["slide"]: o["role"] for o in deck_outline(prs)}
        content = [n for n, r in roles.items() if r == "content"]
        fixed = A.fix_deck(prs, P, content, list(roles))
        found = A.text_findings(prs, content, P)
        if model_pass:
            try:
                answer = planner.audit_deck({"person_asked": state.get("instruction") or "",
                                             "slides": A.inventory(prs, roles), "facts": _facts(state)})
            except Exception as e:            # a check that could not run is a warning, not a failure
                answer = {}
                notes.append(f"audit: the deck check's model call failed ({str(e)[:120]})")
            for f in (answer or {}).get("findings") or []:
                n = f.get("slide")
                if isinstance(n, int) and 1 <= n <= len(prs.slides) and (f.get("fix") or f.get("problem")):
                    text = (f.get("problem") or "").strip()
                    if f.get("fix"):
                        text = (text + " - " if text else "") + "fix: " + f["fix"].strip()
                    found.setdefault(n, []).append(text)
        if fixed:
            save_preserving(prs, str(deck), str(ws.path("audited.pptx")))
            new_uri, versions = publish_version(state, ws, "audited.pptx")
            touched = sorted({int(m.group(1)) for line in fixed if (m := re.match(r"slide (\d+):", line))})
            seen = []
            for u in state.get("previews") or []:
                try:
                    seen.append(int(u.rsplit("/", 1)[-1].split("-")[1].split(".")[0]))
                except (IndexError, ValueError):
                    continue
            pages = sorted({p for p in seen} | {n - 1 for n in touched})
            visible = visible_copy(ws.path("audited.pptx"), ws.path("audited-visible.pptx"))
            previews, _ = make_previews(state, ws, visible, pages or None)
            out.update({"deck_versions": versions, "previews": previews or state.get("previews"),
                        "changelog": (state.get("changelog") or [])
                        + [{"slide": None, "change": f"deck check: {line}"} for line in fixed]})
            log(f"  audit: fixed {len(fixed)} thing(s) by code -> {new_uri}")
    # Each slide goes back once per request: a second miss is reported, not chased.
    tasks = state.get("edit_tasks") or []
    sent = {t["slide"] for t in tasks if str(t.get("id", "")).startswith("a")}
    back, report = [], []
    for n, problems in sorted(found.items()):
        if rnd < s.revision_rounds and n not in sent:
            back.append({"id": f"a{n}", "kind": "tweak", "slide": n, "round": rnd + 1, "was": None,
                         "instruction": "The deck check found this on the slide; fix it: " + "; ".join(problems),
                         "person_asked": state.get("instruction") or ""})
        else:
            report += [f"audit: slide {n}: {p}" for p in problems]
    log(f"  audit: {sum(len(v) for v in found.values())} finding(s) on {len(found)} slide(s)"
        + (f"; sending {len(back)} back" if back else ""))
    out.update({"revise": [{"task": t, "feedback": None} for t in back], "audited": True,
                "vision_notes": (state.get("vision_notes") or []) + notes
                + [r for r in report if r not in (state.get("vision_notes") or [])],
                "trace": [step("audit", fixed=len(fixed), findings=len(found), sent_back=len(back),
                               model_pass=model_pass)]})
    if back:
        out.update({"edit_tasks": tasks + back, "edit_round": rnd + 1})
    return out


def after_audit(state: DeckState):
    """The slides the deck check sent back go through `work` again; otherwise deliver."""
    redo = state.get("revise") or []
    if not redo:
        return "deliver"
    deck = current_deck(state)
    return [Send("work", _payload(state, r["task"], deck, r["feedback"])) for r in redo]
