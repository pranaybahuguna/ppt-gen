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


# --------------------------------------------------------------------------- empty fields' furniture

def _shape(sid, kind, x, y, w, h, text=""):
    inch = 914400
    return {"ref": f"slide:1/shape:{sid}", "kind": kind,
            "geom": {"x": int(x * inch), "y": int(y * inch), "w": int(w * inch), "h": int(h * inch)},
            "paras": [{"text": text}] if text else []}


def test_an_empty_fields_tick_and_rule_go_with_it():
    """Four points, one used: the other three ticks must not hang over nothing."""
    from deck_agent.engine.tidy import companion_steps

    shapes = [_shape(2, "placeholder", 0.5, 0.4, 11, 0.9, "Title")]
    pattern = {"fields": {"title": 2, "page": 99}, "keep": []}
    for i, x in enumerate((1, 4, 7)):
        box, tick, rule = 10 + i * 10, 11 + i * 10, 12 + i * 10
        shapes += [_shape(box, "text", x, 5.5, 2.3, 0.7, "text"),
                   _shape(tick, "picture", x, 4.9, 0.4, 0.4),
                   _shape(rule, "other", x, 6.6, 2.3, 0)]
        pattern["fields"][f"point_{i}"] = box
    shapes.append(_shape(50, "picture", 0, 0, 13.3, 7.5))          # a background photo
    shapes.append(_shape(99, "text", 12, 6.9, 0.4, 0.3, "4"))        # the page number
    steps = companion_steps(pattern, {"shapes": shapes}, filled={2, 10, 99})
    gone = {s["target"] for s in steps}
    assert gone == {"shape:21", "shape:22", "shape:31", "shape:32"}
    assert "shape:11" not in gone, "a filled field keeps its tick"
    assert "shape:50" not in gone, "a big shape is design, not furniture"


def test_nothing_goes_when_every_field_is_filled():
    from deck_agent.engine.tidy import companion_steps

    shapes = [_shape(10, "text", 1, 5.5, 2.3, 0.7, "t"), _shape(11, "picture", 1, 4.9, 0.4, 0.4)]
    assert companion_steps({"fields": {"a": 10}}, {"shapes": shapes}, filled={10}) == []


def test_the_fit_check_sees_text_inside_a_group(tmp_path):
    """The check used to walk only the top of the slide; grouped text was never measured."""
    from pptx import Presentation
    from pptx.util import Inches

    from deck_agent.engine.deck import check

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    box = group.shapes.add_textbox(Inches(1), Inches(5), Inches(1.6), Inches(0.6))
    box.text_frame.word_wrap = True
    box.text_frame.text = ("Visual defect checks: vision review found both project visual defects "
                           "and reported nothing on a clean deck.")
    path = tmp_path / "grouped.pptx"
    prs.save(path)
    findings = check(path)["findings"]
    assert any(f["kind"] == "overflow" for f in findings), findings


def test_a_field_left_empty_loses_its_box():
    """A styled box with nothing in it is an empty card on the slide; it goes."""
    from deck_agent.engine.tidy import empty_field_steps

    shapes = [_shape(2, "placeholder", 0.5, 0.4, 11, 0.9, "Title"),
              _shape(99, "text", 12, 6.9, 0.4, 0.3, "1")]
    shapes += [_shape(sid, "text", 1, 2 + i, 3, 0.5, "x") for i, sid in enumerate((10, 11, 20, 21))]
    pattern = {"fields": {"title": 2, "page": 99, "subtitle": 10},
               "items": {"groups": [[11, 20], [21, 22]], "fields": {"heading": 0, "detail": 1}}}
    steps = empty_field_steps(pattern, {"shapes": shapes}, filled={2, 11})
    assert {s["target"] for s in steps} == {"shape:10", "shape:20", "shape:21"}, \
        "empty subtitle and item boxes go; title, page and filled boxes stay; unknown ids skipped"


def test_deletions_never_conflict_or_remove_filled_content():
    """A box inside a group that is being deleted is not deleted again (the engine would find
    it gone and refuse the slide), and a group holding a filled box is never deleted."""
    from deck_agent.engine.tidy import _consistent

    def group(sid, *children):
        return {"ref": f"slide:1/shape:{sid}", "kind": "group", "children": list(children)}

    box = lambda sid: {"ref": f"slide:1/shape:{sid}", "kind": "text"}
    example = {"shapes": [group(10, box(11), box(12)), group(20, box(21), box(22))]}
    steps = [{"op": "delete_shape", "target": f"shape:{n}"} for n in (10, 11, 20, 22)]
    kept = {s["target"] for s in _consistent(steps, example, filled={21}, existing=set())}
    assert kept == {"shape:10", "shape:22"}, kept


def test_group_membership_comes_from_the_flat_index_too():
    """probe lists every shape at the top level and names its group in `group`; that is the
    form the real templates arrive in (a box three groups deep, deleted after its group)."""
    from deck_agent.engine.tidy import _consistent

    flat = lambda sid, group=None: {"ref": f"slide:1/shape:{sid}", "kind": "text",
                                    **({"group": group} if group else {})}
    example = {"shapes": [flat(123), flat(122, 123), flat(115, 122), flat(116, 115), flat(112, 123)]}
    steps = [{"op": "delete_shape", "target": "shape:116"}]
    assert _consistent(steps, example, filled=set(), existing={"shape:123"}) == []
