# 2. Architecture

Four pieces. Three of them run; the fourth is an image that is started, used once and thrown
away.

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │  agent  (one process, pure Python - no native dependency at all)         │
  │                                                                          │
  │    LangGraph StateGraph over DeckState     src/deck_agent/graph.py       │
  │      nodes: intake route ask ensure_kit outline render_slides            │
  │             assemble repair translate_* edit_* custom_code deliver       │
  │                                                                          │
  │    the deckops engine, called directly     src/deck_agent/engine/        │
  │      probe · compile · apply ops · byte-preserving save                  │
  │      overflow · overlap · structural checks · kit building               │
  │                                                                          │
  │    model calls                             src/deck_agent/llm/           │
  └───┬───────────────────┬──────────────────────────────┬───────────────────┘
      │                   │                              │
      │ state per turn    │ files of record              │ one container per job
      ▼                   ▼                              ▼
  ┌─────────┐       ┌────────────┐              ┌──────────────────────┐
  │postgres │       │ S3         │              │ sandbox image        │
  │         │       │ (SeaweedFS)│              │  --network none      │
  │ one     │       │            │              │  --read-only         │
  │ thread  │       │ v001.pptx  │              │  --cap-drop ALL      │
  │ per deck│       │ v002.pptx  │              │                      │
  │ session │       │ kits/      │              │  model-written code  │
  │         │       │ previews/  │              │  LibreOffice render  │
  └─────────┘       └────────────┘              └──────────────────────┘
```

## agent

One process. Everything deterministic happens here, in Python, with no subprocess:
probing a deck, compiling a plan into typed operations, applying them, saving so that untouched
parts keep their original bytes, predicting text overflow from font metrics, finding overlapping
boxes, building and testing a style kit. `docs/01-why-one-process.md` argues why.

It has no native dependency. The fonts its overflow predictor measures with are copied out of
the sandbox image by `make fonts`, so nothing has to be installed on the machine.

## postgres - the checkpointer

LangGraph writes the state after every node, keyed by `thread_id`. That is what makes the
second turn of a conversation start from "there is a deck, it is v3, it uses this kit" instead
of re-deriving it from a transcript, and what makes `interrupt()` real: the graph stops, the
process may exit, and the run resumes at that node when an answer arrives.

Postgres rather than a file, because more than one worker will answer turns for the same
conversation. `checkpointer=memory` exists for tests and one-shot runs; it is not a smaller
Postgres, it is a different thing, and `docs/05-infrastructure.md` says how.

## S3 - the files of record

Every artefact lives in object storage, and the graph state holds only `s3://` URIs. This is
not decoration: a checkpointed thread may be resumed in another process on another machine, so
a local path in the state is a lie waiting to be told. Local directories are scratch that can
be deleted at any moment without losing anything.

```
<deck_id>/v001.pptx                    every version, never overwritten - this is undo
<deck_id>/v002.pptx
<deck_id>/<run>/outline.json           what the model was given and what it answered
<deck_id>/<run>/plan.json
<deck_id>/<run>/ops.json
<deck_id>/<run>/previews/slide-01.png
<deck_id>/<run>/manifest.json          which engine, which kit, which route, which path
kits/<template content hash>.json      shared by every session using that template
uploads/<hash>-<name>.pptx
```

The development stack runs SeaweedFS, not MinIO: MinIO's public images are no longer pullable
without a registry account, and a stack that cannot start from a fresh clone is not a stack.
The agent speaks plain S3 through boto3, so pointing it at a real S3 is two settings.

## sandbox - one container per job

Not a service. `docker run --rm` per call, with a host directory mounted at `/work`:

```
--network none            nothing in here can reach anything
--read-only               the image's filesystem cannot be written to
--cap-drop ALL            no capabilities
--security-opt no-new-privileges
--memory / --pids-limit / --cpus
--user <host uid:gid>     files in /work come back owned by us, not by root
```

Two jobs, two reasons - a trust boundary and a dependency boundary. `make selftest` proves the
claims from inside: network blocked, DNS blocked, root filesystem read-only, `/work` writable.

## Where each piece of the old design went

| deckops | here |
|---|---|
| `controlplane/orchestrator.py` | decomposed into graph nodes (`src/deck_agent/nodes/`) |
| `controlplane/sandbox.py` | gone for deck work; `src/deck_agent/sandbox/` for code and rendering |
| `dataplane_main.py` (argv protocol) | gone - the functions are called directly |
| `deckops/probe|executor|validate|patterns|autokit` | called in-process via `src/deck_agent/engine/` |
| `controlplane/planner.py` | subclassed, not replaced (`src/deck_agent/llm/planner.py`) |
| `controlplane/slide_flow.py` | used as-is for stage 2 and per-slide retry |
| `runs/<id>/` on local disk | object storage, with the state holding URIs |
| the caller choosing the command | the router |

## Layout

```
src/deck_agent/
  settings.py      all configuration; model credentials only ever from llm.json
  state.py         DeckState - typed fields, and why there is no `messages`
  graph.py         the StateGraph: nodes, edges, named decisions
  facade.py        what a host chat agent calls: create/edit/translate/ask/answer/undo
  cli.py           doctor, init, seed, kit, create, edit, translate, selftest, graph
  engine/          the bridge to deckops, and the pieces built next to it
    __init__.py    sys.path, fonts, the archive guard, the engine's commit
    deck.py        probe, apply ops, checks, baseline, changelog, reorder, reseal
    build.py       compile + apply + check, and the feedback a model can act on
    kitbuild.py    a tested style kit, in-process
    edits.py       edit actions -> engine ops
    fallback.py    what to offer when no layout fits
    tidy.py        two passes for things the checks cannot see
  llm/             prompts, the planner protocol, rules-first routing
  nodes/           one module per route
  infra/           storage, workspace, checkpointer
  sandbox/         docker, render, code, guard
runner/            what lives inside the sandbox image
engine/deckops/    the engine, cloned and unmodified
docker/            compose, the two Dockerfiles, the S3 identities
tests/unit/        pass with nothing running
tests/integration/ need docker, Postgres, S3 - and skip with a reason otherwise
```
