"""ensure_kit - make sure we have a tested style kit for this template, and only build it once.

Building a kit is the most expensive thing this system does: it drafts a pattern per slide,
builds test decks, and measures how many items each layout really holds. Doing that on every
request would be absurd, and doing it per conversation nearly as bad - a company has a handful
of templates and everyone uses them.

So the cache key is the template's **content hash**, and the cache is the bucket, shared by
every thread: `kits/<hash>.json`. A new template is built once, by whoever asks first; the same
template byte-for-byte is never built twice. A changed template has a different hash and so
gets a new kit, with no invalidation logic to get wrong.

With `single_kit` on, `kits/` holds exactly one kit: the template last ingested with `deckops
kit`, which replaces whatever was there. Nothing else writes to `kits/` then. A deck made from
any other template still gets a kit - it cannot be built without one - but the kit is kept
beside that deck (`<deck>/kit-<hash>.json`) and goes when the deck is purged.
"""
from __future__ import annotations

from ..engine.deck import probe
from ..engine.kitbuild import build_kit
from ..engine.normalise import prepare
from ..infra.storage import store
from ..settings import settings
from ..state import DeckState, step
from .common import template_fingerprint, workspace


def kit_key(template_id: str) -> str:
    """Where a template's kit lives in the bucket: `kits/<content hash>.json`.

    Called by: ensure_kit, cmd_kit.
    """
    return f"kits/{template_id}.json"


def ensure_kit(state: DeckState, planner=None, log=print) -> dict:
    """Make sure the session has a tested kit for its template: reuse the cached one, or build it
    once.

    Graph node. Runs: on the create route, after gather.
    Reads: template (or settings.default_template), template_id, kit.
    Writes: kit, template, template_id, kit_patterns; route='ask' when there is no template;
        route='decline' with errors when the template yields no usable layouts.
    Next: graph.after_ensure_kit -> outline | ask | decline.
    Called by: LangGraph, registered in graph.build_graph.
    """
    s = settings()
    st = store()

    template = state.get("template") or s.default_template or None
    if not template:
        return {
            "pending_question": "Which template should the deck follow? Attach the .pptx and I "
                                "will learn its look.",
            "route": "ask",
            "trace": [step("ensure_kit", outcome="no template")],
        }

    with workspace(state) as ws:
        raw = ws.fetch(template, "raw.pptx")

        # Prepare the template before anything reads an id off it. A real template can carry
        # duplicate shape ids - add-ins leave them behind - and the engine refuses those slides,
        # correctly, because its addressing cannot tell two shapes with one id apart. The
        # prepared copy becomes *the* template: the kit addresses its ids, so decks must be
        # built from the same file, and its hash is the cache key.
        local = ws.path("template.pptx")
        prepared = prepare(raw, local)
        if prepared["changed"]:
            log(f"  template: made {prepared['ids_renumbered']} nested shape id(s) unique on "
                f"slides {prepared['slides_touched']}; the engine could not have used them")
            template = ws.publish("template.pptx", versioned=True)
        template_id = template_fingerprint(local)
        cached = f"s3://{st.bucket}/{kit_key(template_id)}"

        if (state.get("kit") and state.get("template_id") == template_id
                and st.exists(state["kit"])):
            kit = st.get_json(state["kit"])
            return {"kit_patterns": sorted(kit["patterns"]),
                    "trace": [step("ensure_kit", outcome="already in state")]}

        if st.exists(cached):
            kit = st.get_json(cached)
            log(f"  kit: reusing the cached kit for this template ({template_id})")
            return {"kit": cached, "template": template, "template_id": template_id,
                    "kit_patterns": sorted(kit["patterns"]),
                    "trace": [step("ensure_kit", outcome="cache hit", template_id=template_id)]}

        log(f"  kit: building one for this template ({template_id}); this happens once")
        from ..sandbox.render import render

        kit, report = build_kit(local, ws.dir("kit"), planner, render=render, log=log)
        if not kit["patterns"]:
            return {
                "errors": [{"kind": "no_patterns", "message":
                            "none of this template's slides could be turned into a reusable "
                            "layout. Templates built entirely from placeholders, or made of "
                            "grouped shapes and SmartArt, often do that.",
                            "detail": report.get("draft_notes")}],
                "route": "decline",
                "route_reason": "I could not learn anything reusable from this template.",
                "trace": [step("ensure_kit", outcome="no patterns")],
            }
        if s.single_kit:
            kit_uri = ws.publish_json(kit, f"kit-{template_id}.json", versioned=True)
            log("  kit: kept with this deck - single_kit is on, and only `deckops kit` "
                "replaces the shared one")
        else:
            kit_uri = st.put_json(kit, kit_key(template_id))
        ws.publish_json(report, "kit_report.json")
        return {
            "kit": kit_uri, "template": template, "template_id": template_id,
            "kit_patterns": sorted(kit["patterns"]),
            "trace": [step("ensure_kit", outcome="built", template_id=template_id,
                           patterns=sorted(kit["patterns"]))],
        }
