"""The edit route's building blocks: reading any slide, slide surgery, and the split's checks."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from pptx import Presentation

from deck_agent.design.profile import extract_profile
from deck_agent.design.render import render_deck
from deck_agent.revise.read import deck_outline, slide_content, slide_shapes
from deck_agent.revise.surgery import apply_changes
from deck_agent.revise.work import INVENT, _label_invented

DATA = Path(__file__).resolve().parents[1] / "data" / "specs"
GALLERY = json.loads((DATA / "gallery.json").read_text())
SPIKE = json.loads((DATA / "spike.json").read_text())


def _deck(template, tmp_path) -> Path:
    out = tmp_path / "deck.pptx"
    render_deck(template, GALLERY[:6], out)           # cover, section, 4 content slides
    return out


def _slide_bytes(path) -> dict:
    z = zipfile.ZipFile(path)
    return {n: hashlib.md5(z.read(n)).hexdigest() for n in z.namelist() if n.startswith("ppt/slides/slide")}


def test_reading_a_slide_gives_content_and_shapes(template, tmp_path):
    prs = Presentation(str(_deck(template, tmp_path)))
    c = slide_content(prs.slides[2])
    assert c["title"].startswith("Three quarters") and any("73%" in t["text"] for t in c["texts"])
    shapes = slide_shapes(prs.slides[2])
    assert all({"id", "kind", "box_in"} <= set(s) for s in shapes)
    outline = deck_outline(prs)
    assert [o["slide"] for o in outline] == [1, 2, 3, 4, 5, 6]


def test_surgery_replaces_inserts_deletes_and_moves_in_one_pass(template, tmp_path):
    src = _deck(template, tmp_path)
    P = extract_profile(src)
    out = tmp_path / "out.pptx"
    done = apply_changes(src, [
        {"op": "replace", "slide": 4, "spec": SPIKE[0]},
        {"op": "insert", "after": 2, "spec": SPIKE[1]},
        {"op": "delete", "slide": 6},
        {"op": "move", "slide": 3, "after": 0}], P, out)
    titles = [o["title"] for o in deck_outline(Presentation(str(out)))]
    assert titles[0].startswith("Three quarters")           # slide 3 moved to the front
    assert done["at"][0] == titles.index(SPIKE[0]["title"]) + 1
    assert done["at"][1] == titles.index(SPIKE[1]["title"]) + 1
    assert len(titles) == 6                                 # 6 + 1 inserted - 1 deleted
    names = zipfile.ZipFile(out).namelist()
    assert len(names) == len(set(names)), "a slide part name was used twice"


def test_untouched_slides_keep_their_bytes(template, tmp_path):
    src = _deck(template, tmp_path)
    out = tmp_path / "out.pptx"
    apply_changes(src, [{"op": "replace", "slide": 4, "spec": SPIKE[0]}], extract_profile(src), out)
    before, after = _slide_bytes(src), _slide_bytes(out)
    same = [n for n in before if after.get(n) == before[n] and not n.endswith("slide4.xml")]
    assert len(same) >= len(before) - 2                     # only slide 4 and its rels changed


def test_made_up_numbers_are_labelled_only_when_asked():
    asked = "change the box to a graph - make up some numbers showing growth"
    assert INVENT.search(asked) and not INVENT.search("make slide 3 more visual")
    assert "Illustrative" in _label_invented({"title": "x"}, asked)["footnote"]
    assert "footnote" not in _label_invented({"title": "x"}, "make it more visual")


def test_split_drops_tasks_for_slides_that_do_not_exist(offline_settings, template):
    from deck_agent.infra.storage import store
    from deck_agent.revise.nodes import after_split, split

    uri = store().put(template, "split-test/deck.pptx")
    n = len(Presentation(str(template)).slides)

    class Fake:
        def split_edit(self, payload):
            return {"tasks": [{"kind": "redesign", "slide": 1, "instruction": "a"},
                              {"kind": "tweak", "slide": n + 5, "instruction": "b"},
                              {"kind": "explode", "slide": 1, "instruction": "c"},
                              {"kind": "delete", "slide": 1, "instruction": "d"}]}

    state = {"thread_id": "split-test", "run_id": "r1", "instruction": "x", "deck_versions": [uri]}
    out = split(state, planner=Fake(), log=lambda *a: None)
    assert [t["kind"] for t in out["edit_tasks"]] == ["redesign", "delete"]
    sends = after_split({**state, **out})
    assert len(sends) == 1 and sends[0].node == "work"      # delete needs no model: straight to join


# --------------------------------------------------------------------------- titles and roles

def test_new_content_slides_share_one_title_position_and_rule(boxed_template, tmp_path):
    from pptx.enum.text import MSO_ANCHOR

    from deck_agent.design.render import RULE_NAME, title_rule_y

    deck = _deck(boxed_template, tmp_path)
    P = extract_profile(deck)
    prs = Presentation(str(deck))
    content = [s for s in prs.slides if s.slide_layout.name == P["layout"]]
    assert len(content) == 4
    for s in content:
        assert s.shapes.title.text_frame.vertical_anchor == MSO_ANCHOR.TOP
        rules = [sh for sh in s.shapes if sh.name == RULE_NAME]
        assert len(rules) == 1 and abs(rules[0].top / 914400 - title_rule_y(P)) < 0.01
    covers = [s for s in prs.slides if s.slide_layout.name != P["layout"]]
    assert covers and not any(sh.name == RULE_NAME for s in covers for sh in s.shapes)
    # the cover is the template's own cover layout, its title box filled
    assert prs.slides[0].slide_layout.name == P["cover_layout"] and prs.slides[0].shapes.title.text_frame.text


def test_roles_tell_cover_and_sections_from_content(template, tmp_path):
    roles = [o["role"] for o in deck_outline(Presentation(str(_deck(template, tmp_path))))]
    assert roles == ["cover", "section", "content", "content", "content", "content"]


def test_align_titles_moves_content_titles_only_and_keeps_one_rule(boxed_template, tmp_path):
    from pptx.util import Emu

    from deck_agent.design.render import RULE_NAME
    from deck_agent.revise.house import align_titles

    deck = _deck(boxed_template, tmp_path)
    prs = Presentation(str(deck))
    for k, s in enumerate(prs.slides):                 # scramble the titles, as a hand edit would
        if s.shapes.title is not None:
            s.shapes.title.top = Emu(int((0.2 + 0.15 * k) * 914400))
    messy = tmp_path / "messy.pptx"
    prs.save(str(messy))
    P = extract_profile(messy)
    content = [o["slide"] for o in deck_outline(Presentation(str(messy))) if o["role"] == "content"]
    out = tmp_path / "aligned.pptx"
    done = align_titles(messy, out, P, content)
    assert done["changed"] == content
    after = Presentation(str(out))
    tops = {round(after.slides[n - 1].shapes.title.top / 914400, 3) for n in content}
    assert tops == {round(P["title"]["box"][1], 3)}
    for n in content:
        assert sum(1 for sh in after.slides[n - 1].shapes if sh.name == RULE_NAME) == 1
    before, now = _slide_bytes(messy), _slide_bytes(out)
    assert before["ppt/slides/slide1.xml"] == now["ppt/slides/slide1.xml"], "the cover was touched"
    assert before["ppt/slides/slide2.xml"] == now["ppt/slides/slide2.xml"], "the section was touched"


def test_a_deck_task_defaults_to_content_slides(offline_settings, template, tmp_path):
    from deck_agent.infra.storage import store
    from deck_agent.revise.nodes import after_split, split

    uri = store().put(_deck(template, tmp_path), "deck-task/deck.pptx")

    class Fake:
        def split_edit(self, payload):
            return {"tasks": [{"kind": "align_titles"}, {"kind": "deck", "instruction": "titles navy"}]}

    state = {"thread_id": "deck-task", "run_id": "r1", "instruction": "x", "deck_versions": [uri]}
    out = split(state, planner=Fake(), log=lambda *a: None)
    for t in out["edit_tasks"]:
        assert t["slides"] == [3, 4, 5, 6] and t["left_alone"] == [1, 2]
    sends = after_split({**state, **out})
    assert [s.node for s in sends] == ["work"], "align_titles needs no model; only the deck task is sent"


# --------------------------------------------------------------------------- following the person

def test_every_task_carries_the_persons_own_words(offline_settings, template, tmp_path):
    from deck_agent.infra.storage import store
    from deck_agent.revise.nodes import split

    uri = store().put(_deck(template, tmp_path), "words/deck.pptx")
    said = "slide 4 bar graph text is a lil off, correct it"

    class Fake:
        def split_edit(self, payload):
            return {"tasks": [{"kind": "tweak", "slide": 4, "instruction": "Fix misspelled chart labels"}]}

    out = split({"thread_id": "words", "run_id": "r1", "instruction": said, "deck_versions": [uri]},
                planner=Fake(), log=lambda *a: None)
    assert out["edit_tasks"][0]["person_asked"] == said


def test_a_worker_sees_the_persons_words_beside_the_task(template, tmp_path):
    from deck_agent.revise.work import redesign

    seen = []

    class Fake:
        def redesign_slide(self, payload, png=None):
            seen.append(payload)
            return dict(SPIKE[0])

    deck = _deck(template, tmp_path)
    task = {"id": "t1", "kind": "redesign", "slide": 3, "instruction": "Fix misspelled labels",
            "person_asked": "the numbers on the chart look broken", "round": 0}
    redesign(task, deck, extract_profile(deck), Fake(), tmp_path / "w", attempts=1)
    assert seen[0]["request"] == "Fix misspelled labels"
    assert seen[0]["person_asked"] == "the numbers on the chart look broken"


def test_a_workers_view_of_a_chart_includes_how_it_is_drawn(template, tmp_path):
    out = tmp_path / "chart.pptx"
    render_deck(template, [SPIKE[0]], out)
    charts = [e["chart"] for e in slide_shapes(Presentation(str(out)).slides[0]) if e.get("chart")]
    fmt = charts[0]["format"]
    assert fmt["value_labels"]["shown"] is True and fmt["value_labels"]["font_pt"]
    assert "colour" in fmt["value_labels"] and "number_format" in fmt["value_labels"]
    assert "series_colours" in fmt and "value_axis" in fmt


def test_a_change_nobody_can_see_is_caught_by_code():
    import io

    from PIL import Image, ImageDraw

    from deck_agent.revise.nodes import barely_changed

    def png(draw_more=False):
        im = Image.new("RGB", (400, 225), "white")
        d = ImageDraw.Draw(im)
        d.rectangle([20, 20, 200, 120], fill="navy")
        if draw_more:
            d.text((40, 150), "a one-word edit", fill="black")
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    assert barely_changed(png(), png()) is True
    assert barely_changed(png(), png(draw_more=True)) is False


def test_slide_shapes_reports_arrowheads():
    from pptx import Presentation
    from deck_agent.design.pieces import line

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    line(slide, 1, 1, 3, 1, "000000", arrow="end")
    line(slide, 1, 2, 3, 2, "000000", arrow="both")
    line(slide, 1, 3, 3, 3, "000000")
    lines = [e for e in slide_shapes(slide) if "arrow" in e]
    assert [e["arrow"] for e in lines] == ["end", "both", "none"]
    assert lines[0]["from_in"] == [1.0, 1.0] and lines[0]["to_in"] == [3.0, 1.0]
