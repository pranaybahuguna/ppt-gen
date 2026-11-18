"""Postgres and the store: the two things that make a conversation survive a restart.

These need `make up && make init` and skip themselves otherwise.
"""
from __future__ import annotations

import urllib.request

import pytest

from deck_agent.infra.storage import FileStore, Ref, sha256_file

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- the store

def test_objects_round_trip(needs_stack, tmp_path, sample_deck, scratch_keys):
    store = FileStore(needs_stack.store_root, needs_stack.store_name)
    scratch_keys.append("tests/round-trip.pptx")
    uri = store.put(sample_deck, "tests/round-trip.pptx")
    assert Ref.parse(uri).key == "tests/round-trip.pptx"
    assert store.exists(uri)

    back = store.get(uri, tmp_path / "back.pptx")
    assert back.read_bytes() == sample_deck.read_bytes(), "a deck must survive the round trip intact"
    assert sha256_file(back) == sha256_file(sample_deck)


def test_json_round_trips_without_mangling_unicode(needs_stack, scratch_keys):
    store = FileStore(needs_stack.store_root, needs_stack.store_name)
    scratch_keys.append("tests/unicode.json")
    payload = {"title": "Résultats du troisième trimestre", "n": 3, "nested": {"a": [1, 2]}}
    uri = store.put_json(payload, "tests/unicode.json")
    assert store.get_json(uri) == payload


def test_listing_is_by_prefix(needs_stack, scratch_keys):
    store = FileStore(needs_stack.store_root, needs_stack.store_name)
    scratch_keys += ["tests/listing/one.json", "tests/listing/two.json"]
    store.put_json({"a": 1}, "tests/listing/one.json")
    store.put_json({"a": 2}, "tests/listing/two.json")
    listed = store.list("tests/listing/")
    assert len(listed) >= 2
    assert all(Ref.parse(u).key.startswith("tests/listing/") for u in listed)


def test_a_link_opens_the_stored_file(needs_stack, scratch_keys):
    """Where a person gets at a deck: a file:// link to it on this machine."""
    store = FileStore(needs_stack.store_root, needs_stack.store_name)
    scratch_keys.append("tests/link.json")
    uri = store.put_json({"hello": "deck"}, "tests/link.json")
    assert urllib.request.urlopen(store.url(uri), timeout=20).read() == b'{\n "hello": "deck"\n}'


def test_a_missing_object_is_a_clear_failure(needs_stack):
    from deck_agent.errors import Boundary

    store = FileStore(needs_stack.store_root, needs_stack.store_name)
    assert not store.exists(f"local://{store.bucket}/tests/definitely-not-here.json")
    with pytest.raises((Boundary, Exception)):
        store.get_bytes(f"local://{store.bucket}/tests/definitely-not-here.json")


FACTS = {"facts": [{"id": "F1", "text": "The pilot answered 38% of tickets without a person."},
                       {"id": "F2", "text": "Median first response fell from 4 hours to 6 minutes."},
                       {"id": "F3", "text": "Agents edited one in five drafts before sending."}],
             "datasets": {}}


# --------------------------------------------------------------------------- the checkpointer

def test_a_conversation_survives_a_new_graph_and_a_new_pool(needs_stack, template, scratch_deck, scratch_keys):
    """The point of Postgres: the next turn may be answered by a different process."""
    from deck_agent import facade
    from deck_agent.llm.planner import StubAgentPlanner

    deck_id = scratch_deck("persist")
    planner = StubAgentPlanner()
    store = FileStore(needs_stack.store_root, needs_stack.store_name)
    template_uri = store.put(template, f"uploads/{sha256_file(template)}-template.pptx")
    scratch_keys.append(Ref.parse(template_uri).key)


    created = facade.create_deck(deck_id, "a deck", FACTS, template_uri,
                                 planner=planner, log=lambda *a, **k: None)
    assert created["ok"], created

    # Every facade call opens its own graph and its own Postgres pool - which is the test.
    state = facade.session(deck_id, planner=planner)
    assert state["exists"] is True
    assert state["deck"] == created["deck"]
    assert state["turn"] == 1

    edited = facade.edit_deck(deck_id, "make slide 1 less wordy", planner=planner,
                              log=lambda *a, **k: None)
    assert edited["ok"], edited
    assert edited["versions"] == 2, "the second turn did not see the first one's deck"

    after = facade.session(deck_id, planner=planner)
    assert after["turn"] == 2
    assert len(after["versions"]) == 2


def test_an_interrupt_can_be_resumed_by_a_later_call(needs_stack, template, scratch_deck, scratch_keys):
    """`interrupt()` is only worth anything if the answer may arrive much later."""
    from deck_agent import facade
    from deck_agent.llm.planner import StubAgentPlanner

    deck_id = scratch_deck("interrupt")
    planner = StubAgentPlanner()
    store = FileStore(needs_stack.store_root, needs_stack.store_name)
    template_uri = store.put(template, f"uploads/{sha256_file(template)}-template.pptx")
    scratch_keys.append(Ref.parse(template_uri).key)
    facade.create_deck(deck_id, "a deck", FACTS, template_uri,
                       planner=planner, log=lambda *a, **k: None)

    asked = facade.ask_deck(deck_id, "do the thing", planner=planner, log=lambda *a, **k: None)
    assert asked["question"]
    assert facade.session(deck_id, planner=planner)["next_nodes"] == ["ask"]

    resumed = facade.answer(deck_id, "translate it into French", planner=planner,
                            log=lambda *a, **k: None)
    assert resumed["ok"], resumed
    assert resumed["route"] == "translate"
