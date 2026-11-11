# 7. Running it

## From a fresh clone

[`docs/00-setup.md`](00-setup.md) is the step-by-step guide for a new machine: prerequisites,
ports, every `make` step and how to tell it worked, the model, the first template and deck, the
full settings table, and fixes for what goes wrong. The short version:

```bash
make setup      # engine + patches, venv (installs the `deckops` command), config, image, fonts, samples
make up         # postgres + S3
make init       # checkpoint tables + bucket
make doctor     # check everything
```

`make setup` needs Docker, `git`, `make` and Python 3.12. It needs **no** LibreOffice, no
poppler and no fonts installed: the image has them, and `make fonts` copies the fonts out.

Commands below use `.venv/bin/deckops`; with the virtualenv activated (`source
.venv/bin/activate`) it is just `deckops`.

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

# a deck, from whatever context you have: a document, or notes typed into the request
.venv/bin/deckops create my-deck \
    "explain the support bot pilot to the operations review" --context notes.md
.venv/bin/deckops create my-deck \
    "a deck on the pilot: it ran eight weeks across two teams, the bot answered 38% ..."

# or with the facts already written (skips the gather step)
.venv/bin/deckops create my-deck "the support bot pilot" --content local/content.json

# carry on in the same session
.venv/bin/deckops edit      my-deck "make slide 3 less wordy"
.venv/bin/deckops translate my-deck fr --slides 1,2
.venv/bin/deckops ask       my-deck "add a risks slide after slide 4"
.venv/bin/deckops answer    my-deck "yes, translate it"

# look at what happened
.venv/bin/deckops session my-deck
.venv/bin/deckops undo    my-deck
.venv/bin/deckops get     s3://deckops/my-deck/v002.pptx deck.pptx

# a template, learned once and cached by content hash
.venv/bin/deckops kit corporate.pptx --out local/templates/corporate/kit.json --make-default
```

## Ingesting a template

```bash
.venv/bin/deckops kit "path/to/Template.pptx" \
    --out local/templates/<name>/kit.json --make-default
```

That one command is the whole ingestion. In order, it:

1. **prepares** the file - makes nested shape ids unique, which real templates often need and
   the engine cannot work around. A template that needs nothing comes through byte-identical;
2. **drafts** a pattern per usable slide (groups included, slides with a real chart skipped);
3. **renders** the slides in the sandbox and has the vision model **label** each box (title,
   heading, body, value ...);
4. **accepts or rejects** each pattern by building test decks from it and checking them, with
   the vision model comparing each test slide to the original;
5. **measures** how many items each accepted pattern really holds.

It then stores two things in the bucket, which is where every deck build looks for them:

| | where | what |
|---|---|---|
| the kit | `kits/<template hash>.json` | the accepted patterns. Looked up by the template's content hash, so it is built once |
| the template | `uploads/<template hash>-<name>.pptx` | the **prepared** copy - the file the kit's shape ids refer to |

and prints both URIs, the patterns that passed, and the ones dropped with the reason for each.
`--out` also writes the kit locally (with `<name>-prepared.pptx` beside it when preparation
changed anything), for reading; nothing reads it from there. `--make-default` writes the
template's URI into `config/agent.json` as `default_template`, so `create` uses it when no
`--template` is given. Without it, pass `--template s3://deckops/uploads/...` to `create`.

A template that yields no patterns is not cached and not uploaded, and says so. To ingest a
template again from scratch, delete its kit (`aws ... s3 rm s3://deckops/kits/<hash>.json`,
or `clean --all`) and run the command again.

### One kit at a time (`single_kit`)

`config/agent.json` currently sets `"single_kit": true`, so `kits/` holds exactly one kit:

* **Ingesting replaces.** Once the new kit and template are safely stored, `deckops kit`
  deletes every other `kits/<hash>.json` together with its template `uploads/<hash>-*.pptx`,
  and lists them under `replaced`. A failed ingest replaces nothing. Uploads that are not a
  kit's template - decks people attached - are left alone.
* **Nothing else writes to `kits/`.** A deck created from some other template still needs a
  kit, so `ensure_kit` builds one, but keeps it beside that deck (`<deck>/kit-<hash>.json`). It
  goes when the deck is purged. `make demo` and the tests work this way, and leave `kits/` as
  it was.
* **Decks built from a replaced kit** keep their files and stay editable. What they lose is a
  rebuild from their template: a layout change falls back to the layouts already in the deck,
  and says why.

With `single_kit` off (the code default), kits accumulate in `kits/`, one per template, and a
template seen for the first time at create time is cached there for everyone.

## Looking in the bucket

```bash
.venv/bin/deckops ls                 # one line per deck, with counts and sizes
.venv/bin/deckops ls my-deck/        # one deck
.venv/bin/deckops ls my-deck/ -r     # every object in it
.venv/bin/deckops get s3://deckops/my-deck/v002.pptx deck.pptx
```

`ls` uses the agent's own settings, so it needs nothing set up. The AWS CLI works too, but it
needs the development credentials: on its own it sends whatever is in `~/.aws/credentials`,
and the local service answers `InvalidAccessKeyId`. Give it a profile once:

```bash
aws configure set aws_access_key_id deckops --profile deckops
aws configure set aws_secret_access_key deckops-dev-secret --profile deckops
aws configure set region us-east-1 --profile deckops
aws --profile deckops --endpoint-url http://localhost:9000 s3 ls s3://deckops/
```

A browser pointed at `localhost:9000` gets `AccessDenied` for the same reason. The `url` in
every result is a presigned link, which carries its own signature and does open.

The layout:

```
<deck_id>/v001.pptx, v002.pptx ...     every version of the deck; undo drops the last
<deck_id>/<run>/manifest.json          what that turn did: route, trace, warnings
<deck_id>/<run>/outline.json, plan.json, ops.json, edit_plan.json, render_attempts.json
<deck_id>/<run>/previews/slide-NN.png  rendered slides
<deck_id>/<run>/custom-slide-N.py      model-written slide code, and its -ctx.json
kits/<hash>.json                       ingested template kits
uploads/<hash>-<name>.pptx             templates and attached decks
```

## Cleaning up

A deck lives in two places: its files in the bucket, and its conversation in Postgres. Deleting
one without the other leaves a session that points at nothing, or files nothing will read
again, so `clean` always does both. **Every option is a dry run** that prints what would go;
add `--yes` to do it.

```bash
deckops clean                          # what is there: per deck, objects, size, has a session
deckops clean --tidy                   # every deck: drop older runs' bulk, keep it usable
deckops clean --tidy --deck my-deck    # just that one
deckops clean --deck my-deck           # that deck, gone: files and conversation
deckops clean --deck 'demo-*'          # shell patterns work
deckops clean --tests                  # whatever an interrupted test run left behind
deckops clean --orphans                # files with no session, sessions with no files
deckops clean --older-than 7d          # decks untouched for a week
deckops clean --all                    # everything, sessions included
deckops clean --all --keep-templates   # everything except kits/ and uploads/
```

(`deckops` is `.venv/bin/deckops`.)

**What a tidy keeps.** Everything the session's state still refers to - every version (undo),
the plan that built the current deck (an edit that changes layouts rebuilds from it), any
model-written slide code (replayed on a rebuild), the template - plus the whole of the latest
run and every run's `manifest.json`, a few KB each, which is the history of what was done.
What goes is earlier runs' previews, outlines, plans and ops, and any version undo already
dropped. A tidied deck can still be edited, translated and undone; the tests check exactly
that.

**After every turn, automatically:** set `"tidy_after_turn": true` in `config/agent.json`.
Once a turn has delivered, the same tidy runs on that deck and the result carries a `tidied`
field with what it removed. A turn that stopped to ask a question is not tidied - its outline
is what the answer will be applied to - and a tidy that fails never fails the turn. It is off by
default because the older runs' artefacts are how a bad turn gets explained afterwards.

The tests clean up after themselves: the integration tests purge every deck and object they
create, even when they fail. `make demo` keeps its `demo-<timestamp>` deck on purpose, so
there is something to look at; `clean --deck 'demo-*'` removes them.

`make clean-scratch` is the local counterpart: it empties `local/workspaces` and other run output
on disk, keeping fonts, samples and `local/templates`.

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
.venv/bin/deckops get s3://deckops/my-deck/<run>/manifest.json m.json
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
* **Retention**: `tidy_after_turn` keeps the bucket from growing per turn; `clean --older-than`
  on a schedule removes abandoned decks with their conversations.
* **Cleanup**: `make clean-scratch` removes run output from `local/` - workspaces, rendered
  previews, downloaded artefacts - and keeps the three things that are depended on: the fonts
  the overflow predictor measures with, the sample decks the tests use, and `local/templates`,
  where a prepared template and its kit live. A kit addresses shapes by id, so the file it was
  built from is not scrap. Nothing of record is in `local/` at all; it is all in object
  storage.
* **If decks arrive from outside the organisation**, read the "what is given up" section of
  `docs/01-why-one-process.md` and consider running the whole agent as the untrusted thing:
  one short-lived worker per request.
