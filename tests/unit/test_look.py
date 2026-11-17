"""The vision review: does what it sees actually reach the repair loop?

The `look` node now serves the translate route (create and edit are reviewed by revise.review).
What is tested here is the wiring, deterministically: a problem a vision model
reports must become a finding, high severity must reach the repair loop, low severity must not,
and a vision model that fails must not be able to stop a deck going out.
"""
from __future__ import annotations


import pytest

from deck_agent.nodes.common import warnings_of
from deck_agent.nodes.look import look, review_previews
from deck_agent.settings import Settings, reset, settings


class FakeVision:
    """A planner that sees exactly what the test tells it to."""

    name = "fake vision"

    def __init__(self, answers):
        self.answers = answers
        self.seen = []

    def review_slide(self, png, context=None):
        self.seen.append((context or {}).get("slide"))
        return self.answers.get((context or {}).get("slide"), {"ok": True, "problems": []})


@pytest.fixture
def previews(tmp_path):
    out = []
    for n in (1, 2, 3):
        path = tmp_path / f"slide-{n:02d}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
        out.append(path)
    return out


def test_a_high_severity_problem_becomes_a_finding(previews):
    planner = FakeVision({2: {"ok": False, "problems": [
        {"what": "a large panel is completely empty", "where": "right of the slide",
         "severity": "high"}]}})
    findings, notes = review_previews(previews, planner, log=lambda *a, **k: None)

    assert len(findings) == 1
    assert findings[0]["kind"] == "appearance"
    assert findings[0]["slide_index"] == 1, "slide-02.png is index 1"
    assert "empty" in findings[0]["what"]
    assert notes == []


def test_a_low_severity_problem_is_noted_and_changes_nothing(previews):
    planner = FakeVision({1: {"ok": True, "problems": [
        {"what": "the chart legend is small", "where": "bottom", "severity": "low"}]}})
    findings, notes = review_previews(previews, planner, log=lambda *a, **k: None)

    assert findings == [], "a matter of degree must not trigger a rewrite"
    assert len(notes) == 1
    assert "legend" in notes[0]


def test_every_slide_is_looked_at(previews):
    planner = FakeVision({})
    review_previews(previews, planner, log=lambda *a, **k: None)
    assert sorted(planner.seen) == [1, 2, 3]


def test_a_vision_model_that_fails_cannot_stop_the_deck(previews):
    class Broken:
        name = "broken"

        def review_slide(self, png, context=None):
            raise RuntimeError("the endpoint fell over")

    findings, notes = review_previews(previews, Broken(), log=lambda *a, **k: None)
    assert findings == [], "a failing reviewer must not invent problems"
    assert len(notes) == 3
    assert "could not be looked at" in notes[0]


def test_an_empty_report_is_believed(previews):
    findings, notes = review_previews(previews, FakeVision({}), log=lambda *a, **k: None)
    assert findings == [] and notes == []


def test_the_node_does_nothing_unless_it_is_asked_to(tmp_path):
    reset(Settings(vision_review=False, local_root=str(tmp_path)))
    try:
        out = look({"previews": ["s3://b/x.png"]}, planner=FakeVision({}))
        assert out["trace"][0]["outcome"] == "not asked for"
        assert "findings" not in out
    finally:
        reset(None)


def test_the_node_says_so_when_there_was_nothing_to_look_at(tmp_path):
    reset(Settings(vision_review=True, local_root=str(tmp_path)))
    try:
        out = look({"previews": []}, planner=FakeVision({}))
        assert out["errors"][0]["kind"] == "not_looked_at"
        assert "render_backend" in out["errors"][0]["message"]
    finally:
        reset(None)


def test_what_was_seen_reaches_the_person(offline_settings):
    """Both kinds end up in the warnings, so nothing a reviewer saw is silently dropped."""
    state = {
        "findings": [{"kind": "appearance", "slide_index": 0, "what": "an empty panel",
                      "where": "right", "severity": "high"}],
        "vision_notes": ["slide 2: the legend is small"],
        "structural": [], "leaked": [],
    }
    warnings = warnings_of(state)
    assert any("empty panel" in w for w in warnings)
    assert any("legend is small" in w for w in warnings)


def test_the_graph_routes_a_finding_on_a_translation_to_repair(offline_settings):
    """Something seen must be something fixed: a translated slide with a problem is repaired."""
    from deck_agent.graph import after_look

    state = {
        "route": "translate", "repair_round": 0, "structural": [], "leaked": [], "vision_notes": [],
        "findings": [{"kind": "overflow", "slide_index": 0, "ref": "slide:1/shape:2", "ratio": 1.3}],
    }
    assert after_look(state) == "translate_repair"

    settings().max_repair_rounds = 0          # out of rounds: hand it over and say so
    assert after_look(state) == "deliver"
