"""The translate route.

    translate_plan   probe the deck, send the paragraphs in scope as tagged runs
    translate_apply  compile to fingerprinted ops, apply, check what came back
    translate_repair first ask for shorter phrasing; if it still does not fit, shrink the font

A paragraph goes to the model as `<r0>Deposit margins </r0><r1>stabilise</r1>` - one tag per
formatted run. The model may reorder, merge or repeat tags, because word order changes between
languages, and each run keeps its own formatting. Translating run by run instead would put the
bold on the wrong word; that is why this is done per paragraph.

Every attempt re-applies to `work_base`, the version this route started from, never to the
output of the previous attempt: the ops carry fingerprints taken from that file, and the engine
refuses the whole batch if they no longer match.
"""
from __future__ import annotations

from ..engine import bootstrap
from ..engine.build import font_fallback_ops
from ..engine.deck import Baseline, apply_ops, changelog, check, paragraphs, preserved_count, probe
from ..settings import settings
from ..state import DeckState, current_deck, step
from .common import make_previews, publish_version, warnings_of, workspace


def _scope(state: DeckState, index: dict) -> set[int] | None:
    numbers = state.get("slide_scope")
    if not numbers:
        return None
    return {n - 1 for n in numbers if 1 <= n <= len(index["slides"])}


def translate_plan(state: DeckState, planner=None, log=print) -> dict:
    base = current_deck(state) or state.get("source_deck")
    if not base:
        return {"route": "decline", "route_reason": "there is no deck here to translate.",
                "trace": [step("translate_plan", outcome="no deck")]}
    lang = state.get("target_lang")
    if not lang:
        return {"route": "ask", "pending_question": "Which language should I translate it into?",
                "trace": [step("translate_plan", outcome="no language")]}

    with workspace(state) as ws:
        deck = ws.fetch(base, "source.pptx")
        index = probe(deck)
        scope = _scope(state, index)
        paras = paragraphs(index, scope)
        log(f"  translate: {len(paras)} paragraphs on "
            f"{len(scope) if scope else len(index['slides'])} slides -> {lang}")
        plan = planner.translate(paras, lang)
        ws.publish_json(plan, "translate_plan.json")

    return {"plan": plan, "work_base": base,
            "slide_scope": sorted(n + 1 for n in scope) if scope else None,
            "extra_ops": [],
            "trace": [step("translate_plan", paragraphs=len(paras), lang=lang)]}


def translate_apply(state: DeckState, log=print) -> dict:
    bootstrap()
    from deckops.compiler import compile_translate

    with workspace(state) as ws:
        base = ws.fetch(state["work_base"], "source.pptx")
        index = probe(base)
        scope = _scope(state, index)
        ops = compile_translate(state["plan"], index) + list(state.get("extra_ops") or [])
        ws.publish_json(ops, f"ops-{state.get('repair_round', 0)}.json")

        ex = apply_ops(base, ops, ws.path("out.pptx"))
        # Differential: the source deck is the yardstick. Real decks are already tight, and
        # translated text is usually longer than English, so absolute thresholds cry wolf.
        baseline = Baseline.of(base, scope)
        report = baseline.filter(check(ws.path("out.pptx"), scope))
        uri, versions = publish_version(state, ws, "out.pptx")
        previews, why_not = make_previews(state, ws, ws.path("out.pptx"),
                                          sorted(scope) if scope else None)
        log(f"  applied {len(ops)} ops, {len(report['findings'])} new findings, "
            f"{preserved_count(ex)} parts byte-identical")
        return {
            "deck_versions": versions, "previews": previews,
            "findings": report["findings"], "structural": report["structural"], "leaked": [],
            "changelog": changelog(index, ops),
            "plan_divergence": [f"translated into {state.get('target_lang')}"] if ops else [],
            "trace": [step("translate_apply", ops=len(ops), findings=len(report["findings"]),
                           preserved=preserved_count(ex), previews=len(previews),
                           previews_skipped=why_not)],
        }


def translate_repair(state: DeckState, planner=None, log=print) -> dict:
    """Round 1: ask for shorter phrasing. Round 2: shrink the font. Then stop and say so.

    The order matters. Shorter wording is what a translator would do; a smaller font is what a
    tool does when it has run out of ideas. Doing the second first hides the problem.
    """
    round_ = state.get("repair_round", 0) + 1
    findings = state.get("findings") or []
    over = [f for f in findings if f["kind"] == "overflow"]
    if not over:
        return {"repair_round": round_, "trace": [step("translate_repair", outcome="nothing to shorten")]}

    if round_ == 1:
        with workspace(state) as ws:
            base = ws.fetch(state["work_base"], "source.pptx")
            index = probe(base)
            by_ref = {p["ref"]: p for p in paragraphs(index)}
            # the text as it stands now (the previous attempt's wording), so the model shortens
            # what is actually in the deck rather than the original English
            current = {p["ref"]: p["tagged"] for p in state["plan"]["paragraphs"]}
            victims = [dict(by_ref[r], tagged=current.get(r, by_ref[r]["tagged"]))
                       for f in over for r in by_ref if r.startswith(f["ref"] + "/")]
            if not victims:
                return {"repair_round": round_,
                        "trace": [step("translate_repair", outcome="no paragraphs to shorten")]}
            short = planner.shorten(victims, state.get("target_lang") or "", 25)
            merged = {p["ref"]: p for p in state["plan"]["paragraphs"]}
            for p in short.get("paragraphs", []):
                merged[p["ref"]] = p
            plan = {**state["plan"], "paragraphs": list(merged.values())}
            log(f"  repair {round_}: the model shortened {len(short.get('paragraphs', []))} paragraphs")
        return {"plan": plan, "repair_round": round_,
                "trace": [step("translate_repair", round=round_,
                               shortened=len(short.get("paragraphs", [])))]}

    extra = font_fallback_ops(over)
    log(f"  repair {round_}: font-size fallback on {len(extra)} shapes")
    return {"extra_ops": extra, "repair_round": round_,
            "trace": [step("translate_repair", round=round_, font_fallback=len(extra))]}


def after_translate(state: DeckState) -> str:
    if state.get("route") in ("ask", "decline"):
        return state["route"]
    if not warnings_of(state):
        return "deliver"
    if state.get("findings") and state.get("repair_round", 0) < settings().max_repair_rounds:
        return "translate_repair"
    return "deliver"
