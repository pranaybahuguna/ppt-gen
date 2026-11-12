"""The server: every operation the command line has, as MCP tools and as plain REST.

    deckops serve                  # http://127.0.0.1:8765  (make serve)

One FastMCP app serves both:

* **MCP** at `/mcp` (streamable HTTP) - for Claude, or any MCP client. Each tool is described by
  its docstring; `wait=False` (the default for the long ones) returns a job to poll.
* **REST** everywhere else - for curl, a browser, a test script:

      POST   /templates                       multipart: file, make_default      ingest a template
      POST   /decks                           JSON or multipart: deck_id, brief, files... create
      POST   /decks/{id}/edit                 {"instruction": ...}
      POST   /decks/{id}/translate            {"lang": "fr", "slides": [1, 2]}
      POST   /decks/{id}/ask                  {"instruction": ...}   the router decides
      POST   /decks/{id}/answer               {"text": ...}          answer a question it asked
      POST   /decks/{id}/attach               multipart: file, instruction   a deck from elsewhere
      POST   /decks/{id}/undo
      GET    /decks                           every deck in the bucket
      GET    /decks/{id}                      what the session knows
      GET    /decks/{id}/download[?version=N] the .pptx
      DELETE /decks/{id}                      the deck's files and conversation
      GET    /jobs/{job_id}                   a job's state, log and result
      GET    /health

  Long operations run as jobs: the POST returns 202 with a job id at once; `?wait=true` holds
  the request until it finishes instead.

Every finished result carries a `summary` (what changed, in lines a person reads) and a
`download_url` served by this server, so nothing needs S3 credentials to get the file.

Jobs are kept in this process's memory: they do not survive a restart, but the decks and
conversations they produced do (S3 and Postgres).
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from . import facade
from .infra.storage import Ref, sha256_file, store
from .settings import settings

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
class Job:
    """One long operation run in the background: its state, its log, and its result."""

    id: str
    kind: str
    deck_id: str | None
    status: str = "queued"                  # queued -> running -> done | failed
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    log: list[str] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    future: Future | None = None

    def view(self) -> dict:
        """The job as a caller sees it (no internals)."""
        return {"job_id": self.id, "kind": self.kind, "deck_id": self.deck_id,
                "status": self.status,
                "seconds": round((self.finished or time.time()) - (self.started or self.created), 1),
                "log": self.log[-40:], "result": self.result, "error": self.error,
                "status_url": f"/jobs/{self.id}"}


class Jobs:
    """The background jobs of this process. One running job per deck at a time.

    Called by: every tool and route that starts a long operation.
    """

    def __init__(self, workers: int):
        """A thread pool of `workers` and an empty registry."""
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="deckops-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, deck_id: str | None, fn) -> Job:
        """Start `fn(log)` in the background. Raises Busy when the deck already has a job running.
        """
        with self._lock:
            if deck_id and any(j.deck_id == deck_id and j.status in ("queued", "running")
                               for j in self._jobs.values()):
                raise Busy(f"deck {deck_id!r} already has a job running; wait for it to finish")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, deck_id=deck_id)
            self._jobs[job.id] = job

        def log(*parts, **_):
            """The log callback the operation is given: each line goes into the job's log."""
            job.log.append(" ".join(str(p) for p in parts).rstrip())

        def run():
            """Run the operation, recording status, result or error, and timing."""
            job.status, job.started = "running", time.time()
            try:
                job.result = _plain(fn(log))
                job.status = "done"
            except Exception as e:                       # noqa: BLE001 - reported, not raised
                job.status, job.error = "failed", f"{e.__class__.__name__}: {e}"
                job.log.append(traceback.format_exc(limit=4))
            finally:
                job.finished = time.time()
            return job

        job.future = self._pool.submit(run)
        return job

    def get(self, job_id: str) -> Job | None:
        """A job by id, or None."""
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        """Every job this process has run, newest first."""
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)


class Busy(Exception):
    """A deck already has a job running."""


_jobs: Jobs | None = None


def jobs() -> Jobs:
    """The process-wide job registry, created on first use."""
    global _jobs
    if _jobs is None:
        _jobs = Jobs(settings().server_workers)
    return _jobs


async def _finish(job: Job, wait: bool) -> dict:
    """Either wait for the job and return it, or return it as it is now (queued or running)."""
    if wait:
        await asyncio.wrap_future(job.future)
    return job.view()


# --------------------------------------------------------------------------- results

def _plain(obj):
    """Anything a result might hold, as plain JSON types."""
    return json.loads(json.dumps(obj, default=str))


def base_url() -> str:
    """Where this server is reachable, for download links in results."""
    s = settings()
    return s.server_url or f"http://{s.server_host}:{s.server_port}"


def describe(result: dict, deck_id: str | None) -> dict:
    """A facade result plus `summary` (lines a person reads) and `download_url`.

    Called by: every operation below, on its way out.
    """
    lines: list[str] = []
    if result.get("question"):
        lines.append(f"question: {result['question']}")
    elif not result.get("ok") and not result.get("deck"):
        lines.append(f"declined: {result.get('reason') or 'nothing was produced'}")
        lines += [f"error: {e.get('message')}" for e in result.get("errors") or []]
    else:
        lines.append(f"{result.get('route') or 'done'}: version {result.get('versions')} of the deck")
        for c in result.get("changelog") or []:
            if c.get("change") == "text":
                lines.append(f"slide {c.get('slide')}: text {c.get('before', '')!r} -> {c.get('after', '')!r}")
            else:
                where = f"slide {c['slide']}: " if c.get("slide") and "slide" not in str(c.get("change")) else ""
                extra = f" - {c['explanation']}" if c.get("explanation") else ""
                counts = c.get("pattern_counts")
                extra += f" ({', '.join(f'{k} x{v}' for k, v in counts.items())})" if counts else ""
                lines.append(f"{where}{c.get('change')}{extra}")
        lines += [f"warning: {w}" for w in result.get("warnings") or []]
        lines += [f"error: {e.get('message')}" for e in result.get("errors") or []]
    out = {**result, "summary": lines}
    if deck_id and result.get("deck"):
        out["download_url"] = f"{base_url()}/decks/{deck_id}/download"
    return out


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


def op_session(deck_id: str) -> dict:
    """What a deck's conversation knows, with download links for each version."""
    info = facade.session(check_deck_id(deck_id))
    if info.get("exists"):
        info["download_url"] = f"{base_url()}/decks/{deck_id}/download"
        info["version_urls"] = [f"{base_url()}/decks/{deck_id}/download?version={i + 1}"
                                for i in range(len(info.get("versions") or []))]
    return _plain(info)


def op_list() -> dict:
    """Every deck in the bucket, with its size and whether it has a conversation."""
    from .infra.checkpointer import checkpointer
    from .infra.cleanup import inventory

    with checkpointer() as saver:
        inv = inventory(store(), saver)
    decks = {k: v for k, v in inv["prefixes"].items() if v["kind"] == "deck"}
    return {"decks": decks, "objects": inv["objects"], "size": inv["size"]}


def op_delete(deck_id: str) -> dict:
    """Delete a deck: its files and its conversation."""
    from .infra import cleanup
    from .infra.checkpointer import checkpointer

    with checkpointer() as saver:
        plan = cleanup.plan_purge(store(), saver, [check_deck_id(deck_id)])
        return {**plan.summary(), **cleanup.execute(store(), saver, plan)}


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

def _start(kind: str, deck_id: str | None, fn) -> Job:
    """Start a long operation as a job.

    Called by: every MCP tool and REST route that starts one.
    """
    return jobs().submit(kind, deck_id, fn)


@mcp.tool
async def ingest_template(path: str, make_default: bool = True, wait: bool = False) -> dict:
    """Ingest a PowerPoint template so decks can be built in its look (2-5 minutes).

    path: the .pptx on the server's machine. make_default: decks use it when none is named.
    wait: hold until done; otherwise returns a job - poll job_status.
    """
    return await _finish(_start("ingest", None, lambda log: op_ingest(path, make_default, log)), wait)


@mcp.tool
async def create_deck(deck_id: str, brief: str, context: str = "", context_paths: list[str] | None = None,
                      template_path: str = "", wait: bool = False) -> dict:
    """Create a new deck (5-10 minutes for about twelve slides).

    deck_id: names the deck and its conversation; reuse it for every later change.
    brief: what the deck is for. context: notes to base it on (pasted text).
    context_paths: documents on the server's machine (.md .txt .docx ...).
    template_path: a template other than the default. wait: hold until done; otherwise
    returns a job - poll job_status.
    """
    check_deck_id(deck_id)
    docs = [upload_path(p) for p in context_paths or []]
    if context.strip():
        docs.append(upload("context.md", context.encode()))
    template = upload_path(template_path) if template_path else None
    return await _finish(_start("create", deck_id, lambda log: op_create(
        deck_id, brief, docs, template, log=log)), wait)


@mcp.tool
async def edit_deck(deck_id: str, instruction: str, wait: bool = True) -> dict:
    """Change the deck: wording, layout of a slide, colours, adding or deleting slides."""
    check_deck_id(deck_id)
    return await _finish(_start("edit", deck_id, lambda log: describe(
        facade.edit_deck(deck_id, instruction, log=log), deck_id)), wait)


@mcp.tool
async def translate_deck(deck_id: str, lang: str, slides: list[int] | None = None,
                         wait: bool = True) -> dict:
    """Translate the deck (or only the given 1-based slides) into a language, keeping formatting.
    """
    check_deck_id(deck_id)
    return await _finish(_start("translate", deck_id, lambda log: describe(
        facade.translate_deck(deck_id, lang, slides, log=log), deck_id)), wait)


@mcp.tool
async def ask(deck_id: str, instruction: str, wait: bool = True) -> dict:
    """Anything about the deck; the router decides whether it is a create, edit or translation."""
    check_deck_id(deck_id)
    return await _finish(_start("ask", deck_id, lambda log: describe(
        facade.ask_deck(deck_id, instruction, log=log), deck_id)), wait)


@mcp.tool
async def answer(deck_id: str, text: str, wait: bool = True) -> dict:
    """Answer a question the deck's last run stopped to ask; the run carries on from there."""
    check_deck_id(deck_id)
    return await _finish(_start("answer", deck_id, lambda log: describe(
        facade.answer(deck_id, text, log=log), deck_id)), wait)


@mcp.tool
async def undo(deck_id: str) -> dict:
    """Go back one version of the deck."""
    return _plain(await asyncio.to_thread(facade.undo, check_deck_id(deck_id)))


@mcp.tool
async def get_deck(deck_id: str) -> dict:
    """What the deck's conversation knows: versions (with download links), template, history."""
    return await asyncio.to_thread(op_session, deck_id)


@mcp.tool
async def list_decks() -> dict:
    """Every deck in the bucket."""
    return await asyncio.to_thread(op_list)


@mcp.tool
async def delete_deck(deck_id: str) -> dict:
    """Delete a deck's files and its conversation. Cannot be undone."""
    return await asyncio.to_thread(op_delete, deck_id)


@mcp.tool
async def job_status(job_id: str) -> dict:
    """A job's state (queued, running, done, failed), its recent log, and when done its result."""
    job = jobs().get(job_id)
    return job.view() if job else {"error": f"no job {job_id!r}"}


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


async def _job_response(job: Job, wait: bool) -> Response:
    """A job as the response: 200 when it has finished, 202 while it is still going."""
    view = await _finish(job, wait)
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
    """Whether the bucket and the checkpointer answer."""
    from .infra.checkpointer import checkpointer

    def probe():
        """Touch the bucket and the checkpointer; raise if either is unreachable."""
        store().ensure_bucket()
        with checkpointer():
            pass
        return {"ok": True, "bucket": store().bucket, "default_template": settings().default_template,
                "jobs_running": sum(j.status == "running" for j in jobs().all())}
    return _json(await asyncio.to_thread(probe))


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
    return await _job_response(_start("ingest", None, lambda log: op_ingest(str(tmp), make_default, log)),
                               _wait(request))


@_route("/decks", ["POST"])
async def rest_create(request: Request) -> Response:
    """Create a deck. Fields: deck_id, brief, context (text), template (s3 URI); files: context,
    template.
    """
    fields, files = await _body(request)
    deck_id = check_deck_id(str(fields.get("deck_id", "")))
    brief = str(fields.get("brief", "")).strip()
    if not brief:
        raise ValueError("'brief' is required: what the deck is for")
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
    return await _job_response(_start("create", deck_id, lambda log: op_create(
        deck_id, brief, docs, template, outline, content_pack, log)), _wait(request))


@_route("/decks", ["GET"])
async def rest_list(request: Request) -> Response:
    """Every deck in the bucket."""
    return _json(await asyncio.to_thread(op_list))


@_route("/decks/{deck_id}", ["GET"])
async def rest_session(request: Request) -> Response:
    """What a deck's conversation knows."""
    return _json(await asyncio.to_thread(op_session, request.path_params["deck_id"]))


@_route("/decks/{deck_id}", ["DELETE"])
async def rest_delete(request: Request) -> Response:
    """Delete a deck's files and conversation."""
    return _json(await asyncio.to_thread(op_delete, request.path_params["deck_id"]))


@_route("/decks/{deck_id}/{action}", ["POST"])
async def rest_action(request: Request) -> Response:
    """edit / translate / ask / answer / attach / undo on a deck."""
    deck_id = check_deck_id(request.path_params["deck_id"])
    action = request.path_params["action"]
    fields, files = await _body(request)
    if action == "undo":
        return _json(_plain(await asyncio.to_thread(facade.undo, deck_id)))
    if action == "edit":
        text = _required(fields, "instruction")
        fn = lambda log: facade.edit_deck(deck_id, text, log=log)
    elif action == "translate":
        lang = _required(fields, "lang")
        slides = fields.get("slides")
        slides = json.loads(slides) if isinstance(slides, str) else slides
        fn = lambda log: facade.translate_deck(deck_id, lang, slides, log=log)
    elif action == "ask":
        text = _required(fields, "instruction")
        fn = lambda log: facade.ask_deck(deck_id, text, log=log)
    elif action == "answer":
        text = _required(fields, "text")
        fn = lambda log: facade.answer(deck_id, text, log=log)
    elif action == "attach":
        if "file" not in files:
            raise ValueError("send the deck as a multipart field named 'file'")
        uri = upload(*files["file"][0])
        text = str(fields.get("instruction") or "what can you do with this deck?")
        fn = lambda log: facade.ask_deck(deck_id, text, log=log, attachments=[uri], source_deck=uri)
    else:
        raise LookupError(f"unknown action {action!r}: edit, translate, ask, answer, attach, undo")
    return await _job_response(_start(action, deck_id, lambda log: describe(fn(log), deck_id)),
                               _wait(request))


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
    job = jobs().get(request.path_params["job_id"])
    if job is None:
        raise LookupError(f"no job {request.path_params['job_id']!r}")
    return _json(job.view())


@_route("/jobs", ["GET"])
async def rest_jobs(request: Request) -> Response:
    """Every job this process has run, newest first (without logs)."""
    return _json([{k: v for k, v in j.view().items() if k not in ("log", "result")}
                  for j in jobs().all()])


# --------------------------------------------------------------------------- running it

def app():
    """The ASGI app: MCP at /mcp, REST everywhere else."""
    return mcp.http_app(path="/mcp")


def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the server until interrupted.

    Called by: `deckops serve` (cli.cmd_serve), `make serve`.
    """
    import uvicorn

    s = settings()
    s.server_host, s.server_port = host or s.server_host, port or s.server_port
    uvicorn.run(app(), host=s.server_host, port=s.server_port, log_level="info")
