"""route - decide what this request is, before spending anything on it.

Rules first, a model only when the rules are unsure. That ordering is the whole point: most
requests are obvious ("make me a deck about X" with nothing attached can only be a new deck;
"translate it into French" can only be a translation), and a model call to confirm the obvious
is pure cost and a new way to be wrong.

The model is given the request and a *summary* of the session - is there a current deck, is
there a template, which patterns does its kit have - never the conversation. A router that
needs the transcript to know which deck is meant is a router built on sand; `state.py` says
more about why.

When neither the rules nor the model are confident, the graph stops and asks one short
question. That is much cheaper than confidently rebuilding the wrong deck.
"""
from __future__ import annotations

from langgraph.types import interrupt

from ..llm.planner import rules_route
from ..settings import settings
from ..state import SUPPORTED, DeckState, step, summarise

MAX_ASKS = 2


def route(state: DeckState, planner=None) -> dict:
    s = settings()
    instruction = state.get("instruction") or ""
    context = summarise(state)

    guess = rules_route(instruction, context)
    by = "rules"
    # The rules are allowed to decide only when they are sure AND they reached a real route.
    if guess["route"] == "ask" or guess["confidence"] < s.router_confidence_floor:
        if planner is not None and hasattr(planner, "route"):
            try:
                answer = planner.route(instruction, context) or {}
                if answer.get("route"):
                    guess, by = {**guess, **answer}, "model"
            except Exception as e:
                guess = {**guess, "reason": f"{guess['reason']} (the router model failed: "
                                            f"{e.__class__.__name__})"}

    chosen = guess.get("route") or "ask"
    confidence = float(guess.get("confidence") or 0)
    asked = sum(1 for t in state.get("trace") or [] if t.get("node") == "ask")
    if confidence < s.router_confidence_floor and chosen != "ask" and asked < MAX_ASKS:
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
    if chosen == "ask":
        if asked >= MAX_ASKS:
            out["route"] = "decline"
            out["route_reason"] = ("I asked twice and still cannot tell what to do with this deck")
        else:
            out["pending_question"] = (guess.get("question")
                                       or "What would you like me to do with this deck?")
    return out


def ask(state: DeckState) -> dict:
    """Stop and ask the person one question. The graph genuinely stops here.

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
    """Where the request goes. One place, named, so the trace reads as a decision."""
    chosen = state.get("route")
    if chosen == "ask":
        return "ask"
    if chosen == "create":
        return "ensure_kit"
    if chosen == "translate":
        return "translate_plan"
    if chosen == "edit":
        return "edit_plan"
    if chosen == "custom":
        return "custom_code"
    return "decline"                      # restyle, or anything we do not do yet


def decline(state: DeckState) -> dict:
    """Say plainly that we will not do this, and why. Never silently do something else."""
    chosen = state.get("route")
    why = {
        "restyle": "moving a deck onto a different template is not supported yet. What I can do "
                   "is build a new deck in the new template from this one's content - say the "
                   "word and I will.",
    }.get(chosen, state.get("route_reason") or "I could not work out what to do with this request.")
    return {
        "delivered": {"ok": False, "route": chosen, "reason": why,
                      "supported": sorted(SUPPORTED)},
        "trace": [step("decline", route=chosen)],
    }
