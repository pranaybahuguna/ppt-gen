"""deliver - hand over the deck, and be straight about what is wrong with it.

Three things are published here, and the third is the one that is easy to skip:

* the deck, as a URI and a time-limited link;
* the previews and the change list, so a person can see what happened without opening it;
* the **warnings**: every finding the checks still have, every bit of template content that
  leaked, every slide that could not be built. A deck handed over as though it were clean when
  it is not is worse than one handed over with two lines of "watch slide 4", because the first
  costs someone their credibility in a meeting.

The manifest is written to the bucket next to the deck, so a run can be explained later from
the artefacts alone: which engine commit, which kit, which route, which path through the
graph, what the model was asked and answered.
"""
from __future__ import annotations

from ..engine import engine_version
from ..infra.storage import store
from ..settings import settings
from ..state import DeckState, current_deck, step
from .common import warnings_of, workspace


def deliver(state: DeckState, log=print) -> dict:
    """Finish the turn: write manifest.json, make a download link, and return the result.

    Graph node. Runs: last on every successful route.
    Reads: deck_versions, previews, changelog, errors, findings and notes (as warnings), trace,
        route details, kit and template, code_slides, run and thread ids.
    Writes: delivered = {ok, deck, url, previews, warnings, changelog, route, versions,
        manifest}.
    Next: END.
    Called by: LangGraph, registered in graph.build_graph.
    """
    s = settings()
    st = store()
    deck = current_deck(state)
    warnings = warnings_of(state)
    errors = state.get("errors") or []

    manifest = {
        "thread": state.get("thread_id"),
        "run": state.get("run_id"),
        "turn": state.get("turn"),
        "route": state.get("route"),
        "route_reason": state.get("route_reason"),
        "route_by": state.get("route_by"),
        "route_confidence": state.get("route_confidence"),
        "engine": engine_version(),
        "kit": state.get("kit"),
        "template": state.get("template"),
        "template_id": state.get("template_id"),
        "deck": deck,
        "versions": state.get("deck_versions"),
        "previews": state.get("previews"),
        "slides": len((state.get("plan") or {}).get("slides") or []) or None,
        "changelog": state.get("changelog"),
        "warnings": warnings,
        "errors": errors,
        "code_slides": state.get("code_slides"),
        "repair_rounds": state.get("repair_round"),
        "trace": state.get("trace"),
        "settings": {k: v for k, v in s.redacted().items()
                     if k in ("render_backend", "code_runner", "checkpointer", "store_backend",
                              "llm_client", "allow_custom_code", "max_repair_rounds")},
    }
    manifest_uri = None
    if deck:
        with workspace(state) as ws:
            manifest_uri = ws.publish_json(manifest, "manifest.json")

    delivered = {
        "ok": bool(deck) and not errors,
        "deck": deck,
        "url": st.url(deck) if deck else None,
        "previews": state.get("previews") or [],
        "warnings": warnings,
        "errors": [{"kind": e.get("kind"), "message": e.get("message")} for e in errors],
        "changelog": state.get("changelog") or [],
        "route": state.get("route"),
        "manifest": manifest_uri,
        "versions": len(state.get("deck_versions") or []),
    }
    log(f"  delivered: {delivered['deck']}  warnings={len(warnings)} errors={len(errors)}")
    return {"delivered": delivered, "trace": [step("deliver", warnings=len(warnings),
                                                   errors=len(errors))]}
