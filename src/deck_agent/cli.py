"""Command line. Enough to run, inspect and debug the whole thing without a host application.

    deckops doctor                       is everything this needs actually working?
    deckops init                         create the checkpoint tables and the bucket
    deckops seed                         put the sample template and content in the bucket
    deckops kit <template.pptx>          build and cache a style kit for a template
    deckops create <id> "<brief>" ...    a new deck
    deckops edit <id> "<instruction>"    change the deck this session has
    deckops translate <id> <lang>        translate it
    deckops ask <id> "<anything>"        let the router decide
    deckops answer <id> "<text>"         answer a question the graph asked
    deckops session <id>                 what this session knows
    deckops undo <id>                    go back one version
    deckops get <s3://...> <path>        fetch an artefact
    deckops graph                        print the graph as mermaid
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
    print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def _upload_if_local(path_or_uri: str | None) -> str | None:
    """A local .pptx becomes an object first: nothing in the graph works on local paths."""
    if not path_or_uri or path_or_uri.startswith("s3://"):
        return path_or_uri
    local = Path(path_or_uri)
    if not local.exists():
        sys.exit(f"no such file: {local}")
    from .infra.storage import sha256_file

    return store().put(local, f"uploads/{sha256_file(local)}-{local.name}")


# --------------------------------------------------------------------------- commands

def cmd_doctor(args) -> int:
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
    from .infra.checkpointer import migrate

    print(migrate())
    st = store()
    st.ensure_bucket()
    print(f"bucket ready: {st.bucket}")
    return 0


def cmd_seed(args) -> int:
    """Put the engine's own sample template and a small content pack in the bucket."""
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
    from .engine.kitbuild import build_kit
    from .nodes.kit import kit_key
    from .infra.storage import sha256_file
    from .sandbox.render import render

    s = settings()
    template = Path(args.template)
    if not template.exists():
        sys.exit(f"no such template: {template}")
    template_id = sha256_file(template)
    workdir = s.workspace_root / f"kit-{template_id}"
    kit, report = build_kit(template, workdir, make_planner(s), render=render)
    uri = store().put_json(kit, kit_key(template_id))
    if args.out:
        Path(args.out).write_text(json.dumps(kit, indent=1))
    _p({"template_id": template_id, "kit": uri, "patterns": sorted(kit["patterns"]),
        "capacity": report.get("capacity"),
        "dropped": {n: r.get("reasons") for n, r in report["patterns"].items()
                    if not n.startswith("_") and not r.get("passed")}})
    return 0


def cmd_create(args) -> int:
    content = json.loads(Path(args.content).read_text()) if args.content else None
    outline = json.loads(Path(args.outline).read_text()) if args.outline else None
    extra = {"outline": outline} if outline else {}
    result = facade.create_deck(args.id, args.brief, content,
                                _upload_if_local(args.template), **extra)
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_edit(args) -> int:
    result = facade.edit_deck(args.id, args.instruction)
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_translate(args) -> int:
    slides = [int(x) for x in args.slides.split(",")] if args.slides else None
    result = facade.translate_deck(args.id, args.lang, slides)
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_ask(args) -> int:
    result = facade.ask_deck(args.id, args.instruction)
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_answer(args) -> int:
    result = facade.answer(args.id, args.text)
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_attach(args) -> int:
    """Hand an existing deck to a session, then do something with it."""
    uri = _upload_if_local(args.deck)
    result = facade.ask_deck(args.id, args.instruction or "what can you do with this deck?",
                             attachments=[uri], source_deck=uri)
    _p(result)
    return 0 if result.get("ok") else 1


def cmd_session(args) -> int:
    _p(facade.session(args.id))
    return 0


def cmd_undo(args) -> int:
    _p(facade.undo(args.id))
    return 0


def cmd_get(args) -> int:
    path = store().get(args.uri, args.path)
    print(path)
    return 0


def cmd_graph(args) -> int:
    from .graph import mermaid

    print(mermaid())
    return 0


def cmd_selftest(args) -> int:
    """Run the sandbox's own checks and print them: network, root filesystem, tools, fonts."""
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
    ap = argparse.ArgumentParser(prog="deckops", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check that everything this needs is working").set_defaults(fn=cmd_doctor)
    sub.add_parser("init", help="create the checkpoint tables and the bucket").set_defaults(fn=cmd_init)
    sub.add_parser("seed", help="upload the sample template and content pack").set_defaults(fn=cmd_seed)
    sub.add_parser("graph", help="print the graph as mermaid").set_defaults(fn=cmd_graph)
    sub.add_parser("selftest", help="prove the sandbox is isolated").set_defaults(fn=cmd_selftest)

    k = sub.add_parser("kit", help="build and cache a style kit for a template")
    k.add_argument("template")
    k.add_argument("--out", help="also write the kit to this file")
    k.set_defaults(fn=cmd_kit)

    c = sub.add_parser("create", help="create a deck")
    c.add_argument("id", help="the deck session id (= the conversation)")
    c.add_argument("brief")
    c.add_argument("--template", help="a .pptx path or an s3:// URI")
    c.add_argument("--content", help="content pack JSON: the facts the deck may use")
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

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
