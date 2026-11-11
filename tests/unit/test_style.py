"""Visual changes: colour, weight, size - and the two things that must stay true.

The engine has no operation for "make this red", so these changes are a deterministic pass of
their own. That means two properties have to be tested here rather than inherited: that a
change reaches only what it was aimed at, and that everything it did not touch keeps its
original bytes.
"""
from __future__ import annotations

import zipfile

import pytest

from deck_agent.engine import deck
from deck_agent.engine.edits import compile_edit, deck_view
from deck_agent.engine.style import (
    StyleChange,
    apply_styles,
    apply_theme_colours,
    normalise_colour,
)
from deck_agent.errors import PlanInvalid


@pytest.mark.parametrize("given, expected", [
    ("#8B0000", "8B0000"),
    ("8b0000", "8B0000"),
    ("dark red", "8B0000"),
    ("Dark Red", "8B0000"),
    ("grey", "6B7280"),
    ("gray", "6B7280"),
])
def test_colours_a_person_would_type(given, expected):
    assert normalise_colour(given) == expected


@pytest.mark.parametrize("given", ["puce", "#12345", "rgb(1,2,3)", "", None, "verdigris"])
def test_anything_else_is_refused_rather_than_guessed(given):
    assert normalise_colour(given) is None


def test_a_colour_change_reaches_only_its_shape(sample_deck, tmp_path):
    index = deck.probe(sample_deck)
    ref = deck_view(index)[0]["boxes"][0]["ref"]
    out = tmp_path / "styled.pptx"

    report = apply_styles(sample_deck, [StyleChange(scope="shape", ref=ref,
                                                    text_colour="8B0000")], out)
    assert report["applied"][0]["runs_touched"] >= 1

    # only slide 1's XML may differ
    before, after = zipfile.ZipFile(sample_deck), zipfile.ZipFile(out)
    others = [n for n in before.namelist()
              if n.startswith("ppt/slides/slide") and n.endswith(".xml")
              and n != "ppt/slides/slide1.xml"]
    assert others
    for name in others:
        assert before.read(name) == after.read(name), f"{name} changed and should not have"
    assert "8B0000" in after.read("ppt/slides/slide1.xml").decode(errors="replace").upper()


def test_a_slide_scoped_change_touches_that_slide_only(sample_deck, tmp_path):
    out = tmp_path / "styled.pptx"
    report = apply_styles(sample_deck, [StyleChange(scope="slide", slide=2, bold=True)], out)
    assert report["applied"][0]["runs_touched"] >= 1

    before, after = zipfile.ZipFile(sample_deck), zipfile.ZipFile(out)
    assert before.read("ppt/slides/slide1.xml") == after.read("ppt/slides/slide1.xml")
    assert before.read("ppt/slides/slide2.xml") != after.read("ppt/slides/slide2.xml")


def test_a_change_that_matches_nothing_says_so(sample_deck, tmp_path):
    report = apply_styles(sample_deck,
                          [StyleChange(scope="shape", ref="slide:9999/shape:1",
                                       text_colour="000000")],
                          tmp_path / "out.pptx")
    assert report["applied"][0]["runs_touched"] == 0
    assert "nothing on the deck matched" in report["applied"][0]["note"]


def test_a_change_that_says_nothing_is_refused(sample_deck, tmp_path):
    with pytest.raises(PlanInvalid):
        apply_styles(sample_deck, [StyleChange(scope="deck")], tmp_path / "out.pptx")


def test_a_theme_colour_change_rewrites_the_scheme(sample_deck, tmp_path):
    out = tmp_path / "themed.pptx"
    report = apply_theme_colours(sample_deck, {"accent1": "008000"}, out)

    assert report["theme"]["accent1"] == "008000"
    assert report["replaced"]["accent1"] != "008000"
    assert "shapes_pinned_to_the_old_colour" in report, \
        "a caller needs to know how much of the deck did not follow"

    themes = [n for n in zipfile.ZipFile(out).namelist() if n.startswith("ppt/theme/")]
    assert themes
    assert "008000" in zipfile.ZipFile(out).read(themes[0]).decode(errors="replace").upper()
    # the slides themselves are untouched: they inherit
    before, after = zipfile.ZipFile(sample_deck), zipfile.ZipFile(out)
    for name in [n for n in before.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]:
        assert before.read(name) == after.read(name), f"{name} should inherit, not be rewritten"


def test_an_unknown_theme_slot_is_refused(sample_deck, tmp_path):
    with pytest.raises(PlanInvalid) as caught:
        apply_theme_colours(sample_deck, {"accent9": "008000"}, tmp_path / "out.pptx")
    assert "accent1" in str(caught.value), "the message should say what is allowed"


# --------------------------------------------------------------------------- the compiler

def test_a_style_action_compiles(sample_deck):
    index = deck.probe(sample_deck)
    ref = deck_view(index)[0]["boxes"][0]["ref"]
    ops, inserts, visual, problems = compile_edit(
        [{"action": "set_style", "scope": "shape", "ref": ref, "text_colour": "dark red"}], index)
    assert ops == [] and inserts == [] and problems == []
    assert visual["styles"][0].text_colour == "8B0000"


def test_a_theme_action_compiles(sample_deck):
    index = deck.probe(sample_deck)
    _, _, visual, problems = compile_edit(
        [{"action": "set_theme_colour", "slot": "accent1", "colour": "green"}], index)
    assert problems == []
    assert visual["theme"] == {"accent1": "107C41"}


def test_a_colour_nobody_can_resolve_is_reported_not_guessed(sample_deck):
    index = deck.probe(sample_deck)
    ref = deck_view(index)[0]["boxes"][0]["ref"]
    with pytest.raises(PlanInvalid) as caught:
        compile_edit([{"action": "set_style", "ref": ref, "text_colour": "puce"}], index)
    assert "puce" in caught.value.detail[0]


def test_a_style_action_aimed_at_nothing_is_reported(sample_deck):
    index = deck.probe(sample_deck)
    with pytest.raises(PlanInvalid) as caught:
        compile_edit([{"action": "set_style", "scope": "slide", "slide": 99,
                       "text_colour": "red"}], index)
    assert "no slide 99" in caught.value.detail[0]


def test_a_style_action_that_changes_nothing_is_reported(sample_deck):
    index = deck.probe(sample_deck)
    ref = deck_view(index)[0]["boxes"][0]["ref"]
    with pytest.raises(PlanInvalid) as caught:
        compile_edit([{"action": "set_style", "scope": "shape", "ref": ref}], index)
    assert "has to say what to change" in caught.value.detail[0]
