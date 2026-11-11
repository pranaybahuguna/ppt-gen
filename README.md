# deckops-agent

A LangGraph agent over the [deckops](https://github.com/pranaybahuguna/deckops) engine.

deckops knows how to edit PowerPoint safely - typed operations, fingerprints, a byte-preserving
save, text that is checked against the template's own baseline. What it did not have was the
layer above: something that decides *what* the person wants, remembers which deck is current
across a conversation, and keeps the files somewhere real. That is this.

```
"write me a deck about the support bot pilot"        → create
"make slide 3 less wordy"                            → edit
"translate it into French"                           → translate
"turn these 8 steps into 6"                          → edit, via the card-grid compiler
"restyle this onto the new template"                 → declined, with the nearest thing offered
```

## The shape of it

Four pieces. Three run; the fourth is an image used once per job and thrown away.

| | What | Why |
|---|---|---|
| **agent** | one process: the LangGraph graph *and* the whole deckops engine | everything deterministic is pure Python - no subprocess, no native dependency |
| **postgres** | the checkpointer | the state after every node, keyed by conversation, so turn two knows what turn one did and `interrupt()` can genuinely stop |
| **S3** | every version of every deck, kits, previews, manifests | a checkpointed thread may resume in another process; a local path in the state would be a lie |
| **sandbox image** | `--network none`, read-only, no capabilities, one container per job | model-written code (a trust boundary) and LibreOffice rendering (a dependency boundary) |

The deckops engine originally split itself into a control plane and a sandboxed data plane. That
boundary turned out to be in the wrong place: probing, compiling, applying edits, the
byte-preserving save and the geometry checks are all pure `python-pptx` and `lxml`, with no
network and no native code. The only part that genuinely needs containing is the native
toolchain - and anything a model wrote. So this runs one process and keeps one container, for
those two jobs. `docs/01-why-one-process.md` makes the argument in full, including what is given
up.

The engine itself is cloned and **used unmodified**; its own 28 tests still pass.

## Start

```bash
make setup     # clone the engine, venv, build the image, extract fonts, build samples
make up        # postgres + S3
make init      # checkpoint tables + bucket
make doctor    # should say READY
make demo      # a whole conversation about a deck, with no model at all
```

Needs Docker, git and Python 3.12. Needs **no** LibreOffice, poppler or fonts installed - the
image has them, and `make fonts` copies the fonts out so the agent measures text exactly.

For a real model, write `llm.json` (git-ignored) and nothing else:

```json
{"base_url": "https://api.openai.com", "model": "gpt-6.1-sol", "api_key": "sk-..."}
```

## Use

```bash
.venv/bin/python -m deck_agent.cli seed
.venv/bin/python -m deck_agent.cli create  my-deck "the support bot pilot" --content local/content.json
.venv/bin/python -m deck_agent.cli edit    my-deck "make slide 3 less wordy"
.venv/bin/python -m deck_agent.cli translate my-deck fr --slides 1,2
.venv/bin/python -m deck_agent.cli session my-deck
.venv/bin/python -m deck_agent.cli undo    my-deck
```

From a host chat agent, the same six verbs:

```python
from deck_agent import facade
facade.create_deck(deck_id, brief, content_pack, template)
facade.edit_deck(deck_id, "make slide 2 shorter")
facade.translate_deck(deck_id, "fr", slides=[1, 2])
facade.ask_deck(deck_id, "...")      # let the router decide
facade.answer(deck_id, "...")        # answer a question the graph asked
facade.undo(deck_id)
```

`deck_id` is the conversation. Everything returns one shape, so the caller needs one branch.

## State: typed fields, not a transcript

There is deliberately **no `messages` field**. deckops is meant to sit under a chat agent that
already owns the conversation; the deck belongs in typed fields, so `deck_versions[-1]` is
always the answer to "which deck" and no model call ever carries the chat history. If deckops
ever talks to a person directly, it is one field and one reducer -
`docs/03-state.md` has the argument.

## Where things are

```
src/deck_agent/   the agent: graph, state, nodes, engine bridge, sandbox clients, model layer
runner/           what lives inside the sandbox image
engine/deckops/   the engine, cloned and unmodified
docker/           compose, the two Dockerfiles, the S3 identities
tests/unit/       84 tests that pass with nothing running
tests/integration/ 18 tests that need Docker, Postgres and S3, and skip with a reason otherwise
tests/demo.py     a whole conversation, offline            -> make demo
tests/validate_real_model.py   every model call, against a real endpoint
docs/             why, how, and what is still missing
```

## Documentation

| | |
|---|---|
| [1. Why one process](docs/01-why-one-process.md) | the central decision, and what it costs |
| [2. Architecture](docs/02-architecture.md) | the four pieces and where the old design went |
| [3. State](docs/03-state.md) | typed fields, and why there is no `messages` |
| [4. The graph](docs/04-graph.md) | nodes, named decisions, and each route step by step |
| [5. Infrastructure](docs/05-infrastructure.md) | Postgres, S3, the image, settings |
| [6. The code runner](docs/06-code-runner.md) | the escape hatch and its four fences |
| [7. Running it](docs/07-operations.md) | setup, use, reading a run, production |
| [8. Status](docs/08-status.md) | what works, what was measured, what is missing |
| [9. Testing](docs/09-testing.md) | how it was tested, and where the coverage is thin |

## State of it

Everything in the handoff document's "missing" list is built: the router, a general edit
planner, the kit cache, a no-fit fallback, and real-model validation of every call (6/6 against
`gpt-6.1-sol`, vision included, 1.00 attempts per slide in stage 2). Visual changes - colours, weight, size, and the deck's own theme colours - work too, as a
deterministic byte-preserving pass, because the engine's op language has no way to express them.
And the "look at the rendered slides" step is automated: a vision model reviews every rendered
slide and what it sees becomes findings the repair loop acts on (`vision_review`, off by
default). It found both of this project's real visual defects cold, and reported nothing on a
clean deck.
Still missing, in order: facts enforcement, structured per-slide output, restyle, picture slots. `docs/08-status.md` has
the full list, the measurements, and the defects found by looking at rendered slides rather than
at test output.
