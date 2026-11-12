"""Designed slides: the layout budget, the choice of diagram, and the frame they are drawn in.

The complaint this answers: a twelve-slide deck built from a six-layout template came out as
the same three layouts over and over. The budget stops a layout being used past its share;
what goes over it is drawn instead, as a diagram, in the template's frame.
"""
from __future__ import annotations

import json

import pytest

from deck_agent.engine.variety import FORMS, SUITS, assign_forms, budget


@pytest.mark.parametrize("chosen, limit, over", [
    # the README deck as it came out: columns six times, text four
    (["cover", "columns", "columns", "text", "columns", "cards", "columns", "columns",
      "text", "text", "columns", "text"], 2, [2, 6, 7, 9, 10, 11]),
    (["cover", "a", "b", "a", "b", "a"], 2, [5]),
    (["cover", "a", "a"], 5, [2]),                     # never twice in a row, whatever the budget
    (["cover", "a", "b", "c"], 2, []),
    (["cover", "a", "a", "a"], 0, []),                 # 0 = no budget
    (["cover", "a@2", "a@3"], 2, [2]),                 # variants of one layout are one layout
])
def test_budget(chosen, limit, over):
    assert budget(chosen, limit) == over


def test_a_designed_slide_breaks_a_run():
    """After the second 'a' is designed, the third 'a' is no longer 'the same as the last'."""
    assert budget(["cover", "a", "a", "a"], 3) == [2]


def test_each_designed_slide_gets_a_different_diagram():
    contents = [{"kind": "list", "points": [1, 2, 3]}] * 5 + [{"kind": "sequence", "points": [1, 2]}]
    forms = assign_forms(contents)
    assert len(set(forms)) == len(forms), forms
    assert forms[-1] in SUITS["sequence"], "a sequence is drawn as something sequential"
    assert all(f in FORMS for f in forms)


def test_diagrams_that_cannot_hold_many_points_are_not_offered_them():
    forms = assign_forms([{"kind": "comparison", "points": list(range(6))}])
    assert forms[0] not in ("2x2 matrix", "big numbers")


def test_the_frame_is_the_templates_content_layout(template):
    from deck_agent.engine.deck import probe
    from deck_agent.nodes.common import design_frame

    index = probe(template)
    kit = json.loads((template.parent / "example_style_kit.json").read_text())
    frame = design_frame(kit, index)
    box, size = frame["body_box"], index["slide_size"]
    assert frame["chrome_layout"] is not None
    assert 0 < box["x"] and box["x"] + box["w"] < size["w"]
    assert 0 < box["y"] and box["y"] + box["h"] <= size["h"] * 0.9


# --------------------------------------------------------------------------- page numbers

def _deck_with_numbers(tmp_path, numbers):
    from pptx import Presentation

    prs = Presentation()
    layout = prs.slide_layouts[1]
    source = next(ph for ph in layout.placeholders if int(ph.placeholder_format.type) == 13)
    for n in numbers:
        slide = prs.slides.add_slide(layout)
        if n is not None:
            slide.shapes.clone_placeholder(source)
            ph = next(p for p in slide.placeholders if int(p.placeholder_format.type) == 13)
            ph.text_frame.text = str(n)
    path = tmp_path / "numbers.pptx"
    prs.save(path)
    return path


def _numbers(path):
    from pptx import Presentation

    out = []
    for slide in Presentation(str(path)).slides:
        found = [p.text_frame.text for p in slide.placeholders
                 if int(p.placeholder_format.type) == 13]
        out.append(found[0] if found else None)
    return out


def test_bare_slide_numbers_are_renumbered_after_an_insert(tmp_path):
    """The compiler wrote 1, 2, 3; a designed slide went in second; the numbers must follow."""
    from deck_agent.engine.tidy import renumber_pages

    src = _deck_with_numbers(tmp_path, [1, None, 2, 3])
    renumber_pages(src, tmp_path / "out.pptx")
    assert _numbers(tmp_path / "out.pptx") == ["1", None, "3", "4"]


def test_a_drawn_slide_gets_the_layouts_page_number(tmp_path):
    from deck_agent.engine.tidy import add_slide_number

    src = _deck_with_numbers(tmp_path, [1, None, 3])
    assert add_slide_number(src, 1, tmp_path / "out.pptx")
    assert _numbers(tmp_path / "out.pptx") == ["1", "2", "3"]
    assert not add_slide_number(tmp_path / "out.pptx", 1, tmp_path / "again.pptx"), \
        "a slide that already has one is left alone"


def test_the_prompt_and_the_contract_agree():
    from deck_agent.llm.prompts import CODE_SYSTEM
    from deck_agent.sandbox.code import CONTRACT

    for key in ("chrome_layout", "body_box", "form", "avoid_forms", "target_slide", "shapes"):
        assert key in CONTRACT and key in CODE_SYSTEM


# --------------------------------------------------------------------------- keeping content in place


def test_repair_rewrites_a_slide_with_its_own_content(offline_settings, template, planner):
    """The bug: with slides 2 and 3 designed, plan slide 2 is outline slide 4 - and repair read
    outline slide 2, putting one slide's words in place of another's."""
    from deck_agent import facade
    from deck_agent.infra.checkpointer import checkpointer
    from deck_agent.infra.cleanup import state_of
    from deck_agent.infra.storage import sha256_file, store
    from deck_agent.nodes.create import repair

    from .test_graph import CONTENT, OUTLINE

    step = OUTLINE["slides"][1]
    outline = {**OUTLINE, "slides": [OUTLINE["slides"][0]] + [
        {**step, "title": f"Step group {n}"} for n in (1, 2, 3)]}
    tpl = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")
    assert facade.create_deck("r", "x", CONTENT, tpl, planner=planner, outline=outline,
                              log=lambda *a, **k: None)["ok"]
    with checkpointer() as saver:
        state = state_of(saver, "r")

    seen = []

    class Watching(type(planner)):
        def render_slide(self, slide, options, context, feedback=None, previous=None):
            seen.append(slide["title"])
            return super().render_slide(slide, options, context, feedback, previous)

    # pretend slides 2 and 3 were designed: the plan holds outline slides 0 and 3
    state["plan"] = {**state["plan"], "slides": [state["plan"]["slides"][0],
                                                 state["plan"]["slides"][3]]}
    state["plan_jobs"] = [0, 3]
    state["findings"] = [{"kind": "overflow", "slide_index": 1,
                          "ref": "slide:0/shape:2", "ratio": 1.4}]
    from unittest import mock
    with mock.patch("deck_agent.nodes.create.repair_feedback", return_value={1: ["too long"]}):
        repair(state, planner=Watching(), log=lambda *a, **k: None)
    assert seen and set(seen) == {"Step group 3"}, seen


def test_vision_findings_are_numbered_for_the_plan(offline_settings, monkeypatch):
    import importlib

    from deck_agent import settings as settings_module
    look_module = importlib.import_module("deck_agent.nodes.look")

    object.__setattr__(offline_settings, "vision_review", True)
    monkeypatch.setattr(look_module, "review_previews", lambda *a, **k: (
        [{"kind": "appearance", "slide_index": 1, "what": "on a designed slide"},
         {"kind": "appearance", "slide_index": 3, "what": "on a template slide"}], []))
    from deck_agent.infra.storage import store
    uris = [store().put_bytes(b"png", f"t/r/previews/slide-0{i}.png") for i in range(1, 5)]
    state = {"thread_id": "t", "run_id": "r", "previews": uris, "findings": [],
             "code_slides": [{"position": 1, "form": "timeline"}]}
    out = look_module.look(state, planner=object(), log=lambda *a, **k: None)
    assert [f["slide_index"] for f in out["findings"]] == [2], \
        "deck slide 4 is plan slide 3 when one designed slide sits before it"
    assert any("designed" in n for n in out["vision_notes"])
    settings_module.settings()







def test_designed_slides_are_not_reviewed_with_the_template_slides(offline_settings, monkeypatch):
    import importlib

    look_module = importlib.import_module("deck_agent.nodes.look")
    object.__setattr__(offline_settings, "vision_review", True)
    reviewed = []
    monkeypatch.setattr(look_module, "review_previews",
                        lambda paths, *a, **k: (reviewed.extend(p.name for p in paths) or ([], [])))
    from deck_agent.infra.storage import store
    uris = [store().put_bytes(b"png", f"t/r/previews/slide-0{i}.png") for i in range(1, 5)]
    state = {"thread_id": "t", "run_id": "r", "previews": uris, "findings": [],
             "pending_code": [{"position": 1, "rework": True}],
             "code_slides": [{"position": 2, "rework": True}]}
    look_module.look(state, planner=object(), log=lambda *a, **k: None)
    assert sorted(reviewed) == ["slide-01.png", "slide-04.png"]



# --------------------------------------------------------------------------- reworking a slide

def _built(offline_settings, template, planner, deck_id="rw"):
    from deck_agent import facade
    from deck_agent.infra.checkpointer import checkpointer
    from deck_agent.infra.cleanup import state_of
    from deck_agent.infra.storage import sha256_file, store

    from .test_graph import CONTENT, OUTLINE

    object.__setattr__(offline_settings, "allow_custom_code", True)
    object.__setattr__(offline_settings, "layout_reuse", 1)
    step = OUTLINE["slides"][1]
    outline = {**OUTLINE, "slides": [OUTLINE["slides"][0]] + [
        {**step, "title": f"Step group {n}"} for n in (1, 2, 3)]}
    tpl = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")
    result = facade.create_deck(deck_id, "x", CONTENT, tpl, planner=planner, outline=outline,
                                log=lambda *a, **k: None)
    with checkpointer() as saver:
        return result, state_of(saver, deck_id)


def test_an_over_budget_slide_stays_in_the_plan_with_its_content(offline_settings, template,
                                                                 planner):
    """No frame and no insertion: the slide is built from its template layout, in its place,
    and marked to be reworked. If the rework cannot happen - code is off in unit tests - it is
    still there, in its template layout, with all its words."""
    result, state = _built(offline_settings, template, planner)
    rendered = next(t for t in state["trace"] if t["node"] == "render_slides")
    assert rendered.get("designed"), "the over-budget slide was marked for rework"
    titles = [sl["fields"].get("title") for sl in state["plan"]["slides"]]
    assert titles[1:] == ["Step group 1", "Step group 2", "Step group 3"], titles
    assert result["ok"], result
    assert any("keeps its template layout" in w for w in result["warnings"]), result["warnings"]


def test_the_code_is_told_what_is_on_the_slide(offline_settings, template, planner, tmp_path):
    from deck_agent.infra.storage import store
    from deck_agent.nodes.custom_code import shape_list

    result, _ = _built(offline_settings, template, planner, "rw2")
    deck = store().get(result["deck"], tmp_path / "deck.pptx")
    shapes = shape_list(deck, 1, {"x": 0, "y": int(0.15 * 6858000), "w": 12192000,
                                  "h": int(0.75 * 6858000)})
    roles = {sh["role"] for sh in shapes}
    assert "content" in roles
    assert all({"id", "role", "x", "y", "w", "h", "text"} <= set(sh) for sh in shapes)
    assert any(sh["text"] for sh in shapes if sh["role"] == "content"), "its words are listed"


def test_the_first_slide_keeps_the_cover_layout(offline_settings, template):
    """Points on a cover pushed it out of the cover layout into whatever held two points."""
    import json

    from deck_agent.engine import bootstrap
    from deck_agent.nodes.create import _keep_the_cover

    bootstrap()
    from controlplane import slide_flow

    kit = json.loads((template.parent / "example_style_kit.json").read_text())
    outline = {"slides": [
        {"kind": "cover", "title": "Pilot", "subtitle": "Eight weeks",
         "points": [{"heading": f"Point {n}", "text": "more than a cover holds"} for n in range(7)]},
        {"kind": "list", "title": "More", "points": [{"heading": "A", "text": "a"}]}]}
    jobs = slide_flow.expand(outline, kit)
    note = _keep_the_cover(jobs, kit, len(jobs))
    assert {o["pattern"].split("@")[0] for o in jobs[0]["options"]} == {"cover"}
    assert "points" not in jobs[0]["content"] and note
