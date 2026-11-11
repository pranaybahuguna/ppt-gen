# 3. State: typed fields, not a transcript

This is the decision the whole agent hangs on.

## Two things persist, and they are not the same

A conversation about a deck carries:

* **the conversation** - what was asked, what was said back;
* **the deck** - which version is current, which template and kit, what the outline says, what
  changed last, what the checks found.

The second must not live inside the first. If "the current deck" is only something mentioned
three messages ago, every step has to re-read the transcript to work out which file, which
version and which slide ids are meant. That is a text-matching problem invented out of nothing,
and it goes wrong exactly when the conversation gets long - which is when it matters.

So the deck lives in typed fields, and `deck_versions[-1]` is *the* answer to "which deck",
always.

## There is deliberately no `messages` field

deckops is meant to sit under a chat agent that already owns the conversation. That agent turns
"now make slide 2 shorter" into `edit_deck(deck_id, "make slide 2 shorter")`. Keeping messages
out means:

* **no transcript is fed to any model call.** The router gets a short structured summary of the
  session, not the chat. Stage 1 gets the brief and the content pack. Stage 2 gets one slide's
  content and the layouts that fit it. After ten edits, no call is carrying pages of old tool
  chatter.
* **the graph is testable.** Every node takes a dict and returns a dict. `tests/unit/test_graph.py`
  drives a whole conversation with no model and no services.
* **it is smaller.** There is no summarisation, no windowing, no "which message was that in".

If deckops ever has to talk to a person directly, the change is one field and one reducer:

```python
from langgraph.graph import add_messages

class DeckState(TypedDict, total=False):
    messages: Annotated[list, add_messages]     # the only line that is new
    ...
```

and the router reads `state["messages"][-1]` instead of `state["instruction"]`. Nothing else in
the graph needs to know, because nothing else in the graph reads the conversation.

## The fields

From `src/deck_agent/state.py`:

```python
class DeckState(TypedDict, total=False):
    # identity
    thread_id: str              # one deck session: the LangGraph thread and the S3 prefix
    run_id: str                 # one turn through the graph
    turn: int

    # the request, this turn
    instruction: str
    attachments: list[str]      # object URIs the caller supplied
    content_pack: dict | None   # the facts and datasets a new deck may use

    # routing
    route: str | None           # create | edit | translate | restyle | custom | ask | decline
    route_reason: str | None
    route_confidence: float | None
    route_by: str | None        # "rules" or "model" - worth knowing when a route is wrong
    target_lang: str | None
    slide_scope: list[int] | None

    # the deck
    source_deck: str | None     # the deck the person brought in, untouched
    template: str | None
    template_id: str | None     # content hash of the template: the kit cache key
    kit: str | None             # object URI of kit.json
    kit_patterns: list[str]     # names, for prompts and cheap decisions
    deck_versions: list[str]    # every version, oldest first. The last one is current.
    work_base: str | None       # the version this route started from

    # content
    outline: dict | None        # stage 1: what each slide says
    plan: dict | None           # stage 2: which layout, with what in it
    edit_actions: list[dict] | None
    content_pack: dict | None   # the facts the deck may state - written by `gather` from
    context_docs: list[str]     #   the request and these documents, unless supplied
    plan_jobs: list[int] | None # plan slide i holds outline slide plan_jobs[i]
    pending_code: list[dict]    # slides owed to code: reworks (over the layout budget,
                                #   already in the deck) and slides no layout can hold
    extra_ops: list[dict]       # deterministic repair ops (the font fallback)
    changelog: list[dict]

    # what the checks found
    findings: list[dict]
    structural: list[str]
    leaked: list[str]
    previews: list[str]
    repair_round: int

    # model-written code
    code_attempt: int
    code_slides: list[dict]     # each slide's code and context, kept so a rebuild redraws it
    code_feedback: list[str]

    # a person in the loop
    pending_question: str | None
    answer: str | None
    approved: dict

    # bookkeeping
    trace: Annotated[list[dict], operator.add]
    errors: Annotated[list[dict], operator.add]
    delivered: dict | None
```

## Three details that are deliberate

**`deck_versions` is a plain list, not an appending reducer.** With `operator.add` it could only
grow, and undo is dropping the last entry. Only one node writes it at a time, so a plain list is
safe - and `facade.undo()` is three lines because of it. The file the dropped version pointed at
stays in the bucket; undo is not a delete.

**`trace` and `errors` accumulate.** Several nodes append to them in one run, and a lost trace
entry is a lost debugging session. Every node adds exactly one `trace` entry, so the trace *is*
the path taken - and it is written into the delivered manifest.

**`work_base` exists because of fingerprints.** A translation or an edit compiles operations
that carry the fingerprint of the text they replace. Every attempt must re-apply to the version
the route *started* from, never to the output of the previous attempt, or the fingerprints no
longer match and the engine refuses the batch - correctly. `work_base` is that version.

## What a turn resets

`state.turn_reset()` is one visible place listing what a new request clears: the route, the
plan, the findings, the repair counter, the pending question. What survives is the deck itself -
its versions, its template, its kit, its outline. Without this, the second turn inherits the
first one's plan and "now translate it" quietly reuses a stale outline.

## What a router is given

Never the transcript:

```python
{"has_current_deck": True, "has_attached_deck": False, "deck_versions": 3,
 "has_template": True, "has_kit": True,
 "kit_patterns": ["chart", "closing", "columns", "cover", "panels", "stats", "steps", "table"],
 "turn": 4, "last_route": "edit"}
```
