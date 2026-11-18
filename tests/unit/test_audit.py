"""The deck check (revise.audit): what only shows across slides or in a chart's numbers."""
from __future__ import annotations

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from deck_agent import facade
from deck_agent.design.profile import extract_profile
from deck_agent.design.render import render_deck
from deck_agent.infra.storage import sha256_file, store
from deck_agent.llm.planner import StubAgentPlanner
from deck_agent.revise import audit as A
from deck_agent.revise.read import deck_outline

from .test_graph import CONTENT
from .test_revise import GALLERY


def _deck(template, tmp_path):
    out = tmp_path / "deck.pptx"
    render_deck(template, GALLERY[:6], out)               # cover, section, 4 content slides
    return out


def _chart(slide, series: dict[str, list[float]], colours: list[str], cats=("Q1", "Q2", "Q3")):
    data = CategoryChartData()
    data.categories = list(cats)
    for name, values in series.items():
        data.add_series(name, values)
    ch = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(5), Inches(3),
                                data).chart
    for s, c in zip(ch.plots[0].series, colours):
        s.format.fill.solid()
        s.format.fill.fore_color.rgb = RGBColor.from_string(c)
    return ch


def test_percent_values_shown_with_a_percent_format_are_not_multiplied(template, tmp_path):
    prs = Presentation(str(_deck(template, tmp_path)))
    whole = _chart(prs.slides[2], {"Share": [8, 21, 62]}, ["363C58"])
    whole.plots[0].has_data_labels = True
    whole.plots[0].data_labels.number_format = "0%"
    whole.plots[0].data_labels.number_format_is_linked = False
    fraction = _chart(prs.slides[3], {"Share": [0.08, 0.21, 0.62]}, ["363C58"])
    fraction.plots[0].has_data_labels = True
    fraction.plots[0].data_labels.number_format = "0%"
    fraction.plots[0].data_labels.number_format_is_linked = False

    done = A.fix_percent_formats(prs, [3, 4])
    assert done and done[0].startswith("slide 3:")
    codes = lambda ch: {nf.get("formatCode") for nf in ch._chartSpace.iter(qn("c:numFmt"))}
    assert codes(whole) == {'0"%"'}                    # 8 shows as 8%, not 800%
    assert codes(fraction) == {"0%"}                    # 0.08 shows as 8%: left alone
    assert A.fix_percent_formats(prs, [3, 4]) == []     # and a second look changes nothing


def test_chart_colours_match_across_slides_differ_within_a_chart_and_keep_to_the_palette(template, tmp_path):
    src = _deck(template, tmp_path)
    P = extract_profile(src)
    pal = A.chart_palette(P)
    prs = Presentation(str(src))
    a = _chart(prs.slides[2], {"Electric": [1, 2, 3], "Diesel": [3, 2, 1]}, [pal[2], pal[0]])
    b = _chart(prs.slides[3], {"Electricity": [1, 2, 3], "Diesel": [3, 2, 1]}, ["7A5195", pal[0]])
    c = _chart(prs.slides[4], {"Hybrid": [1, 2, 3], "Petrol": [3, 2, 1]}, [pal[0], "222C3A"])

    done = A.fix_chart_colours(prs, [3, 4, 5], P)
    colour = lambda ch, k: str(ch.plots[0].series[k].format.fill.fore_color.rgb)
    assert colour(b, 0) == colour(a, 0) == pal[2]       # "Electricity" is "Electric": one colour
    assert colour(b, 1) == colour(a, 1)                 # Diesel the same on both slides
    assert A._dist(colour(c, 0), colour(c, 1)) >= A.TOO_CLOSE   # two near-identical navies split
    assert all(colour(ch, k) in pal for ch in (a, b, c) for k in (0, 1))
    assert any("Electricity" in d for d in done)
    assert A.fix_chart_colours(prs, [3, 4, 5], P) == []


def test_content_titles_take_the_common_size_and_tiny_text_is_found(boxed_template, tmp_path):
    src = _deck(boxed_template, tmp_path)
    P = extract_profile(src)
    prs = Presentation(str(src))
    sizes = {3: 24, 4: 24, 5: 30, 6: 24}
    for n, pt in sizes.items():
        for r in prs.slides[n - 1].shapes.title.text_frame.paragraphs[0].runs:
            r.font.size = Pt(pt)
    done = A.fix_title_sizes(prs, [3, 4, 5, 6], P)
    assert done == ["slide 5: title 30pt -> 24pt, like the other content slides"]
    assert A._title_pt(prs.slides[4]) == 24

    box = prs.slides[3].shapes.add_textbox(Inches(1), Inches(3), Inches(3), Inches(1))
    box.text_frame.text = "Read me if you can"
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(6)
    found = A.text_findings(prs, [3, 4, 5, 6], P)
    assert list(found) == [4] and "6pt" in found[4][0]


def test_the_inventory_carries_what_a_chart_says_for_the_model(template, tmp_path):
    prs = Presentation(str(_deck(template, tmp_path)))
    ch = _chart(prs.slides[2], {"Cost per km": [0.22, 0.33, 0.41]}, ["363C58"], cats=("EV", "Hybrid", "Diesel"))
    ch.value_axis.has_title = True
    ch.value_axis.axis_title.text_frame.text = "Number of vans"
    roles = {o["slide"]: o["role"] for o in deck_outline(prs)}
    inv = A.inventory(prs, roles)
    chart = inv[2]["charts"][0]
    assert chart["value_axis_title"] == "Number of vans"
    assert chart["categories"] == ["EV", "Hybrid", "Diesel"] and chart["series"][0]["values"] == [0.22, 0.33, 0.41]
    assert inv[0]["role"] == "cover"


class _Auditor(StubAgentPlanner):
    """The stub, with a deck check that finds one thing and a tweak that records what it was told."""

    def __init__(self):
        super().__init__()
        self.audits, self.tweaks = [], []

    def audit_deck(self, payload):
        self.audits.append(payload)
        return {"findings": [{"slide": 2, "problem": "the axis says vans, the values are euros per km",
                              "fix": "set the value axis title to 'Cost per km (EUR)'"}]}

    def tweak_slide(self, payload, png=None):
        self.tweaks.append(payload)
        return super().tweak_slide(payload, png)


def test_the_deck_check_runs_after_review_and_sends_its_findings_back_once(offline_settings, template):
    uploaded = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")
    planner = _Auditor()
    quiet = lambda *a, **k: None
    created = facade.create_deck("test-audit", "a deck", CONTENT, uploaded, planner=planner, log=quiet)
    assert created["ok"], created
    assert len(planner.audits) == 1, "one model check per request"
    assert planner.audits[0]["facts"] == CONTENT["facts"]
    told = [t for t in planner.tweaks if t.get("slide_number") == 2]
    # one task (its worker may try up to three times), never a second one from a later check
    assert 1 <= len(told) <= 3 and all("Cost per km (EUR)" in t["request"] for t in told)
    assert not any("audit: slide 2" in w for w in created["warnings"]), \
        "a finding that went back for fixing is not also a warning"


def test_the_deck_check_can_be_switched_off(offline_settings, template):
    offline_settings.audit_deck = False
    uploaded = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")
    planner = _Auditor()
    created = facade.create_deck("test-audit-off", "a deck", CONTENT, uploaded, planner=planner,
                                 log=lambda *a, **k: None)
    assert created["ok"] and planner.audits == [] and planner.tweaks == []


def test_a_bar_is_judged_by_its_fill_not_a_leftover_marker_colour(template, tmp_path):
    """Code that styles points often leaves a marker colour on a bar chart, where it never shows;
    the bar's own fill is what a person sees."""
    src = _deck(template, tmp_path)
    P = extract_profile(src)
    pal = A.chart_palette(P)
    prs = Presentation(str(src))
    ch = _chart(prs.slides[2], {"Share": [1, 2]}, [pal[0]], cats=("Vans", "Depots"))
    for i, (fill, marker) in enumerate([("8C6D31", pal[2]), ("7A5195", pal[0])]):
        pt = ch.plots[0].series[0].points[i]
        pt.format.fill.solid()
        pt.format.fill.fore_color.rgb = RGBColor.from_string(fill)
        pt.marker.format.fill.solid()
        pt.marker.format.fill.fore_color.rgb = RGBColor.from_string(marker)
    assert [h["colour"] for h in A._holders(prs.slides[2].shapes[-1])] == ["8C6D31", "7A5195"]
    A.fix_chart_colours(prs, [3], P)
    after = [str(ch.plots[0].series[0].points[i].format.fill.fore_color.rgb) for i in (0, 1)]
    assert all(c in pal for c in after) and after[0] != after[1]
