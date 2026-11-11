"""Routing: the rules, and when they should give up and let a model or a person decide.

The table includes the six example requests the engine's own backlog names as the acceptance
criteria for a router, so that this file answers that item directly.
"""
from __future__ import annotations

import pytest

from deck_agent.llm.planner import _slide_numbers, rules_route

HAS_DECK = {"has_current_deck": True}
NO_DECK: dict = {}


@pytest.mark.parametrize("request_text, context, expected", [
    # --- the backlog's six example requests -------------------------------------------
    ("write me a deck about the support bot pilot", NO_DECK, "create"),
    ("make slide 3 less wordy", HAS_DECK, "edit"),
    ("translate it into French", HAS_DECK, "translate"),
    ("a new deck in this style", NO_DECK, "create"),
    ("summarise this deck", HAS_DECK, "ask"),          # not something we do; ask rather than guess
    ("restyle this onto the new corporate template", HAS_DECK, "restyle"),
    # --- the rest ----------------------------------------------------------------------
    ("add a risks slide after slide 4", HAS_DECK, "edit"),
    ("delete the last slide", HAS_DECK, "edit"),
    ("shorten slide 7", HAS_DECK, "edit"),
    ("traduis les slides 2 et 5 en français", HAS_DECK, "translate"),
    ("", HAS_DECK, "ask"),
    ("", NO_DECK, "create"),
])
def test_routes(request_text, context, expected):
    assert rules_route(request_text, context)["route"] == expected


def test_no_deck_always_means_create():
    """With nothing to work on, an edit verb cannot be an edit."""
    assert rules_route("make slide 3 less wordy", NO_DECK)["route"] == "create"


def test_translate_wins_over_edit():
    """"translate slide 3 and fix the typo" names a slide, but it is still a translation."""
    answer = rules_route("translate slide 3 into German and fix the typo", HAS_DECK)
    assert answer["route"] == "translate"
    assert answer["target_lang"] == "de"
    assert answer["slides"] == [3]


def test_language_from_a_code_as_well_as_a_name():
    assert rules_route("translate into fr", HAS_DECK)["target_lang"] == "fr"
    assert rules_route("translate it to Spanish", HAS_DECK)["target_lang"] == "es"


def test_unsure_gives_a_low_confidence_and_a_question():
    answer = rules_route("do the thing", HAS_DECK)
    assert answer["route"] == "ask"
    assert answer["confidence"] < 0.6
    assert answer["question"]


def test_confident_when_it_should_be():
    assert rules_route("write me a deck about X", NO_DECK)["confidence"] >= 0.9
    assert rules_route("translate it into French", HAS_DECK)["confidence"] >= 0.9


@pytest.mark.parametrize("text, expected", [
    ("slide 3", [3]),
    ("slides 2 and 5", [2, 5]),
    ("slides 2-4", [2, 3, 4]),
    ("slides 2 to 4", [2, 3, 4]),
    ("the first slide", [1]),
    ("the last slide", [-1]),
    ("no numbers here", []),
])
def test_slide_numbers(text, expected):
    assert _slide_numbers(text) == expected


def test_the_router_node_asks_when_the_model_is_unsure(offline_settings):
    """A low-confidence answer must stop and ask, not proceed on a coin flip."""
    from deck_agent.nodes.router import route
    from deck_agent.state import new_state

    class Unsure:
        name = "unsure"

        def route(self, instruction, context):
            return {"route": "edit", "confidence": 0.2, "reason": "guessing"}

    state = new_state("t", "do something with this")
    state["deck_versions"] = ["s3://b/v001.pptx"]
    out = route(state, planner=Unsure())
    assert out["route"] == "ask"
    assert out["pending_question"]


def test_the_router_stops_asking_after_two_goes(offline_settings):
    """Asking forever is worse than admitting defeat."""
    from deck_agent.nodes.router import route
    from deck_agent.state import new_state, step

    state = new_state("t", "???")
    state["deck_versions"] = ["s3://b/v001.pptx"]
    state["trace"] = [step("ask"), step("ask")]
    out = route(state, planner=None)
    assert out["route"] == "decline"
    assert "twice" in (out["route_reason"] or "")


def test_a_model_is_only_consulted_when_the_rules_are_unsure(offline_settings):
    from deck_agent.nodes.router import route
    from deck_agent.state import new_state

    calls = []

    class Counting:
        name = "counting"

        def route(self, instruction, context):
            calls.append(instruction)
            return {"route": "edit", "confidence": 0.9, "reason": "model"}

    confident = new_state("t", "translate it into French")
    confident["deck_versions"] = ["s3://b/v001.pptx"]
    assert route(confident, planner=Counting())["route_by"] == "rules"
    assert calls == [], "a model was asked to confirm something the rules were sure about"

    unsure = new_state("t", "do the thing with this deck")
    unsure["deck_versions"] = ["s3://b/v001.pptx"]
    assert route(unsure, planner=Counting())["route_by"] == "model"
    assert len(calls) == 1


# --------------------------------------------------------------------------- restyle: one slide vs the deck

@pytest.mark.parametrize("request_text, slides", [
    ("restyle slide 3", [3]),
    ("restyle slide 3 to look like slide 5", [3]),
    ("restyle slides 2-4", [2, 3, 4]),
    ("redesign slide 4, it looks dated", [4]),
])
def test_restyling_a_slide_is_an_edit(request_text, slides):
    """One slide looking different, inside the template it already uses, is an edit.

    It used to be treated as moving the whole deck onto another template and declined, with
    the slide number thrown away. The edit route can already do it: change_layout, set_style.
    """
    answer = rules_route(request_text, HAS_DECK)
    if "redesign" in request_text:
        # not a restyle word; it reaches edit through the slide number. Either way: edit.
        assert answer["route"] == "edit"
        return
    assert answer["route"] == "edit"
    assert answer["slides"] == slides
    assert answer["confidence"] >= 0.6, "a named slide is a confident edit"


def test_restyling_this_slide_without_a_number_is_unsure():
    """"This slide" names no slide, so the rules should not guess which one."""
    answer = rules_route("restyle this slide", HAS_DECK)
    assert answer["route"] == "edit"
    assert answer["confidence"] < 0.6, "below the floor, so a model or the person decides"


@pytest.mark.parametrize("request_text", [
    "restyle the deck onto the new corporate template",
    "restyle this onto our brand",
    "put this into the new corporate template",
    "restyle slides 2-4 onto the new template",
])
def test_moving_onto_another_template_is_still_a_restyle(request_text):
    """The deck-level job is the one that genuinely is not built, and still says so."""
    assert rules_route(request_text, HAS_DECK)["route"] == "restyle"


def test_a_restyle_keeps_the_slide_numbers_it_was_given():
    answer = rules_route("restyle slides 2-4 onto the new template", HAS_DECK)
    assert answer["slides"] == [2, 3, 4]
