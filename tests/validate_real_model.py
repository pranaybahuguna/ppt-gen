"""Exercise every model call against a real endpoint, and report what it actually did.

    .venv/bin/python tests/validate_real_model.py [--vision] [--code]

The engine's handoff document lists "real-model validation" as not done: the prompts had only
ever been run against an offline stub and a fake HTTP server. This script is that validation.
It makes one call of each kind, checks the answer is the shape the code downstream expects, and
prints what came back, so a prompt that drifts shows up as a named failure rather than as a
strange deck three steps later.

What it covers, in the order the graph uses them:

    route             what does this person want done?
    outline           stage 1: what each slide says
    render_slide      stage 2: that content placed into one of the layouts offered
    plan_edit         an instruction about an existing deck turned into typed actions
    write_slide_code  the escape hatch: python-pptx for a slide no layout holds  (--code)
    label_slide       the vision call that labels a template's boxes              (--vision)

It needs `llm.json`. It costs real tokens: about half a dozen calls, plus one per slide for
stage 2, plus one per candidate slide for the vision labels.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from deck_agent.engine import deck  # noqa: E402
from deck_agent.engine.fallback import relaxed_options  # noqa: E402
from deck_agent.llm.planner import planner as make_planner  # noqa: E402
from deck_agent.settings import Settings, reset, settings  # noqa: E402

CONTENT = {
    "facts": [
        {"id": "F1", "text": "4,100 support tickets arrived in September 2026", "source": "pilot report"},
        {"id": "F2", "text": "38% of incoming tickets were answered by the bot", "source": "pilot report"},
        {"id": "F3", "text": "median first response fell from 11 hours to 3 hours", "source": "pilot report"},
        {"id": "F4", "text": "the pilot covered two teams over eight weeks", "source": "pilot report"},
        {"id": "F5", "text": "no agent was taken off the rota during the pilot", "source": "pilot report"},
    ],
    "datasets": {"weekly_tickets": {"columns": ["week", "opened", "answered_by_bot"],
                                    "rows": [["W1", 980, 120], ["W2", 1010, 290],
                                             ["W3", 1040, 380], ["W4", 1070, 410]]}},
}

results: list[dict] = []


def check(name: str, fn):
    """Run one call, time it, and record whether the answer was usable."""
    print(f"\n--- {name} " + "-" * (62 - len(name)))
    started = time.time()
    try:
        problems, detail = fn()
    except Exception as e:
        seconds = round(time.time() - started, 1)
        print(f"  FAILED after {seconds}s: {e.__class__.__name__}: {str(e)[:400]}")
        results.append({"call": name, "ok": False, "seconds": seconds,
                        "problems": [f"{e.__class__.__name__}: {str(e)[:200]}"]})
        return None
    seconds = round(time.time() - started, 1)
    for line in detail:
        print(f"  {line}")
    if problems:
        for p in problems:
            print(f"  PROBLEM: {p}")
    print(f"  {'ok' if not problems else 'PROBLEMS'} in {seconds}s")
    results.append({"call": name, "ok": not problems, "seconds": seconds, "problems": problems})
    return None


def main() -> int:
    reset(Settings.load())
    settings().llm_client = "openai-compat"
    want_vision = "--vision" in sys.argv
    want_look = want_vision or "--look" in sys.argv
    want_code = "--code" in sys.argv

    planner = make_planner()
    print(f"model: {planner.name}")

    template = REPO / "local" / "samples" / "example_style.pptx"
    if not template.exists():
        print(f"{template} is missing. Run: make samples")
        return 1
    kit = json.loads((REPO / "local" / "samples" / "example_style_kit.json").read_text())
    kit["template"] = template.name
    index = deck.probe(template)

    state: dict = {}

    # ---------------------------------------------------------------- route
    def do_route():
        problems, detail = [], []
        cases = [
            ("write me a deck about the support bot pilot", {}, "create"),
            ("make slide 3 less wordy", {"has_current_deck": True}, "edit"),
            ("translate it into French", {"has_current_deck": True}, "translate"),
            ("turn these 8 steps into 6", {"has_current_deck": True}, "edit"),
            ("summarise this deck for an email", {"has_current_deck": True}, None),
        ]
        for text, context, expected in cases:
            answer = planner.route(text, context)
            got = answer.get("route")
            detail.append(f'"{text[:40]}" -> {got} ({answer.get("confidence")}) '
                          f'{str(answer.get("reason"))[:60]}')
            if not got:
                problems.append(f'no route for "{text}"')
            elif expected and got != expected:
                problems.append(f'"{text}" routed to {got}, expected {expected}')
        return problems, detail

    check("route", do_route)

    # ---------------------------------------------------------------- outline (stage 1)
    def do_outline():
        from controlplane.slide_flow import max_points

        outline = planner.outline(
            "the support bot pilot, for the monthly operations review; eight slides at most",
            CONTENT, max_points(kit))
        state["outline"] = outline
        problems, detail = [], []
        slides = outline.get("slides") or []
        detail.append(f"{len(slides)} slides, title {outline.get('title')!r}")
        if not slides:
            return ["the outline has no slides"], detail
        allowed_kinds = {"cover", "numbers", "sequence", "comparison", "list", "table",
                         "chart", "text", "closing"}
        known_facts = {f["id"] for f in CONTENT["facts"]}
        for i, s in enumerate(slides, start=1):
            detail.append(f"  {i}. [{s.get('kind')}] {str(s.get('title'))[:52]} "
                          f"({len(s.get('points') or [])} points)")
            if s.get("kind") not in allowed_kinds:
                problems.append(f"slide {i}: kind {s.get('kind')!r} is not one of the nine")
            if not s.get("title"):
                problems.append(f"slide {i}: no title")
            for point in s.get("points") or []:
                fact = point.get("fact")
                if fact and fact not in known_facts:
                    problems.append(f"slide {i}: cites {fact!r}, which is not in the content pack")
        if slides and slides[0].get("kind") != "cover":
            problems.append("the first slide is not a cover")
        return problems, detail

    check("outline (stage 1)", do_outline)

    # ---------------------------------------------------------------- render_slide (stage 2)
    def do_render():
        from controlplane.slide_flow import expand, render_one

        outline = state.get("outline")
        if not outline:
            return ["skipped: there is no outline"], []
        jobs = expand(outline, kit)
        problems, detail = [], []
        total_attempts = 0
        placed = 0
        for i, job in enumerate(jobs):
            if not job["options"]:
                options, relaxed = relaxed_options(job["content"], kit, i + 1, len(jobs))
                job["options"] = options
                if options:
                    detail.append(f"  slide {i + 1}: no layout fits strictly; {relaxed}")
            if not job["options"]:
                detail.append(f"  slide {i + 1}: nothing fits - would go to the escape hatch")
                continue
            context = {"deck_title": outline.get("title", ""), "slide": i + 1, "of": len(jobs)}
            slide, attempts = render_one(planner, job, context, index, kit, CONTENT, retries=2,
                                         log=lambda *a, **k: None)
            total_attempts += len(attempts)
            if slide is None:
                problems.append(f"slide {i + 1}: still invalid after "
                                f"{len(attempts)} attempts: {attempts[-1].get('errors')}")
                continue
            placed += 1
            detail.append(f"  slide {i + 1}: {slide['pattern']} "
                          f"({len(slide.get('items') or [])} items, {len(attempts)} attempt(s))")
        state["plan"] = {"title": outline.get("title", ""), "footer": outline.get("footer", ""),
                         "slides": []}
        per_slide = total_attempts / max(1, len(jobs))
        detail.append(f"{placed}/{len(jobs)} placed, {per_slide:.2f} attempts per slide")
        # The engine's own acceptance bar for this: under one retry per slide on average.
        if per_slide > 2.0:
            problems.append(f"{per_slide:.2f} attempts per slide: the stage-2 prompt needs work")
        return problems, detail

    check("render_slide (stage 2)", do_render)

    # ---------------------------------------------------------------- plan_edit
    def do_edit():
        from deck_agent.engine.edits import compile_edit, deck_view

        sample = REPO / "local" / "samples" / "sample.pptx"
        sample_index = deck.probe(sample)
        view = deck_view(sample_index)
        problems, detail = [], []
        for instruction in ("change the title on slide 1 to 'Third quarter review'",
                            "make slide 4 less wordy",
                            "turn these 8 steps into 6",
                            "make the title on slide 1 dark red",
                            "use a green accent colour instead of blue"):
            answer = planner.plan_edit(instruction, view, {"slides": len(sample_index["slides"])})
            actions = answer.get("actions") or []
            detail.append(f'"{instruction[:44]}" -> {len(actions)} action(s): '
                          f'{sorted({a.get("action") for a in actions}) or answer.get("cannot")}')
            if not actions and not answer.get("cannot"):
                problems.append(f'"{instruction}": no actions and no reason why not')
            if actions:
                try:
                    ops, inserts, visual, action_problems = compile_edit(actions, sample_index)
                    detail.append(f"    compiles to {len(ops)} ops, {len(inserts)} insert(s), "
                                  f"{len(visual['styles'])} style change(s)"
                                  + (f", problems: {action_problems}" if action_problems else ""))
                    if action_problems:
                        problems.append(f'"{instruction}": {action_problems[0]}')
                except Exception as e:
                    problems.append(f'"{instruction}": does not compile: {str(e)[:150]}')
        return problems, detail

    check("plan_edit", do_edit)

    # ---------------------------------------------------------------- write_slide_code
    def do_code():
        from deck_agent.nodes.common import blank_layout_index, style_brief
        from deck_agent.sandbox.code import CONTRACT, run_builder
        from deck_agent.sandbox.guard import inspect

        style = style_brief(kit, index)
        content = {"title": "Three views no layout holds",
                   "points": [{"heading": "Volume", "text": "about a thousand tickets a week"},
                              {"heading": "Deflection", "text": "rising every week of the pilot"},
                              {"heading": "Workload", "text": "the gap is what agents still handle"}]}
        ctx = {"slide_size": index["slide_size"], "style": style, "content": content,
               "insert_after": 1, "blank_layout": blank_layout_index(index)}
        problems, detail = [], []
        answer = planner.write_slide_code(CONTRACT, content, style, ctx)
        code = answer.get("code") or ""
        detail.append(f"{len(code)} characters; {answer.get('explanation')}")
        verdict = inspect(code)
        if not verdict.ok:
            return [f"the static gate rejected it: {verdict.problems[0]}"], detail
        detail.append("passed the static gate")
        if settings().code_runner == "off":
            detail.append("not run (code_runner=off)")
            return problems, detail
        result = run_builder(code, ctx, template)
        if not result.ok:
            return [f"it did not run: {result.error}"], detail + [str(result.problems[:1])]
        detail.append(f"ran in {result.seconds}s, slides {result.slides_before} -> "
                      f"{result.slides_after}")
        sealed = Path(settings().workspace_root) / "validate-code.pptx"
        stats = deck.reseal(template, result.deck, sealed)
        detail.append(f"resealed: {deck.preserved_count(stats)} parts byte-identical")
        new_index = len(deck.probe(sealed)["slides"]) - 1
        report = deck.check(sealed, {new_index})
        if report["findings"] or report["structural"]:
            problems.append(f"the slide it drew fails the checks: "
                            f"{report['findings'][:1] or report['structural'][:1]}")
        else:
            detail.append("the slide it drew passes the overflow and overlap checks")
        return problems, detail

    if want_code:
        check("write_slide_code (the escape hatch)", do_code)
    else:
        print("\n--- write_slide_code: skipped (pass --code) " + "-" * 24)

    # ---------------------------------------------------------------- label_slide (vision)
    def do_vision():
        from controlplane.kit_merge import boxes_for_vision
        from deck_agent.engine.kitbuild import draft, marked_pictures
        from deck_agent.sandbox.render import render

        if not getattr(planner, "vision", False):
            return ["this planner says it cannot see pictures"], []
        draft_kit, draft_index, _ = draft(template)
        workdir = Path(settings().workspace_root) / "validate-vision"
        marked = marked_pictures(template, draft_kit, draft_index, workdir / "examples", render)
        problems, detail = [], []
        if not marked:
            return ["no pictures were produced, so the vision call cannot be tested "
                    "(is render_backend off, or the sandbox image missing?)"], detail
        detail.append(f"{len(marked)} example slides rendered and marked up")
        from controlplane.kit_merge import KINDS, ROLES

        for n, png in sorted(marked.items())[:4]:
            pattern = next(p for p in draft_kit["patterns"].values() if p["slide"] == n)
            boxes = boxes_for_vision(pattern, draft_index["slides"][n - 1])
            answer = planner.label_slide(png, boxes, "unknown", slide=n)
            kind, labelled = answer.get("kind"), answer.get("boxes") or {}
            detail.append(f"  slide {n}: kind={kind} offer={answer.get('offer')} "
                          f"{len(labelled)} boxes labelled - {str(answer.get('use_for'))[:48]}")
            if kind and kind not in KINDS:
                problems.append(f"slide {n}: kind {kind!r} is not one of {sorted(KINDS)}")
            for box_id, role in labelled.items():
                if role not in ROLES and role != "fixed":
                    problems.append(f"slide {n}: box {box_id} got role {role!r}, which is not known")
        return problems, detail

    if want_vision:
        check("label_slide (vision)", do_vision)
    else:
        print("\n--- label_slide (vision): skipped (pass --vision) " + "-" * 19)

    # ---------------------------------------------------------------- review_slide (vision)
    def do_review():
        """The eyeball check, automated. Two slides with a known answer: one good, one bad."""
        from deck_agent.nodes.look import review_previews
        from deck_agent.sandbox.render import render

        from deck_agent.engine.build import baseline_for_kit, build_from_kit

        if not getattr(planner, "vision", False):
            return ["this planner cannot look at pictures"], []
        workdir = Path(settings().workspace_root) / "validate-review"
        workdir.mkdir(parents=True, exist_ok=True)

        # Build a small, deliberately correct deck first. The template's *own* example slides
        # are full of placeholder text by design - a reviewer that passed them would be the
        # broken one. (It does in fact report them, correctly, which is a second check on the
        # leak guard for free.)
        plan = {"title": "Pilot", "footer": "Internal", "slides": [
            {"pattern": "cover", "fields": {"title": "Support bot pilot",
                                            "subtitle": "Eight weeks, two teams"},
             "items": [{"value": "38%", "label": "answered by the bot"},
                       {"value": "3h", "label": "first response"}]},
            {"pattern": "steps", "fields": {"title": "How we rolled it out"},
             "items": [{"heading": "Shadow", "body": "The bot drafted replies nobody sent."},
                       {"heading": "Assist", "body": "Agents edited the drafts before sending."},
                       {"heading": "Answer", "body": "The bot answered the clearest tickets."}]},
        ]}
        built = build_from_kit(template, index, kit, plan, CONTENT, workdir / "good.pptx",
                               baseline_for_kit(template, kit))
        if not built.clean:
            return [f"the baseline deck is not clean, so this proves nothing: {built.summary()}"], []
        shots = render(built.out_path, workdir, [0, 1])
        if not shots.ok:
            return [f"nothing to look at: {shots.skipped}"], []
        findings, notes = review_previews(shots.previews, planner, log=lambda *a, **k: None)
        detail = [f"{len(shots.previews)} slides of a known-good deck looked at: "
                  f"{len(findings)} to fix, {len(notes)} noted"]
        for f in findings:
            detail.append(f"  slide {f['slide_index'] + 1}: [{f['severity']}] {f['what'][:72]}")
        for n in notes[:3]:
            detail.append(f"  noted: {n[:76]}")
        problems = []
        # The template's own example slides are a reasonable deck. A reviewer that calls them
        # broken is a reviewer that will rewrite good slides, which is worse than not looking.
        if findings:
            problems.append(f"{len(findings)} high-severity problem(s) on a deck that passes "
                            "every geometric check: the review prompt is too eager, and would "
                            "rewrite slides that are fine")
        return problems, detail

    if want_look:
        check("review_slide (the eyeball check)", do_review)
    else:
        print("\n--- review_slide: skipped (pass --look) " + "-" * 29)

    # ---------------------------------------------------------------- summary
    print("\n" + "=" * 70)
    failed = [r for r in results if not r["ok"]]
    for r in results:
        print(f"  {'ok  ' if r['ok'] else 'FAIL'}  {r['call']:34} {r['seconds']:>5}s"
              + (f"  {len(r['problems'])} problem(s)" if r["problems"] else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} model calls usable")
    out = REPO / "local" / "real_model_validation.json"
    out.write_text(json.dumps({"model": planner.name, "results": results}, indent=1))
    print(f"written to {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
