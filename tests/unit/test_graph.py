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
    for node in ("intake", "route", "ask", "decline", "gather", "storyline", "approve_outline",
                 "write", "build", "split", "work", "join", "review", "translate_plan",
                 "translate_apply", "translate_repair", "look", "deliver"):
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
    assert "storyline" in state["trace"] and "build" in state["trace"]


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


def test_every_run_leaves_a_manifest_that_explains_it(offline_settings, uploaded_template, planner):
    deck_id = "test-manifest"
    created = facade.create_deck(deck_id, "a deck", CONTENT, uploaded_template, planner=planner,
                                 log=lambda *a, **k: None, outline=OUTLINE)
    manifest = store().get_json(created["manifest"])
    assert manifest["route"] == "create"
    assert manifest["engine"]["commit"], "a delivered deck must say which engine built it"
    assert manifest["template"] and manifest["template_id"]
    assert [entry["node"] for entry in manifest["trace"]][:4] == ["intake", "route", "gather", "storyline"]
    assert manifest["settings"]["render_backend"] == "off"


def test_a_requests_intermediate_versions_are_dropped_so_undo_goes_back_one_request(
        offline_settings, uploaded_template, planner):
    """An edit whose review sent it round again publishes two versions; only the last is kept,
    so undo lands on the deck as it was before the request - not on its first pass."""
    deck_id = "test-one-version-per-request"
    quiet = lambda *a, **k: None
    created = facade.create_deck(deck_id, "a deck", CONTENT, uploaded_template, planner=planner, log=quiet)
    first_pass = store().put_bytes(b"pass", f"{deck_id}/v002.pptx")
    final = store().put_bytes(b"final", f"{deck_id}/v003.pptx")
    with facade.deck_graph(planner, quiet) as graph:     # as if a two-pass edit had just finished
        graph.update_state(facade._config(deck_id),
                           {"deck_versions": [created["deck"], first_pass, final], "turn_base": 1})
    out = facade._keep_recent_versions(deck_id, {"deck": final}, offline_settings)
    assert out["versions_deleted"] == 1
    assert facade.session(deck_id, planner=planner)["versions"] == [created["deck"], final]
    assert not store().exists(first_pass)
    assert facade.undo(deck_id, planner=planner)["deck"] == created["deck"]


def test_attaching_to_an_existing_deck_makes_the_file_the_deck(offline_settings, uploaded_template,
                                                                sample_deck, planner):
    """Attaching is how a hand-edited copy comes back: it becomes the newest version and the
    instruction works on it; undo can still go back past it."""
    from pptx import Presentation

    deck_id = "test-attach-existing"
    quiet = lambda *a, **k: None
    created = facade.create_deck(deck_id, "a deck", CONTENT, uploaded_template, planner=planner, log=quiet)
    attached_uri = store().put(sample_deck, "uploads/hand-edited.pptx")
    result = facade.attach_deck(deck_id, attached_uri, "make slide 2 less wordy", planner=planner, log=quiet)
    assert result["ok"], result
    versions = facade.session(deck_id, planner=planner)["versions"]
    assert len(versions) == 3 and versions[0] == created["deck"]
    local = store().get(versions[-1], offline_settings.workspace_root / "after.pptx")
    assert len(Presentation(str(local)).slides) == len(Presentation(str(sample_deck)).slides), \
        "the edit ran on the attached deck, not the one the session had"
    assert facade.undo(deck_id, planner=planner)["deck"] == versions[1]      # the attached file
    assert facade.undo(deck_id, planner=planner)["deck"] == created["deck"]
