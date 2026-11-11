# deckops-agent

A LangGraph agent over the [deckops](https://github.com/pranaybahuguna/deckops) engine.

deckops knows how to edit PowerPoint safely - typed operations, fingerprints, a byte-preserving
save, text that is checked against the template's own baseline. What it did not have was the
layer above: something that decides *what* the person wants, remembers which deck is current
across a conversation, and keeps the files somewhere real. That is this.

```
"a deck explaining this, for engineers" + notes.md   → create: facts gathered from the notes,
                                                       written into the template's look
"make slide 3 less wordy"                            → edit
"slide 4 looks repetitive, change the layout"        → edit, onto another of the template's layouts
"translate it into French"                           → translate
"restyle this onto the new template"                 → declined, with the nearest thing offered
```

A deck is built from your corporate template's own slides, and a template's few layouts are not
allowed to repeat: each is used at most twice per deck, and the slides over that are reworked by
model-written code - run in a locked-down container - into diagrams in the template's colours,
on its own header and footer.

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

The engine is cloned and **patched** (`patches/`, for templates built from grouped shapes);
its own 28 tests still pass with the patch applied.

## Start

**[docs/00-setup.md](docs/00-setup.md) takes a new machine from clone to first deck**, step by
step: prerequisites, ports, the model, ingesting a template, and what to do when a step fails.
In short:

```bash
git clone https://github.com/pranaybahuguna/ppt-gen.git && cd ppt-gen
make setup     # engine + patches, venv, config, sandbox image, fonts, samples  (~10 min)
make up        # postgres + S3, in Docker
make init      # checkpoint tables + bucket
make doctor    # should say READY
make demo      # a whole conversation about a deck, with no model at all
```

Needs Docker, git, make and Python 3.12. Needs **no** LibreOffice, poppler or fonts installed -
the image has them, and `make fonts` copies the fonts out so the agent measures text exactly.

For a real model, copy `llm.example.json` to `llm.json` (git-ignored) and fill it in:

```json
{"base_url": "https://api.openai.com", "model": "gpt-6.1-sol", "api_key": "sk-..."}
```

## Use

```bash
.venv/bin/deckops kit "Your Template.pptx" --out local/templates/x/kit.json --make-default
.venv/bin/deckops create  my-deck "explain the pilot to the ops review" --context notes.md
.venv/bin/deckops edit    my-deck "make slide 3 less wordy"
.venv/bin/deckops translate my-deck fr --slides 1,2
.venv/bin/deckops session my-deck
.venv/bin/deckops undo    my-deck
.venv/bin/deckops get     <deck uri> my-deck.pptx
.venv/bin/deckops ls                  # what is in the bucket
.venv/bin/deckops clean --tidy        # dry run; add --yes. See docs/07
```

From a host chat agent, the same six verbs:

```python
from deck_agent import facade
facade.create_deck(deck_id, brief, template=uri, context=[doc_uri])   # or content_pack=...
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
src/deck_agent/   the agent: graph, state, nodes, engine helpers, sandbox clients, model layer
runner/           what lives inside the sandbox image
engine/deckops/   the engine, cloned and patched (`make engine`; git-ignored)
patches/          the engine patch, and why
config/           agent.example.json - copy to agent.json (`make config`)
docker/           compose, the two Dockerfiles, the S3 identities
tests/unit/       244 tests that pass with nothing running
tests/integration/ 19 tests that need Docker, Postgres and S3, and skip with a reason otherwise
tests/demo.py     a whole conversation, offline            -> make demo
tests/validate_real_model.py   every model call, against a real endpoint
docs/             why, how, and what is still missing
```

## Documentation

| | |
|---|---|
| [0. Setting it up](docs/00-setup.md) | **a new machine, from clone to first deck** |
| [1. Why one process](docs/01-why-one-process.md) | the central decision, and what it costs |
| [2. Architecture](docs/02-architecture.md) | the four pieces and where the old design went |
| [3. State](docs/03-state.md) | typed fields, and why there is no `messages` |
| [4. The graph](docs/04-graph.md) | nodes, named decisions, and each route step by step |
| [5. Infrastructure](docs/05-infrastructure.md) | Postgres, S3, the image, settings |
| [6. The code runner](docs/06-code-runner.md) | model-written code: reworked slides, its four fences |
| [7. Running it](docs/07-operations.md) | use, ingesting templates, the bucket, clean-up, production |
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

Since then: decks are written from whatever context you bring (`--context`, or notes in the
request), every fact checked against its source; the layout budget and reworked slides stop a
deck repeating three layouts; `deckops clean` / `ls` keep the bucket and the conversations
tidy together; and one template kit is kept at a time (`single_kit`).

Still missing, in order: facts enforcement on finished slides, structured per-slide output,
restyle, picture slots. `docs/08-status.md` has
the full list, the measurements, and the defects found by looking at rendered slides rather than
at test output.
