"""Ingesting a template: from a .pptx to a tested kit in the bucket.

One function, used by the command line (`deckops kit`) and the server (`POST /templates`, the
`ingest_template` tool), so both do exactly the same thing.
"""
from __future__ import annotations

import json
from pathlib import Path

from .infra.storage import sha256_file, store
from .settings import REPO, settings


def ingest(source: str | Path, out: str | Path | None = None, make_default: bool = False,
           planner=None, log=print) -> dict:
    """Ingest a template. -> {ok, template_id, kit, template, patterns, capacity, dropped, ...}

    Prepares the file (unique shape ids), builds and tests a kit (engine.kitbuild.build_kit),
    stores the kit in `kits/<hash>.json` and the prepared template in `uploads/`. With
    `single_kit` on, other kits and their templates are removed - only after the new one is
    stored, so a failed ingest replaces nothing. With make_default, the template becomes
    `default_template`, both in config/agent.json and in this process's settings. A template
    with no usable layouts is not stored, and `ok` is False.

    Called by: cli.cmd_kit; server.ingest_template and the server's POST /templates.
    """
    from .engine.kitbuild import build_kit
    from .engine.normalise import prepare
    from .llm.planner import planner as make_planner
    from .nodes.kit import kit_key
    from .sandbox.render import render

    s = settings()
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"no such template: {source}")

    # Prepare it first; the prepared copy is what the kit addresses, so it is what decks must
    # be built from. When nothing needed changing the bytes come through unchanged, so the
    # template keeps its own hash and its own file.
    workdir = s.workspace_root / f"kit-{sha256_file(source)}"
    workdir.mkdir(parents=True, exist_ok=True)
    prepared_path = workdir / "prepared.pptx"
    prepared = prepare(source, prepared_path)
    template = prepared_path if prepared["changed"] else source
    if prepared["changed"]:
        log(f"prepared the template: {prepared['ids_renumbered']} nested shape id(s) made "
            f"unique on slides {prepared['slides_touched']}, which the engine could not "
            "otherwise have used")
    template_id = sha256_file(template)
    kit, report = build_kit(template, workdir, planner or make_planner(s), render=render, log=log)

    # A kit with no patterns is a failure, not a result: caching it would hand the same empty
    # answer back for ever, keyed by the template's hash, even after the cause was fixed.
    uri = template_uri = replaced = None
    if kit["patterns"]:
        uri = store().put_json(kit, kit_key(template_id))
        # The prepared copy is the one the kit's shape ids refer to.
        name = source.stem + ("-prepared" if prepared["changed"] else "") + ".pptx"
        template_uri = store().put(template, f"uploads/{template_id}-{name.replace(' ', '-')}")
        if s.single_kit:
            from .infra.cleanup import keep_only_kit

            replaced = keep_only_kit(store(), template_id)
        if make_default:
            cfg_path = REPO / "config" / "agent.json"
            cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
            cfg["default_template"] = template_uri
            cfg_path.write_text(json.dumps(cfg, indent=1) + "\n")
            s.default_template = template_uri          # this process too, not only the next
    if out:
        Path(out).write_text(json.dumps(kit, indent=1))
    beside = None
    if prepared["changed"] and kit["patterns"] and out:
        # The kit refers to this file's ids; hand it over beside the kit, for reading.
        beside = Path(out).with_name(source.stem + "-prepared.pptx")
        beside.write_bytes(prepared_path.read_bytes())
    return {
        "ok": bool(kit["patterns"]),
        "template_id": template_id, "kit": uri, "template": template_uri,
        "default_template": bool(make_default and template_uri),
        "replaced": replaced if s.single_kit else "single_kit is off: other kits kept",
        "local_copy": str(beside) if beside else None,
        "patterns": sorted(kit["patterns"]),
        "capacity": report.get("capacity"),
        "dropped": {n: r.get("reasons") for n, r in report["patterns"].items()
                    if not n.startswith("_") and not r.get("passed")},
        **({} if kit["patterns"] else {"reason": "no usable layouts: nothing was stored; the "
                                                  "reasons are per layout in 'dropped'"}),
    }
