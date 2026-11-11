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

from concurrent.futures import ThreadPoolExecutor

from ..engine.deck import check, move_slide, new_slide_index, preserved_count, probe, reseal
from ..engine.tidy import add_slide_number, renumber_pages, without_citations
from ..engine.variety import FORMS, assign_forms
from ..infra.storage import store
from ..sandbox.code import CONTRACT, run_builder
from ..settings import settings
from ..state import DeckState, current_deck, step
from .common import (blank_layout_index, design_frame, make_previews, publish_version,
                     style_brief, workspace)
from .edit import _look_at_one


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
    made, still_pending, feedback_log, notes = [], [], [], []

    with workspace(state) as ws:
        deck = ws.fetch(base, "current.pptx")
        deck_index = probe(deck)
        # The look comes from the template, not from whatever the deck has become: its
        # palette and type, the layout its content slides sit on, and the area they keep for
        # content. See common.design_frame.
        template_index = (probe(ws.fetch(state["template"], "template.pptx"))
                          if state.get("template") else deck_index)
        style = style_brief(kit, template_index)
        frame = design_frame(kit, template_index) if kit else {"chrome_layout": None,
                                                               "body_box": None}
        # Two kinds of slide are owed. A *rework* is already in the deck, in its place, built
        # from a template layout with its content on it; the code turns it into a diagram. A
        # slide no layout could hold is not in the deck at all; the code adds it, and it is
        # moved into place afterwards. Until those are added, a rework sits that many places
        # earlier than its final position.
        def now_at(item, missing_before: int) -> int:
            return item["position"] - missing_before

        not_yet = lambda p: sum(1 for q in pending if not q.get("rework")
                                and q["position"] < p["position"])
        forms = [p.get("form") for p in pending]
        if not all(forms):
            assigned = assign_forms([p["content"] for p in pending])
            forms = [f or a for f, a in zip(forms, assigned)]

        ctxs = []
        for k, item in enumerate(pending):
            ctxs.append({
                "slide_size": deck_index["slide_size"],
                "style": style,
                # Citations are stripped out entirely, not just out of the prose. The plan
                # keeps `fact: "F2"` because that is where it can be checked; a model asked to
                # *draw* a slide has no use for it and will print it, which it did.
                "content": without_citations(item["content"]),
                "insert_after": item.get("position"),
                "chrome_layout": frame["chrome_layout"],
                "body_box": frame["body_box"] or {"x": 0, "y": 0, **deck_index["slide_size"]},
                "form": forms[k],
                "form_hint": FORMS.get(forms[k], ""),
                "avoid_forms": sorted({f for j, f in enumerate(forms) if j != k}),
                # a rework: which slide, and what is on it now (set again before each run)
                "target_slide": now_at(item, not_yet(item)) if item.get("rework") else None,
                "shapes": (shape_list(deck, now_at(item, not_yet(item)), frame["body_box"])
                           if item.get("rework") else None),
                "blank_layout": blank_layout_index(deck_index),
            })

        # First drafts for every slide at once: the model calls are the slow part and they do
        # not depend on each other. Running the code, checking it and placing it stays one at
        # a time, because each slide is added to the deck the previous one produced.
        initial = list(state.get("code_feedback") or []) or None

        def draft(k):
            try:
                return planner.write_slide_code(CONTRACT, pending[k]["content"], style, ctxs[k],
                                                feedback=initial)
            except Exception as e:                       # noqa: BLE001
                return {"code": "", "error": f"{e.__class__.__name__}: {e}"}

        with ThreadPoolExecutor(max_workers=max(1, s.render_workers)) as pool:
            drafts = list(pool.map(draft, range(len(pending))))

        skipped = 0          # slides before this one that could not be drawn
        for k, item in enumerate(pending):
            ctx = ctxs[k]
            answer, feedback, previous = drafts[k], None, None
            placed = False
            for tries in range(s.code_attempts):
                if tries:
                    answer = planner.write_slide_code(CONTRACT, item["content"], style, ctx,
                                                      feedback=feedback or None,
                                                      previous=previous)
                code = (answer or {}).get("code") or ""
                rework = bool(item.get("rework"))
                if rework:
                    ctx["target_slide"] = now_at(item, skipped)
                result = run_builder(code, ctx, deck, s)
                if not result.ok:
                    log(f"  designed slide {k + 1}: attempt {tries + 1} failed: {result.error}")
                    feedback, previous = result.feedback(), code
                    feedback_log.append({"slide": item.get("position"), "attempt": tries + 1,
                                         "problems": result.feedback(),
                                         "rejected_before_running": result.rejected_by_guard})
                    continue

                # Reseal: the code went through python-pptx, which rewrites every part it
                # opened. Comparing part by part against the deck we started from puts the
                # untouched slides back to their original bytes.
                sealed = ws.path(f"custom-{k}-{tries}.pptx")
                stats = reseal(deck, result.deck, sealed)
                before_index, sealed_index = probe(deck), probe(sealed)
                total = len(sealed_index["slides"])
                if rework:
                    # Reworked in place: nothing to find, nothing to move. A slide count that
                    # changed means the code added or removed one anyway.
                    if total != len(before_index["slides"]):
                        feedback, previous = ["rework prs.slides[ctx['target_slide']] in place; "
                                              "do not add or remove slides"], code
                        continue
                    target = ctx["target_slide"]
                    moved = sealed
                else:
                    # Where did the code actually put its slide? It is told the harness will
                    # place it, but a model that moves it anyway must not scramble the deck.
                    drawn = new_slide_index(before_index, sealed_index)
                    if drawn is None:
                        log(f"  designed slide {k + 1}: the code added no new slide")
                        feedback, previous = ["your code did not add a slide to the deck"], code
                        continue
                    target = min(max(0, item.get("position", total - 1) - skipped), total - 1)
                    moved = ws.path(f"custom-{k}-{tries}-moved.pptx")
                    move_slide(sealed, drawn, target, moved)

                # The new slide has no example slide to be compared against, so it is checked
                # absolutely - which is the right standard for something drawn from scratch.
                report = check(moved, {target})
                problems = _as_feedback(report) if (report["findings"] or report["structural"]) else []
                # Then looked at, when a vision model is configured: the arithmetic is happy
                # with a diagram whose labels sit on top of its arrows.
                if not problems:
                    seen = _look_at_one(moved, target, planner, log)
                    if seen and tries < s.code_attempts - 1:
                        problems = seen
                    elif seen:
                        notes.append(f"slide {target + 1} (drawn as {ctx['form']}): "
                                     + "; ".join(x.removeprefix("looking at the slide: ")
                                                 for x in seen[:2]))
                if problems:
                    log(f"  designed slide {k + 1}: drawn but rejected: {problems[0][:110]}")
                    feedback, previous = problems, code
                    feedback_log.append({"slide": item.get("position"), "attempt": tries + 1,
                                         "problems": problems})
                    continue

                if rework:
                    deck = moved                  # it kept the template's page number
                else:
                    numbered = ws.path(f"custom-{k}-numbered.pptx")
                    deck = numbered if add_slide_number(moved, target, numbered) else moved
                # The code and the exact context it ran with are both kept, so this slide can
                # be drawn again on a deck that was rebuilt from its plan. Without the context
                # it could not: the style, the slide size and the content all came from here.
                code_uri = ws.store.put_bytes(code.encode(), ws.key(f"custom-slide-{k}.py"))
                ctx_uri = ws.store.put_json(ctx, ws.key(f"custom-slide-{k}-ctx.json"))
                form = (answer or {}).get("form") or ctx["form"]
                made.append({"slide": target + 1, "form": form,
                             "explanation": (answer or {}).get("explanation"),
                             "why": item.get("why"),
                             # where it belongs in the finished deck, for a rebuild
                             "code": code_uri, "ctx": ctx_uri, "rework": rework,
                             "position": item.get("position", target),
                             "preserved": preserved_count(stats), "attempts": tries + 1})
                log(f"  designed slide {k + 1}: accepted at position {target + 1} as {form} "
                    f"({tries + 1} attempt(s), {preserved_count(stats)} parts byte-identical)")
                placed = True
                break
            if not placed and item.get("rework"):
                # Still in the deck as its template layout, with all its content: a repeated
                # layout, which is what the budget was avoiding - but nothing is lost.
                notes.append(f"slide {now_at(item, skipped) + 1} keeps its template layout "
                             f"({item.get('layout')}): it could not be reworked as a "
                             f"{ctx['form']} in {s.code_attempts} attempts")
            elif not placed:
                still_pending.append(item)
                skipped += 1

        # A designed slide that could not be drawn goes back to the template layout it would
        # have had. Its content is in the deck either way; the layout budget is the thing given
        # up, and the result says so.
        if not made:
            ws.publish_json(feedback_log, "custom_code_attempts.json")
            return {
                "pending_code": [], "code_attempt": attempt + 1,
                "vision_notes": [*(state.get("vision_notes") or []), *notes],
                "errors": ([{"kind": "code_failed", "message":
                             "generated code could not draw the slides that no layout fits",
                             "detail": feedback_log[-3:]}] if still_pending else []),
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
                      *[{"change": f"designed slide {m['slide']} as a {m['form']}",
                         "slide": m["slide"], "why": m.get("why"),
                         "explanation": m["explanation"]} for m in made]],
        # Only the slides this node actually drew. `without_page_number` also lists slides
        # whose page number is not of the form "n / total" - this template numbers them with a
        # bare figure in a coloured square - and calling those "drawn by generated code" was
        # both wrong and alarming.
        "vision_notes": [*(state.get("vision_notes") or []), *notes,
                         *([f"slide {m['slide']} has no page number: it was drawn by generated "
                            "code, which has no page-number box to fill"
                            for m in made if m["slide"] in pages["without_page_number"]])],
        "errors": ([{"kind": "no_fit", "message":
                     f"{len(still_pending)} slide(s) could not be drawn either",
                     "detail": [p["content"].get("title") for p in still_pending]}]
                   if still_pending else []),
        "trace": [step("custom_code", drawn=len(made), still_pending=len(still_pending),
                       previews=len(previews), previews_skipped=why_not,
                       pages_renumbered=pages["renumbered"])],
    }


def shape_list(deck, index: int, body_box: dict | None) -> list[dict]:
    """What is on a slide to be reworked, as the code's author needs to know it.

    Each top-level shape: its id (to find it with `{s.shape_id: s for s in slide.shapes}`),
    what it is, where it is, its words, and a role - `title` and `page` are the template's and
    stay; `frame` is header or footer art outside the content area and stays; `content` is
    what the slide says now, in the layout being replaced, and is the code's to move, restyle,
    reuse or delete.
    """
    from pptx import Presentation

    prs = Presentation(str(deck))
    slide, area = prs.slides[index], prs.slide_width * prs.slide_height
    box = body_box or {}
    top, bottom = box.get("y", 0), box.get("y", 0) + box.get("h", 10 ** 12)
    out = []
    for sh in slide.shapes:
        kind = None
        try:
            kind = int(sh.placeholder_format.type) if sh.is_placeholder else None
        except ValueError:
            pass
        words = sh.text_frame.text.strip() if getattr(sh, "has_text_frame", False) else ""
        if kind in (1, 3):
            role = "title"
        elif kind == 13:
            role = "page"
        elif sh.height and (sh.top + sh.height <= top or sh.top >= bottom):
            role = "frame"
        elif sh.width and sh.height and not words and sh.width * sh.height >= 0.25 * area:
            role = "frame"                         # large wordless art: a header band, a photo
        else:
            role = "content"
        if getattr(sh, "shape_type", None) == 6:
            words = " | ".join(c.text_frame.text.strip() for c in sh.shapes
                               if getattr(c, "has_text_frame", False) and c.text_frame.text.strip())
        out.append({"id": sh.shape_id, "role": role, "name": sh.name,
                    "kind": str(sh.shape_type).split(".")[-1].split(" ")[0].lower(),
                    "x": sh.left, "y": sh.top, "w": sh.width, "h": sh.height,
                    "text": words[:120]})
    return out


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
    """Back to the normal decision, not straight out of the door.

    The escape hatch sits between `assemble` and `deliver`, and returning "deliver"
    unconditionally meant anything still wrong with the deck skipped the repair loop entirely.
    A deck went out with four problems a vision model had just named, none of them looked at
    again, because one slide happened to need a custom layout and the hatch was switched off.
    """
    from ..settings import settings as _settings

    s = _settings()
    if state.get("findings") and state.get("repair_round", 0) < s.max_repair_rounds:
        return "repair"
    return "deliver"
