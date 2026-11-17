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
    the profile is also written there as JSON. `planner` is accepted for compatibility and unused.

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
    return {"ok": True, "template_id": template_id, "template": uri,
            "default_template": bool(make_default), "slides": len(Presentation(str(source)).slides),
            "profile": {k: profile[k] for k in ("layout", "blank_layout", "font", "body_pt", "colours")}}
