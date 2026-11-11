# 7. Running it

## From a fresh clone

```bash
make setup      # clone the engine, make the venv, build the image, extract the fonts, build samples
make up         # postgres + S3
make init       # checkpoint tables + bucket
make doctor     # check everything
```

`make setup` needs Docker, `git`, and Python 3.12. It needs **no** LibreOffice, no poppler and
no fonts installed: the image has them, and `make fonts` copies the fonts out.

A healthy `doctor`:

```
"engine":    {"commit": "e40b7b2"}
"fonts":     {"calibri": "Carlito-Regular.ttf", "_exact": true}
"sandbox":   {"ok": true, "detail": "docker 24.0.2, image deckops/sandbox:0.1.0"}
"checkpointer": {"ok": true, "kind": "PostgresSaver"}
"object_storage": {"ok": true, "bucket": "deckops"}
"problems": []
READY
```

Each problem names its fix. A missing `llm.json` is reported but is not a problem - the offline
stub planner runs the whole graph without one.

## A model

```json
{"base_url": "https://api.openai.com", "model": "gpt-6.1-sol", "api_key": "sk-..."}
```

in `llm.json` at the repository root. It is git-ignored, and nothing else reads a model
credential - not the environment, not `config/agent.json`. Any OpenAI-compatible
`/v1/chat/completions` endpoint works: a gateway, vLLM, a hosted API.

`llm_client=langchain` sends the same prompts through `langchain-openai` instead, for tracing
and provider switching. `llm_client=stub` is the offline planner.

## Using it

```bash
# the whole thing, with no model at all
make demo

# a deck
.venv/bin/python -m deck_agent.cli seed                      # sample template into the bucket
.venv/bin/python -m deck_agent.cli create my-deck \
    "the support bot pilot, for the operations review" --content local/content.json

# carry on in the same session
.venv/bin/python -m deck_agent.cli edit      my-deck "make slide 3 less wordy"
.venv/bin/python -m deck_agent.cli translate my-deck fr --slides 1,2
.venv/bin/python -m deck_agent.cli ask       my-deck "add a risks slide after slide 4"
.venv/bin/python -m deck_agent.cli answer    my-deck "yes, translate it"

# look at what happened
.venv/bin/python -m deck_agent.cli session my-deck
.venv/bin/python -m deck_agent.cli undo    my-deck
.venv/bin/python -m deck_agent.cli get     s3://deckops/my-deck/v002.pptx deck.pptx

# a template, learned once and cached by content hash
.venv/bin/python -m deck_agent.cli kit corporate.pptx --out kit.json
```

**Ingesting a template** is that one command. It prepares the file first (making nested shape
ids unique, which real templates often need and the engine cannot work around), drafts a
pattern per usable slide, checks every id, renders the slides, has a vision model label the
boxes, builds test decks to accept or reject each pattern, and measures how many items each
one really holds. It prints what passed and what was dropped, with the reason for each.

If preparation changed anything it writes `<name>-prepared.pptx` beside the kit and names it
as `use_this_template`. **Use that file from then on**: a kit addresses shapes by id, so it
only makes sense against the copy it was built from. A template that needed no preparation
comes through byte-identical and keeps its own name and hash.

A template that yields no patterns is not cached, and says so.

Every command prints the same JSON shape the host application receives.

## From a host application

```python
from deck_agent import facade

facade.create_deck(deck_id, brief, content_pack, template)
facade.edit_deck(deck_id, "make slide 2 shorter")
facade.translate_deck(deck_id, "fr", slides=[1, 2])
facade.ask_deck(deck_id, "...")        # let the router decide
facade.answer(deck_id, "...")          # answer a question the graph asked
facade.session(deck_id)                # what this session knows
facade.undo(deck_id)
```

`deck_id` is the conversation: the LangGraph thread and the prefix in the bucket. One shape
back, so the caller needs one branch:

```json
{"ok": true,  "deck": "s3://...", "url": "https://...", "previews": [...],
 "warnings": [], "changelog": [...], "route": "edit", "versions": 3,
 "manifest": "s3://..."}

{"ok": false, "question": "Which language should I translate it into?"}

{"ok": false, "reason": "...", "errors": [...]}
```

## Reading a run afterwards

Every run writes a manifest next to the deck:

```bash
.venv/bin/python -m deck_agent.cli get s3://deckops/my-deck/<run>/manifest.json m.json
```

```json
{"route": "create", "route_by": "rules", "route_confidence": 0.95,
 "engine": {"commit": "e40b7b2"}, "kit": "s3://deckops/kits/11afad46f4704c93.json",
 "warnings": [], "repair_rounds": 0, "code_slides": [...],
 "trace": [{"node": "intake"}, {"node": "route", "route": "create", "by": "rules"}, ...]}
```

The `trace` is the path through the graph. With the outline, plan and ops files beside it, a
run can be explained from the artefacts alone - which model saw what, what it answered, what
the checks said.

## When something is wrong

| What you see | What it means |
|---|---|
| `doctor` says fonts are not exact | `make fonts`. Text measurement is approximate until then. |
| `image ... is not built` | `make image` |
| `Postgres is not reachable` | `make up && make init` |
| `object storage is not reachable` | `make up` |
| a deck with `warnings` | the checks found something and the repair rounds ran out. The warnings name the slides. |
| `no layout in this template can hold this slide` | the relaxed fallback found nothing either. Turn on `allow_custom_code`, or give the template a layout for that shape of content. |
| `the engine refused this batch of edits` | the deck changed between planning and applying. Re-run; the state already points at the newer version. |
| `rendering failed in the sandbox` | look at the message; it carries LibreOffice's own output. |

`make selftest` checks the sandbox's isolation. `make test` runs everything that needs nothing
running. `make test-engine` runs the engine's own 28 tests as a regression check.

## Production, honestly

What is here is a development stack. For a real deployment:

* **Sandbox**: the container is started through the Docker CLI. In production, give the agent a
  daemon it is allowed to drive (rootless, or sysbox/Kata), or a small job service it asks -
  never the host's socket. The interface in `sandbox/docker.py` is one function.
* **Storage**: point `s3_endpoint` and the credentials at the real bucket; scope credentials to
  the thread prefix per request if decks can come from outside the organisation.
* **Postgres**: managed, with the checkpoint tables in their own schema. Checkpoints accumulate;
  `PostgresSaver` has `delete_thread`, and old threads should be pruned on the same schedule as
  the decks they refer to.
* **Workers**: the agent is stateless between turns - everything is in the checkpoint and the
  bucket - so run several. Nothing in this design assumes a single process.
* **Cleanup**: `local/workspaces` is scratch and safe to delete at any time. Nothing of record
  is there.
* **If decks arrive from outside the organisation**, read the "what is given up" section of
  `docs/01-why-one-process.md` and consider running the whole agent as the untrusted thing:
  one short-lived worker per request.
