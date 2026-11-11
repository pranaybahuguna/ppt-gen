import os
import sys

import pytest
from pptx import Presentation
from pptx.util import Pt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deckops.compiler import CompileError, compile_create, compile_translate, pick_layout  # noqa: E402
from deckops.executor import ExecError, execute  # noqa: E402
from deckops.probe import iter_paragraphs, layout_catalog, probe  # noqa: E402
from deckops.refs import Ref, parse_tagged  # noqa: E402


@pytest.fixture
def deck(tmp_path):
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Hello"
    p = s.placeholders[1].text_frame.paragraphs[0]
    for i, t in enumerate(["Revenue grew ", "12%", " YoY"]):
        r = p.add_run()
        r.text = t
        if i == 1:
            r.font.bold = True
            r.font.size = Pt(30)
    s.placeholders[1].text_frame.add_paragraph().text = "Second <para> & co"
    path = str(tmp_path / "d.pptx")
    prs.save(path)
    return path


def _para(index, text):
    return next(p for p in iter_paragraphs(index) if p["text"] == text)


def test_ref_roundtrip():
    for s in ["slide:256", "slide:256/shape:4/para:0/run:1", "slide:256/shape:9/cell:2,3/para:0", "slide:256/notes/para:1"]:
        assert str(Ref.parse(s)) == s


def test_tagged_escaping(deck):
    idx = probe(deck)
    p = _para(idx, "Second <para> & co")
    assert p["tagged"] == "<r0>Second &lt;para&gt; &amp; co</r0>"
    assert parse_tagged(p["tagged"]) == [("r", 0, "Second <para> & co")]


def test_reorder_keeps_run_formatting(deck, tmp_path):
    idx = probe(deck)
    p = _para(idx, "Revenue grew 12% YoY")
    plan = {"paragraphs": [{"ref": p["ref"], "tagged": "<r1>+12 %</r1><r0> de croissance</r0><r2> sur un an</r2>"}]}
    out = str(tmp_path / "o.pptx")
    execute(deck, compile_translate(plan, idx), out)
    runs = Presentation(out).slides[0].placeholders[1].text_frame.paragraphs[0].runs
    assert [r.text for r in runs] == ["+12 %", " de croissance", " sur un an"]
    assert runs[0].font.bold and runs[0].font.size == Pt(30)      # formatting travelled with the run
    assert not runs[1].font.bold


def test_compile_rejects_invented_tags(deck):
    idx = probe(deck)
    p = _para(idx, "Revenue grew 12% YoY")
    with pytest.raises(CompileError):
        compile_translate({"paragraphs": [{"ref": p["ref"], "tagged": "<r7>x</r7>"}]}, idx)
    with pytest.raises(CompileError):
        compile_translate({"paragraphs": [{"ref": p["ref"], "tagged": "loose <r0>x</r0>"}]}, idx)


def test_unchanged_paragraph_emits_no_op(deck):
    idx = probe(deck)
    p = _para(idx, "Hello")
    assert compile_translate({"paragraphs": [{"ref": p["ref"], "tagged": p["tagged"]}]}, idx) == []


def test_stale_expect_refuses_whole_batch(deck, tmp_path):
    idx = probe(deck)
    a, b = _para(idx, "Hello"), _para(idx, "Revenue grew 12% YoY")
    ops = [{"op": "set_paragraph_text", "target": a["ref"], "expect": {"text_sha": a["sha"]}, "value": "<r0>Bonjour</r0>"},
           {"op": "set_paragraph_text", "target": b["ref"], "expect": {"text_sha": "deadbeef00"}, "value": "<r0>x</r0>"}]
    out = str(tmp_path / "o.pptx")
    with pytest.raises(ExecError):
        execute(deck, ops, out)
    assert not os.path.exists(out)


def test_layout_picking_by_role_shape():
    cat = layout_catalog(Presentation())
    assert pick_layout("title", cat)["name"] == "Title Slide"
    assert pick_layout("title_and_body", cat)["name"] == "Title and Content"
    assert pick_layout("two_content", cat)["name"] == "Two Content"
    assert pick_layout("section", cat)["name"] == "Section Header"


def test_create_fails_loudly_on_missing_role():
    cat = layout_catalog(Presentation())
    plan = {"slides": [{"layout_family": "title_only", "blocks": [
        {"role": "title", "content": {"kind": "text", "text": "a"}},
        {"role": "body", "content": {"kind": "text", "text": "b"}}]}]}
    with pytest.raises(CompileError):
        compile_create(plan, cat)
