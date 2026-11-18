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
    ("restyle this onto the new corporate template", HAS_DECK, "edit"),   # in the deck's own template
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


def _with_deck(text):
    from deck_agent.state import new_state

    st = new_state("t", text)
    st["deck_versions"] = ["s3://b/v001.pptx"]
    return st


class _Model:
    name = "fake model"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def route(self, instruction, context):
        self.calls.append((instruction, context))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_the_model_routes_every_request_even_the_obvious_ones(offline_settings):
    from deck_agent.nodes.router import route

    model = _Model({"route": "translate", "target_lang": "fr", "confidence": 0.95, "reason": "language"})
    out = route(_with_deck("translate it into French"), planner=model)
    assert out["route"] == "translate" and out["route_by"] == "model" and out["target_lang"] == "fr"
    assert len(model.calls) == 1
    instruction, context = model.calls[0]
    assert context["has_current_deck"] and "messages" not in context, "a summary, never the transcript"


def test_phrasings_the_word_lists_miss_are_routed_by_the_model(offline_settings):
    from deck_agent.nodes.router import route
    from deck_agent.llm.planner import rules_route

    text = "tidy up the third one, it's a mess"
    assert rules_route(text, HAS_DECK)["route"] == "ask"         # the word lists cannot tell
    out = route(_with_deck(text), planner=_Model({"route": "edit", "slides": [3], "confidence": 0.85,
                                                   "reason": "the third slide"}))
    assert out["route"] == "edit" and out["slide_scope"] == [3]


def test_a_model_answer_is_checked_before_it_is_acted_on(offline_settings):
    from deck_agent.nodes.router import route
    from deck_agent.state import new_state

    # an unknown route becomes a question
    out = route(_with_deck("x"), planner=_Model({"route": "rebuild", "confidence": 0.99}))
    assert out["route"] == "ask" and out["pending_question"]
    # no deck in the session: nothing to edit, whatever the model says
    out = route(new_state("t", "shorten slide 2"), planner=_Model({"route": "edit", "confidence": 0.9}))
    assert out["route"] == "ask" and "deck" in out["pending_question"].lower()
    # a translation needs a language
    out = route(_with_deck("translate it"), planner=_Model({"route": "translate", "confidence": 0.9}))
    assert out["route"] == "ask" and "language" in out["pending_question"].lower()
    # junk slide numbers are dropped, a missing confidence is zero (so: ask)
    out = route(_with_deck("x"), planner=_Model({"route": "edit", "slides": ["two", 3, -1]}))
    assert out["route"] == "ask" and out["route_confidence"] == 0.0


def test_the_word_rules_stand_in_when_the_model_fails(offline_settings):
    from deck_agent.nodes.router import route

    out = route(_with_deck("translate it into French"), planner=_Model(TimeoutError("slow")))
    assert out["route"] == "translate" and out["route_by"] == "rules"
    assert "router model failed" in out["route_reason"]


def test_the_router_model_decides_the_palette(offline_settings):
    from deck_agent.nodes.router import route

    out = route(_with_deck("make it livelier"),
                planner=_Model({"route": "edit", "palette": "extended", "confidence": 0.8}))
    assert out["palette"] == "extended"
    out = route(_with_deck("fix the typo on slide 2"),
                planner=_Model({"route": "edit", "slides": [2], "palette": None, "confidence": 0.9}))
    assert "palette" not in out, "nothing said about colour: the deck keeps its palette"


# --------------------------------------------------------------------------- restyle is an edit

@pytest.mark.parametrize("request_text, slides", [
    ("restyle slide 3", [3]),
    ("restyle slide 3 to look like slide 5", [3]),
    ("restyle slides 2-4", [2, 3, 4]),
    ("redesign slide 4, it looks dated", [4]),
])
def test_restyling_a_slide_is_an_edit(request_text, slides):
    """A slide looking different, inside the template it already uses, is an edit (a redesign)."""
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
])
def test_restyling_the_whole_deck_is_an_edit_the_rules_are_unsure_about(request_text):
    """There is no route that moves a deck onto another template: a restyle is an edit in the
    deck's own template. With no slides named, the rules leave it to a model or the person."""
    answer = rules_route(request_text, HAS_DECK)
    assert answer["route"] == "edit" and answer["confidence"] < 0.6


def test_a_restyle_keeps_the_slide_numbers_it_was_given():
    answer = rules_route("restyle slides 2-4 onto the new template", HAS_DECK)
    assert answer["route"] == "edit" and answer["slides"] == [2, 3, 4]
