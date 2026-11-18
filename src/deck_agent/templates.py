"""Ingesting a template: check the file, read its design profile, store it, maybe make it the default.

There is no kit any more. Slides are drawn from pieces in the template's own look, which is read
from the file in seconds (design.profile): the layout its slides are built on, where the title
sits, the safe area, its real brand colours and its fonts. Ingesting a template up front just
checks it and shows you what will be used; a create request can also name any template directly.

One function, used by the command line (`deckops template`) and the server (`POST /templates`,
the `ingest_template` tool), so both do exactly the same thing.
"""
from __future__ import annotations

import json
from pathlib import Path

from .infra.storage import sha256_file, store
from .settings import REPO, settings


def ingest(source: str | Path, out: str | Path | None = None, make_default: bool = False,
           planner=None, log=print) -> dict:
    """Ingest a template. -> {ok, template_id, template, default_template, profile, slides}

    Stores the file in `uploads/<hash>-<name>.pptx`. With make_default, it becomes
    `default_template`, both in config/agent.json and in this process's settings. With `out`,
    the profile is also written there as JSON.

    A template with guideline slides (a colour palette, a style guide, graphical elements) also
    gets its guide read (design.guide.build_guide: the palette by code, the writing rules with
    one call to `planner`, made from the settings when none is given) and stored beside it as
    `<template>.guide.json`; every deck made on it then uses it.

    Called by: cli.cmd_template; server.ingest_template and the server's POST /templates.
    """
    from pptx import Presentation

    from .design.profile import extract_profile
    from .engine import guard_deck

    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"no such template: {source}")
    guard_deck(source)                                 # untrusted input: check the archive first
    try:
        profile = extract_profile(source)
    except StopIteration:
        return {"ok": False, "reason": "no slide layout in this file has a title box, so there is "
                                       "nowhere for a slide title to go"}
    template_id = sha256_file(source)
    uri = store().put(source, f"uploads/{template_id}-{source.name.replace(' ', '-')}")
    from .design.guide import _guide_bytes, apply_guide, build_guide, guideline_slides

    guide = None
    if guideline_slides(Presentation(str(source))):
        if planner is None:
            from .llm.planner import planner as make_planner

            planner = make_planner()
        guide = build_guide(source, planner, log)
        store().put_bytes(json.dumps(guide, indent=1, ensure_ascii=False).encode(), _guide_key(uri))
        _guide_bytes.cache_clear()
        profile = apply_guide(profile, guide)
    if make_default:
        cfg_path = REPO / "config" / "agent.json"
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        cfg["default_template"] = uri
        cfg_path.write_text(json.dumps(cfg, indent=1) + "\n")
        settings().default_template = uri              # this process too, not only the next
    if out:
        Path(out).write_text(json.dumps(profile, indent=1))
    log(f"  template: {uri}  layout={profile['layout']!r}  primary={profile['colours']['primary']} "
        f"accent={profile['colours']['accent']}  font={profile['font']}")
    out = {"ok": True, "template_id": template_id, "template": uri,
           "default_template": bool(make_default), "slides": len(Presentation(str(source)).slides),
           "profile": {k: profile[k] for k in ("layout", "blank_layout", "font", "body_pt", "colours")}}
    if guide:
        out["guide"] = {"guideline_slides": guide["slides"], "palette": guide["palette"],
                        "rules": guide["rules"], "checks": guide["checks"],
                        "graphical_elements": len(guide["graphics"]), "stored": uri + ".guide.json"}
    return out


def _guide_key(uri: str) -> str:
    """The object key of a template's guide: the template's own key plus ".guide.json"."""
    from .infra.storage import Ref

    ref = Ref.parse(uri)
    return ref.key + ".guide.json"
