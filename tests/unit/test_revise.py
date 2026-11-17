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
