"""A template's own guidelines (design.guide): palette, writing rules, and every slide following them."""
from __future__ import annotations

import json

import pytest
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt

from deck_agent.design import guide as G
from deck_agent.design.plan import check_spec
from deck_agent.design.profile import _content_layout, extract_profile, with_palette
from deck_agent.infra.storage import store
from deck_agent.llm.planner import StubAgentPlanner

MAIN = [(0, 145, 90), (0, 104, 94), (163, 196, 57), (82, 205, 197)]
HIGHLIGHT = [(239, 123, 91), (255, 160, 91)]
GREYS = [(89, 89, 89), (217, 217, 217), (243, 243, 243)]
CHECKS = {"spelling": "UK", "title_case": "sentence", "bullet_full_stop": False, "currency_symbol": "before",
          "space_before_unit": False, "thousands_separator": ",",
          "scale": {"thousand": "k", "million": "m", "billion": "bn"}, "date_format": "day month year"}


def _guided_template(template, tmp_path):
    """The example template plus three guideline slides: a style guide, a palette, graphics."""
    prs = Presentation(str(template))
    layout = _content_layout(prs)
    s = prs.slides.add_slide(layout)
    s.shapes.title.text = "House style guide"
    box = s.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(8), Inches(3))
    box.text_frame.text = "UK spelling. Millions: 5m. Currency symbol before figures, no space: €5m."
    for r in box.text_frame.paragraphs[0].runs:
        r.font.size = Pt(7)
    s = prs.slides.add_slide(layout)
    s.shapes.title.text = "Colour palette"
    y = 1.6
    for label, colours in (("Brand colours", MAIN), ("Additional colours (use for highlight)", HIGHLIGHT),
                           ("Greyscale palette", GREYS)):
        lab = s.shapes.add_textbox(Inches(0.4), Inches(y + 0.2), Inches(1.5), Inches(0.5))
        lab.text_frame.text = label
        for k, c in enumerate(colours):
            sw = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(2.2 + k * 1.1), Inches(y), Inches(0.9), Inches(0.8))
            sw.fill.solid()
            sw.fill.fore_color.rgb = RGBColor(*c)
            sw.text_frame.text = f"R {c[0]:03d}\nG {c[1]:03d}\nB {c[2]:03d}"
        y += 1.3
    s = prs.slides.add_slide(layout)
    s.shapes.title.text = "Graphical elements"
    chev = s.shapes.add_shape(MSO_SHAPE.CHEVRON, Inches(1), Inches(2), Inches(1), Inches(1))
    chev.name = "Element: chevron"
    out = tmp_path / "guided.pptx"
    prs.save(out)
    return out


def test_guideline_slides_are_found_and_the_palette_is_read_by_row(template, tmp_path):
    path = _guided_template(template, tmp_path)
    prs = Presentation(str(path))
    found = G.guideline_slides(prs)
    n = len(prs.slides)
    assert found == {n - 2: "style", n - 1: "palette", n: "graphics"}
    pal = G.read_palette(prs.slides[n - 2])
    hexes = lambda cs: ["".join(f"{v:02X}" for v in c) for c in cs]
    assert pal["main"] == hexes(MAIN)
    assert pal["highlight"] == hexes(HIGHLIGHT)
    assert pal["grey"] == hexes(GREYS)
    assert pal["mismatches"] == []


def test_guideline_slides_do_not_count_as_the_templates_text_or_colours(template, tmp_path):
    plain, guided = extract_profile(template), extract_profile(_guided_template(template, tmp_path))
    assert guided["body_pt"] == plain["body_pt"], "a style guide set at 7pt is not the body size"
    assert guided["colours"]["primary"] == plain["colours"]["primary"]


def test_the_guide_gives_the_profile_its_official_colours(template, tmp_path):
    path = _guided_template(template, tmp_path)
    guide = G.build_guide(path, None, log=lambda *a: None)
    P = G.apply_guide(extract_profile(path), guide)
    c = P["colours"]
    assert c["primary"] == "00915A" and c["primary_dark"] == "00685E"
    assert c["accent"] in ("A3C439", "52CDC5") and c["alert"] == "EF7B5B"
    assert (c["muted"], c["line"], c["panel"]) == ("595959", "D9D9D9", "F3F3F3")
    ext = with_palette(P, "extended")["colours"]
    allowed = set(guide["palette"]["main"] + guide["palette"]["highlight"])
    assert all(ext[k] in allowed for k in ("teal", "blue", "amber", "plum")), "more colour stays on the palette"


class _RulesPlanner(StubAgentPlanner):
    def __init__(self):
        super().__init__()
        self.asked = []

    def house_rules(self, payload):
        self.asked.append(payload)
        return {"rules": ["Use UK spelling", "Write millions as 5m"], "checks": CHECKS}


def test_ingesting_stores_the_guide_and_every_profile_of_a_deck_uses_it(offline_settings, template, tmp_path):
    from deck_agent.templates import ingest

    path = _guided_template(template, tmp_path)
    planner = _RulesPlanner()
    result = ingest(path, planner=planner, log=lambda *a: None)
    assert result["ok"] and result["guide"]["rules"] == ["Use UK spelling", "Write millions as 5m"]
    assert "Millions: 5m" in planner.asked[0]["slides"][0]["text"], "the model reads the style slide"
    assert json.loads(store().get_bytes(result["template"] + ".guide.json"))["checks"] == CHECKS
    P = G.designed(path, result["template"])
    assert P["colours"]["primary"] == "00915A"
    assert G.rules_of(P) == {"house_rules": ["Use UK spelling", "Write millions as 5m"]}


def test_a_template_without_guidelines_gets_no_guide(offline_settings, template):
    from deck_agent.templates import ingest

    result = ingest(template, log=lambda *a: None)
    assert result["ok"] and "guide" not in result
    assert G.designed(template, result["template"]).get("guide") is None


@pytest.mark.parametrize("kind, text, expected", [
    ("body", "Our color strategy", "'colour'"),
    ("title", "Revenue Grew In The Fourth Quarter", "capital letter at the start only"),
    ("bullet", "Churn fell to 7.2%.", "no full stop at the end of a bullet"),
    ("body", "ARR reached $31.5M", "'31.5m'"),
    ("body", "Revenue of 5 million", "'5m'"),
    ("body", "Costs of € 5m", "'€5m'"),
    ("body", "a 5m€ budget", "currency symbol before the figure"),
    ("body", "1200000 visitors", "'1,200,000'"),
    ("body", "Signed on February 10, 2026", "day month year"),
    ("body", "Margin of 79 %", "'79%'"),
])
def test_the_checkable_rules_find_what_breaks_them(kind, text, expected):
    problems = G.house_problems([(kind, text)], CHECKS)
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize("kind, text", [
    ("title", "Brightpath grew ARR by 38% in 2025"),
    ("title", "Scheduler generated $17.6m of revenue"),
    ("bullet", "Churn fell to 7.2%"),
    ("body", "ARR reached $31.5m in 2025, from 2,660 customers"),
    ("bullet", "Partners, resellers, etc."),
])
def test_text_that_follows_the_rules_passes(kind, text):
    assert G.house_problems([(kind, text)], CHECKS) == []


def test_a_slide_spec_that_breaks_the_style_guide_goes_back_to_its_writer(template):
    P = {**extract_profile(template), "guide": {"rules": ["UK spelling"], "checks": CHECKS}}
    spec = {"type": "content", "title": "Revenue grew to $28.4M in the year",
            "rows": [{"weight": 1, "items": [{"span": 12, "piece": "points", "items": [
                {"text": "Scheduler drove the growth"}, {"text": "Analytics revenue doubled."}]}]}]}
    problems = check_spec(spec, "28.4 Scheduler Analytics", template, P)
    assert any("'28.4m'" in p for p in problems)
    assert any("no full stop" in p for p in problems)
