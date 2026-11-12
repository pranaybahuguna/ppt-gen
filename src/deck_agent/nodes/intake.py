"""intake - make sense of what arrived, before anything decides anything.

Every file the caller supplies is checked here with the engine's archive guard and probed
once, so that from this point on the graph is reasoning about JSON rather than about bytes.
This is also where a template gets its content hash, which is the kit cache key.

The guard used to live inside the sandbox, because the sandbox was the trust boundary. In this
design it runs at the door instead: a zip bomb, an encrypted entry, a path-traversing name or
a file that is simply not a .pptx is rejected before any parser sees it, and the run stops with
a plain reason.
"""
from __future__ import annotations

from ..engine import guard_deck
from ..engine.deck import probe
from ..state import DeckState, step
from .common import new_run_id, template_fingerprint, workspace


def intake(state: DeckState) -> dict:
    """Start a turn: give it a run id, and sort any attached decks into the deck to work on and the
    template.

    Graph node. Runs: first, on every turn (START -> intake).
    Reads: attachments, run_id, source_deck, template, deck_versions.
    Writes: run_id; source_deck and template when decks were attached (each one safety-checked
        and probed first).
    Next: route.
    Called by: LangGraph, registered in graph.build_graph.
    """
    run_id = state.get("run_id") or new_run_id()
    out: dict = {"run_id": run_id, "trace": [step("intake", attachments=len(state.get("attachments") or []))]}
    attachments = state.get("attachments") or []
    if not attachments:
        return out

    with workspace({**state, "run_id": run_id}) as ws:
        decks: list[tuple[str, int]] = []
        for i, uri in enumerate(attachments):
            local = ws.fetch(uri, f"attached-{i}.pptx")
            guard_deck(local)                      # untrusted input: check the archive first
            index = probe(local)
            decks.append((uri, len(index["slides"])))

        # Which attachment is which? A template is the thing we are told is one, or - when two
        # decks arrive and one is already the deck being worked on - the other one.
        named_template = state.get("template")
        source = state.get("source_deck")
        for uri, n_slides in decks:
            if uri == named_template or uri == source:
                continue
            if named_template is None and (source or state.get("deck_versions")):
                named_template = uri
            elif source is None:
                source = uri
            else:
                named_template = named_template or uri

        if source:
            out["source_deck"] = source
        if named_template:
            out["template"] = named_template
            local = ws.fetch(named_template, "template.pptx")
            out["template_id"] = template_fingerprint(local)
        out["trace"] = [step("intake", decks=len(decks), source=bool(source),
                             template=bool(named_template))]
    return out
