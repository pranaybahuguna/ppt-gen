"""Building a deck from a kit, compiling edits, and the feedback that goes back to a model."""
from __future__ import annotations

import pytest

from deck_agent.engine import build, deck
from deck_agent.engine.edits import compile_edit, deck_view
from deck_agent.engine.fallback import relaxed_options
from deck_agent.errors import PlanInvalid


@pytest.fixture
def index(template):
    return deck.probe(template)


def test_a_deck_is_built_from_a_kit_and_comes_out_clean(template, hand_kit, index, tmp_path):
    plan = {"title": "Pilot results", "footer": "Internal", "slides": [
        {"pattern": "cover", "fields": {"title": "Support bot pilot", "subtitle": "November 2026"},
         "items": [{"value": "38%", "label": "answered by the bot"},
                   {"value": "3h", "label": "first response"}]},
        # `steps` in this kit wants at least three items - the kit says so, and the compiler
        # enforces it, which is the whole point of measuring rather than guessing.
        {"pattern": "steps", "fields": {"title": "How we rolled it out"},
         "items": [{"heading": "Shadow", "body": "The bot drafted replies nobody sent."},
                   {"heading": "Assist", "body": "Agents edited the drafts."},
                   {"heading": "Answer", "body": "The bot answered the clearest tickets."}]},
    ]}
    result = build.build_from_kit(template, index, hand_kit, plan, {}, tmp_path / "out.pptx",
                                  build.baseline_for_kit(template, hand_kit))
    assert result.clean, (result.findings, result.structural, result.leaked)
    assert len(result.out_index["slides"]) == 2
    assert result.summary()["parts_preserved"] > 0


def test_the_template_own_slides_do_not_survive_into_a_new_deck(template, hand_kit, index, tmp_path):
    """A new deck must not carry the template's example words. The engine calls this a leak."""
    plan = {"title": "X", "footer": "f", "slides": [
        {"pattern": "steps", "fields": {"title": "Only this title"},
         "items": [{"heading": "One", "body": "First thing."},
                   {"heading": "Two", "body": "Second thing."},
                   {"heading": "Three", "body": "Third thing."}]}]}
    result = build.build_from_kit(template, index, hand_kit, plan, {}, tmp_path / "out.pptx",
                                  build.baseline_for_kit(template, hand_kit))
    assert result.leaked == []


def test_an_unknown_pattern_is_a_plan_error_with_a_usable_message(template, hand_kit, index):
    with pytest.raises(PlanInvalid) as caught:
        build.compile_plan({"slides": [{"pattern": "nonexistent", "fields": {}}]},
                           index, hand_kit, {})
    assert any("unknown pattern" in m for m in caught.value.detail)
    assert any("cover" in m for m in caught.value.detail), "the message should list what exists"


def test_too_many_items_is_a_plan_error(template, hand_kit, index):
    with pytest.raises(PlanInvalid) as caught:
        build.compile_plan({"slides": [{"pattern": "stats", "fields": {"title": "t"},
                                        "items": [{"value": str(i)} for i in range(9)]}]},
                           index, hand_kit, {})
    assert any("items" in m for m in caught.value.detail)


def test_repair_feedback_gives_a_character_budget_not_a_percentage():
    """Text wraps by whole lines, so "10% shorter" does nothing. A model needs a length."""
    out_index = {"slides": [{"shapes": [
        {"ref": "slide:1/shape:2", "paras": [{"text": "x" * 200}]}]}]}
    findings = [{"kind": "overflow", "ref": "slide:1/shape:2", "slide_index": 0,
                 "needed_pt": 120.0, "available_pt": 40.0, "ratio": 3.0, "max_font_pt": 14.0}]
    messages = build.repair_feedback(findings, out_index)
    assert 0 in messages
    assert "at most" in messages[0][0]
    budget = int(messages[0][0].split("at most ")[1].split(" characters")[0])
    assert 8 <= budget < 200, "the budget must be shorter than the text that did not fit"


def test_overlap_feedback_says_something_actionable():
    findings = [{"kind": "overlap", "slide_index": 1, "shapes": [3, 4], "share": 0.4}]
    messages = build.repair_feedback(findings, {"slides": []})
    assert "overlap" in messages[1][0]


def test_font_fallback_is_deterministic_and_only_for_overflow():
    ops = build.font_fallback_ops([
        {"kind": "overflow", "ref": "slide:1/shape:2", "suggest_size_pt": 12.5},
        {"kind": "overlap", "shapes": [1, 2]},
        {"kind": "overflow", "ref": "slide:1/shape:3", "suggest_size_pt": None},
    ])
    assert ops == [{"op": "set_font_size", "target": "slide:1/shape:2", "size_pt": 12.5}]


# --------------------------------------------------------------------------- edits

def test_the_deck_view_shows_addresses_and_words_and_nothing_else(sample_deck):
    view = deck_view(deck.probe(sample_deck))
    assert len(view) == 7
    box = view[0]["boxes"][0]
    assert set(box) == {"ref", "paragraphs"}
    assert set(box["paragraphs"][0]) == {"ref", "text"}
    assert "geom" not in str(view), "the edit model must not be shown geometry"


def test_card_grids_are_flagged_so_a_model_knows_where_relayout_is_possible(sample_deck):
    view = deck_view(deck.probe(sample_deck))
    with_cards = [slide for slide in view if "cards" in slide]
    assert with_cards, "the sample deck has a numbered card grid"
    assert with_cards[0]["cards"][0]["n"] == 1


def test_set_text_carries_the_fingerprint_of_what_it_replaces(sample_deck):
    index = deck.probe(sample_deck)
    ref = deck_view(index)[0]["boxes"][0]["paragraphs"][0]["ref"]
    ops, inserts, _visual, problems = compile_edit([{"action": "set_text", "ref": ref, "text": "New"}], index)
    assert problems == [] and inserts == []
    assert ops[0]["expect"][deck.EXPECT_FIELD]


def test_a_text_change_that_changes_nothing_is_not_an_op(sample_deck):
    index = deck.probe(sample_deck)
    paragraph = deck_view(index)[0]["boxes"][0]["paragraphs"][0]
    ops, _, _visual, problems = compile_edit(
        [{"action": "set_text", "ref": paragraph["ref"], "text": paragraph["text"]}], index)
    assert ops == []
    assert problems == []


def test_an_invented_address_is_reported_not_applied(sample_deck):
    index = deck.probe(sample_deck)
    with pytest.raises(PlanInvalid) as caught:
        compile_edit([{"action": "set_text", "ref": "slide:999/shape:1/para:0", "text": "x"}], index)
    assert "no text at" in caught.value.detail[0]


def test_an_unknown_action_is_named_in_the_problem(sample_deck):
    index = deck.probe(sample_deck)
    ops, inserts, _visual, problems = compile_edit(
        [{"action": "set_text", "ref": deck_view(index)[0]["boxes"][0]["paragraphs"][0]["ref"],
          "text": "New"},
         {"action": "teleport_slide", "slide": 2}], index)
    assert ops, "the valid action should still be compiled"
    assert "teleport_slide" in problems[0]


def test_inserts_come_back_separately_because_they_need_a_second_pass(sample_deck):
    index = deck.probe(sample_deck)
    ops, inserts, _visual, _ = compile_edit(
        [{"action": "insert_slide", "after": 2, "content": {"title": "Risks"}}], index)
    assert ops == []
    assert inserts == [{"after": 2, "content": {"title": "Risks"}}]


def test_relayout_cards_compiles_against_a_real_card_grid(sample_deck):
    """"turn these cards into fewer" - the one structural edit the engine can do."""
    index = deck.probe(sample_deck)
    view = deck_view(index)
    slide = next(s for s in view if "cards" in s)
    existing = [c["n"] for c in slide["cards"]]
    assert len(existing) >= 2
    merged = [{"from": existing[:2], "heading": "Merged", "body": "Two cards became one."}]
    merged += [{"from": [n], "heading": f"Card {n}", "body": "unchanged"} for n in existing[2:]]
    # The grid on that slide has two rows, so the remaining cards have to fit in two rows:
    # the engine refuses a column count that would need a third. That refusal is the feature.
    ops, inserts, _visual, problems = compile_edit(
        [{"action": "relayout_cards", "slide": slide["slide"], "columns": 4, "cards": merged}], index)
    assert problems == []
    assert any(o["op"] == "set_geometry" for o in ops), "the grid should be re-laid out"
    assert any(o["op"] == "delete_shape" for o in ops), "the merged-away card should be removed"


def test_using_a_card_twice_is_refused(sample_deck):
    index = deck.probe(sample_deck)
    slide = next(s for s in deck_view(index) if "cards" in s)
    n = slide["cards"][0]["n"]
    _, _, _visual, problems = compile_edit(
        [{"action": "relayout_cards", "slide": slide["slide"], "columns": 1,
          "cards": [{"from": [n], "heading": "a", "body": "b"},
                    {"from": [n], "heading": "c", "body": "d"}]},
         {"action": "delete_slide", "slide": 1}], index)
    assert any("twice" in p for p in problems)


# --------------------------------------------------------------------------- no-fit fallback

def test_a_title_slide_with_no_points_gets_a_layout_rather_than_nothing(hand_kit):
    """The case that sent a plain cover slide to generated code until this existed.

    The hand-written kit shipped with the engine happens to allow a cover with zero items
    (`min: 0`), so it does not show the problem. The kit this project *builds automatically*
    from the same template measures that cover as needing two to four items, because that is
    what its example slide has - and then a title-and-subtitle slide fits nothing at all. The
    kit is adjusted here to reproduce exactly that, rather than relying on which kit a fixture
    happens to be.
    """
    from controlplane.slide_flow import options_for

    hand_kit["patterns"]["cover"]["items"]["min"] = 2
    cover = {"kind": "cover", "title": "Support bot pilot", "subtitle": "Eight weeks"}
    assert options_for(cover, hand_kit, 1, 5) == [], "the strict rules reject it, which is the premise"
    options, relaxed = relaxed_options(cover, hand_kit, 1, 5)
    assert options, "the fallback should find a layout from the template"
    assert relaxed and "position" in relaxed or "without repeated items" in relaxed


def test_the_fallback_refuses_to_drop_a_table_or_a_chart(hand_kit):
    """Relaxing into a prose layout would silently throw the data away."""
    table_slide = {"kind": "table", "title": "t",
                   "table": {"header": ["a"], "rows": [["x"]] * 40}}
    assert relaxed_options(table_slide, hand_kit, 2, 5) == ([], None)
    chart_slide = {"kind": "chart", "title": "t", "charts": {"a": {}, "b": {}, "c": {}}}
    assert relaxed_options(chart_slide, hand_kit, 2, 5) == ([], None)
