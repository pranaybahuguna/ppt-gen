"""Two-stage generation: which layouts are offered, splitting, and sending errors back."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from controlplane import slide_flow  # noqa: E402
from controlplane.planner import StubPlanner  # noqa: E402
from deckops.compiler import CompileError  # noqa: E402
from deckops.probe import probe  # noqa: E402


@pytest.fixture(scope="module")
def tpl(tmp_path_factory):
    d = tmp_path_factory.mktemp("kit")
    subprocess.run([sys.executable, os.path.join(ROOT, "samples/make_style_template.py"), str(d)], check=True,
                   capture_output=True)
    return json.load(open(d / "example_style_kit.json")), probe(str(d / "example_style.pptx"))


def pts(n, **extra):
    return [{"heading": f"Point {i}", "text": f"What point {i} says.", **extra} for i in range(1, n + 1)]


def names(opts):
    return [o["pattern"] for o in opts]


def test_only_layouts_that_can_hold_the_content(tpl):
    kit, _ = tpl
    six = names(slide_flow.options_for({"kind": "sequence", "points": pts(6)}, kit, 2, 5))
    assert six[0] == "steps" and "stats" not in six and "cover" not in six    # stats holds 2-4
    assert "cover" in names(slide_flow.options_for({"points": pts(2)}, kit, 1, 5))   # cover only first
    table = {"header": ["A", "B"], "rows": [["1", "2"]] * 3}
    assert names(slide_flow.options_for({"table": table}, kit, 2, 5)) == ["table"]


def test_too_many_points_are_split(tpl):
    kit, _ = tpl
    jobs = slide_flow.expand({"slides": [{"kind": "sequence", "title": "Plan", "points": pts(20)}]}, kit)
    assert [len(j["content"]["points"]) for j in jobs] == [8, 8, 4]
    assert jobs[0]["content"]["title"] == "Plan (1/3)" and all(j["options"] for j in jobs)


class FirstTryWrong(StubPlanner):
    """Answers with too many items first; fixes it once told."""
    def __init__(self):
        self.calls = []

    def render_slide(self, slide, options, context, feedback=None, previous=None):
        self.calls.append(feedback)
        ans = super().render_slide(slide, options, context, feedback, previous)
        if feedback is None:
            ans["items"] = ans["items"] * 3                 # 3 x 3 = 9 items; columns holds at most 4
        return ans


def test_error_goes_back_to_the_model(tpl):
    kit, index = tpl
    job = {"content": {"kind": "comparison", "title": "Three themes", "points": pts(3)},
           "options": [slide_flow.option("columns", kit["patterns"]["columns"])]}
    planner = FirstTryWrong()
    plan_slide, attempts = slide_flow.render_one(planner, job, {"slide": 2, "of": 5}, index, kit, {}, log=lambda *a: 0)
    assert len(attempts) == 2 and "needs 2-4 items, got 9" in attempts[0]["errors"][0]
    assert "got 9" in planner.calls[1][0]                   # the model was shown the error
    assert len(plan_slide["items"]) == 4


class AlwaysWrong(StubPlanner):
    def render_slide(self, slide, options, context, feedback=None, previous=None):
        return {"pattern": "columns", "fields": {"nonsense": "x"}, "items": []}


def test_gives_up_after_retries(tpl):
    kit, index = tpl
    outline = {"slides": [{"kind": "comparison", "title": "T", "points": pts(3)}]}
    with pytest.raises(CompileError, match="still invalid after 3 attempts"):
        slide_flow.plan_slides(outline, kit, index, {}, AlwaysWrong(), retries=2, log=lambda *a: 0)


def test_offline_renderer_fills_matching_fields(tpl):
    kit, index = tpl
    outline = {"title": "Deck", "slides": [
        {"kind": "numbers", "title": "Results", "intro": "What changed.",
         "points": [{"heading": "Solved", "value": "38%", "text": "of tickets"},
                    {"heading": "Response", "value": "2 min", "text": "was 4 hours"}]}]}
    plan, _, _ = slide_flow.plan_slides(outline, kit, index, {}, StubPlanner(), log=lambda *a: 0)
    s = plan["slides"][0]
    assert s["pattern"] == "stats" and s["fields"]["intro"] == "What changed."
    assert s["items"][0] == {"label": "Solved", "value": "38%", "caption": "of tickets"}
