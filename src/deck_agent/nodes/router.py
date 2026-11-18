"""route - decide what this request is, before spending anything on it.

A model decides. People say the same thing in too many ways ("tidy up the third one", "make it
pop", "can you do this one in German") for word lists to keep up, and a wrong route is the
costliest mistake the graph can make: it rebuilds or rewrites the wrong thing.

The model is given the request and a *summary* of the session - is there a current deck, is
there a template - never the conversation. A router that needs the transcript to know which
deck is meant is a router built on sand; `state.py` says more about why.

Its answer is checked, not trusted: an unknown route becomes a question, slide numbers must be
numbers, and with no deck in the session there is nothing to edit or translate. The word rules
(llm.planner.rules_route) are only a fallback: offline (the stub planner routes with them) or
when the model call fails.

When the model is not confident, the graph stops and asks one short question. That is much
cheaper than confidently rebuilding the wrong deck.
"""
from __future__ import annotations

from langgraph.types import interrupt

from ..llm.planner import rules_route
from ..settings import settings
from ..state import SUPPORTED, DeckState, step, summarise

MAX_ASKS = 2
ROUTES = {"create", "edit", "translate", "ask", "decline"}


def checked(answer: dict, context: dict) -> dict:
    """A router answer made safe to act on. Called by: route."""
    chosen = answer.get("route")
    out = {"route": chosen if chosen in ROUTES else "ask",
           "slides": sorted({int(n) for n in answer.get("slides") or []
                             if isinstance(n, (int, float, str)) and str(n).strip().isdigit() and int(n) > 0}),
           "target_lang": answer.get("target_lang") or None,
           "reason": str(answer.get("reason") or ""),
           "question": answer.get("question"),
           "palette": answer.get("palette") if answer.get("palette") in ("brand", "extended") else None}
    try:
        out["confidence"] = max(0.0, min(1.0, float(answer.get("confidence"))))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    if chosen not in ROUTES:
        out["question"] = out["question"] or "Do you want a new deck, changes to this one, or a translation?"
    has_deck = context.get("has_current_deck") or context.get("has_attached_deck")
    if out["route"] in ("edit", "translate") and not has_deck:
        out.update(route="ask", question="Which deck should I work on? Attach it and say again.",
                   reason="the request is about a deck, but there is none in this session")
    if out["route"] == "translate" and not out["target_lang"]:
        out.update(route="ask", question="Which language should I translate it into?")
    return out


def route(state: DeckState, planner=None) -> dict:
    """Decide what the request is: create, edit, translate, ask or decline.

    Graph node. Runs: after intake on every turn, and again after ask once the person has
        answered.
    Reads: instruction, slide_scope, target_lang, trace (to count questions already asked), and
        a summary of the session (is there a deck).
    Writes: route, route_reason, route_confidence, route_by, target_lang, slide_scope, palette;
        pending_question when it has to ask.
    Next: after_route -> gather (create) | split (edit) | translate_plan | ask | decline.
    Called by: LangGraph, registered in graph.build_graph.
    """
    s = settings()
    instruction = state.get("instruction") or ""
    context = summarise(state)

    guess, by = None, "model"
    if planner is not None and hasattr(planner, "route"):
        by = getattr(planner, "routes_by", "model")
        try:
            guess = checked(planner.route(instruction, context) or {}, context)
        except Exception as e:                 # the model is down: the word rules stand in
            fallback = rules_route(instruction, context)
            guess = checked({**fallback, "reason": f"{fallback['reason']} (the router model failed: "
                                                   f"{e.__class__.__name__}; decided by word rules)"},
                            context)
            by = "rules"
    if guess is None:
        guess, by = checked(rules_route(instruction, context), context), "rules"

    chosen = guess["route"]
    confidence = guess["confidence"]
    asked = sum(1 for t in state.get("trace") or [] if t.get("node") == "ask")
    if confidence < s.router_confidence_floor and chosen not in ("ask", "decline") and asked < MAX_ASKS:
        chosen = "ask"

    out: dict = {
        "route": chosen,
        "route_reason": guess.get("reason"),
        "route_confidence": confidence,
        "route_by": by,
        "target_lang": guess.get("target_lang") or state.get("target_lang"),
        "slide_scope": guess.get("slides") or state.get("slide_scope") or None,
        "trace": [step("route", route=chosen, by=by, confidence=confidence)],
    }
    if guess.get("palette"):                   # only when the request said something about colour
        out["palette"] = guess["palette"]
    if chosen == "ask":
        if asked >= MAX_ASKS:
            out["route"] = "decline"
            out["route_reason"] = ("I asked twice and still cannot tell what to do with this deck")
        else:
            out["pending_question"] = (guess.get("question")
                                       or "What would you like me to do with this deck?")
    return out


def ask(state: DeckState) -> dict:
    """Stop the run and ask the person one question (LangGraph interrupt).

    Graph node. Runs: when route, gather, storyline or another node sets route='ask'. The
        process may exit here; facade.answer() resumes the thread at this line.
    Reads: pending_question, instruction.
    Writes: answer; instruction (the answer is appended to it); pending_question cleared.
    Next: route, so the answer can change the route entirely.
    Called by: LangGraph, registered in graph.build_graph.

    Stop and ask the person one question. The graph genuinely stops here.

    `interrupt()` saves the state and raises; the process may exit. When the caller resumes the
    thread with an answer, execution continues from this line with `answer` set. That is only
    possible because there is a checkpointer - with the in-memory one it works within a single
    process and no further.
    """
    question = state.get("pending_question") or "What would you like me to do?"
    answer = interrupt({"question": question, "session": summarise(state)})
    text = answer if isinstance(answer, str) else (answer or {}).get("answer", "")
    return {
        "answer": text,
        "pending_question": None,
        # The answer *is* the instruction now; keeping the original gives the router both halves.
        "instruction": f"{state.get('instruction', '')}\n{text}".strip(),
        "trace": [step("ask", question=question, answered=bool(text))],
    }


def after_route(state: DeckState) -> str:
    """Decision after route: map the chosen route to the first node of that route.

    Called by: LangGraph, as the conditional edge out of route (graph.build_graph).

    Where the request goes. One place, named, so the trace reads as a decision.
    """
    chosen = state.get("route")
    if chosen == "ask":
        return "ask"
    if chosen == "create":
        return "gather"
    if chosen == "translate":
        return "translate_plan"
    if chosen == "edit":
        return "split"
    if chosen == "custom":
        return "split"                    # a custom slide is an edit: add or draw it
    return "decline"                      # anything we do not do


def decline(state: DeckState) -> dict:
    """Say plainly that the request will not be done, and why - never quietly do something else.

    Graph node. Runs: when any decision returns 'decline' (unsupported route, nothing
        applicable, too many questions).
    Reads: route, route_reason.
    Writes: delivered = {ok: false, reason, supported}.
    Next: END.
    Called by: LangGraph, registered in graph.build_graph.

    Say plainly that we will not do this, and why. Never silently do something else.
    """
    chosen = state.get("route")
    why = state.get("route_reason") or "I could not work out what to do with this request."
    return {
        "delivered": {"ok": False, "route": chosen, "reason": why,
                      "supported": sorted(SUPPORTED)},
        "trace": [step("decline", route=chosen)],
    }
