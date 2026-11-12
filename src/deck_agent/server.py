"""The server: every operation the command line has, as MCP tools and as plain REST.

    deckops serve                  # http://127.0.0.1:8765  (make serve)

One FastMCP app serves both:

* **MCP** at `/mcp` (streamable HTTP) - for Claude, or any MCP client. Each tool is described by
  its docstring; `wait=False` (the default for the long ones) returns a job to poll.
* **REST** everywhere else - for curl, a browser, a test script:

      POST   /templates                       multipart: file, make_default      ingest a template
      POST   /decks                           JSON or multipart: brief, deck_id?, title?, files...
      create
      POST   /decks/{id}/edit                 {"instruction": ...}
      POST   /decks/{id}/translate            {"lang": "fr", "slides": [1, 2]}
      POST   /decks/{id}/ask                  {"instruction": ...}   the router decides
      POST   /decks/{id}/answer               {"text": ...}          answer a question it asked
      POST   /decks/{id}/attach               multipart: file, instruction   a deck from elsewhere
      POST   /decks/{id}/undo
      GET    /decks                           every deck (the decks table)
      GET    /decks/{id}                      the deck's row and what its conversation knows
      PATCH  /decks/{id}                      {"title": ...}  rename
      GET    /decks/{id}/download[?version=N] the .pptx
      DELETE /decks/{id}                      the deck's files and conversation
      GET    /jobs/{job_id}                   a job's state, log and result
      GET    /jobs[?deck_id=...&limit=50]     jobs, newest first (the jobs table)
      GET    /health

  Long operations run as jobs: the POST returns 202 with a job id at once; `?wait=true` holds
  the request until it finishes instead.

Every finished result carries a `summary` (what changed, in lines a person reads) and a
`download_url` served by this server, so nothing needs S3 credentials to get the file.

Every job and every deck is a row in Postgres (db/), written as the job moves along: queued
when it arrives, running when it starts, done or failed with its summary when it ends. A job
still running when the server stops is marked failed the next time it starts. The live log and
the full result of a job are kept in memory while this process runs; afterwards the row keeps
the summary, the error and the log.
"""
from __future__ import annotations

import asyncio
import json
import re
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from . import facade
from .db import repo
from .infra.storage import Ref, sha256_file, store
from .settings import settings
from .summary import base_url, describe

DECK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

mcp = FastMCP(
    "deckops",
    instructions=(
        "Builds and edits PowerPoint decks in the user's corporate template. Ingest a template "
        "once (ingest_template), then create_deck from a brief and context, and carry on in the "
        "same deck_id with edit_deck, translate_deck, ask, answer and undo. Long operations "
        "return a job id: poll job_status until it is done. Every result has a summary of "
        "what changed and a download_url for the .pptx."),
)


# --------------------------------------------------------------------------- jobs

@dataclass
class Live:
    """What this process holds for a job beyond its row: the task, the live log, the full result.
    """

    task: asyncio.Task | None = None
    log: list[str] = field(default_factory=list)
    result: dict | None = None


class Jobs:
    """The background jobs: rows in the jobs table, run as asyncio tasks. One per deck at a time.

    The work itself (the graph, the engine, the sandbox) is synchronous, so each job runs it in
    a worker thread (asyncio.to_thread); at most `workers` at once.

    Called by: every tool and route that starts a long operation.
    """

    def __init__(self, workers: int):
        """No jobs yet; the concurrency limit and the lock are made on first use, in the loop."""
        self._workers = workers
        self._live: dict[str, Live] = {}
        self._sem: asyncio.Semaphore | None = None
        self._lock: asyncio.Lock | None = None

    async def submit(self, kind: str, deck_id: str | None, request: dict, fn, *,
                     title: str | None = None, template: str | None = None) -> str:
        """Record the job and start `fn(log)` in the background. -> job_id

        Raises Busy when the deck already has a job queued or running.
        """
        if self._lock is None:
            self._lock, self._sem = asyncio.Lock(), asyncio.Semaphore(self._workers)
        async with self._lock:
            if deck_id and await repo.running_job(deck_id):
                raise Busy(f"deck {deck_id!r} already has a job running; wait for it to finish")
            job_id = await repo.start_job(kind, deck_id, request, title=title, template=template)
        live = Live()
        self._live[job_id] = live
        live.task = asyncio.create_task(self._run(job_id, fn, live))
        return job_id

    async def _run(self, job_id: str, fn, live: Live) -> None:
        """Run the work in a thread; record running, then done or failed with the summary."""
        def log(*parts, **_):
            """The log callback the operation is given: each line goes into the job's log."""
            live.log.append(" ".join(str(p) for p in parts).rstrip())

        async with self._sem:
            await repo.mark_running(job_id)
            ok, error, result = False, None, None
            try:
                result = _plain(await asyncio.to_thread(fn, log))
                if not isinstance(result, dict):
                    result = {"ok": True, "value": result}
                ok = bool(result.get("ok", True)) or bool(result.get("question"))
            except Exception as e:                       # noqa: BLE001 - recorded, not raised
                error = f"{e.__class__.__name__}: {e}"
                live.log.append(traceback.format_exc(limit=4))
            live.result = result
            await repo.finish_job(job_id, ok=ok, error=error or (result or {}).get("reason"),
                                  summary=(result or {}).get("summary") or [],
                                  log=live.log[-200:], deck_file=(result or {}).get("deck"))

    async def view(self, job_id: str, wait: bool = False) -> dict | None:
        """A job as a caller sees it: its row, plus the live log and full result while this
        process has them. With wait, first wait for it to finish."""
        live = self._live.get(job_id)
        if wait and live and live.task:
            await asyncio.shield(live.task)
        row = await repo.get_job(job_id)
        if row is None:
            return None
        view = repo.job_view(row)
        view["log"] = (live.log if live else row.log)[-40:]
        view["result"] = live.result if live and live.result is not None else (
            {"summary": row.summary} if row.status in ("done", "failed") else None)
        if row.deck_id and row.status == "done" and view["result"] is not None:
            view["result"].setdefault("download_url", f"{base_url()}/decks/{row.deck_id}/download")
        view["status_url"] = f"/jobs/{job_id}"
        return view

    async def running(self) -> int:
        """How many jobs this process is running now."""
        return sum(1 for live in self._live.values() if live.task and not live.task.done())


class Busy(Exception):
    """A deck already has a job running."""


_jobs: Jobs | None = None


def jobs() -> Jobs:
    """The process-wide job runner, created on first use."""
    global _jobs
    if _jobs is None:
        _jobs = Jobs(settings().server_workers)
    return _jobs


async def _run_job(kind: str, deck_id: str | None, request: dict, fn, wait: bool, **deck) -> dict:
    """Start a job and return it - finished, when wait is set.

    Called by: every MCP tool and REST route that starts a long operation.
    """
    job_id = await jobs().submit(kind, deck_id, request, fn, **deck)
    return await jobs().view(job_id, wait)


# --------------------------------------------------------------------------- results

def _plain(obj):
    """Anything a result might hold, as plain JSON types."""
    return json.loads(json.dumps(obj, default=str))


def new_deck_id(brief: str) -> str:
    """A deck name made from the brief: its first words, and a short random part.

    `explain the support bot pilot` -> `explain-the-support-bot-pilot-7f3a9c`
    """
    words = re.sub(r"[^a-z0-9]+", "-", brief.lower()).strip("-")[:40].strip("-") or "deck"
    return f"{words}-{uuid.uuid4().hex[:6]}"


def check_deck_id(deck_id: str) -> str:
    """A deck id is a folder in the bucket and a conversation id: letters, digits, . _ -"""
    if not deck_id or not DECK_ID.match(deck_id):
        raise ValueError("deck_id must be 1-64 letters, digits, '.', '_' or '-', starting with "
                         "a letter or digit")
    return deck_id


def upload(name: str, data: bytes) -> str:
    """Put a file a caller sent into the bucket's uploads/, named by content. -> its URI."""
    import hashlib

    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).name) or "file"
    return store().put_bytes(data, f"uploads/{hashlib.sha256(data).hexdigest()[:16]}-{safe}")


def upload_path(path: str) -> str:
    """A file on this machine, into uploads/. -> its URI. (s3:// URIs pass through.)"""
    if path.startswith("s3://"):
        return path
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {p}")
    return store().put(p, f"uploads/{sha256_file(p)}-{re.sub(r'[^A-Za-z0-9._-]+', '-', p.name)}")


# --------------------------------------------------------------------------- operations

def op_create(deck_id, brief, context=None, template=None, outline=None, content_pack=None,
              log=print) -> dict:
    """Create a deck. `context`: object URIs of documents the facts are gathered from."""
    return describe(facade.create_deck(deck_id, brief, content_pack, template, log=log,
                                       context=context or [],
                                       **({"outline": outline} if outline else {})), deck_id)


def op_ingest(path: str, make_default: bool, log=print) -> dict:
    """Ingest a template from a local file."""
    from .templates import ingest

    return ingest(path, None, make_default, log=log)


async def op_session(deck_id: str) -> dict:
    """A deck's row and what its conversation knows, with download links for each version."""
    info = await asyncio.to_thread(facade.session, check_deck_id(deck_id))
    row = await repo.get_deck(deck_id)
    if row is not None:
        info = {**info, **repo.deck_view(row)}
    if info.get("exists"):
        info["download_url"] = f"{base_url()}/decks/{deck_id}/download"
        info["version_urls"] = [f"{base_url()}/decks/{deck_id}/download?version={i + 1}"
                                for i in range(len(info.get("versions") or []))]
    return _plain(info)


async def op_list() -> dict:
    """Every deck, from the decks table, newest change first, with a download link each."""
    rows = await repo.list_decks()
    return {"decks": [{**repo.deck_view(d, n),
                       "download_url": f"{base_url()}/decks/{d.deck_id}/download"
                       if d.current_file else None} for d, n in rows]}


async def op_delete(deck_id: str) -> dict:
    """Delete a deck: its files, its conversation, and its rows (the deck and its jobs)."""
    from .infra import cleanup
    from .infra.checkpointer import checkpointer

    check_deck_id(deck_id)

    def files_and_conversation():
        """The bucket and the checkpointer are synchronous."""
        with checkpointer() as saver:
            plan = cleanup.plan_purge(store(), saver, [deck_id])
            return {**plan.summary(), **cleanup.execute(store(), saver, plan)}
    out = await asyncio.to_thread(files_and_conversation)
    out["deleted_rows"] = await repo.delete_decks([deck_id])
    return out


async def op_undo(deck_id: str) -> dict:
    """Go back one version, recorded as an `undo` job; the deck's current file follows."""
    check_deck_id(deck_id)
    job_id = await repo.start_job("undo", deck_id, {})
    result = _plain(await asyncio.to_thread(facade.undo, deck_id))
    await repo.finish_job(job_id, ok=bool(result.get("ok")), deck_file=result.get("deck"),
                          error=result.get("reason"),
                          summary=[f"undo: back to version {result.get('versions')}"]
                          if result.get("ok") else [])
    return result


def deck_file(deck_id: str, version: int | None = None) -> tuple[bytes, str]:
    """The bytes of a deck version (the latest by default) and a file name for it."""
    info = facade.session(check_deck_id(deck_id))
    versions = info.get("versions") or []
    if not versions:
        raise LookupError(f"deck {deck_id!r} has no versions")
    if version is None:
        uri = versions[-1]
    elif 1 <= version <= len(versions):
        uri = versions[version - 1]
    else:
        raise LookupError(f"deck {deck_id!r} has versions 1-{len(versions)}")
    return store().get_bytes(uri), f"{deck_id}-{Ref.parse(uri).name}"


# --------------------------------------------------------------------------- MCP tools

@mcp.tool
async def ingest_template(path: str, make_default: bool = True, wait: bool = False) -> dict:
    """Ingest a PowerPoint template so decks can be built in its look (2-5 minutes).

    path: the .pptx on the server's machine. make_default: decks use it when none is named.
    wait: hold until done; otherwise returns a job - poll job_status.
    """
    return await _run_job("ingest", None, {"path": path, "make_default": make_default},
                          lambda log: op_ingest(path, make_default, log), wait)


@mcp.tool
async def create_deck(brief: str, deck_id: str = "", title: str = "", context: str = "",
                      context_paths: list[str] | None = None, template_path: str = "",
                      wait: bool = False) -> dict:
    """Create a new deck (5-10 minutes for about twelve slides).

    brief: what the deck is for. deck_id: its permanent name (made from the brief if empty);
    reuse it for every later change. title: a human name (the brief if empty).
    context: notes to base it on (pasted text). context_paths: documents on the server's
    machine (.md .txt .docx ...). template_path: a template other than the default.
    wait: hold until done; otherwise returns a job - poll job_status.
    """
    deck_id = check_deck_id(deck_id or new_deck_id(brief))
    docs = [upload_path(p) for p in context_paths or []]
    if context.strip():
        docs.append(upload("context.md", context.encode()))
    template = upload_path(template_path) if template_path else None
    return await _run_job("create", deck_id, {"brief": brief, "context": docs},
                          lambda log: op_create(deck_id, brief, docs, template, log=log), wait,
                          title=title or brief[:80], template=template)


@mcp.tool
async def edit_deck(deck_id: str, instruction: str, wait: bool = True) -> dict:
    """Change the deck: wording, layout of a slide, colours, adding or deleting slides."""
    check_deck_id(deck_id)
    return await _run_job("edit", deck_id, {"instruction": instruction}, lambda log: describe(
        facade.edit_deck(deck_id, instruction, log=log), deck_id), wait)


@mcp.tool
async def translate_deck(deck_id: str, lang: str, slides: list[int] | None = None,
                         wait: bool = True) -> dict:
    """Translate the deck (or only the given 1-based slides) into a language, keeping formatting.
    """
    check_deck_id(deck_id)
    return await _run_job("translate", deck_id, {"lang": lang, "slides": slides}, lambda log: describe(
        facade.translate_deck(deck_id, lang, slides, log=log), deck_id), wait)


@mcp.tool
async def ask(deck_id: str, instruction: str, wait: bool = True) -> dict:
    """Anything about the deck; the router decides whether it is a create, edit or translation."""
    check_deck_id(deck_id)
    return await _run_job("ask", deck_id, {"instruction": instruction}, lambda log: describe(
        facade.ask_deck(deck_id, instruction, log=log), deck_id), wait)


@mcp.tool
async def answer(deck_id: str, text: str, wait: bool = True) -> dict:
    """Answer a question the deck's last run stopped to ask; the run carries on from there."""
    check_deck_id(deck_id)
    return await _run_job("answer", deck_id, {"text": text}, lambda log: describe(
        facade.answer(deck_id, text, log=log), deck_id), wait)


@mcp.tool
async def undo(deck_id: str) -> dict:
    """Go back one version of the deck."""
    return await op_undo(deck_id)


@mcp.tool
async def get_deck(deck_id: str) -> dict:
    """A deck: title, status, versions (with download links), template, history."""
    return await op_session(deck_id)


@mcp.tool
async def rename_deck(deck_id: str, title: str) -> dict:
    """Give a deck a new title. Its deck_id never changes."""
    if not await repo.rename_deck(check_deck_id(deck_id), title):
        return {"error": f"no deck {deck_id!r}"}
    return repo.deck_view(await repo.get_deck(deck_id))


@mcp.tool
async def list_decks() -> dict:
    """Every deck: name, title, status, latest file, when it last changed."""
    return await op_list()


@mcp.tool
async def delete_deck(deck_id: str) -> dict:
    """Delete a deck's files, its conversation and its records. Cannot be undone."""
    return await op_delete(deck_id)


@mcp.tool
async def job_status(job_id: str) -> dict:
    """A job's state (queued, running, done, failed), its recent log, and when done its result."""
    return await jobs().view(job_id) or {"error": f"no job {job_id!r}"}


@mcp.tool
async def list_jobs(deck_id: str = "", limit: int = 20) -> list[dict]:
    """Recent jobs, newest first; only one deck's when deck_id is given."""
    return [repo.job_view(j) for j in await repo.list_jobs(deck_id or None, limit)]


# --------------------------------------------------------------------------- REST

def _json(obj, status: int = 200) -> Response:
    """A JSON response; anything not JSON-native is written with str()."""
    return Response(json.dumps(obj, indent=1, default=str, ensure_ascii=False), status_code=status,
                    media_type="application/json")


def _error(e: Exception) -> Response:
    """An exception as a JSON error: 400 for bad input, 404 for missing, 409 when busy, else 500.
    """
    status = {ValueError: 400, FileNotFoundError: 400, LookupError: 404, Busy: 409}
    code = next((v for k, v in status.items() if isinstance(e, k)), 500)
    return _json({"error": f"{e.__class__.__name__}: {e}"}, code)


async def _body(request: Request) -> tuple[dict, dict[str, list]]:
    """A request's fields and uploaded files, from JSON or a multipart form."""
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        fields, files = {}, {}
        for key, value in form.multi_items():
            if hasattr(value, "read"):
                files.setdefault(key, []).append((value.filename, await value.read()))
            else:
                fields[key] = value
        return fields, files
    raw = await request.body()
    return (json.loads(raw) if raw.strip() else {}), {}


def _wait(request: Request) -> bool:
    """Whether the request asked to be held until its job finishes (?wait=true)."""
    return request.query_params.get("wait", "").lower() in ("1", "true", "yes")


def _job_response(view: dict) -> Response:
    """A job as the response: 200 when it has finished, 202 while it is still going."""
    return _json(view, 200 if view["status"] in ("done", "failed") else 202)


def _route(path: str, methods: list[str]):
    """Register a REST route whose errors become JSON with the right status code."""
    def wrap(fn):
        """Register `fn` as the route's handler, wrapped so errors become JSON."""
        async def handler(request: Request) -> Response:
            """Call the route; turn any exception into an error response."""
            try:
                return await fn(request)
            except Exception as e:                       # noqa: BLE001
                return _error(e)
        handler.__name__ = fn.__name__
        return mcp.custom_route(path, methods=methods)(handler)
    return wrap


@_route("/health", ["GET"])
async def rest_health(request: Request) -> Response:
    """Whether the bucket, the checkpointer and the database answer."""
    from .infra.checkpointer import checkpointer

    def probe():
        """Touch the bucket and the checkpointer; raise if either is unreachable."""
        store().ensure_bucket()
        with checkpointer():
            pass
    await asyncio.to_thread(probe)
    await repo.list_jobs(limit=1)                       # the database, and its tables
    return _json({"ok": True, "bucket": store().bucket,
                  "default_template": settings().default_template,
                  "jobs_running": await jobs().running()})


@_route("/templates", ["POST"])
async def rest_ingest(request: Request) -> Response:
    """Ingest an uploaded template (multipart field `file`)."""
    import tempfile

    fields, files = await _body(request)
    if "file" not in files:
        raise ValueError("send the template as a multipart field named 'file'")
    name, data = files["file"][0]
    tmp = Path(tempfile.mkdtemp(dir=settings().workspace_root)) / Path(name or "template.pptx").name
    tmp.write_bytes(data)
    make_default = str(fields.get("make_default", "true")).lower() in ("1", "true", "yes")
    return _job_response(await _run_job(
        "ingest", None, {"file": tmp.name, "make_default": make_default},
        lambda log: op_ingest(str(tmp), make_default, log), _wait(request)))


@_route("/decks", ["POST"])
async def rest_create(request: Request) -> Response:
    """Create a deck. Fields: brief (required), deck_id, title, context (text), template (s3 URI),
    context_uris; files: context, template.
    """
    fields, files = await _body(request)
    brief = str(fields.get("brief", "")).strip()
    if not brief:
        raise ValueError("'brief' is required: what the deck is for")
    deck_id = check_deck_id(str(fields.get("deck_id") or "") or new_deck_id(brief))
    docs = [upload(n, d) for n, d in files.get("context", [])]
    docs += [upload_path(u) for u in fields.get("context_uris", []) or []]
    if str(fields.get("context", "")).strip():
        docs.append(upload("context.md", str(fields["context"]).encode()))
    template = fields.get("template") or None
    if files.get("template"):
        template = upload(*files["template"][0])
    outline = fields.get("outline")
    outline = json.loads(outline) if isinstance(outline, str) else outline
    content_pack = fields.get("content_pack")
    content_pack = json.loads(content_pack) if isinstance(content_pack, str) else content_pack
    return _job_response(await _run_job(
        "create", deck_id, {"brief": brief, "context": docs, "template": template},
        lambda log: op_create(deck_id, brief, docs, template, outline, content_pack, log),
        _wait(request), title=str(fields.get("title") or brief[:80]), template=template))


@_route("/decks", ["GET"])
async def rest_list(request: Request) -> Response:
    """Every deck, from the decks table."""
    return _json(await op_list())


@_route("/decks/{deck_id}", ["GET"])
async def rest_session(request: Request) -> Response:
    """A deck's row and what its conversation knows."""
    return _json(await op_session(request.path_params["deck_id"]))


@_route("/decks/{deck_id}", ["PATCH"])
async def rest_rename(request: Request) -> Response:
    """Rename a deck: {"title": ...}."""
    deck_id = check_deck_id(request.path_params["deck_id"])
    fields, _ = await _body(request)
    if not await repo.rename_deck(deck_id, _required(fields, "title")):
        raise LookupError(f"no deck {deck_id!r}")
    return _json(repo.deck_view(await repo.get_deck(deck_id)))


@_route("/decks/{deck_id}", ["DELETE"])
async def rest_delete(request: Request) -> Response:
    """Delete a deck's files, conversation and rows."""
    return _json(await op_delete(request.path_params["deck_id"]))


@_route("/decks/{deck_id}/{action}", ["POST"])
async def rest_action(request: Request) -> Response:
    """edit / translate / ask / answer / attach / undo on a deck."""
    deck_id = check_deck_id(request.path_params["deck_id"])
    action = request.path_params["action"]
    fields, files = await _body(request)
    if action == "undo":
        return _json(await op_undo(deck_id))
    if action == "edit":
        text = _required(fields, "instruction")
        req, fn = {"instruction": text}, lambda log: facade.edit_deck(deck_id, text, log=log)
    elif action == "translate":
        lang = _required(fields, "lang")
        slides = fields.get("slides")
        slides = json.loads(slides) if isinstance(slides, str) else slides
        req = {"lang": lang, "slides": slides}
        fn = lambda log: facade.translate_deck(deck_id, lang, slides, log=log)
    elif action == "ask":
        text = _required(fields, "instruction")
        req, fn = {"instruction": text}, lambda log: facade.ask_deck(deck_id, text, log=log)
    elif action == "answer":
        text = _required(fields, "text")
        req, fn = {"text": text}, lambda log: facade.answer(deck_id, text, log=log)
    elif action == "attach":
        if "file" not in files:
            raise ValueError("send the deck as a multipart field named 'file'")
        uri = upload(*files["file"][0])
        text = str(fields.get("instruction") or "what can you do with this deck?")
        req = {"file": uri, "instruction": text}
        fn = lambda log: facade.ask_deck(deck_id, text, log=log, attachments=[uri], source_deck=uri)
    else:
        raise LookupError(f"unknown action {action!r}: edit, translate, ask, answer, attach, undo")
    return _job_response(await _run_job(action, deck_id, req,
                                        lambda log: describe(fn(log), deck_id), _wait(request)))


def _required(fields: dict, name: str) -> str:
    """A required field's value; ValueError (a 400) when it is missing or empty."""
    value = str(fields.get(name, "")).strip()
    if not value:
        raise ValueError(f"'{name}' is required")
    return value


@_route("/decks/{deck_id}/download", ["GET"])
async def rest_download(request: Request) -> Response:
    """The deck's .pptx - the latest version, or ?version=N."""
    version = request.query_params.get("version")
    data, name = await asyncio.to_thread(deck_file, request.path_params["deck_id"],
                                         int(version) if version else None)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument."
                                     "presentationml.presentation",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@_route("/jobs/{job_id}", ["GET"])
async def rest_job(request: Request) -> Response:
    """A job's state, log and result."""
    view = await jobs().view(request.path_params["job_id"], _wait(request))
    if view is None:
        raise LookupError(f"no job {request.path_params['job_id']!r}")
    return _json(view)


@_route("/jobs", ["GET"])
async def rest_jobs(request: Request) -> Response:
    """Jobs from the jobs table, newest first: ?deck_id= for one deck, ?limit= (default 50)."""
    deck_id = request.query_params.get("deck_id") or None
    limit = int(request.query_params.get("limit") or 50)
    return _json([repo.job_view(j) for j in await repo.list_jobs(deck_id, limit)])


# --------------------------------------------------------------------------- running it

def app():
    """The ASGI app: MCP at /mcp, REST everywhere else."""
    return mcp.http_app(path="/mcp")


def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the server until interrupted.

    Called by: `deckops serve` (cli.cmd_serve), `make serve`.
    """
    import uvicorn

    from .db.engine import run

    s = settings()
    s.server_host, s.server_port = host or s.server_host, port or s.server_port
    stale = run(repo.fail_stale_jobs)          # jobs a previous server left running
    if stale:
        print(f"marked {stale} job(s) failed: the server stopped before they finished")
    uvicorn.run(app(), host=s.server_host, port=s.server_port, log_level="info")
