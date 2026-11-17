"""The graph's state: typed fields, not a transcript.

This is the design decision the whole agent hangs on, so it is worth stating plainly.

Two different things persist across a conversation about a deck:

* **the conversation** - what was asked, what was answered;
* **the deck** - which version is current, which template it uses, what its storyline
  says, what changed last, what the checks found.

They are not the same, and the second must not live inside the first. If "the current deck" is
only something that was mentioned three messages ago, then every step has to re-read the
transcript to work out which file, which version and which slide ids are meant - and that goes
wrong. So the deck lives in typed fields, and `deck_versions[-1]` is *the* answer to "which
deck", always.

**There is deliberately no `messages` field.** deckops is meant to sit under a chat agent that
already owns the conversation; that agent turns "now make slide 2 shorter" into
`edit_deck(deck_id, "make slide 2 shorter")`. Keeping messages out makes this graph smaller,
cheaper to run (no transcript is fed into any model call) and far easier to test.

If deckops ever has to talk to a person directly, the change is one field and one reducer::

    from langgraph.graph import add_messages
    class DeckState(TypedDict, total=False):
        messages: Annotated[list, add_messages]
        ...

and the router reads `state["messages"][-1]` instead of `state["instruction"]`. Nothing else in
the graph needs to know.

Reducers: `trace` and `errors` accumulate, because several nodes append to them in one run and
a lost trace entry is a lost debugging session. Everything else is last-write-wins, which is
what makes `deck_versions` able to *shrink* - that is how undo works.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

#: A reducer that accumulates within a turn but can be emptied at the start of one.
#: `operator.add` cannot: returning `[]` from a node appends nothing, so there is no way to
#: say "forget the last turn's errors" - and a deck that failed once then reported that
#: failure on every later turn, which is how this was found.
CLEAR = "__clear__"


def accumulate(old: list, new):
    """Reducer for list fields that build up over a turn (errors, slide_specs, task_results): appends what a
    node returns, and empties the list when a node returns CLEAR.

    Called by: LangGraph, whenever a node returns one of those fields.
    """
    if new == CLEAR:
        return []
    return (old or []) + (new or [])

Route = Literal["create", "edit", "translate", "restyle", "custom", "ask"]

#: Routes the graph can actually carry out today. `restyle` is recognised and declined with a
#: straight answer rather than silently doing something else (deckops backlog item 11).
SUPPORTED: set[str] = {"create", "edit", "translate", "custom"}


class DeckState(TypedDict, total=False):
    # ---- identity ------------------------------------------------------------------
    """Everything one conversation about a deck knows, checkpointed after every node.

    Typed fields rather than a chat transcript: the current deck is deck_versions[-1], files are
    s3:// references, and per-turn fields are reset by turn_reset at the start of each turn.
    Each field's comment says what it holds; the nodes' docstrings say who reads and writes it.
    """
    thread_id: str                  # one deck session; the LangGraph thread and the S3 prefix
    run_id: str                     # one turn through the graph
    turn: int                       # how many requests this session has handled

    # ---- the request, this turn ----------------------------------------------------
    instruction: str                # what to do, in the person's words
    attachments: list[str]          # object URIs the caller supplied (a deck, a template)
    content_pack: dict | None       # the facts and datasets a new deck may use - written by
                                    # `gather` from the context, unless the caller supplies it
    context_docs: list[str]         # object URIs of documents the deck should be based on

    # ---- routing -------------------------------------------------------------------
    route: Route | None
    route_reason: str | None
    route_confidence: float | None
    route_by: str | None            # "rules" or "model" - worth knowing when a route is wrong
    target_lang: str | None
    slide_scope: list[int] | None   # 1-based slide numbers the request is about; None = all

    # ---- the deck ------------------------------------------------------------------
    source_deck: str | None         # the deck the person brought in, untouched
    template: str | None            # the template whose look we are matching
    template_id: str | None         # content hash of the template
    deck_versions: list[str]        # every version, oldest first. The last one is current.
    work_base: str | None           # the version this route started from. Ops carry fingerprints
                                    # taken from it, so every attempt re-applies to *this* file,
                                    # never to the output of a previous attempt.

    # ---- content -------------------------------------------------------------------
    outline: dict | None            # the storyline, as shown for approval
    plan: dict | None               # the translate route's plan for this turn
    # ---- the create route: storyline -> write (parallel) -> build -> review ----------------
    story: dict | None              # the deck's argument: title, sections, a plan per slide
    deck_frame: list[dict] | None   # [{spec, plan}] in deck order; plan None = cover / divider
    slide_specs: Annotated[list[dict], accumulate]    # one per finished `write`
    slides_wanted: str | None       # how many content slides, e.g. "8-12"
    # ---- the edit route: split -> work (parallel) -> join -> review ------------------------
    edit_tasks: list[dict] | None   # [{id, kind, slide, after, instruction, round}] from split
    edit_summary: str | None        # split's one-sentence account of what will change
    task_results: Annotated[list[dict], accumulate]   # one per finished `work`, every round
    edit_round: int                 # 0 = first pass; 1 = the pass after review
    changed: list[dict]             # [{task, kind, instruction, slide}] slide = its position now
    revise: list[dict]              # review's second-pass tasks with their feedback
    extra_ops: list[dict]           # deterministic repair ops added on top of a compiled plan
                                    # (the font-size fallback), kept apart so the plan stays the
                                    # model's answer and the fallback stays ours
    changelog: list[dict]           # what the last applied batch did, in words

    # ---- what the checks found -----------------------------------------------------
    findings: list[dict]            # overflow / overlap, already filtered against the baseline
    structural: list[str]
    leaked: list[str]               # template example content left in a new deck
    previews: list[str]             # object URIs of rendered PNGs
    vision_notes: list[str]         # what the vision model saw but was not asked to fix
    repair_round: int

    # ---- the escape hatch ----------------------------------------------------------

    # ---- a person in the loop ------------------------------------------------------
    pending_question: str | None    # set when the graph stopped to ask
    answer: str | None              # their reply, on resume
    approved: dict                  # {"outline": True} once approved

    # ---- bookkeeping ---------------------------------------------------------------
    trace: Annotated[list[dict], operator.add]
    errors: Annotated[list[dict], accumulate]
    delivered: dict | None          # the result: deck URI, previews, warnings, changelog


def new_state(thread_id: str, instruction: str, **kw: Any) -> DeckState:
    """A state for a first turn. Later turns resume the thread and keep what is already there.

    Called by: facade._invoke.
    """
    base: DeckState = {
        "thread_id": thread_id, "instruction": instruction, "turn": 1,
        "attachments": [], "context_docs": [], "deck_versions": [],
        "findings": [], "structural": [], "leaked": [], "previews": [],
        "vision_notes": [],
        "repair_round": 0, "extra_ops": [],
        "approved": {}, "changelog": [], "trace": [], "errors": [],
        "pending_question": None, "answer": None, "delivered": None,
    }
    base.update(kw)  # type: ignore[typeddict-item]
    return base


def turn_reset(instruction: str, run_id: str, turn: int, **kw) -> dict:
    """The fields a new request must clear.

    Without this, the second turn in a conversation inherits the first one's plan, findings and
    repair counter, and then "now translate it" quietly reuses a stale outline. What *survives*
    a turn is the deck itself: its versions, its template, its storyline. Everything
    about the last piece of work is cleared here, on purpose, in one visible place.

    Called by: facade._invoke.
    """
    return {
        "instruction": instruction, "run_id": run_id, "turn": turn,
        "route": None, "route_reason": None, "route_confidence": None, "route_by": None,
        "plan": None, "work_base": None,
        "edit_tasks": None, "edit_summary": None, "task_results": CLEAR, "edit_round": 0,
        "slide_specs": CLEAR, "deck_frame": None,
        "changed": [], "revise": [],
        "findings": [], "structural": [], "leaked": [], "previews": [], "vision_notes": [],
        "repair_round": 0, "extra_ops": [], "changelog": [], "pending_question": None, "answer": None,
        "delivered": None, "slide_scope": None, "target_lang": None,
        # errors belong to the turn that produced them, not to the session
        "errors": CLEAR,
        **kw,
    }


def current_deck(state: DeckState) -> str | None:
    """The one answer to "which deck are we talking about".

    Called by: nodes.deliver.deliver, nodes.translate.translate_plan, revise.nodes (split,
        join, review), summarise.
    """
    versions = state.get("deck_versions") or []
    return versions[-1] if versions else None


def summarise(state: DeckState) -> dict:
    """The short description of the session a router sees - never the transcript.

    Called by: facade._invoke, facade._result, facade.answer, facade.session, nodes.router.ask,
        nodes.router.route.
    """
    return {
        "has_current_deck": bool(current_deck(state)),
        "has_attached_deck": bool(state.get("source_deck")),
        "deck_versions": len(state.get("deck_versions") or []),
        "has_template": bool(state.get("template")),
        "turn": state.get("turn", 1),
        "last_route": state.get("route"),
    }


def step(name: str, **fields: Any) -> dict:
    """One entry for `trace`. Every node adds exactly one, so the trace is the path taken.

    Called by: every graph node (design.nodes, revise.nodes, nodes.*).
    """
    import time

    return {"node": name, "at": round(time.time(), 3), **fields}
