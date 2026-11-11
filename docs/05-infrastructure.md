# 5. Infrastructure

Everything in `docker/compose.yml`, started with `make up`. Ports bind to `127.0.0.1` only.

## Postgres - the checkpointer

```yaml
postgres:16-alpine
127.0.0.1:55432 -> 5432          # 55432, because a developer machine usually has a Postgres
volume: pgdata
```

`make init` runs `PostgresSaver.setup()`, which creates LangGraph's checkpoint tables and is
idempotent - LangGraph tracks its own migrations.

**Why 55432.** The standard port is usually taken on a working machine, and a development stack
should not have to win a fight for it. Inside the compose network the port is still 5432, which
is what the agent container uses.

**Why Postgres and not a file.** More than one worker will answer turns for the same
conversation, and a checkpoint has to be visible to all of them. `checkpointer=memory` is
offered for tests and one-shot CLI runs, and it is *not* a smaller Postgres: when the process
ends the conversation is gone, and an `interrupt()` can only be resumed inside the same
process. One nuance worth knowing - the in-memory saver is a single process-wide object, because
a fresh saver per call is not a checkpointer at all; the second turn would find no thread. A
test caught that.

**What it stores.** One row per graph step: the whole `DeckState`, which is JSON plus object
URIs. No deck bytes ever go in Postgres.

## S3 - the files of record

```yaml
chrislusf/seaweedfs:4.53
127.0.0.1:9000 -> 8333           # the S3 API
127.0.0.1:9333                   # the master's status page
volume: s3data
config: docker/seaweedfs/s3.json # one identity, so the access key means something
```

**Why not MinIO.** MinIO's public container images are no longer pullable without a registry
account - `docker pull minio/minio` fails with "pull access denied", and so does quay.io. A
stack that cannot start from a fresh clone is not a stack. SeaweedFS is a single small
container with an S3 gateway that boto3 drives unchanged: buckets, objects, listings and
presigned URLs all behave, verified in `tests/integration/test_stack.py`.

**Why the identities file.** Without `-s3.config`, SeaweedFS accepts any signature. That is
convenient and wrong: the agent's settings carry an access key, and a store that ignores it
hides credential mistakes until something real is pointed at. One identity, matching the
defaults in `settings.py`. There is a test that a wrong key is rejected.

**Pointing at a real S3.** Two settings - `s3_endpoint` (empty for AWS) and the credentials.
`infra/storage.py` is plain boto3 with path-style addressing, which real S3 accepts too.

## The sandbox image

```
docker/sandbox.Dockerfile  ->  deckops/sandbox:0.1.0   (~564 MB)

  python:3.12-slim-bookworm
  + libreoffice-impress, libreoffice-core    pptx -> pdf
  + poppler-utils                            pdf -> png
  + fonts-crosextra-carlito                  metric-compatible Calibri
  + fonts-crosextra-caladea                  metric-compatible Cambria
  + fonts-liberation                         metric-compatible Arial / Times
  + python-pptx, lxml, Pillow, XlsxWriter
  + runner/                                  render.py, code_harness.py, selftest.py
```

Deliberately **no model client, no AWS SDK, no HTTP library**. Nothing in this image is
expected to reach the network, and containers are always started with `--network none`.

Not a service: `docker run --rm` per job. `docker compose --profile tools run --rm sandbox`
runs its self-test.

### The metric-compatible fonts

The overflow predictor measures text with real font metrics. Corporate templates ask for
Calibri and Arial; the engine substitutes Carlito and Liberation, which have the same advance
widths. Rather than make installing those a prerequisite, `make fonts` copies them out of the
image into `local/fonts`, and `engine/use_local_fonts()` points the engine at that directory.

`make doctor` reports whether the match is exact:

```json
"fonts": {"calibri": "Carlito-Regular.ttf", "arial": "LiberationSans-Regular.ttf",
          "cambria": "Caladea-Regular.ttf", "_exact": true}
```

Without them the predictor falls back to whatever TrueType font it can find - on a Mac, Arial -
and the measurements drift. It still works; it is just less accurate, and `doctor` says so
rather than letting it pass silently.

### A LibreOffice detail worth recording

Containers run as the *host* user's uid so that files written to `/work` come back owned by the
person who started them. That uid has no `/etc/passwd` entry, and LibreOffice then refuses to
start:

```
LibreOffice 7.4 - Fatal Error: The application cannot be started.
User installation could not be completed.
```

Setting `HOME` is not enough. The fix is to place the profile explicitly, on the container's
tmpfs, which is also tidier - it is gone when the container is:

```
soffice -env:UserInstallation=file:///tmp/libreoffice-profile --headless ...
```

(The `failed to launch javaldx` warning is harmless: there is no JRE in the image, and
Impress-to-PDF does not need one.)

## agent (optional)

`docker/agent.Dockerfile` builds the agent itself: pure Python, no LibreOffice, no poppler -
which is the single-process design showing up as a small image.

**The socket mount is the catch.** The agent starts sandbox containers, so inside a container it
needs a Docker daemon to talk to, and the simple answer - mounting the host's socket - gives
that container root on the host. Acceptable in a throwaway development stack; not acceptable
anywhere else. In production, replace it with a daemon the agent is *allowed* to drive: a
rootless daemon of its own, a sysbox or Kata runtime, or a small job service it asks instead.
Running the agent on the host, as `make` does, has none of this problem.

## Settings

`src/deck_agent/settings.py`. Two kinds, kept apart, following the engine's sixth hard rule:

* **Model settings** - base URL, model name, API key - come **only** from a git-ignored
  `llm.json`. Never from the environment, never from code, never committed.
* **Infrastructure settings** - DSN, endpoint, image name, limits - come from
  `config/agent.json` if present, overridable by `DECKOPS_<FIELD>` environment variables so
  compose can wire containers together.

`Settings.redacted()` is what you print; `make doctor` uses it.

The dials worth knowing:

| Setting | Default | What it changes |
|---|---|---|
| `render_backend` | `docker` | `local` uses an installed `soffice`; `off` skips previews and says so |
| `code_runner` | `docker` | `off` disables the escape hatch entirely |
| `allow_custom_code` | `false` | whether the graph may reach the escape hatch at all |
| `vision_review` | `false` | look at every rendered slide with a vision model; one call per slide |
| `approve_outline` | `false` | pause for a person between stage 1 and building |
| `max_repair_rounds` | `2` | how many times findings go back to the model |
| `render_retries` | `2` | per-slide retries in stage 2 |
| `router_confidence_floor` | `0.6` | below this, ask the person |
| `checkpointer` | `postgres` | or `memory` |
| `store_backend` | `s3` | or `memory` |
| `llm_client` | `openai-compat` | or `langchain`, or `stub` for offline |
