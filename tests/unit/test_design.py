"""The designed path: profile, measuring, spec checks, rendering, and planning with a fake model."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pptx import Presentation

from deck_agent.design import measure
from deck_agent.design.plan import frame_specs, lint_story, plan_deck, write_spec
from deck_agent.design.profile import extract_profile
from deck_agent.design.render import render_deck
from deck_agent.design.spec import catalog, unsourced, validate

DATA = Path(__file__).resolve().parents[1] / "data" / "specs"
GALLERY = json.loads((DATA / "gallery.json").read_text())
SPIKE = json.loads((DATA / "spike.json").read_text())


# --------------------------------------------------------------------------- profile, measure

def test_profile_reads_layout_area_and_colours(template):
    P = extract_profile(template)
    assert P["layout"] and P["title"]["box"][2] > 0
    x, y, w, h = P["area"]
    assert w > 0 and h > 0 and y > P["title"]["box"][1]
    for role in ("primary", "accent", "text", "muted", "line", "alert"):
        assert len(P["colours"][role]) == 6


def test_fit_shrinks_long_text_and_refuses_impossible():
    short = measure.fit("Short", 3, 0.5, 30, 10)
    long = measure.fit("A much longer heading that has to wrap or shrink", 3, 0.5, 30, 10)
    assert short == 30 and long is not None and long < 30
    assert measure.fit("word " * 200, 1, 0.3, 30, 10) is None
    assert measure.words_that_fit(3, 1, 14) > measure.words_that_fit(1, 0.3, 14) > 0


# --------------------------------------------------------------------------- spec checks

def test_hand_written_specs_pass_except_lone_short_pieces():
    flagged = {i for i, s in enumerate(GALLERY + SPIKE) if validate(s)}
    assert flagged == {3, 4}                    # cards alone, compare alone
    assert "half empty" in validate(GALLERY[3])[0]


def test_validate_catches_the_mistakes_a_writer_makes():
    s = copy.deepcopy(SPIKE[0])
    s["title"] = "Explain the throughput math"
    s["rows"][0]["items"][0]["items"][0]["value"] = "4,000-5,000 data access points"
    s["rows"][1]["items"][0]["span"] = 4
    s["rows"].append({"weight": 1, "items": [{"span": 12, "piece": "big_number", "value": "twice", "label": "x"}]})
    problems = " | ".join(validate(s))
    for expected in ("describes the slide", "at most 8", "too narrow", "not a number"):
        assert expected in problems
    assert "unknown piece" in validate({"title": "A real point is made here", "rows": [
        {"items": [{"piece": "hexagon"}]}]})[0]


def test_numbers_must_come_from_the_facts():
    spec = {"title": "Throughput rose 300% to 320 scans", "rows": [{"items": [
        {"piece": "kpis", "items": [{"value": "320", "label": "a"}, {"value": "4", "label": "b"}]}]}]}
    assert unsourced(spec, "the pool of 16 reached 320 scans an hour") == ["300"]


def test_catalog_lists_every_piece():
    for name in ("kpis", "chart", "steps", "points", "cards", "compare", "timeline", "table",
                 "big_number", "statement", "matrix", "callout"):
        assert f"  {name} (span" in catalog()


# --------------------------------------------------------------------------- rendering

def test_render_deck_draws_every_spec_without_problems(template, tmp_path):
    out = tmp_path / "gallery.pptx"
    result = render_deck(template, GALLERY + SPIKE, out)
    assert result["problems"] == {}
    prs = Presentation(str(out))
    assert len(prs.slides) == len(GALLERY) + len(SPIKE)
    content = prs.slides[2]
    assert content.shapes.title.text_frame.text.startswith("Three quarters")
    charts = [sh for s in prs.slides for sh in s.shapes if sh.has_chart]
    assert len(charts) == 2                     # the line chart and the spike's column chart


def test_titles_lose_their_full_stop(template, tmp_path):
    spec = copy.deepcopy(SPIKE[1])
    spec["title"] = "Four guardrails keep model-written slides safe to ship."
    render_deck(template, [spec], tmp_path / "t.pptx")
    title = Presentation(str(tmp_path / "t.pptx")).slides[0].shapes.title.text_frame.text
    assert title == "Four guardrails keep model-written slides safe to ship"


# --------------------------------------------------------------------------- planning

FACTS = {"facts": [{"id": f"F{i}", "text": t, "quote": t} for i, t in enumerate([
    "One worker caps throughput at 20 scans an hour.",
    "A pool of 16 workers reaches 320 scans an hour.",
    "The target is 200 scans an hour.",
    "Each scan takes about 3 minutes.",
    "Between 4,000 and 5,000 applications are in scope.",
    "Pools of 4 and 8 workers reach 80 and 160 scans an hour."], start=1)], "datasets": {}, "gaps": []}
SOURCE = " ".join(f["text"] for f in FACTS["facts"])

STORY = {"title": "Scaling the scanner", "subtitle": "Why and how", "sections": ["Today", "Plan"],
         "slides": [
             {"type": "content", "section": "Today", "title": "One worker caps us at 20 scans an hour",
              "job": "see the ceiling", "visual": "numbers", "facts": ["F1", "F3"]},
             {"type": "content", "section": "Plan", "title": "Four guardrails keep model-written slides safe",
              "job": "trust it", "visual": "sequence", "facts": ["F2"]}]}


class FakePlanner:
    """Answers like the model would; the first spec for each slide breaks a rule on purpose."""

    def __init__(self):
        self.spec_calls = []

    def gather(self, request, sources):
        return {"facts": [{"text": f["text"], "quote": f["quote"]} for f in FACTS["facts"]]}

    def storyline(self, payload):
        return copy.deepcopy(STORY)

    def slide_spec(self, payload):
        self.spec_calls.append(payload)
        good = copy.deepcopy(SPIKE[0] if "20 scans" in payload["slide"]["title"] else SPIKE[1])
        good["title"] = payload["slide"]["title"]
        good.pop("footnote", None)
        if "feedback" not in payload:
            bad = copy.deepcopy(good)
            bad["title"] = "Explain this"          # a meta title: must come back with feedback
            return bad
        return good


def test_lint_story_flags_labels_and_repeats():
    story = copy.deepcopy(STORY)
    story["slides"][1]["title"] = "Overview"
    story["slides"][1]["visual"] = "numbers"
    problems = " | ".join(lint_story(story, FACTS))
    assert "is a label" in problems and "vary the visuals" in problems
    assert lint_story(STORY, FACTS) == ["only 2 content slides; the facts support more"]


def test_frame_adds_cover_and_sections_only_for_long_decks():
    short = frame_specs(STORY)
    assert [p is None for _, p in short] == [True, False, False]
    assert short[0][0]["type"] == "cover" and short[0][0]["agenda"] == ["Today", "Plan"]
    assert sum(1 for s, p in short if s.get("type") == "section") == 0
    long = copy.deepcopy(STORY)
    long["slides"] = [dict(STORY["slides"][i % 2], title=f"Point number {i} is made here") for i in range(8)]
    sections = [s for s, _ in frame_specs(long) if s.get("type") == "section"]
    assert sections and sections[0]["agenda"]


def test_write_spec_sends_problems_back_and_accepts_the_fix(template):
    fake = FakePlanner()
    P = extract_profile(template)
    spec, problems, tries = write_spec(STORY["slides"][0], STORY, FACTS, fake, template, P)
    assert problems == [] and tries == 2
    assert any("describes the slide" in f for f in fake.spec_calls[1]["feedback"])


def test_a_failing_model_stops_the_run(template, tmp_path):
    class Broke(FakePlanner):
        def slide_spec(self, payload):
            raise RuntimeError("model endpoint answered 429: no credits")

    with pytest.raises(RuntimeError, match="429"):
        plan_deck("a deck", [{"name": "notes.md", "text": SOURCE}], template, tmp_path / "d.pptx",
                  Broke(), log=lambda *a: None)


def test_plan_deck_end_to_end_with_a_fake_model(template, tmp_path):
    out = tmp_path / "deck.pptx"
    result = plan_deck("a deck", [{"name": "notes.md", "text": SOURCE}], template, out, FakePlanner(),
                       log=lambda *a: None)
    assert out.exists() and out.with_suffix(".specs.json").exists()
    assert [s.get("type", "content") for s in result["specs"]] == ["cover", "content", "content"]
    assert all(not s["problems"] for s in result["report"]["slides"])
    assert len(Presentation(str(out)).slides) == 3


def test_dry_run_refuses_text_shrunk_below_reading_size(template):
    from deck_agent.design.plan import dry_run

    P = extract_profile(template)
    crowded = {"type": "content", "title": "Five reasons the plan works as intended today", "rows": [
        {"weight": 1, "items": [{"span": 4, "piece": "points", "items": [
            {"heading": f"Reason {i}", "text": " ".join(["a sentence long enough to need several lines in a narrow column"] * 3)}
            for i in range(5)]}, {"span": 8, "piece": "statement", "text": "One sentence."}]}]}
    problems = dry_run(crowded, template, P)
    assert problems and ("shrink" in problems[0] or "does not fit" in problems[0])
    assert dry_run(SPIKE[0], template, P) == []
