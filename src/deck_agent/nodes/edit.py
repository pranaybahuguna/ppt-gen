"""The edit route: change a deck someone already has.

    edit_plan   show the model the deck's text with addresses, get back typed actions
    edit_apply  compile the actions to ops, apply them, insert any new slides, check

What the model sees is deliberately thin: for each slide, each text box, each paragraph's
address and its current words. No geometry, no ids, no XML. What it returns is a short list of
actions - change this text, delete that slide, re-lay out these cards, insert a slide here -
and plain code turns those into the engine's ops.

Inserting a slide uses the deck as its own template: the engine's rule-based kit drafting is
run over the deck being edited, so a new slide is a *copy of one of that deck's own slides*,
filled with new words. Nothing about the look is invented. The copy lands at the end, because
the op language cannot position a copied slide, so the node moves it afterwards - a reorder of
the slide list and nothing else.

Every text change carries a fingerprint. If the deck changed between planning and applying,
the engine refuses the whole batch rather than apply half of it.
"""
from __future__ import annotations

from ..engine import bootstrap
from ..engine.build import repair_feedback
from ..engine.deck import Baseline, apply_ops, changelog, check, move_slide, preserved_count, probe
from ..engine.edits import compile_edit, deck_view
from ..engine.style import apply_styles, apply_theme_colours
from ..engine.tidy import remove_orphans, renumber_pages
from ..errors import PlanInvalid
from ..settings import settings
from ..state import DeckState, current_deck, step
from .common import make_previews, publish_version, workspace


def edit_plan(state: DeckState, planner=None, log=print) -> dict:
    base = current_deck(state) or state.get("source_deck")
    if not base:
        return {"route": "decline", "route_reason": "there is no deck here to edit.",
                "trace": [step("edit_plan", outcome="no deck")]}

    with workspace(state) as ws:
        deck = ws.fetch(base, "source.pptx")
        index = probe(deck)
        numbers = state.get("slide_scope") or []
        scope = {n - 1 for n in numbers if 1 <= n <= len(index["slides"])} or None
        view = deck_view(index, scope)
        answer = planner.plan_edit(state.get("instruction") or "", view,
                                   {"slides": len(index["slides"])}) or {}
        actions = answer.get("actions") or []
        ws.publish_json({"view": view, "answer": answer}, "edit_plan.json")

    if not actions:
        why = answer.get("cannot") or "I could not turn that into a change I know how to make."
        return {"route": "decline", "route_reason": why,
                "trace": [step("edit_plan", outcome="no actions")]}
    log(f"  edit: {len(actions)} actions ({sorted({a.get('action') for a in actions})})")
    return {"edit_actions": actions, "work_base": base,
            "trace": [step("edit_plan", actions=len(actions),
                           kinds=sorted({a.get("action") for a in actions}))]}


def edit_apply(state: DeckState, planner=None, log=print) -> dict:
    with workspace(state) as ws:
        base = ws.fetch(state["work_base"], "source.pptx")
        index = probe(base)
        try:
            ops, inserts, visual, problems = compile_edit(state["edit_actions"], index)
        except PlanInvalid as e:
            return {"errors": [{"kind": e.kind, "message": str(e), "detail": e.detail}],
                    "route": "decline",
                    "route_reason": "none of those edits could be applied: "
                                    + "; ".join((e.detail or [])[:2]),
                    "trace": [step("edit_apply", outcome="nothing applicable")]}

        touched = (_touched_slides(ops, index) | _styled_slides(visual, index)
                   | _relaid_slides(visual))
        baseline = Baseline.of(base, touched or None)

        current = base
        applied = 0
        if ops:
            ex = apply_ops(base, ops, ws.path("edited.pptx"))
            applied = ex["ops_applied"]
            current = ws.path("edited.pptx")
            log(f"  applied {applied} ops, {preserved_count(ex)} parts byte-identical")

        # Visual changes have no op in the engine's language, so they are their own
        # deterministic pass (engine/style.py), applied after the text edits and before any
        # inserted slide, so a new slide inherits the deck's new look rather than the old one.
        visual_report = []
        if visual["styles"]:
            styled = ws.path("styled.pptx")
            report = apply_styles(current, visual["styles"], styled)
            current = styled
            visual_report += report["applied"]
            log(f"  style: {'; '.join(a['change'] for a in report['applied'])[:150]}")
        if visual["theme"]:
            themed = ws.path("themed.pptx")
            report = apply_theme_colours(current, visual["theme"], themed)
            current = themed
            note = {"change": "changed the deck's theme colours",
                    "theme": report["theme"]}
            if report["shapes_pinned_to_the_old_colour"]:
                note["note"] = (f"{report['shapes_pinned_to_the_old_colour']} shape(s) had that "
                                "colour typed in rather than inherited, so they did not change")
                problems.append(note["note"])
            visual_report.append(note)
            log(f"  theme: {report['theme']} "
                f"({report['shapes_pinned_to_the_old_colour']} shapes pinned to the old colour)")

        inserted = []
        if inserts:
            current, inserted, insert_problems = _insert_slides(ws, current, inserts, planner, log)
            problems = list(problems) + insert_problems

        relaid, new_plan = [], None
        local_relayout = False
        if visual.get("relayouts"):
            current, relaid, relayout_problems, new_plan = _change_layouts(
                ws, current, visual["relayouts"], state, planner, log)
            problems = list(problems) + relayout_problems
            # A layout taken from the deck is described by the deck's own field names, not the
            # template's, so the plan can no longer be rebuilt against the template.
            local_relayout = any(c.get("how") == "used another layout from this deck"
                                 for c in relaid)

        # Anything that changed how many slides there are, or which slide is where, leaves
        # every "n / total" footer stale - including the one the compiler just wrote on a
        # rebuilt slide from a one-slide plan, which would otherwise read "1 / 1".
        final = ws.path("out.pptx")
        pages = {"renumbered": 0, "without_page_number": []}
        if inserted or relaid:
            pages = renumber_pages(current, final)
        elif str(current) != str(final):
            import shutil
            shutil.copy(current, final)
        else:
            import shutil
            shutil.copy(current, final)

        if relaid:
            touched |= {c["slide"] - 1 for c in relaid}

        # Nothing changed, so there is nothing to publish. A version that is identical to the
        # one before it is noise: it makes `undo` take two goes and tells the person their deck
        # moved when it did not.
        if not (applied or inserted or relaid or visual_report):
            return {
                "errors": [{"kind": "nothing_applied",
                            "message": "none of those edits could be made",
                            "detail": problems}] if problems else [],
                "route": "decline" if problems else state.get("route"),
                "route_reason": problems[0] if problems else None,
                "trace": [step("edit_apply", outcome="nothing changed",
                               problems=problems or None)],
            }

        report = baseline.filter(check(final, touched or None))
        uri, versions = publish_version(state, ws, "out.pptx")
        previews, why_not = make_previews(state, ws, final,
                                          sorted(touched | {i for i, _ in inserted}) or None)
        changes = (changelog(index, ops) + visual_report + relaid
                   + [{"change": "added a slide", "after": after} for _, after in inserted])
        out: dict = {
            "deck_versions": versions, "previews": previews,
            "findings": report["findings"], "structural": report["structural"], "leaked": [],
            "changelog": changes,
            "errors": ([{"kind": "partly_applied", "message": "some of the requested edits "
                         "could not be made", "detail": problems}] if problems else []),
            "trace": [step("edit_apply", ops=applied, inserted=len(inserted),
                           relaid_out=len(relaid), pages_renumbered=pages["renumbered"],
                           findings=len(report["findings"]), previews=len(previews),
                           previews_skipped=why_not, problems=problems or None)],
        }
        diverged = []
        if applied:
            diverged.append(f"{applied} text edit(s) made directly on the deck")
        if visual_report:
            diverged.append("colour or type changes made directly on the deck")
        if inserted:
            diverged.append(f"{len(inserted)} slide(s) inserted from the deck's own layouts")
        if local_relayout:
            diverged.append("a slide re-laid out using the deck's own layouts")
        if diverged:
            out["plan_divergence"] = diverged

        if new_plan:
            # The deck's record of itself has to follow the deck, or a second "lay this out
            # differently" would work from the layout the slide no longer uses.
            out["deck_plan"] = new_plan
        return out


def _relaid_slides(visual: dict) -> set[int]:
    return {r["slide"] - 1 for r in visual.get("relayouts") or [] if r.get("slide")}


def _styled_slides(visual: dict, index: dict) -> set[int]:
    """Which slides a visual change could have altered. A theme change touches all of them."""
    if visual.get("theme"):
        return set(range(len(index["slides"])))
    by_ref = {s["ref"]: i for i, s in enumerate(index["slides"])}
    out: set[int] = set()
    for change in visual.get("styles", []):
        if change.scope == "deck":
            return set(range(len(index["slides"])))
        if change.scope == "slide" and change.slide:
            out.add(change.slide - 1)
        elif change.ref:
            slide_ref = change.ref.split("/")[0]
            if slide_ref in by_ref:
                out.add(by_ref[slide_ref])
    return out


def _touched_slides(ops: list[dict], index: dict) -> set[int]:
    """Which slides an op batch affects, so the checks look only there."""
    by_ref = {s["ref"]: i for i, s in enumerate(index["slides"])}
    out: set[int] = set()
    for op in ops:
        target = op.get("target") or ""
        slide_ref = target.split("/")[0]
        if slide_ref in by_ref:
            out.add(by_ref[slide_ref])
    return out


def _insert_slides(ws, deck, inserts: list[dict], planner, log):
    """Build each new slide from the deck's own slides, then move it into position.

    -> (the deck, what was inserted, problems worth telling the caller about)

    The kit here is the *light* one (draft and label, no acceptance test, no capacity
    measurement): a single inserted slide does not justify building test decks, and the result
    is checked by the same rules as any other slide anyway.
    """
    bootstrap()
    from controlplane import slide_flow

    from ..engine.kitbuild import light_kit
    from ..engine.build import compile_plan

    kit, _ = light_kit(deck, planner, log)
    problems: list[str] = []
    if not kit["patterns"]:
        log("  insert: this deck has no slide that can be reused as a layout")
        return deck, [], ["this deck has no slide whose layout I can safely reuse for a new one"]

    index = probe(deck)
    inserted = []
    current = deck
    existing = {_flat(x) for s in index["slides"] for x in _slide_texts(s)}
    for k, spec in enumerate(sorted(inserts, key=lambda s: s["after"])):
        content = spec["content"]

        # "I don't see a title slide" on a deck whose first slide *is* the title slide, only
        # laid out badly, made the model add a second slide with the same heading. Two
        # identical headings is a worse answer than saying what actually happened.
        title = _flat(content.get("title", ""))
        if title and title in existing:
            problems.append(
                f"slide {_slide_number_with(index, title)} already carries the heading "
                f"{content.get('title')!r}, so adding another would duplicate it. If it does "
                "not look the way you want, ask me to change that slide's layout instead.")
            continue
        job = {"content": content,
               "options": slide_flow.options_for(content, kit, 2, max(3, len(index["slides"])))}
        if not job["options"]:
            log(f"  insert: nothing in this deck can hold the new slide after {spec['after']}")
            problems.append(f"no slide in this deck has a layout that fits the new slide "
                            f"after slide {spec['after']}")
            continue
        ctx = {"deck_title": "", "slide": spec["after"] + 1, "of": len(index["slides"]) + 1}

        # Build, check, and if the new slide does not hold its text, send the reason back and
        # try again - the same character-budget loop the create route uses. An inserted slide
        # gets the same standard as a built one; without this, a slide whose text overflows is
        # delivered with a warning nobody asked for and nobody can act on.
        slide, _attempts = slide_flow.render_one(planner, job, ctx, index, kit, {},
                                                 settings().render_retries, log=log)
        placed = False
        feedback = None
        for attempt in range(settings().max_repair_rounds + 1):
            if slide is None:
                break
            # `remove_orphans` runs here too, not only at create time: a layout's decorative
            # panel whose fields are all empty is left behind otherwise, and an inserted title
            # slide came out as a large empty grey panel with a label in the corner.
            one = {"slides": [slide]}
            ops, _ = remove_orphans(compile_plan(one, index, kit, {}), one, index, kit)
            ops = [o for o in ops if o["op"] == "add_slide_from_pattern"]
            step_out = ws.path(f"inserted-{k}-{attempt}.pptx")
            apply_ops(current, ops, step_out)
            total = len(probe(step_out)["slides"])
            target = min(spec["after"], total - 1)
            moved = ws.path(f"moved-{k}-{attempt}.pptx")
            move_slide(step_out, total - 1, target, moved)

            report = check(moved, {target})
            seen = _look_at_one(moved, target, planner, log) if not report["findings"] else []
            if not report["findings"] and not seen:
                current, index = moved, probe(moved)
                inserted.append((target, spec["after"]))
                log(f"  insert: new slide placed after slide {spec['after']}")
                placed = True
                break
            feedback = (list(repair_feedback(report["findings"], probe(moved)).get(target, []))
                        + seen)
            log(f"  insert: the new slide does not fit ({len(report['findings'])} problem(s)); "
                f"asking again" if attempt < settings().max_repair_rounds else
                f"  insert: the new slide still does not fit after {attempt + 1} attempts")
            if attempt == settings().max_repair_rounds:
                break
            slide, _attempts = slide_flow.render_one(planner, job, ctx, index, kit, {},
                                                     settings().render_retries, feedback=feedback,
                                                     previous=slide, log=log)

        if not placed:
            # Better to say the slide could not be added than to hand over a broken one. The
            # deck is left exactly as it was before this insert was attempted.
            problems.append(f"I could not add the slide after {spec['after']} in a way that fits "
                            f"this deck's layout" + (f": {feedback[0]}" if feedback else ""))
            log(f"  insert: giving up on the slide after {spec['after']}; the deck is unchanged")
    return current, inserted, problems


def after_edit(state: DeckState) -> str:
    if state.get("route") in ("ask", "decline"):
        return state["route"]
    return "deliver"


def _flat(text) -> str:
    if isinstance(text, list):
        text = " ".join(text)
    return " ".join(str(text or "").split()).strip().lower()


def _slide_number_with(index: dict, flat_title: str) -> int:
    for i, slide in enumerate(index["slides"], start=1):
        if any(_flat(x) == flat_title for x in _slide_texts(slide)):
            return i
    return 0


def _look_at_one(path, position: int, planner, log) -> list[str]:
    """Render one slide and ask a vision model what is wrong with it. -> reasons to try again.

    The geometric checks answer "does the text fit". They are happy with a slide that fits
    perfectly and looks empty - which is exactly what an inserted title slide came out as: a
    large grey panel with a label in the corner and nothing else. Nobody's arithmetic catches
    that; looking at it does.

    Only high-severity problems come back, and only when `vision_review` is on. A reviewer that
    cannot be reached returns nothing, because a slide that was built correctly must not be
    rejected because a model endpoint was down.
    """
    if not settings().vision_review or not getattr(planner, "review_slide", None):
        return []
    import tempfile

    from ..sandbox.render import render
    from .look import review_previews

    with tempfile.TemporaryDirectory(dir=settings().workspace_root) as tmp:
        shots = render(path, tmp, [position])
        if not shots.ok or not shots.previews:
            return []
        findings, _ = review_previews(shots.previews, planner, log=lambda *a, **k: None)
    reasons = [f"looking at the slide: {f['what']}"
               + (f" ({f['where']})" if f.get("where") else "") for f in findings]
    if reasons:
        log(f"  look: the new slide was rejected - {reasons[0][:96]}")
    return reasons


def _slide_texts(slide: dict) -> list[str]:
    """Every line of text on a slide, longest first - used to find it in the plan."""
    out = []
    for shape in slide["shapes"]:
        text = " ".join(p["text"] for p in shape.get("paras", [])).strip()
        if text:
            out.append(text)
    return sorted(out, key=len, reverse=True)


def _change_layouts(ws, deck, relayouts: list[dict], state, planner, log):
    """Put a slide's own words into a *different* layout from the same template.

    This is the answer to "this slide looks repetitive, lay it out differently". It needs two
    things, from two different places, and getting that wrong is an easy mistake:

    * **the words** come from the plan that built the deck (`deck_plan`), which says exactly
      what each slide holds - title, items, headings, bodies. Nothing is reworded and nothing
      is guessed at from the finished slide.
    * **the layouts** come from *this deck*, drafted with `light_kit`, not from the template's
      kit. That is not a shortcut, it is the only thing that can work: the template's kit
      addresses shapes by id on the template's own slides, and `add_slide_from_pattern` copies
      a slide *within one file*. Compiling the template's kit against an edited deck asks it to
      copy a slide that is not there, and fails with "this slide has no shape 14".

    It also gives the right meaning. "Lay this out differently" becomes "lay it out like
    another slide in this deck", so the result is a layout the deck already uses.

    The slide is rebuilt, not patched: the new slide is added, the old one deleted in the same
    batch, and the new one moved into the old one's place. If it does not pass the checks, the
    reason goes back and it is tried again; if it still fails, the slide is left exactly as it
    was. A slide that looked repetitive is better than one that is broken.

    -> (deck, what changed, problems, the updated plan or None)
    """
    from ..engine import bootstrap
    from ..engine.build import compile_plan, repair_feedback
    from ..engine.tidy import remove_orphans
    from ..engine.deck import new_slide_index
    from ..engine.fallback import relaxed_options
    from ..engine.edits import content_from_plan_slide, match_plan_slide
    from ..infra.storage import store

    bootstrap()
    from controlplane import slide_flow

    from ..engine.kitbuild import light_kit

    # First choice: rebuild from the plan, using the *template's* whole kit. A deck only
    # contains the layouts it happened to use, so "make this a title slide" can be impossible
    # on the deck itself while the template has a cover sitting right there.
    can, why_not = _can_rebuild(state)
    if can:
        rebuilt, changed, problems, updated = _rebuild_with_layouts(
            ws, relayouts, state, planner, log)
        if rebuilt is not None:
            return rebuilt, changed, problems, updated
        log("  layout: the template's layouts gave nothing; trying the deck's own")
    else:
        log(f"  layout: rebuilding is not possible ({why_not}); using the deck's own layouts")

    if not state.get("deck_plan"):
        return deck, [], ["I can only change the layout of a slide whose deck I built in this "
                          "session - I need the record of what each slide holds, and for a deck "
                          "that arrived from somewhere else I do not have one"], None

    plan = store().get_json(state["deck_plan"])
    kit, _ = light_kit(deck, planner, log)
    if not kit["patterns"]:
        return deck, [], ["this deck has no other slide whose layout I could safely reuse"], None

    # Structure from the deck, capacity from the measurement.
    #
    # `light_kit` has to take each layout's item limit from however many items that slide
    # happens to show - three steps on a slide means "max 3". But these slides were built from
    # the template, whose kit *measured* the real limit by building test slides until the text
    # stopped fitting: the same steps layout holds twelve. Without this, a slide with four
    # points can only ever move to a layout that already shows four, and "lay this out
    # differently" answers "nothing else fits" on a deck with five perfectly good layouts.
    if state.get("kit"):
        measured = store().get_json(state["kit"]).get("patterns", {})
        for name, pattern in kit["patterns"].items():
            limits = (measured.get(name) or {}).get("items")
            if limits and "items" in pattern:
                pattern["items"]["min"] = min(pattern["items"].get("min", 1),
                                              limits.get("min", 1))
                pattern["items"]["max"] = max(pattern["items"].get("max", 1),
                                              limits.get("max", 1))
    content_pack = state.get("content_pack") or {}
    current, changed, problems = deck, [], []
    plan_changed = False

    for spec in relayouts:
        index = probe(current)
        n = spec["slide"]
        position = n - 1
        if not 0 <= position < len(index["slides"]):
            problems.append(f"this deck has no slide {n}")
            continue

        # Find this slide in the plan by what it says. Position cannot be used: a slide drawn
        # by generated code, or one inserted earlier, shifts everything after it.
        plan_index = plan_slide = None
        for text in _slide_texts(index["slides"][position]):
            plan_index, plan_slide = match_plan_slide(plan, text)
            if plan_slide is not None:
                break
        if plan_slide is None:
            problems.append(f"slide {n}: I do not have a record of how this slide was built, "
                            "so I cannot move its content to another layout")
            continue

        was = plan_slide.get("pattern")
        content = content_from_plan_slide(plan_slide)
        # Never offer the slide its own layout back: a pattern drafted *from* this slide would
        # copy it and change nothing.
        def elsewhere(offered):
            """Layouts that are genuinely *other*.

            Two exclusions, both needed. A pattern drafted from this very slide would copy the
            slide onto itself. And a pattern of the same kind as the one the slide already uses
            is drafted from a different slide but looks identical - it reported "panels ->
            panels" and changed nothing a reader would notice.
            """
            return [o for o in offered
                    if kit["patterns"][o["pattern"]]["slide"] != n
                    and o["pattern"].split("@")[0] != str(was).split("@")[0]]

        options = elsewhere(slide_flow.options_for(content, kit, n, len(index["slides"])))
        if not options:
            # The same relaxation the create route uses before it gives up. A kit drafted from
            # one deck has item counts taken from whatever its slides happen to show, so the
            # strict rules reject almost everything; a layout with no repeated items usually
            # holds the content perfectly well.
            relaxed, why = relaxed_options(content, kit, n, len(index["slides"]))
            options = elsewhere(relaxed)
            if options:
                log(f"  layout: slide {n}: no layout fits strictly; {why}")
        if not options:
            problems.append(f"slide {n}: no other slide in this deck has a layout that holds "
                            f"what is on it (it is laid out as {was!r})")
            continue

        job = {"content": content, "options": options}
        ctx = {"deck_title": plan.get("title", ""), "slide": n, "of": len(index["slides"])}
        feedback = None
        rebuilt = None
        for attempt in range(settings().max_repair_rounds + 1):
            candidate, _ = slide_flow.render_one(planner, job, ctx, index, kit, content_pack,
                                                 settings().render_retries, feedback=feedback,
                                                 previous=rebuilt, log=log)
            if candidate is None:
                break
            one = {"slides": [candidate]}
            built_ops, _ = remove_orphans(compile_plan(one, index, kit, content_pack),
                                          one, index, kit)
            ops = [o for o in built_ops if o["op"] == "add_slide_from_pattern"]
            ops.append({"op": "delete_slide", "target": index["slides"][position]["ref"]})
            built = ws.path(f"relayout-{n}-{attempt}.pptx")
            apply_ops(current, ops, built)

            after = probe(built)
            fresh = new_slide_index(index, after)
            if fresh is None:
                problems.append(f"slide {n}: the new layout produced no slide")
                break
            moved = ws.path(f"relayout-{n}-{attempt}-moved.pptx")
            move_slide(built, fresh, position, moved)

            report = check(moved, {position})
            seen = _look_at_one(moved, position, planner, log) if not report["findings"] else []
            if not report["findings"] and not seen:
                current = moved
                plan["slides"][plan_index] = candidate
                plan_changed = True
                changed.append({"change": "changed a slide's layout", "slide": n,
                                "from": was, "to": candidate["pattern"],
                                "why": spec.get("why"),
                                "how": "used another layout from this deck"})
                log(f"  layout: slide {n} moved from {was!r} to {candidate['pattern']!r}")
                rebuilt = None
                break
            feedback = (list(repair_feedback(report["findings"], probe(moved)).get(position, []))
                        + seen)
            rebuilt = candidate
            log(f"  layout: slide {n} as {candidate['pattern']!r} does not fit; asking again")
        else:
            problems.append(f"slide {n}: every other layout I tried had the same trouble, so I "
                            f"left it as {was!r}")

    updated = None
    if plan_changed:
        updated = ws.publish_json(plan, "plan.json")
    return current, changed, problems, updated


# --------------------------------------------------------------------------- rebuild from plan

def _can_rebuild(state) -> tuple[bool, str]:
    """Is the deck still exactly what its plan says it is? -> (yes/no, why not)

    Rebuilding gives back the template's *whole* kit - the proper cover, the closing slide, the
    measured capacities - instead of only the layouts this deck happens to contain. The price
    is that anything not written down in the plan is not rebuilt, so this asks first.
    """
    if not (state.get("deck_plan") and state.get("kit") and state.get("template")):
        return False, ("I did not build this deck from a template in this session, so I have no "
                       "plan to rebuild it from")
    diverged = state.get("plan_divergence") or []
    if diverged:
        return False, ("this deck has changes the plan does not describe (" +
                       "; ".join(diverged) + "), and rebuilding it would lose them")
    return True, ""


def _replay_code_slides(ws, deck, code_slides, planner, log):
    """Draw the generated slides again on a rebuilt deck, from the code that drew them.

    The code and the context it ran with were both kept for exactly this. Replaying beats
    re-generating: the slide a person already accepted is the slide they get back, and no model
    call is spent. If a replay fails the slide is simply missing, which is said out loud.
    """
    from ..engine.deck import new_slide_index, reseal
    from ..infra.storage import store
    from ..sandbox.code import run_builder

    current, replayed, problems = deck, [], []
    for entry in code_slides or []:
        if not entry.get("code") or not entry.get("ctx"):
            problems.append(f"the slide drawn by code at position {entry.get('slide')} could "
                            "not be put back: its source was not kept")
            continue
        code = store().get_bytes(entry["code"]).decode()
        ctx = store().get_json(entry["ctx"])
        result = run_builder(code, ctx, current)
        if not result.ok:
            problems.append(f"the slide drawn by code at position {entry.get('slide')} could "
                            f"not be drawn again: {result.error}")
            continue
        before = probe(current)
        sealed = ws.path(f"replay-{entry.get('slide')}.pptx")
        reseal(current, result.deck, sealed)
        after = probe(sealed)
        drawn = new_slide_index(before, after)
        if drawn is None:
            problems.append(f"the replayed code at position {entry.get('slide')} added no slide")
            continue
        target = min(entry.get("position", len(after["slides"]) - 1), len(after["slides"]) - 1)
        moved = ws.path(f"replay-{entry.get('slide')}-moved.pptx")
        move_slide(sealed, drawn, target, moved)
        current = moved
        replayed.append(target + 1)
        log(f"  rebuild: put the generated slide back at position {target + 1}")
    return current, replayed, problems


def _rebuild_with_layouts(ws, relayouts: list[dict], state, planner, log):
    """Re-plan a slide onto one of the *template's* layouts, then rebuild the deck.

    This is the answer to "the deck has no cover layout": it does not, but the template does,
    and the plan plus the template is enough to build the whole deck again. The slide's words
    are untouched; only which layout holds them changes.

    -> (deck, what changed, problems, the updated plan)
    """
    from ..engine import bootstrap
    from ..engine.build import baseline_for_kit, build_from_kit
    from ..engine.edits import content_from_plan_slide
    from ..engine.fallback import relaxed_options
    from ..infra.storage import store

    bootstrap()
    from controlplane import slide_flow

    kit = store().get_json(state["kit"])
    plan = store().get_json(state["deck_plan"])
    content_pack = state.get("content_pack") or {}
    template = ws.fetch(state["template"], "template.pptx")
    index = probe(template)
    total = len(plan.get("slides") or [])

    changed, problems = [], []
    for spec in relayouts:
        n = spec["slide"]
        # The plan's order is the deck's order, because a divergent deck never gets here.
        position = _plan_position(plan, state, n)
        if position is None:
            problems.append(f"slide {n}: I have no record of how this slide was built, so I "
                            "cannot move its content to another layout")
            continue
        plan_slide = plan["slides"][position]
        was = plan_slide.get("pattern")
        content = content_from_plan_slide(plan_slide)
        options = [o for o in slide_flow.options_for(content, kit, position + 1, total)
                   if o["pattern"].split("@")[0] != str(was).split("@")[0]]
        if not options:
            relaxed, why = relaxed_options(content, kit, position + 1, total)
            options = [o for o in relaxed
                       if o["pattern"].split("@")[0] != str(was).split("@")[0]]
            if options:
                log(f"  layout: slide {n}: no layout fits strictly; {why}")
        if not options:
            problems.append(f"slide {n}: no other layout in this template can hold what is on "
                            f"it (it is laid out as {was!r})")
            continue
        ctx = {"deck_title": plan.get("title", ""), "slide": position + 1, "of": total}
        job = {"content": content, "options": options}
        candidate, _ = slide_flow.render_one(planner, job, ctx, index, kit, content_pack,
                                             settings().render_retries, log=log)
        if candidate is None:
            problems.append(f"slide {n}: I could not place its content in any other layout")
            continue
        plan["slides"][position] = candidate
        changed.append({"change": "changed a slide's layout", "slide": n, "from": was,
                        "to": candidate["pattern"], "why": spec.get("why"),
                        "how": "rebuilt the deck from its plan, using the template's layouts"})

    if not changed:
        return None, [], problems, None

    result = build_from_kit(template, index, kit, plan, content_pack,
                            ws.path("rebuilt.pptx"), baseline_for_kit(template, kit))
    log(f"  rebuild: {result.summary()}")
    if result.structural:
        return None, [], problems + [f"the rebuilt deck is not sound: {result.structural[:1]}"], None

    deck, replayed, replay_problems = _replay_code_slides(
        ws, result.out_path, state.get("code_slides"), planner, log)
    problems += replay_problems
    updated = ws.publish_json(plan, "plan.json")
    return deck, changed, problems, updated


def _plan_position(plan: dict, state, slide_number: int) -> int | None:
    """Which plan entry is slide `slide_number` of the current deck.

    Slides drawn by generated code sit in the deck but not in the plan, so every one of them
    before this slide shifts it by one.
    """
    before = sum(1 for c in (state.get("code_slides") or [])
                 if (c.get("slide") or 0) <= slide_number)
    position = slide_number - 1 - before
    slides = plan.get("slides") or []
    if any((c.get("slide") or 0) == slide_number for c in state.get("code_slides") or []):
        return None                     # this slide *is* a generated one: it has no plan entry
    return position if 0 <= position < len(slides) else None
