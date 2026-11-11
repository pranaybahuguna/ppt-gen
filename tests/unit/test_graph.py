"""The graph, end to end, with nothing running.

This is the test that proves the design claim: a whole conversation about a deck - create it,
edit it, translate it, ask a question, resume, undo - with no container, no Postgres, no object
storage service and no model. In-memory store, in-memory checkpointer, the offline stub
planner, rendering switched off. Everything else is the real code path.
"""
from __future__ import annotations

import pytest

from deck_agent import facade
from deck_agent.graph import build_graph, mermaid
from deck_agent.infra.storage import sha256_file, store

OUTLINE = {
    "title": "Support bot pilot",
    "footer": "Internal",
    "slides": [
        {"purpose": "open", "kind": "cover", "title": "Support bot pilot",
         "subtitle": "Eight weeks, two teams",
         "points": [{"heading": "answered by the bot", "value": "38%", "text": "of tickets"},
                    {"heading": "first response", "value": "3h", "text": "down from 11"}]},
        {"purpose": "sequence", "kind": "sequence", "title": "How we rolled it out",
         "points": [{"heading": "Shadow", "text": "The bot drafted replies nobody sent."},
                    {"heading": "Assist", "text": "Agents edited the drafts."},
                    {"heading": "Answer", "text": "The bot answered the clearest tickets."}]},
    ],
}
CONTENT = {"facts": [{"id": "F1", "text": "38% of tickets were answered by the bot"}], "datasets": {}}


@pytest.fixture
def uploaded_template(offline_settings, template):
    return store().put(template, f"uploads/{sha256_file(template)}-template.pptx")


def test_the_graph_compiles_and_every_edge_has_a_destination():
    compiled = build_graph().compile()
    graph = compiled.get_graph()
    names = {n for n in graph.nodes}
    for node in ("intake", "route", "ask", "decline", "ensure_kit", "outline", "render_slides",
                 "assemble", "repair", "translate_plan", "translate_apply", "translate_repair",
                 "edit_plan", "edit_apply", "custom_code", "deliver"):
        assert node in names, f"{node} is missing from the graph"
    assert "flowchart" in mermaid() or "graph TD" in mermaid()


def test_create_then_edit_then_translate_in_one_session(offline_settings, uploaded_template, planner):
    deck_id = "test-session"

    created = facade.create_deck(deck_id, "the support bot pilot", CONTENT, uploaded_template,
                                 planner=planner, log=lambda *a, **k: None, outline=OUTLINE)
    assert created["ok"], created
    assert created["route"] == "create"
    assert created["deck"].startswith("s3://")
    assert created["versions"] == 1
    assert created["warnings"] == []

    edited = facade.edit_deck(deck_id, "make slide 2 less wordy", planner=planner,
                              log=lambda *a, **k: None)
    assert edited["ok"], edited
    assert edited["route"] == "edit"
    assert edited["versions"] == 2, "an edit must add a version, not overwrite one"
    assert edited["deck"] != created["deck"]

    translated = facade.translate_deck(deck_id, "fr", [1], planner=planner,
                                       log=lambda *a, **k: None)
    assert translated["ok"], translated
    assert translated["route"] == "translate"
    assert translated["versions"] == 3


def test_the_session_knows_which_deck_is_current(offline_settings, uploaded_template, planner):
    deck_id = "test-current"
    facade.create_deck(deck_id, "a deck", CONTENT, uploaded_template, planner=planner,
                       log=lambda *a, **k: None, outline=OUTLINE)
    state = facade.session(deck_id, planner=planner)
    assert state["exists"] is True
    assert state["has_current_deck"] is True
    assert state["deck"] == state["versions"][-1]
    assert state["kit_patterns"], "the kit's patterns should be in the state for cheap decisions"
    assert "ensure_kit" in state["trace"]


def test_undo_drops_the_last_version_and_keeps_the_file(offline_settings, uploaded_template, planner):
    deck_id = "test-undo"
    facade.create_deck(deck_id, "a deck", CONTENT, uploaded_template, planner=planner,
                       log=lambda *a, **k: None, outline=OUTLINE)
    second = facade.edit_deck(deck_id, "make slide 2 less wordy", planner=planner,
                              log=lambda *a, **k: None)
    assert second["versions"] == 2

    undone = facade.undo(deck_id, planner=planner)
    assert undone["ok"] is True
    assert undone["versions"] == 1
    assert store().exists(undone["dropped"]), "undo must not destroy the file it dropped"

    assert facade.undo(deck_id, planner=planner)["ok"] is False, "nothing left to undo"


def test_a_vague_request_stops_and_asks_then_resumes(offline_settings, uploaded_template, planner):
    """The interrupt is real: the run stops, and the answer continues it from that node."""
    deck_id = "test-ask"
    facade.create_deck(deck_id, "a deck", CONTENT, uploaded_template, planner=planner,
                       log=lambda *a, **k: None, outline=OUTLINE)

    asked = facade.ask_deck(deck_id, "do the thing", planner=planner, log=lambda *a, **k: None)
    assert asked["ok"] is False
    assert asked["question"], "it should have asked something"

    state = facade.session(deck_id, planner=planner)
    assert state["next_nodes"] == ["ask"], "the run should be parked at the node that asked"

    resumed = facade.answer(deck_id, "translate it into French", planner=planner,
                            log=lambda *a, **k: None)
    assert resumed["ok"], resumed
    assert resumed["route"] == "translate"


def test_something_we_do_not_do_is_declined_with_a_reason(offline_settings, uploaded_template, planner):
    deck_id = "test-decline"
    facade.create_deck(deck_id, "a deck", CONTENT, uploaded_template, planner=planner,
                       log=lambda *a, **k: None, outline=OUTLINE)
    answer = facade.ask_deck(deck_id, "restyle this onto the new corporate template",
                             planner=planner, log=lambda *a, **k: None)
    assert answer["ok"] is False
    assert answer["route"] == "restyle"
    assert "not supported yet" in answer["reason"]
    assert "build a new deck" in answer["reason"], "a plain no should still offer the nearest thing"


def test_create_with_no_template_asks_for_one(offline_settings, planner):
    answer = facade.create_deck("test-no-template", "a deck about nothing", CONTENT, None,
                               planner=planner, log=lambda *a, **k: None, outline=OUTLINE)
    assert answer["ok"] is False
    assert "template" in (answer.get("question") or answer.get("reason") or "").lower()


def test_the_kit_is_built_once_and_then_reused(offline_settings, uploaded_template, planner):
    """The cache key is the template's content hash, shared across sessions."""
    builds = []
    import deck_agent.nodes.kit as kit_node

    original = kit_node.build_kit

    def counting(*args, **kwargs):
        builds.append(1)
        return original(*args, **kwargs)

    kit_node.build_kit = counting
    try:
        facade.create_deck("session-a", "a deck", CONTENT, uploaded_template, planner=planner,
                           log=lambda *a, **k: None, outline=OUTLINE)
        facade.create_deck("session-b", "another deck", CONTENT, uploaded_template,
                           planner=planner, log=lambda *a, **k: None, outline=OUTLINE)
    finally:
        kit_node.build_kit = original
    assert len(builds) == 1, "the second session should have reused the cached kit"


def test_every_run_leaves_a_manifest_that_explains_it(offline_settings, uploaded_template, planner):
    deck_id = "test-manifest"
    created = facade.create_deck(deck_id, "a deck", CONTENT, uploaded_template, planner=planner,
                                 log=lambda *a, **k: None, outline=OUTLINE)
    manifest = store().get_json(created["manifest"])
    assert manifest["route"] == "create"
    assert manifest["engine"]["commit"], "a delivered deck must say which engine built it"
    assert manifest["kit"] and manifest["template_id"]
    assert [entry["node"] for entry in manifest["trace"]][:3] == ["intake", "route", "ensure_kit"]
    assert manifest["settings"]["render_backend"] == "off"
