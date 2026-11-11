"""The two tidying passes - both of which exist because of something seen on a rendered slide.

Neither defect is a fitting problem, so neither the overflow checker nor the overlap checker
would ever have reported them. They were found by looking, which is why `tests/demo.py` and the
engine's own working notes both insist on it.
"""
from __future__ import annotations

import pytest

from deck_agent.engine import build, deck
from deck_agent.engine.tidy import CITATION, clean_plan, orphan_steps, strip_citations


# --------------------------------------------------------------------------- citations

@pytest.mark.parametrize("before, after", [
    ("The bot answered 38% of incoming tickets. [F2]", "The bot answered 38% of incoming tickets."),
    ("Median first response fell to 3 hours [F3]", "Median first response fell to 3 hours"),
    ("Two teams over eight weeks (F4, F5)", "Two teams over eight weeks"),
    ("Volume held steady [F1; F2]", "Volume held steady"),
])
def test_citation_markers_are_removed_from_visible_text(before, after):
    assert strip_citations(before) == after


@pytest.mark.parametrize("before, after", [
    ("Source: F14", ""),
    ("Sources: F1, F2", ""),
    ("Build test slides to establish capacity. Source: F14",
     "Build test slides to establish capacity."),
])
def test_a_citation_written_as_its_own_line_is_removed(before, after):
    """A deck put "Source: F14" on a slide as a bullet of its own. Found by looking at it."""
    assert strip_citations(before) == after


@pytest.mark.parametrize("text", [
    "Revenue grew (and margins held)",
    "Source: pilot report",
    "Sources of funding were reviewed",
    "Reference: internal audit",
    "The F1 season starts in March",
    "Teams [A and B] both improved",
    "See the appendix (page 4)",
])
def test_ordinary_prose_is_left_alone(text):
    assert strip_citations(text) == text


def test_the_fact_field_keeps_its_id_because_that_is_where_it_belongs():
    """The citation is data the plan should carry; it is only wrong on the slide."""
    plan = {"slides": [{"pattern": "stats", "fields": {"title": "What changed [F2]"},
                        "items": [{"value": "38%", "caption": "of tickets [F2]", "fact": "F2"}],
                        "sources": ["F2", "F3"]}]}
    cleaned, found = clean_plan(plan)
    assert found == 2
    assert cleaned["slides"][0]["fields"]["title"] == "What changed"
    assert cleaned["slides"][0]["items"][0]["caption"] == "of tickets"
    assert cleaned["slides"][0]["items"][0]["fact"] == "F2", "the citation itself must survive"
    assert cleaned["slides"][0]["sources"] == ["F2", "F3"]


def test_the_pattern_name_is_never_touched():
    cleaned, _ = clean_plan({"slides": [{"pattern": "chart", "fields": {}}]})
    assert cleaned["slides"][0]["pattern"] == "chart"


def test_the_regex_does_not_match_across_a_sentence():
    assert CITATION.findall("[F2] and then [F3]") == ["F2", "F3"]


# --------------------------------------------------------------------------- orphaned decoration

#: A *panel* is a shape with real width and real height. A shape that is long and thin - the
#: accent bar down the left edge, the hairline above the footer - has a large area but is not a
#: panel, and must never be removed. Measuring by area alone would catch both.
PANEL_MIN_EMU = 914_400          # one inch in each direction


def _is_panel(geom: dict) -> bool:
    return geom["w"] >= PANEL_MIN_EMU and geom["h"] >= PANEL_MIN_EMU

@pytest.fixture
def chart_pattern(hand_kit, template):
    """A pattern whose side text sits on a decorative panel, which is the case that matters."""
    index = deck.probe(template)
    for name, pattern in hand_kit["patterns"].items():
        if "charts" not in pattern:
            continue
        example = index["slides"][pattern["slide"] - 1]
        shapes = {int(s["ref"].rsplit("shape:", 1)[1]): s for s in example["shapes"]}
        # a panel: a text-capable shape with no words, big enough to sit behind a field
        panels = [sid for sid, s in shapes.items()
                  if not any(p["text"].strip() for p in s.get("paras", []))
                  and sid not in pattern["fields"].values()
                  and _is_panel(s["geom"])]
        if panels:
            return name, pattern, example, panels
    pytest.skip("this template has no chart pattern with a decorative panel")


def test_a_panel_whose_fields_are_all_empty_is_removed(chart_pattern):
    name, pattern, example, panels = chart_pattern
    filled = {pattern["fields"]["title"], pattern["fields"]["page"], pattern["fields"]["footer"]}
    steps = orphan_steps(pattern, example, filled)
    removed = {s["target"] for s in steps}
    assert any(f"shape:{sid}" in removed for sid in panels), \
        "the panel behind the empty side text should go with it"


def test_a_panel_whose_fields_are_used_is_kept(chart_pattern):
    name, pattern, example, panels = chart_pattern
    everything = set(pattern["fields"].values()) | set(pattern.get("charts", {}).values())
    steps = orphan_steps(pattern, example, everything)
    removed = {s["target"] for s in steps}
    assert not any(f"shape:{sid}" in removed for sid in panels), \
        "a panel with content on it must stay"


def test_global_decoration_is_never_removed(hand_kit, template):
    """An accent bar or a rule above the footer encloses no field, so the rule cannot touch it."""
    index = deck.probe(template)
    for name, pattern in hand_kit["patterns"].items():
        example = index["slides"][pattern["slide"] - 1]
        shapes = {int(s["ref"].rsplit("shape:", 1)[1]): s for s in example["shapes"]}
        removed = {s["target"] for s in orphan_steps(pattern, example, set())}
        for sid, shape in shapes.items():
            geom = shape["geom"]
            thin = geom["w"] < 200_000 or geom["h"] < 200_000     # a bar or a rule
            if thin and f"shape:{sid}" in removed:
                pytest.fail(f"{name}: thin decoration shape {sid} was removed")


def test_the_empty_panel_really_is_gone_from_the_built_deck(template, hand_kit, tmp_path):
    """The end-to-end version: build a chart slide with no side text and look at what came out."""
    index = deck.probe(template)
    chart = next((n for n, p in hand_kit["patterns"].items() if "charts" in p and len(p["charts"]) == 1),
                 None)
    if chart is None:
        pytest.skip("no single-chart pattern in this kit")
    slot = next(iter(hand_kit["patterns"][chart]["charts"]))
    plan = {"title": "t", "footer": "f", "slides": [{
        "pattern": chart, "fields": {"title": "Weekly tickets"},
        "charts": {slot: {"type": "column", "dataset": "d", "category": "week",
                          "series": ["opened"]}}}]}
    content = {"datasets": {"d": {"columns": ["week", "opened"],
                                  "rows": [["W1", 10], ["W2", 20]]}}}
    result = build.build_from_kit(template, index, hand_kit, plan, content,
                                  tmp_path / "out.pptx", build.baseline_for_kit(template, hand_kit))
    assert result.tidied["orphaned_decorations_removed"] >= 1

    out = result.out_index["slides"][0]
    survivors = [s for s in out["shapes"]
                 if s.get("kind") == "text"
                 and not any(p["text"].strip() for p in s.get("paras", []))
                 and _is_panel(s["geom"])]
    assert not survivors, f"an empty panel survived into the deck: {[s['ref'] for s in survivors]}"
