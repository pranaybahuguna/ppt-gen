"""custom_code - the escape hatch, and the only place a model writes anything spatial.

It is reached for one well-defined reason: the deterministic path produced a slide's content
but **no layout in the template can hold it**, so there is nothing to fill. Rather than drop
the slide or squeeze it into a layout it does not fit, the model is asked to draw it.

The four fences, in order:

1. `allow_custom_code` is off by default, and this node is only on the path after the
   deterministic attempt has actually failed. It is a fallback, never a shortcut.
2. The code is read before it runs (`sandbox.guard`): a wrong import or a reach for `eval`
   costs a millisecond and a precise sentence back to the model, not a container.
3. The code runs in a throwaway container - no network, read-only root filesystem, no
   capabilities, half a gigabyte, seconds of CPU - and the container is the real boundary.
4. What comes back is **resealed** and then **checked**: slides the code did not touch go back
   to their original bytes, and the new slide faces the same overflow and overlap rules as
   every other slide. A slide that fails goes back to the model with the reason.

If every attempt fails, the deck is delivered without that slide and the result says so. The
previous version is still in the bucket, so nothing has been lost.
"""
from __future__ import annotations

from ..engine.deck import check, move_slide, new_slide_index, preserved_count, probe, reseal
from ..engine.tidy import renumber_pages, without_citations
from ..infra.storage import store
from ..sandbox.code import CONTRACT, run_builder
from ..settings import settings
from ..state import DeckState, current_deck, step
from .common import blank_layout_index, make_previews, publish_version, style_brief, workspace


def custom_code(state: DeckState, planner=None, log=print) -> dict:
    s = settings()
    pending = list(state.get("pending_code") or [])
    if not pending:
        return {"trace": [step("custom_code", outcome="nothing to draw")]}
    if not s.allow_custom_code:
        return {
            "pending_code": [],
            "errors": [{"kind": "no_fit", "message":
                        f"{len(pending)} slide(s) have content that no layout in this template "
                        "can hold. I can draw them with generated code, but that is switched "
                        "off (allow_custom_code).",
                        "detail": [p["content"].get("title") for p in pending]}],
            "trace": [step("custom_code", outcome="not allowed", pending=len(pending))],
        }
    base = current_deck(state)
    if not base:
        return {"trace": [step("custom_code", outcome="no deck to add to")]}

    kit = store().get_json(state["kit"]) if state.get("kit") else {}
    attempt = state.get("code_attempt", 0)
    made, still_pending, feedback_log = [], [], []

    with workspace(state) as ws:
        deck = ws.fetch(base, "current.pptx")
        template_index = probe(deck)
        style = style_brief(kit, template_index)

        for k, item in enumerate(pending):
            ctx = {
                "slide_size": template_index["slide_size"],
                "style": style,
                # Citations are stripped out entirely, not just out of the prose. The plan
                # keeps `fact: "F2"` because that is where it can be checked; a model asked to
                # *draw* a slide has no use for it and will print it, which it did.
                "content": without_citations(item["content"]),
                "insert_after": item.get("position"),
                "blank_layout": blank_layout_index(template_index),
            }
            feedback, previous = list(state.get("code_feedback") or []), None
            placed = False
            for tries in range(s.code_attempts):
                answer = planner.write_slide_code(CONTRACT, item["content"], style, ctx,
                                                  feedback=feedback or None, previous=previous)
                code = (answer or {}).get("code") or ""
                result = run_builder(code, ctx, deck, s)
                if not result.ok:
                    log(f"  custom slide {k + 1}: attempt {tries + 1} failed: {result.error}")
                    feedback, previous = result.feedback(), code
                    feedback_log.append({"slide": item.get("position"), "attempt": tries + 1,
                                         "problems": result.feedback(),
                                         "rejected_before_running": result.rejected_by_guard})
                    continue

                # Reseal: the code went through python-pptx, which rewrites every part it
                # opened. Comparing part by part against the deck we started from puts the
                # untouched slides back to their original bytes.
                sealed = ws.path(f"custom-{k}.pptx")
                stats = reseal(deck, result.deck, sealed)
                before_index, sealed_index = probe(deck), probe(sealed)
                total = len(sealed_index["slides"])
                # Where did the code actually put its slide? It is told the harness will place
                # it, but a model that moves it anyway must not be able to scramble the deck.
                drawn = new_slide_index(before_index, sealed_index)
                if drawn is None:
                    log(f"  custom slide {k + 1}: the code added no new slide")
                    feedback, previous = ["your code did not add a slide to the deck"], code
                    continue
                target = min(item.get("position", total - 1), total - 1)
                moved = ws.path(f"custom-{k}-moved.pptx")
                move_slide(sealed, drawn, target, moved)

                # The new slide has no example slide to be compared against, so it is checked
                # absolutely - which is the right standard for something drawn from scratch.
                report = check(moved, {target})
                if report["findings"] or report["structural"]:
                    problems = _as_feedback(report)
                    log(f"  custom slide {k + 1}: drawn but rejected by the checks: {problems[0]}")
                    feedback, previous = problems, code
                    feedback_log.append({"slide": item.get("position"), "attempt": tries + 1,
                                         "problems": problems})
                    continue

                deck = moved
                # The code and the exact context it ran with are both kept, so this slide can
                # be drawn again on a deck that was rebuilt from its plan. Without the context
                # it could not: the style, the slide size and the content all came from here.
                code_uri = ws.store.put_bytes(code.encode(), ws.key(f"custom-slide-{k}.py"))
                ctx_uri = ws.store.put_json(ctx, ws.key(f"custom-slide-{k}-ctx.json"))
                made.append({"slide": target + 1, "explanation": (answer or {}).get("explanation"),
                             "code": code_uri, "ctx": ctx_uri, "position": item.get("position"),
                             "preserved": preserved_count(stats), "attempts": tries + 1})
                log(f"  custom slide {k + 1}: accepted at position {target + 1} "
                    f"({preserved_count(stats)} parts byte-identical)")
                placed = True
                break
            if not placed:
                still_pending.append(item)

        if not made:
            return {
                "pending_code": [], "code_attempt": attempt + 1,
                "errors": [{"kind": "code_failed", "message":
                            "generated code could not draw the slides that no layout fits",
                            "detail": feedback_log[-3:]}],
                "trace": [step("custom_code", outcome="all attempts failed",
                               pending=len(pending))],
            }

        # The deck is longer than the plan thought, so every "n / total" is now wrong, and
        # the drawn slide has no page number at all. Renumber what exists and say what does not.
        final = ws.path("out.pptx")
        pages = renumber_pages(deck, final)
        if pages["without_page_number"]:
            log(f"  pages: renumbered {pages['renumbered']}; slides "
                f"{pages['without_page_number']} have no page number of their own")
        ws.publish_json(feedback_log, "custom_code_attempts.json")
        uri, versions = publish_version(state, ws, "out.pptx")
        previews, why_not = make_previews(state, ws, final)

    return {
        "deck_versions": versions, "previews": previews,
        "pending_code": still_pending, "code_attempt": attempt + 1,
        "code_slides": [*(state.get("code_slides") or []), *made],
        "changelog": [*(state.get("changelog") or []),
                      *[{"change": "drew a slide with generated code", "slide": m["slide"],
                         "explanation": m["explanation"]} for m in made]],
        "vision_notes": [*(state.get("vision_notes") or []),
                         *([f"slide {n} has no page number: it was drawn by generated code, "
                            "which has no page-number box to fill"
                            for n in pages["without_page_number"]])],
        "errors": ([{"kind": "no_fit", "message":
                     f"{len(still_pending)} slide(s) could not be drawn either",
                     "detail": [p["content"].get("title") for p in still_pending]}]
                   if still_pending else []),
        "trace": [step("custom_code", drawn=len(made), still_pending=len(still_pending),
                       previews=len(previews), previews_skipped=why_not,
                       pages_renumbered=pages["renumbered"])],
    }


def _as_feedback(report: dict) -> list[str]:
    out = []
    for f in report["findings"]:
        if f["kind"] == "overflow":
            out.append(f"a text box on your slide is too small for its text "
                       f"(it needs {f['ratio']:.2f}x the height): make the box taller, "
                       f"the text shorter, or the font smaller")
        elif f["kind"] == "overlap":
            out.append("two of your text boxes overlap: leave a gap between them")
    out += [f"the slide is structurally wrong: {p}" for p in report["structural"]]
    return out or ["the slide did not pass the checks"]


def after_code(state: DeckState) -> str:
    return "deliver"
