"""Changing a slide's layout without changing a word of it.

The apply step itself needs a deck, a kit and a model, so it is exercised end to end by hand
and in `tests/integration`. What is pinned here is the part that has a right answer: recovering
a built slide's content from the plan, finding it again by title, and the reducer that stopped
one turn's errors leaking into the next.
"""
from __future__ import annotations

import pytest

from deck_agent.engine.edits import compile_edit, content_from_plan_slide, match_plan_slide
from deck_agent.errors import PlanInvalid
from deck_agent.state import CLEAR, accumulate

PLAN = {
    "title": "Pilot", "footer": "Internal",
    "slides": [
        {"pattern": "cover", "fields": {"title": "Support bot pilot", "subtitle": "Eight weeks"},
         "items": [{"value": "38%", "label": "answered by the bot"}]},
        {"pattern": "panels", "fields": {"title": "Protect what must not change",
                                         "intro": "Three guarantees.", "takeaway": "Trust."},
         "items": [{"heading": "Untouched slides", "body": "Stay byte-identical."},
                   {"heading": "Stale edits", "body": "The whole batch is refused."},
                   {"heading": "Baseline", "body": "Compared with the template."}]},
        {"pattern": "chart", "fields": {"title": "Weekly tickets"},
         "charts": {"main": {"type": "column", "dataset": "weekly"}}},
        {"pattern": "text", "fields": {"title": "A prose slide",
                                       "body": "One paragraph.", "body_2": "And another."}},
    ],
}


def test_the_words_come_back_out_unchanged():
    content = content_from_plan_slide(PLAN["slides"][1])
    assert content["title"] == "Protect what must not change"
    assert content["intro"] == "Three guarantees."
    assert content["takeaway"] == "Trust."
    assert [p["heading"] for p in content["points"]] == \
        ["Untouched slides", "Stale edits", "Baseline"]
    assert content["points"][0]["text"] == "Stay byte-identical."


def test_a_figure_keeps_its_place():
    content = content_from_plan_slide(PLAN["slides"][0])
    assert content["points"][0]["value"] == "38%"
    assert content["points"][0]["heading"] == "answered by the bot"


def test_prose_in_long_fields_is_content_too():
    """A layout with no repeated items keeps its text in fields; it must not be lost."""
    content = content_from_plan_slide(PLAN["slides"][3])
    assert [p["text"] for p in content["points"]] == ["One paragraph.", "And another."]


def test_a_chart_survives_the_round_trip():
    content = content_from_plan_slide(PLAN["slides"][2])
    assert content["charts"]["main"]["dataset"] == "weekly"
    assert content["kind"] == "chart"


@pytest.mark.parametrize("slide, kind", [
    (PLAN["slides"][0], "numbers"), (PLAN["slides"][2], "chart"), (PLAN["slides"][3], "text"),
])
def test_the_kind_hint_comes_from_what_the_slide_holds(slide, kind):
    assert content_from_plan_slide(slide)["kind"] == kind


def test_a_slide_is_found_by_title_not_by_position():
    """Position cannot be used: a slide drawn by code shifts everything after it."""
    i, slide = match_plan_slide(PLAN, "Protect what must not change")
    assert i == 1 and slide["pattern"] == "panels"


def test_matching_ignores_spacing_and_case():
    i, _ = match_plan_slide(PLAN, "  PROTECT   what must not change ")
    assert i == 1


def test_a_slide_with_no_record_is_reported_not_guessed():
    i, slide = match_plan_slide(PLAN, "A slide that was drawn by generated code")
    assert i is None and slide is None


def test_change_layout_compiles(sample_deck):
    from deck_agent.engine import deck

    index = deck.probe(sample_deck)
    ops, inserts, visual, problems = compile_edit(
        [{"action": "change_layout", "slide": 2, "why": "repetitive"}], index)
    assert ops == [] and inserts == []
    assert visual["relayouts"] == [{"slide": 2, "why": "repetitive"}]
    assert problems == []


def test_change_layout_on_a_slide_that_does_not_exist_is_refused(sample_deck):
    from deck_agent.engine import deck

    with pytest.raises(PlanInvalid) as caught:
        compile_edit([{"action": "change_layout", "slide": 99}], deck.probe(sample_deck))
    assert "no slide 99" in caught.value.detail[0]


# --------------------------------------------------------------------------- the reducer

def test_errors_accumulate_within_a_turn():
    assert accumulate([{"a": 1}], [{"b": 2}]) == [{"a": 1}, {"b": 2}]


def test_errors_can_be_emptied_at_the_start_of_a_turn():
    """`operator.add` cannot do this, and a deck reported its first failure on every later turn."""
    assert accumulate([{"a": 1}, {"b": 2}], CLEAR) == []


def test_a_turn_clears_the_last_turn_s_errors():
    from deck_agent.state import turn_reset

    assert turn_reset("do something", "run-2", 2)["errors"] == CLEAR


# --------------------------------------------------------------------------- insert guards

def test_after_zero_means_before_slide_one(sample_deck):
    """`a.get("after") or len(slides)` turned 0 into "at the end", and a title slide
    requested for the front of the deck was appended to the back."""
    from deck_agent.engine import deck

    index = deck.probe(sample_deck)
    _, inserts, _, _ = compile_edit(
        [{"action": "insert_slide", "after": 0, "content": {"title": "Title"}}], index)
    assert inserts[0]["after"] == 0


def test_a_missing_after_still_means_the_end(sample_deck):
    from deck_agent.engine import deck

    index = deck.probe(sample_deck)
    _, inserts, _, _ = compile_edit(
        [{"action": "insert_slide", "content": {"title": "Title"}}], index)
    assert inserts[0]["after"] == len(index["slides"])


def test_after_is_clamped_to_the_deck(sample_deck):
    from deck_agent.engine import deck

    index = deck.probe(sample_deck)
    _, inserts, _, _ = compile_edit(
        [{"action": "insert_slide", "after": 99, "content": {"title": "T"}},
         {"action": "insert_slide", "after": -5, "content": {"title": "U"}}], index)
    assert [i["after"] for i in inserts] == [len(index["slides"]), 0]


def test_a_decline_is_honoured_before_a_route_s_own_decision(offline_settings):
    """An edit that applied nothing fell through to the create route's decision and was
    delivered as though it had worked."""
    from deck_agent.graph import after_look

    assert after_look({"route": "decline", "findings": []}) == "decline"
    assert after_look({"route": "ask", "findings": []}) == "ask"


# --------------------------------------------------------------------------- rebuild from plan

def test_a_deck_built_here_and_unchanged_can_be_rebuilt():
    from deck_agent.nodes.edit import _can_rebuild

    ok, why = _can_rebuild({"deck_plan": "s3://b/plan.json", "kit": "s3://b/kit.json",
                            "template": "s3://b/t.pptx", "plan_divergence": []})
    assert ok and why == ""


@pytest.mark.parametrize("missing", ["deck_plan", "kit", "template"])
def test_a_deck_we_did_not_build_cannot_be_rebuilt(missing):
    from deck_agent.nodes.edit import _can_rebuild

    state = {"deck_plan": "s3://b/p.json", "kit": "s3://b/k.json", "template": "s3://b/t.pptx",
             "plan_divergence": []}
    state.pop(missing)
    ok, why = _can_rebuild(state)
    assert not ok
    assert "no plan to rebuild it from" in why


def test_changes_the_plan_does_not_describe_block_a_rebuild():
    """Rebuilding would silently throw them away, so it says what it would lose."""
    from deck_agent.nodes.edit import _can_rebuild

    ok, why = _can_rebuild({"deck_plan": "p", "kit": "k", "template": "t",
                            "plan_divergence": ["3 text edit(s) made directly on the deck"]})
    assert not ok
    assert "text edit" in why and "lose" in why


def test_a_generated_slide_shifts_the_plan_positions_after_it():
    """A slide drawn by code sits in the deck but not in the plan."""
    from deck_agent.nodes.edit import _plan_position

    plan = {"slides": [{"pattern": "a"}, {"pattern": "b"}, {"pattern": "c"}, {"pattern": "d"}]}
    state = {"code_slides": [{"slide": 2}]}          # the deck's slide 2 was drawn by code
    assert _plan_position(plan, state, 1) == 0       # before it: unshifted
    assert _plan_position(plan, state, 2) is None    # it *is* the generated slide
    assert _plan_position(plan, state, 3) == 1       # after it: shifted by one
    assert _plan_position(plan, state, 5) == 3


def test_positions_are_unshifted_when_nothing_was_generated():
    from deck_agent.nodes.edit import _plan_position

    plan = {"slides": [{"pattern": "a"}, {"pattern": "b"}]}
    assert _plan_position(plan, {}, 1) == 0
    assert _plan_position(plan, {}, 2) == 1
    assert _plan_position(plan, {}, 3) is None


def test_a_code_slide_with_no_stored_source_is_reported_not_skipped(offline_settings, tmp_path):
    """Replay is only possible because the code and its context were kept."""
    from deck_agent.infra.workspace import open_workspace
    from deck_agent.nodes.edit import _replay_code_slides

    with open_workspace("t", "r", offline_settings) as ws:
        deck, replayed, problems = _replay_code_slides(
            ws, tmp_path / "x.pptx", [{"slide": 3, "explanation": "..."}], None,
            lambda *a, **k: None)
    assert replayed == []
    assert "its source was not kept" in problems[0]
