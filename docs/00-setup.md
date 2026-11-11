# 0. Setting it up on a new machine

Everything needed to go from a fresh clone to a deck built from your own template. Follow it
top to bottom; each step says how to tell it worked.

## What the machine needs

| | Version | Why |
|---|---|---|
| macOS or Linux | | Windows works under WSL2; nothing here is tested on native Windows |
| **Docker** | Desktop 4.x or Engine 24+, running | Postgres, the S3 service, and the sandbox image all run in it. Give it 4 GB of memory or more |
| **Python** | **3.12**, as `python3.12` on the PATH | the agent; `make venv` calls `python3.12` by name |
| **git**, **make** | any recent | cloning the engine, the Makefile |
| AWS CLI | optional | only for browsing the bucket by hand (`deckops ls` does the same) |

Network access, once, during setup: GitHub (the engine), Docker Hub (`postgres:16-alpine`,
`chrislusf/seaweedfs:4.53`, `python:3.12-slim-bookworm`), Debian's package mirrors (the sandbox
image installs LibreOffice), and PyPI. Afterwards only the model endpoint is reached.

Free local ports: **55432** (Postgres), **9000** (S3 API), **9333** (S3 status page). All three
are bound to `127.0.0.1` only. If one is taken, change it in `docker/compose.yml` and the
matching `pg_dsn` / `s3_endpoint` in `config/agent.json`.

LibreOffice, poppler and fonts are **not** needed on the machine: they live in the sandbox image,
and `make fonts` copies the fonts out for the text measurer.

## 1. Clone and build (about 10 minutes, mostly the image)

```bash
git clone https://github.com/pranaybahuguna/ppt-gen.git
cd ppt-gen
make setup
```

`make setup` runs, in order:

| Step | What it does | Done when |
|---|---|---|
| `make engine` | clones the deckops engine into `engine/deckops/` and applies `patches/*.patch` | prints `applied patches/0001-group-support.patch` (or `already applied`) |
| `make venv` | creates `.venv`, installs the pinned dependencies and the project itself | prints the Python version; `.venv/bin/deckops --help` works |
| `make config` | writes `config/agent.json` from `config/agent.example.json` if there is none | prints `wrote config/agent.json` |
| `make image` | builds `deckops/sandbox:0.1.0` (Python, LibreOffice, poppler, fonts) | `docker images` lists it |
| `make fonts` | copies the metric-compatible fonts out of the image into `local/fonts/` | prints a count of fonts |
| `make samples` | builds the engine's sample deck and template, used by tests and the demo | lists `local/samples/` |

## 2. Start the services

```bash
make up        # Postgres and SeaweedFS (S3), in Docker, with named volumes
make init      # the checkpoint tables and the bucket
make doctor    # checks everything; must end with READY
```

`doctor` checks the engine, the fonts, the sandbox image, Postgres, the bucket and the model, and
names the fix for each problem. A missing `llm.json` is reported but does not stop `READY`: the
offline planner runs the whole graph without a model.

```bash
make selftest  # the sandbox really has no network and a read-only root
make test      # 244 unit tests, nothing running needed
make test-all  # + 19 integration tests against Docker, Postgres and S3
```

## 3. Give it a model

```bash
cp llm.example.json llm.json
```

and fill it in:

```json
{"base_url": "https://api.openai.com", "model": "gpt-6.1-sol", "api_key": "sk-..."}
```

Any OpenAI-compatible `/v1/chat/completions` endpoint works. `llm.json` is git-ignored and is
the **only** place a model credential is read from - not the environment, not
`config/agent.json`. Run `make doctor` again; `model` should now be `ok`.

The model needs to accept images for the vision steps (kit labelling, `vision_review`). Without
that, set `"vision_review": false` and kit labelling falls back to rules.

## 4. Ingest your template

```bash
.venv/bin/deckops kit "path/to/Your Template.pptx" \
    --out local/templates/yours/kit.json --make-default
```

Two to five minutes with a model. It prepares the file, finds its reusable layouts, renders
them, has the model label each box, builds test decks to accept or reject each layout, and
measures how many items each one holds. It prints what was accepted and what was dropped, with
the reason for each.

It stores the kit in `kits/<hash>.json` and the prepared template in `uploads/` in the bucket,
and `--make-default` writes the template's address into `config/agent.json`, so decks use it
without being told. With `"single_kit": true` (the example config's setting) ingesting a
template replaces any kit that was there.

A template that yields no usable layouts is not stored, and says why.

## 5. Make a deck

From a document:

```bash
.venv/bin/deckops create my-deck \
    "explain this to an engineering audience: what it is, how it works, where it stands" \
    --context path/to/notes.md
```

or from notes in the request itself:

```bash
.venv/bin/deckops create pilot "a deck on the support bot pilot: it ran eight weeks across \
two teams; the bot answered 38% of tickets; median first response fell from 11 to 3 hours"
```

Five to ten minutes for a twelve-slide deck with `vision_review` on. It prints each step, and a
JSON result whose `deck` is the file in the bucket and whose `url` is a download link. Then:

```bash
.venv/bin/deckops get <the "deck" from the result> my-deck.pptx       # the file
.venv/bin/deckops edit my-deck "make slide 4 less wordy"                # carry on
.venv/bin/deckops translate my-deck fr                                   # or translate it
.venv/bin/deckops session my-deck                                        # what it knows
.venv/bin/deckops undo my-deck                                           # one version back
```

`deck_id` (`my-deck`) is the conversation: run any of these later, from another terminal or
another process, and it carries on from the same deck.

## 6. Look around, and clean up

```bash
.venv/bin/deckops ls                          # the bucket, one line per deck
.venv/bin/deckops ls my-deck/ -r              # everything for one deck
.venv/bin/deckops clean                       # what is there, and which decks have sessions
.venv/bin/deckops clean --tidy --yes          # drop old runs' bulk, keep every deck usable
.venv/bin/deckops clean --deck my-deck --yes  # one deck, files and conversation together
make clean-scratch                            # local run output in local/
```

Every `clean` is a dry run until `--yes`. `docs/07-operations.md` has the rest.

## Settings

`config/agent.json` (git-ignored; `make config` writes it from the example). Any key can also be
set for one run with an environment variable `DECKOPS_<KEY>`, e.g. `DECKOPS_VISION_REVIEW=true`.

| Key | Example config | What it does |
|---|---|---|
| `s3_endpoint`, `s3_bucket` | `http://localhost:9000`, `deckops` | the object store; any S3 |
| `pg_dsn` | the local Postgres | the checkpointer |
| `render_backend` | `docker` | `docker`, `local` (soffice on PATH) or `off` |
| `code_runner` | `docker` | `docker` or `off`; never local - it runs model-written code |
| `default_template` | set by `kit --make-default` | the template a deck uses when none is named |
| `allow_custom_code` | `true` | lets model-written code rework slides and draw slides no layout holds |
| `layout_reuse` | `2` | each template layout at most this many times per deck; slides over it are reworked into diagrams. `0` turns it off |
| `code_attempts` | `3` | tries the model gets per reworked slide |
| `vision_review` | `false` | a vision model looks at every rendered slide; slower, catches what arithmetic cannot |
| `approve_outline` | `false` | stop after the outline and wait for an `answer` |
| `single_kit` | `true` | keep one template kit; ingesting replaces it |
| `tidy_after_turn` | `false` | after each turn, drop the deck's older run artefacts |
| `max_repair_rounds` | `2` (default) | rounds of "rewrite what did not fit" |
| `llm_client` | `openai-compat` (default) | `langchain`, or `stub` for no model at all |

The rest (sandbox limits, timeouts, worker counts) are in `src/deck_agent/settings.py`, each with
a comment.

## What lives where

| Where | What | In git? |
|---|---|---|
| the repository | code, docs, patches, Docker files, example configs | yes |
| `engine/deckops/` | the engine clone, patched | no - `make engine` |
| `.venv/` | the virtualenv | no - `make venv` |
| `llm.json`, `config/agent.json` | your credentials and your settings | **no, never** |
| `local/` | fonts, sample decks, ingest output, run scratch | no - `make fonts`, `make samples` |
| the `deckops` bucket (SeaweedFS volume) | every deck version, kit, template, preview, manifest | no - Docker volume |
| Postgres (Docker volume) | every conversation's state | no - Docker volume |

**Moving to another machine** moves the code, not the data: decks and conversations are in the
Docker volumes of the machine that made them. On the new machine, run this guide, ingest your
template again, and build decks there. To keep data, point both machines' `s3_endpoint` and
`pg_dsn` at shared services instead of the local containers - nothing else changes.

## When something is wrong

| What you see | Fix |
|---|---|
| `make venv`: `python3.12: command not found` | install Python 3.12 (`brew install python@3.12`, `apt install python3.12 python3.12-venv`) |
| `make engine`: `COULD NOT APPLY patches/...` | the engine moved upstream; check out the commit the patch was made against, or redo the patch (`patches/README.md`) |
| `make up`: `port is already allocated` | another service has 55432 or 9000; change the port in `docker/compose.yml` and in `config/agent.json` |
| `doctor`: `Postgres is not reachable` / `object storage is not reachable` | `make up`, then `make init` |
| `doctor`: `image ... is not built` | `make image` |
| `doctor`: fonts not exact | `make fonts` |
| `No module named deck_agent` | use `.venv/bin/deckops`, or run `make venv` again - it installs the project |
| `aws s3 ls`: `InvalidAccessKeyId` | the AWS CLI sent your real AWS keys; use the dev profile in `docs/07-operations.md`, or `deckops ls` |
| rendering fails with a LibreOffice error | the message carries LibreOffice's output; `make selftest` checks the image |
| a deck with `warnings` | the checks found something the repair rounds could not fix; each warning names its slide |
| `the template's ... layout ... keeps its template layout` | a slide over the layout budget could not be reworked in `code_attempts` tries; its content is there, in the template's layout |

`make destroy-data` deletes the Docker volumes - every deck and every conversation - for a
truly fresh start.
