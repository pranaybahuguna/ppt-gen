"""Command line. Enough to run, inspect and debug the whole thing without a host application.

    deckops doctor                       is everything this needs actually working?
    deckops init                         create the checkpoint tables and the bucket
    deckops seed                         put the sample template and content in the bucket
    deckops kit <template.pptx>          ingest a template: kit cached in kits/, template in
    uploads/
    deckops create <id> "<brief>" ...    a new deck
    deckops edit <id> "<instruction>"    change the deck this session has
    deckops translate <id> <lang>        translate it
    deckops ask <id> "<anything>"        let the router decide
    deckops answer <id> "<text>"         answer a question the graph asked
    deckops session <id>                 what this session knows
    deckops undo <id>                    go back one version
    deckops get <s3://...> <path>        fetch an artefact
    deckops ls [prefix]                  list what is in the bucket
    deckops clean [...]                  delete decks, tidy them, or clear everything (dry run
                                         unless --yes)
    deckops graph                        print the graph as mermaid
    deckops serve                        the MCP + REST server (http://127.0.0.1:8765)
    deckops selftest                     prove the sandbox is isolated
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import facade
from .engine import engine_version, fonts_available
from .infra.storage import store
from .llm.planner import planner as make_planner
from .settings import REPO, settings


def _p(obj) -> None:
    """Print an object as indented JSON - the one output format every command uses.

    Called by: every cmd_* function.
    """
    print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def _upload_if_local(path_or_uri: str | None) -> str | None:
    """A local .pptx becomes an object first: nothing in the graph works on local paths.

    Called by: cmd_attach, cmd_create, cmd_seed.
    """
    if not path_or_uri or path_or_uri.startswith("s3://"):
        return path_or_uri
    local = Path(path_or_uri)
    if not local.exists():
        sys.exit(f"no such file: {local}")
    from .infra.storage import sha256_file

    return store().put(local, f"uploads/{sha256_file(local)}-{local.name}")


def recorded(kind: str, deck_id: str | None, request: dict, call, *, title: str | None = None,
             template: str | None = None) -> dict:
    """Run `call()` recorded as a job in the database, as the server does: a row 'queued' and
    'running' before, 'done' or 'failed' with the summary after, and the deck's row updated.

    If the database cannot be reached the work still runs, and a line on stderr says it was
    not recorded.

    Called by: the commands that change a deck or ingest a template.
    """
    from .db import repo
    from .db.engine import run
    from .summary import describe

    try:
        job_id = run(lambda: repo.start_job(kind, deck_id, request, title=title, template=template))
        run(lambda: repo.mark_running(job_id))
    except Exception as e:                               # noqa: BLE001
        print(f"(not recorded in the database: {e.__class__.__name__}: {e})", file=sys.stderr)
        return call()
    try:
        result = call()
    except Exception as e:
        err = f"{e.__class__.__name__}: {e}"
        run(lambda: repo.finish_job(job_id, ok=False, error=err))
        raise
    summary = describe(result, deck_id)["summary"] if deck_id else []
    ok = bool(result.get("ok")) or bool(result.get("question"))
    run(lambda: repo.finish_job(job_id, ok=ok, summary=summary, deck_file=result.get("deck"),
                                error=None if ok else result.get("reason")))
    return result


# --------------------------------------------------------------------------- commands

def cmd_doctor(args) -> int:
    """`deckops doctor`: check the engine, fonts, sandbox image, object storage, checkpointer and
    model; print each with the fix for any problem, then READY or a count of problems.

    Called by: main(); `make doctor`. Exit code 1 when anything is wrong.
    """
    s = settings()
    out: dict = {"settings": s.redacted(), "engine": None, "fonts": None,
                 "sandbox": None, "checkpointer": None, "object_storage": None}
    problems: list[str] = []

    try:
        out["engine"] = engine_version()
    except Exception as e:
        problems.append(f"engine: {e}")
    try:
        fonts = fonts_available()
        out["fonts"] = fonts
        if not fonts.get("_exact"):
            problems.append("fonts: no metric-compatible fonts found, so text measurement is "
                            "approximate. Run: make fonts")
    except Exception as e:
        problems.append(f"fonts: {e}")

    from .sandbox.docker import available

    ok, why = available(s)
    out["sandbox"] = {"ok": ok, "detail": why}
    if not ok and (s.render_backend == "docker" or s.code_runner == "docker"):
        problems.append(f"sandbox: {why}")

    try:
        st = store()
        st.ensure_bucket()
        out["object_storage"] = {"ok": True, "bucket": st.bucket,
                                 "objects": len(st.list(""))}
    except Exception as e:
        problems.append(f"object storage: {e}")
        out["object_storage"] = {"ok": False, "error": str(e)}

    try:
        from .db import repo
        from .db.engine import run, url

        run(lambda: repo.list_jobs(limit=1))
        out["database"] = {"ok": True, "url": url().split("@")[-1]}
    except Exception as e:
        problems.append(f"database (decks and jobs tables): {e.__class__.__name__}: {e}. "
                        "Run: make up && make init")
        out["database"] = {"ok": False, "error": str(e)[:200]}

    try:
        from .infra.checkpointer import checkpointer

        with checkpointer(s) as saver:
            out["checkpointer"] = {"ok": True, "kind": type(saver).__name__}
    except Exception as e:
        problems.append(f"checkpointer: {e}")
        out["checkpointer"] = {"ok": False, "error": str(e)}

    try:
        p = make_planner(s)
        out["model"] = {"ok": True, "planner": p.name}
    except FileNotFoundError as e:
        out["model"] = {"ok": False, "note": str(e).split("\n")[0]}

    out["problems"] = problems
    _p(out)
    print("\nREADY" if not problems else f"\n{len(problems)} problem(s) above")
    return 0 if not problems else 1


def cmd_init(args) -> int:
    """`deckops init`: create the checkpoint tables, the decks and jobs tables, and the bucket.
    Idempotent.

    Called by: main(); `make init`, once after `make up`.
    """
    from .db.engine import ready, run, url
    from .infra.checkpointer import migrate

    print(migrate())
    run(ready)
    print(f"decks and jobs tables ready in {url().split('@')[-1]}")
    st = store()
    st.ensure_bucket()
    print(f"bucket ready: {st.bucket}")
    return 0


def cmd_seed(args) -> int:
    """Put the engine's own sample template and a small content pack in the bucket.

    Called by: main.
    """
    samples = REPO / "local" / "samples"
    template = samples / "example_style.pptx"
    if not template.exists():
        sys.exit(f"{template} is missing. Run: make samples")
    uri = _upload_if_local(str(template))
    content = {
        "facts": [
            {"id": "F1", "text": "4,100 support tickets arrived in September 2026",
             "source": "pilot report"},
            {"id": "F2", "text": "38% of incoming tickets were answered by the bot",
             "source": "pilot report"},
            {"id": "F3", "text": "median first response fell from 11 hours to 3 hours",
             "source": "pilot report"},
            {"id": "F4", "text": "the pilot covered two teams and ran for eight weeks",
             "source": "pilot report"},
        ],
        "datasets": {"weekly_tickets": {"columns": ["week", "opened", "answered_by_bot"],
                                        "rows": [["W1", 980, 120], ["W2", 1010, 290],
                                                 ["W3", 1040, 380], ["W4", 1070, 410]]}},
    }
    content_uri = store().put_json(content, "seed/content.json")
    _p({"template": uri, "content": content_uri,
        "hint": "put the template URI in config/agent.json as default_template"})
    return 0


def cmd_kit(args) -> int:
    """`deckops kit <template.pptx>`: ingest a template (templates.ingest) and print the result.

    Called by: main(). Exit code 1 when the template yields no usable layouts.
    """
    from .templates import ingest

    try:
        result = recorded("ingest", None, {"file": str(args.template),
                                           "make_default": args.make_default},
                          lambda: ingest(args.template, args.out, args.make_default))
    except FileNotFoundError as e:
        sys.exit(str(e))
    _p(result)
    if not result["ok"]:
        print("\nNo usable layouts: this template was not cached, and no deck can be built "
              "from it yet. The reasons above are per layout.")
        return 1
    return 0


def cmd_create(args) -> int:
    """`deckops create <id> <brief>`: a new deck. Uploads --template and --context files first,
    then
    calls facade.create_deck and prints the result.

    Called by: main().
    """
    content = json.loads(Path(args.content).read_text()) if args.content else None
    outline = json.loads(Path(args.outline).read_text()) if args.outline else None
    extra = {"outline": outline} if outline else {}
    context = [_upload_if_local(c) for c in args.context or []]
    template = _upload_if_local(args.template)
    result = recorded("create", args.id, {"brief": args.brief, "context": context},
                      lambda: facade.create_deck(args.id, args.brief, content, template,
                                                 context=context, **extra),
                      title=args.brief[:80], template=template)
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_edit(args) -> int:
    """`deckops edit <id> <instruction>`. Calls facade.edit_deck on the session's current deck.

    Called by: main(), when that subcommand is given. Returns the process exit code.
    """
    result = recorded("edit", args.id, {"instruction": args.instruction},
                      lambda: facade.edit_deck(args.id, args.instruction))
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_translate(args) -> int:
    """`deckops translate <id> <lang>`. Calls facade.translate_deck, optionally for --slides only.

    Called by: main(), when that subcommand is given. Returns the process exit code.
    """
    slides = [int(x) for x in args.slides.split(",")] if args.slides else None
    result = recorded("translate", args.id, {"lang": args.lang, "slides": slides},
                      lambda: facade.translate_deck(args.id, args.lang, slides))
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_ask(args) -> int:
    """`deckops ask <id> <instruction>`. Calls facade.ask_deck: the router decides what is wanted.

    Called by: main(), when that subcommand is given. Returns the process exit code.
    """
    result = recorded("ask", args.id, {"instruction": args.instruction},
                      lambda: facade.ask_deck(args.id, args.instruction))
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_answer(args) -> int:
    """`deckops answer <id> <text>`. Calls facade.answer to resume a run that stopped to ask.

    Called by: main(), when that subcommand is given. Returns the process exit code.
    """
    result = recorded("answer", args.id, {"text": args.text},
                      lambda: facade.answer(args.id, args.text))
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_attach(args) -> int:
    """Hand an existing deck to a session, then do something with it.

    Called by: main.
    """
    uri = _upload_if_local(args.deck)
    text = args.instruction or "what can you do with this deck?"
    result = recorded("attach", args.id, {"file": uri, "instruction": text},
                      lambda: facade.ask_deck(args.id, text, attachments=[uri], source_deck=uri))
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_session(args) -> int:
    """`deckops session <id>`. Prints facade.session: versions, template, kit, trace.

    Called by: main(), when that subcommand is given. Returns the process exit code.
    """
    _p(facade.session(args.id))
    return 0


def cmd_undo(args) -> int:
    """`deckops undo <id>`. Calls facade.undo: drops the newest version.

    Called by: main(), when that subcommand is given. Returns the process exit code.
    """
    _p(recorded("undo", args.id, {}, lambda: facade.undo(args.id)))
    return 0


def cmd_get(args) -> int:
    """`deckops get <s3://...> <path>`. Downloads one object from the bucket.

    Called by: main(), when that subcommand is given. Returns the process exit code.
    """
    path = store().get(args.uri, args.path)
    print(path)
    return 0


def cmd_ls(args) -> int:
    """The bucket, without needing the AWS CLI or its credentials set up.

    Called by: main.
    """
    from .infra.cleanup import human

    objs = store().objects(args.prefix or "")
    if not args.recursive:
        # one line per "folder" at this level, like `aws s3 ls`
        depth = (args.prefix or "").count("/")
        folders: dict[str, list] = {}
        files = []
        for o in objs:
            parts = o.key.split("/")
            if len(parts) > depth + 1:
                folders.setdefault("/".join(parts[:depth + 1]) + "/", []).append(o)
            else:
                files.append(o)
        for name, group in sorted(folders.items()):
            print(f"{'DIR':>10}  {len(group):>5} objects {human(sum(o.size for o in group)):>10}  {name}")
        objs = files
    for o in objs:
        print(f"{human(o.size):>10}  {o.key}")
    return 0


def cmd_clean(args) -> int:
    """Plan a clean-up, show it, and only carry it out with --yes.

    Called by: main.
    """
    from .infra import cleanup
    from .infra.checkpointer import checkpointer

    st = store()
    with checkpointer() as saver:
        chosen = [args.all, bool(args.deck), args.tidy, args.tests, args.orphans,
                  bool(args.older_than)]
        if not any(chosen):
            _p(cleanup.inventory(st, saver))
            print("\nNothing deleted. Choose what to clean: --deck ID, --tidy, --tests, "
                  "--orphans, --older-than 7d or --all (then --yes to do it).")
            return 0
        if args.all and sum(map(bool, chosen)) > 1:
            sys.exit("--all already covers everything; use it on its own")

        if args.all:
            plan = cleanup.plan_everything(st, saver, keep_templates=args.keep_templates)
        else:
            plan = cleanup.Plan()
            if args.tidy:
                # with --deck, tidy those decks; without, every deck that has a session
                plan.add(cleanup.plan_tidy_all(st, saver, args.deck or None))
            elif args.deck:
                plan.add(cleanup.plan_purge(st, saver, args.deck))
            if args.tests:
                plan.add(cleanup.plan_purge(st, saver, list(cleanup.TEST_PATTERNS)))
            if args.orphans:
                plan.add(cleanup.plan_orphans(st, saver))
            if args.older_than:
                plan.add(cleanup.plan_older_than(st, saver, cleanup.parse_age(args.older_than)))

        summary = plan.summary()
        if not args.yes:
            _p({"dry_run": True, **summary})
            if plan.objects or plan.threads:
                print("\nDry run: nothing deleted. Add --yes to delete the above.")
            return 0
        done = cleanup.execute(st, saver, plan)
    # The decks gone from the bucket and the checkpointer go from the tables too, with their jobs.
    gone = None if args.all else sorted(
        set(plan.threads) | {o.key.split("/", 1)[0] for o in plan.objects} - set(cleanup.SHARED))
    if args.all or (gone and not args.tidy):
        from .db import repo
        from .db.engine import run

        try:
            done["deleted_rows"] = run(lambda: repo.delete_decks(gone))
        except Exception as e:                           # noqa: BLE001
            done["deleted_rows"] = f"not reached: {e.__class__.__name__}"
    _p({"dry_run": False, **summary, **done})
    return 0


def cmd_serve(args) -> int:
    """`deckops serve`: run the MCP + REST server (server.serve) until interrupted.

    Called by: main(); `make serve`.
    """
    from .server import serve

    serve(args.host, args.port)
    return 0


def cmd_graph(args) -> int:
    """`deckops graph`. Prints the graph as mermaid (graph.mermaid).

    Called by: main(), when that subcommand is given. Returns the process exit code.
    """
    from .graph import mermaid

    print(mermaid())
    return 0


def cmd_selftest(args) -> int:
    """Run the sandbox's own checks and print them: network, root filesystem, tools, fonts.

    Called by: main.
    """
    import tempfile

    from .sandbox.docker import read_result, run_container

    s = settings()
    with tempfile.TemporaryDirectory(dir=s.workspace_root) as tmp:
        (Path(tmp) / "out").mkdir()
        res = run_container(["python3", "/opt/runner/selftest.py", "out"], tmp,
                            timeout_s=120, memory="512m", cpus="1", pids=64)
        report = read_result(tmp)
    _p({"container": res.detail() | {"stdout": "(see report)"}, "report": report})
    return 0 if report.get("ok") else 1


# --------------------------------------------------------------------------- parser

def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run one subcommand.

    Called by: the `deckops` console script (pyproject.toml), `python -m deck_agent.cli`, and the
    Makefile targets. Returns the subcommand's exit code.
    """
    ap = argparse.ArgumentParser(prog="deckops", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check that everything this needs is working").set_defaults(fn=cmd_doctor)
    sub.add_parser("init", help="create the checkpoint tables and the bucket").set_defaults(fn=cmd_init)
    sub.add_parser("seed", help="upload the sample template and content pack").set_defaults(fn=cmd_seed)
    sub.add_parser("graph", help="print the graph as mermaid").set_defaults(fn=cmd_graph)
    sv = sub.add_parser("serve", help="run the MCP + REST server")
    sv.add_argument("--host", help="default: server_host (127.0.0.1)")
    sv.add_argument("--port", type=int, help="default: server_port (8765)")
    sv.set_defaults(fn=cmd_serve)
    sub.add_parser("selftest", help="prove the sandbox is isolated").set_defaults(fn=cmd_selftest)

    k = sub.add_parser("kit", help="build and cache a style kit for a template")
    k.add_argument("template")
    k.add_argument("--out", help="also write the kit to this file")
    k.add_argument("--make-default", action="store_true",
                   help="make this template config/agent.json's default_template")
    k.set_defaults(fn=cmd_kit)

    c = sub.add_parser("create", help="create a deck")
    c.add_argument("id", help="the deck session id (= the conversation)")
    c.add_argument("brief")
    c.add_argument("--template", help="a .pptx path or an s3:// URI")
    c.add_argument("--context", action="append", metavar="FILE",
                   help="a document the deck should be based on (.md .txt .docx ...). "
                        "Repeatable. The facts are written from it; see the gather node")
    c.add_argument("--content", help="a ready-made content pack JSON, instead of --context")
    c.add_argument("--outline", help="use this ready-made outline instead of calling a model")
    c.set_defaults(fn=cmd_create)

    e = sub.add_parser("edit", help="change the deck in this session")
    e.add_argument("id")
    e.add_argument("instruction")
    e.set_defaults(fn=cmd_edit)

    t = sub.add_parser("translate", help="translate the deck in this session")
    t.add_argument("id")
    t.add_argument("lang")
    t.add_argument("--slides", help="1-based, comma separated, e.g. 1,2,5")
    t.set_defaults(fn=cmd_translate)

    a = sub.add_parser("ask", help="let the router decide what to do")
    a.add_argument("id")
    a.add_argument("instruction")
    a.set_defaults(fn=cmd_ask)

    an = sub.add_parser("answer", help="answer a question the graph asked")
    an.add_argument("id")
    an.add_argument("text")
    an.set_defaults(fn=cmd_answer)

    at = sub.add_parser("attach", help="give a session an existing deck")
    at.add_argument("id")
    at.add_argument("deck", help="a .pptx path or an s3:// URI")
    at.add_argument("instruction", nargs="?")
    at.set_defaults(fn=cmd_attach)

    s_ = sub.add_parser("session", help="what this session knows")
    s_.add_argument("id")
    s_.set_defaults(fn=cmd_session)

    u = sub.add_parser("undo", help="go back one version")
    u.add_argument("id")
    u.set_defaults(fn=cmd_undo)

    g = sub.add_parser("get", help="download an artefact")
    g.add_argument("uri")
    g.add_argument("path")
    g.set_defaults(fn=cmd_get)

    ls = sub.add_parser("ls", help="list the bucket")
    ls.add_argument("prefix", nargs="?", help="e.g. my-deck/ ; omit for the top level")
    ls.add_argument("-r", "--recursive", action="store_true", help="every object, not folders")
    ls.set_defaults(fn=cmd_ls)

    cl = sub.add_parser(
        "clean", help="delete decks and their sessions, tidy them, or clear everything",
        description="With no option: show what is there. Every option is a dry run until --yes.\n"
                    "A deck is its files AND its conversation; both are always cleaned together.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    cl.add_argument("--deck", action="append", default=[], metavar="ID",
                    help="purge this deck (files + session). Repeatable; shell patterns work: 'demo-*'")
    cl.add_argument("--tidy", action="store_true",
                    help="keep each deck usable but drop older runs' previews, plans and ops "
                         "(all decks, or only those named with --deck)")
    cl.add_argument("--tests", action="store_true", help="what the test suites left behind")
    cl.add_argument("--orphans", action="store_true",
                    help="files with no session, and sessions whose files are gone")
    cl.add_argument("--older-than", metavar="AGE", help="purge decks untouched for e.g. 7d, 12h")
    cl.add_argument("--all", action="store_true", help="the whole bucket and every session")
    cl.add_argument("--keep-templates", action="store_true",
                    help="with --all: keep kits/ and uploads/ (ingested templates)")
    cl.add_argument("--yes", action="store_true", help="actually delete")
    cl.set_defaults(fn=cmd_clean)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
