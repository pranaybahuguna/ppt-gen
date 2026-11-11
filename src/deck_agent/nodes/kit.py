"""ensure_kit - make sure we have a tested style kit for this template, and only build it once.

Building a kit is the most expensive thing this system does: it drafts a pattern per slide,
builds test decks, and measures how many items each layout really holds. Doing that on every
request would be absurd, and doing it per conversation nearly as bad - a company has a handful
of templates and everyone uses them.

So the cache key is the template's **content hash**, and the cache is the bucket, shared by
every thread: `kits/<hash>.json`. A new template is built once, by whoever asks first; the same
template byte-for-byte is never built twice. A changed template has a different hash and so
gets a new kit, with no invalidation logic to get wrong.
"""
from __future__ import annotations

from ..engine.deck import probe
from ..engine.kitbuild import build_kit
from ..infra.storage import store
from ..settings import settings
from ..state import DeckState, step
from .common import template_fingerprint, workspace


def kit_key(template_id: str) -> str:
    return f"kits/{template_id}.json"


def ensure_kit(state: DeckState, planner=None, log=print) -> dict:
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
        local = ws.fetch(template, "template.pptx")
        template_id = state.get("template_id") or template_fingerprint(local)
        cached = f"s3://{st.bucket}/{kit_key(template_id)}"

        if state.get("kit") and state.get("template_id") == template_id:
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
        kit_uri = st.put_json(kit, kit_key(template_id))
        ws.publish_json(report, "kit_report.json")
        return {
            "kit": kit_uri, "template": template, "template_id": template_id,
            "kit_patterns": sorted(kit["patterns"]),
            "trace": [step("ensure_kit", outcome="built", template_id=template_id,
                           patterns=sorted(kit["patterns"]))],
        }


def after_kit(state: DeckState) -> str:
    if state.get("route") in ("ask", "decline"):
        return "ask" if state["route"] == "ask" else "decline"
    return "outline"
