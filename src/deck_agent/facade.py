"""The outside of this system: what a host application calls.

deckops is meant to live under a chat agent that already owns the conversation. That agent
turns "now make slide 2 shorter" into `edit_deck(deck_id, "make slide 2 shorter")`, and these
functions are the whole surface it needs. `deck_id` is the conversation: it is the LangGraph
thread (so the state is already there) and the folder in the store (so the files are too).

Everything returns the same shape - a `delivered` dict, or a question - so a host agent needs
one branch, not five:

    {"ok": true,  "deck": "local://...", "url": "...", "previews": [...],
     "warnings": [...], "changelog": [...], "route": "edit", "versions": 3}
    {"ok": false, "question": "Which language should I translate it into?"}
    {"ok": false, "reason": "...", "errors": [...]}

`answer()` is how a question gets answered: it resumes the thread at the exact node that asked.
"""
from __future__ import annotations

from pathlib import Path

import uuid

from .graph import deck_graph
from .settings import Settings, settings
from .state import DeckState, new_state, summarise, turn_reset


def _config(deck_id: str) -> dict:
    """The LangGraph config that selects this deck's conversation: thread_id = deck_id.

    Called by: _invoke, answer, session, undo.
    """
    return {"configurable": {"thread_id": deck_id}}


def _run_id() -> str:
    """A new run id, `YYYYmmdd-HHMMSS-xxxxxx`: one per turn, and the run folder's name in the
    store.

    Called by: _invoke, when the caller does not supply one.
    """
    import time

    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _keep_recent_versions(deck_id: str, result: dict, s: Settings) -> dict:
    """After a delivered request: one version per request, then the newest `keep_versions`.

    A request can publish several versions on its way - an edit's first pass, then the pass the
    review sent it round for. Only the last is the request's result; the ones before it are
    dropped here, so every version that remains is the deck after one request, and undo goes
    back exactly one request. Then, with `keep_versions` on, only the newest N are kept (2 =
    the current deck and the one undo goes back to).

    Versions are dropped from the conversation's state first, then their files deleted, so a
    failure halfway leaves files nothing points at (which `clean --orphans` finds), never a
    state pointing at files that are gone.

    Called by: _invoke and answer, after each turn that produced a deck.
    """
    if not result.get("deck"):
        return result
    from .infra.checkpointer import checkpointer
    from .infra.storage import store

    with checkpointer(s) as saver:
        from .graph import build_graph

        graph = build_graph(None, lambda *a, **k: None).compile(checkpointer=saver)
        snapshot = graph.get_state(_config(deck_id))
        values = snapshot.values if snapshot else {}
        versions = list(values.get("deck_versions") or [])
        base = min(values.get("turn_base") or 0, len(versions))
        passes = versions[base:-1]                       # this request's intermediate versions
        kept = versions[:base] + versions[-1:]
        if s.keep_versions > 0 and len(kept) > s.keep_versions:
            old, kept = kept[:-s.keep_versions], kept[-s.keep_versions:]
        else:
            old = []
        dropped = passes + old
        if not dropped:
            return result
        graph.update_state(_config(deck_id), {"deck_versions": kept})
    try:
        store(s).delete(dropped)
    except Exception:                                    # noqa: BLE001 - orphans, found by clean
        pass
    return {**result, "versions": len(kept), "versions_deleted": len(dropped)}


def _tidy_if_asked(deck_id: str, result: dict, s: Settings) -> dict:
    """`tidy_after_turn`: once a turn has delivered, drop the bulk the session no longer needs.

    Only after a delivered result: a turn that stopped to ask a question is still running, and
    its outline is exactly what the answer will be applied to. A failed tidy never fails the
    turn - the deck was already delivered - it is reported beside it instead.

    Called by: _invoke, answer.
    """
    if not (s.tidy_after_turn and result.get("ok")):
        return result
    from .infra.checkpointer import checkpointer
    from .infra.cleanup import tidy_session
    from .infra.storage import store

    try:
        with checkpointer(s) as saver:
            result = {**result, "tidied": tidy_session(store(s), saver, deck_id)}
    except Exception as e:                       # noqa: BLE001
        result = {**result, "tidied": {"error": f"{e.__class__.__name__}: {e}"}}
    return result


def _result(state: dict) -> dict:
    """One shape for every outcome, so the caller has one branch.

    Called by: _invoke, answer.
    """
    if state.get("pending_question"):
        return {"ok": False, "question": state["pending_question"],
                "session": summarise(state)}      # type: ignore[arg-type]
    delivered = state.get("delivered")
    if delivered:
        return delivered
    return {"ok": False, "reason": state.get("route_reason") or "nothing was produced",
            "errors": state.get("errors") or []}


def _invoke(deck_id: str, payload: dict, planner=None, log=print, s: Settings | None = None) -> dict:
    """Run one turn of a deck's conversation through the graph and return the result.

    On a new deck_id it builds a fresh state (state.new_state); on an existing one it resets the
    per-turn fields (state.turn_reset) and keeps the deck, its template and storyline. Stops early with a
    question when the graph interrupts. Afterwards, with tidy_after_turn on, tidies the deck.

    Called by: create_deck, edit_deck, translate_deck, ask_deck.
    """
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
        # Where this request's versions begin: undo goes back to the version before it.
        start = payload["deck_versions"] if "deck_versions" in payload else (
            (existing.values.get("deck_versions") if existing and existing.values else None) or [])
        state["turn_base"] = len(start)
        out = graph.invoke(state, config=_config(deck_id))
        if out.get("__interrupt__"):
            question = out["__interrupt__"][0].value
            return {"ok": False, "question": question.get("question") if isinstance(question, dict)
                    else str(question), "session": summarise(out)}
    return _tidy_if_asked(deck_id, _keep_recent_versions(deck_id, _result(out), s), s)


# --------------------------------------------------------------------------- the tools

def create_deck(deck_id: str, brief: str, content_pack: dict | None = None,
                template: str | None = None, planner=None, log=print,
                context: list[str] | None = None, **kw) -> dict:
    """A new deck in a template's look.

    `brief` is what the person said - it may carry all the context there is. `context` is any
    documents (object URIs) the deck should be based on. From those two, `gather` writes the
    facts the deck may state. `content_pack` skips that and supplies the facts directly.

    Called by: cli.cmd_create; tests/demo.py.
    """
    return _invoke(deck_id, {"instruction": brief, "content_pack": content_pack,
                             "template": template, "route": None,
                             "context_docs": list(context or []),
                             "attachments": [a for a in [template] if a], **kw},
                   planner, log)


def edit_deck(deck_id: str, instruction: str, planner=None, log=print, **kw) -> dict:
    """Change the deck this session is working on.

    Called by: cli.cmd_edit; tests/demo.py.
    """
    return _invoke(deck_id, {"instruction": instruction, **kw}, planner, log)


def translate_deck(deck_id: str, lang: str, slides: list[int] | None = None,
                   planner=None, log=print, **kw) -> dict:
    """The same deck in another language. `slides` are 1-based; None means all of them.

    Called by: cli.cmd_translate; tests/demo.py.
    """
    return _invoke(deck_id, {"instruction": f"translate into {lang}", "target_lang": lang,
                             "slide_scope": slides, "route": "translate", **kw}, planner, log)


def attach_deck(deck_id: str, deck_uri: str, instruction: str | None = None, planner=None,
                log=print, s: Settings | None = None) -> dict:
    """Make an existing .pptx this deck's newest version, then do what the instruction says.

    The file is checked like any upload, copied into the deck's folder as the next version, and
    the instruction runs on it - whether or not the deck already had versions (attaching is how
    a hand-edited copy comes back). Earlier versions stay, so undo can go back to them. A file
    meant as a *template* is passed to create_deck instead.

    Called by: cli.cmd_attach; the server's POST /decks/{id}/attach.
    """
    import tempfile

    from .engine import guard_deck
    from .infra.storage import store
    from .nodes.common import version_number

    s = s or settings()
    with deck_graph(planner, lambda *a, **k: None, s) as graph:
        existing = graph.get_state(_config(deck_id))
        versions = list((existing.values if existing and existing.values else {}).get("deck_versions") or [])
    data = store(s).get_bytes(deck_uri)
    with tempfile.TemporaryDirectory(dir=s.workspace_root) as tmp:
        local = Path(tmp) / "attached.pptx"
        local.write_bytes(data)
        guard_deck(local)                               # untrusted input: check the archive first
    newest = version_number(versions[-1]) if versions else 0
    new = store(s).put_bytes(data, f"{deck_id}/v{(newest or len(versions)) + 1:03d}.pptx")
    text = instruction or "what can you do with this deck?"
    return _invoke(deck_id, {"instruction": text, "deck_versions": versions + [new],
                             "source_deck": deck_uri}, planner, log, s)


def ask_deck(deck_id: str, instruction: str, planner=None, log=print, **kw) -> dict:
    """Let the router work out what is wanted. The general entry point.

    Called by: cli.cmd_ask; tests/demo.py.
    """
    return _invoke(deck_id, {"instruction": instruction, **kw}, planner, log)


def answer(deck_id: str, text: str, planner=None, log=print, s: Settings | None = None) -> dict:
    """Answer a question the graph asked, and carry on from the node that asked it.

    Called by: cli.cmd_answer; tests/demo.py.
    """
    from langgraph.types import Command

    s = s or settings()
    with deck_graph(planner, log, s) as graph:
        out = graph.invoke(Command(resume=text), config=_config(deck_id))
        if out.get("__interrupt__"):
            question = out["__interrupt__"][0].value
            return {"ok": False, "question": question.get("question") if isinstance(question, dict)
                    else str(question), "session": summarise(out)}
    return _tidy_if_asked(deck_id, _keep_recent_versions(deck_id, _result(out), s), s)


# --------------------------------------------------------------------------- the session

def session(deck_id: str, planner=None, s: Settings | None = None) -> dict:
    """What this session knows: which deck, how many versions, which template.

    Called by: cli.cmd_session; tests/demo.py.
    """
    s = s or settings()
    with deck_graph(planner, lambda *a, **k: None, s) as graph:
        snapshot = graph.get_state(_config(deck_id))
        if not (snapshot and snapshot.values):
            return {"exists": False, "deck_id": deck_id}
        values = snapshot.values
        return {"exists": True, "deck_id": deck_id, **summarise(values),
                "deck": (values.get("deck_versions") or [None])[-1],
                "versions": values.get("deck_versions") or [],
                "template": values.get("template"),
                "pending_question": values.get("pending_question"),
                "next_nodes": list(snapshot.next or ()),
                "trace": [t.get("node") for t in values.get("trace") or []]}


def undo(deck_id: str, planner=None, s: Settings | None = None) -> dict:
    """Go back one request: each version is the deck after one request (_keep_recent_versions).

    Versions are appended and never overwritten, so undo is dropping the last entry, and the
    file it pointed at is still in the store if anyone wants it back.

    Called by: cli.cmd_undo; tests/demo.py.
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
