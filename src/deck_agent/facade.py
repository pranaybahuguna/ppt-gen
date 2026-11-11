"""The outside of this system: what a host application calls.

deckops is meant to live under a chat agent that already owns the conversation. That agent
turns "now make slide 2 shorter" into `edit_deck(deck_id, "make slide 2 shorter")`, and these
functions are the whole surface it needs. `deck_id` is the conversation: it is the LangGraph
thread (so the state is already there) and the prefix in the bucket (so the files are too).

Everything returns the same shape - a `delivered` dict, or a question - so a host agent needs
one branch, not five:

    {"ok": true,  "deck": "s3://...", "url": "...", "previews": [...],
     "warnings": [...], "changelog": [...], "route": "edit", "versions": 3}
    {"ok": false, "question": "Which language should I translate it into?"}
    {"ok": false, "reason": "...", "errors": [...]}

`answer()` is how a question gets answered: it resumes the thread at the exact node that asked.
"""
from __future__ import annotations

import uuid

from .graph import deck_graph
from .settings import Settings, settings
from .state import DeckState, new_state, summarise, turn_reset


def _config(deck_id: str) -> dict:
    return {"configurable": {"thread_id": deck_id}}


def _run_id() -> str:
    import time

    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _result(state: dict) -> dict:
    """One shape for every outcome, so the caller has one branch."""
    if state.get("pending_question"):
        return {"ok": False, "question": state["pending_question"],
                "session": summarise(state)}      # type: ignore[arg-type]
    delivered = state.get("delivered")
    if delivered:
        return delivered
    return {"ok": False, "reason": state.get("route_reason") or "nothing was produced",
            "errors": state.get("errors") or []}


def _invoke(deck_id: str, payload: dict, planner=None, log=print, s: Settings | None = None) -> dict:
    s = s or settings()
    with deck_graph(planner, log, s) as graph:
        existing = graph.get_state(_config(deck_id))
        first = not (existing and existing.values)
        if first:
            state: DeckState = new_state(deck_id, payload.get("instruction", ""))
            state.update(payload)                 # type: ignore[typeddict-item]
            state["run_id"] = payload.get("run_id") or _run_id()
        else:
            turn = (existing.values.get("turn") or 1) + 1
            state = turn_reset(payload.get("instruction", ""), payload.get("run_id") or _run_id(),
                               turn, **{k: v for k, v in payload.items()
                                        if k not in ("instruction", "run_id")})  # type: ignore
        out = graph.invoke(state, config=_config(deck_id))
        if out.get("__interrupt__"):
            question = out["__interrupt__"][0].value
            return {"ok": False, "question": question.get("question") if isinstance(question, dict)
                    else str(question), "session": summarise(out)}
        return _result(out)


# --------------------------------------------------------------------------- the tools

def create_deck(deck_id: str, brief: str, content_pack: dict | None = None,
                template: str | None = None, planner=None, log=print, **kw) -> dict:
    """A new deck in a template's look. `content_pack` is the facts it is allowed to use."""
    return _invoke(deck_id, {"instruction": brief, "content_pack": content_pack,
                             "template": template, "route": None,
                             "attachments": [a for a in [template] if a], **kw},
                   planner, log)


def edit_deck(deck_id: str, instruction: str, planner=None, log=print, **kw) -> dict:
    """Change the deck this session is working on."""
    return _invoke(deck_id, {"instruction": instruction, **kw}, planner, log)


def translate_deck(deck_id: str, lang: str, slides: list[int] | None = None,
                   planner=None, log=print, **kw) -> dict:
    """The same deck in another language. `slides` are 1-based; None means all of them."""
    return _invoke(deck_id, {"instruction": f"translate into {lang}", "target_lang": lang,
                             "slide_scope": slides, "route": "translate", **kw}, planner, log)


def ask_deck(deck_id: str, instruction: str, planner=None, log=print, **kw) -> dict:
    """Let the router work out what is wanted. The general entry point."""
    return _invoke(deck_id, {"instruction": instruction, **kw}, planner, log)


def answer(deck_id: str, text: str, planner=None, log=print, s: Settings | None = None) -> dict:
    """Answer a question the graph asked, and carry on from the node that asked it."""
    from langgraph.types import Command

    s = s or settings()
    with deck_graph(planner, log, s) as graph:
        out = graph.invoke(Command(resume=text), config=_config(deck_id))
        if out.get("__interrupt__"):
            question = out["__interrupt__"][0].value
            return {"ok": False, "question": question.get("question") if isinstance(question, dict)
                    else str(question), "session": summarise(out)}
        return _result(out)


# --------------------------------------------------------------------------- the session

def session(deck_id: str, planner=None, s: Settings | None = None) -> dict:
    """What this session knows: which deck, how many versions, which template and kit."""
    s = s or settings()
    with deck_graph(planner, lambda *a, **k: None, s) as graph:
        snapshot = graph.get_state(_config(deck_id))
        if not (snapshot and snapshot.values):
            return {"exists": False, "deck_id": deck_id}
        values = snapshot.values
        return {"exists": True, "deck_id": deck_id, **summarise(values),
                "deck": (values.get("deck_versions") or [None])[-1],
                "versions": values.get("deck_versions") or [],
                "pending_question": values.get("pending_question"),
                "next_nodes": list(snapshot.next or ()),
                "trace": [t.get("node") for t in values.get("trace") or []]}


def undo(deck_id: str, planner=None, s: Settings | None = None) -> dict:
    """Go back one version.

    This is why versions are appended and never overwritten: undo is dropping the last entry,
    and the file it pointed at is still in the bucket if anyone wants it back.
    """
    s = s or settings()
    with deck_graph(planner, lambda *a, **k: None, s) as graph:
        snapshot = graph.get_state(_config(deck_id))
        versions = list((snapshot.values if snapshot else {}).get("deck_versions") or [])
        if len(versions) < 2:
            return {"ok": False, "reason": "there is no earlier version to go back to",
                    "versions": len(versions)}
        dropped = versions.pop()
        graph.update_state(_config(deck_id), {"deck_versions": versions, "delivered": None,
                                              "findings": [], "structural": [], "leaked": []})
        return {"ok": True, "deck": versions[-1], "dropped": dropped, "versions": len(versions)}
