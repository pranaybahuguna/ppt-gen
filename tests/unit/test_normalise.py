"""Making a real template usable: duplicate shape ids.

The first real commercial template this project was given had fifteen of its twenty slides
refused with "shapes share ids". The engine was right to refuse - its addressing is
`slide:<sldId>/shape:<id>` and two shapes with one id cannot be told apart - but every
duplicate turned out to be inside an embedded think-cell object, where nothing addresses it.
Among the top-level shapes, which are the only ones the engine ever touches, there were none.

The property that makes this safe is narrow and absolute: **a top-level shape never has its id
changed.** The kit addresses those ids, and an edit planned against one must not land on
another.
"""
from __future__ import annotations

import zipfile

import pytest

from deck_agent.engine import bootstrap, deck
from deck_agent.engine.normalise import _is_top_level, shape_id_problems, unique_shape_ids


def top_level_ids(path):
    bootstrap()
    from pptx import Presentation

    return [[s.shape_id for s in slide.shapes] for slide in Presentation(str(path)).slides]


def test_a_clean_deck_reports_no_problems(sample_deck):
    assert shape_id_problems(sample_deck) == {}


def test_a_clean_deck_comes_through_untouched(sample_deck, tmp_path):
    out = tmp_path / "out.pptx"
    report = unique_shape_ids(sample_deck, out)
    assert report["ids_renumbered"] == 0
    assert report["slides_touched"] == []

    before, after = zipfile.ZipFile(sample_deck), zipfile.ZipFile(out)
    slides = [n for n in before.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
    assert slides
    for name in slides:
        assert before.read(name) == after.read(name), f"{name} was rewritten for no reason"


@pytest.fixture
def deck_with_a_duplicate(sample_deck, tmp_path):
    """A deck whose first slide has a nested duplicate id, as think-cell leaves behind."""
    bootstrap()
    from pptx import Presentation
    from pptx.oxml.ns import qn

    prs = Presentation(str(sample_deck))
    slide = prs.slides[0]
    tree = slide.shapes._spTree
    nested = next((e for e in tree.iter(qn("p:cNvPr")) if not _is_top_level(e, tree)), None)
    if nested is None:
        pytest.skip("this sample deck has no nested shape to plant a duplicate in")
    nested.set("id", str(list(slide.shapes)[0].shape_id))
    out = tmp_path / "dup.pptx"
    prs.save(str(out))
    return out


def test_the_duplicate_is_found(deck_with_a_duplicate):
    problems = shape_id_problems(deck_with_a_duplicate)
    assert 1 in problems, "the planted duplicate on slide 1 should be reported"


def test_the_duplicate_is_removed(deck_with_a_duplicate, tmp_path):
    out = tmp_path / "fixed.pptx"
    report = unique_shape_ids(deck_with_a_duplicate, out)
    assert report["ids_renumbered"] >= 1
    assert shape_id_problems(out) == {}


def test_a_nested_duplicate_never_costs_a_top_level_shape_its_id(deck_with_a_duplicate, tmp_path):
    """The property the whole pass rests on, for the case it exists to handle."""
    out = tmp_path / "fixed.pptx"
    report = unique_shape_ids(deck_with_a_duplicate, out)
    assert report["renumbered_top_level_shapes"] == 0
    assert top_level_ids(deck_with_a_duplicate) == top_level_ids(out)


def test_nothing_visible_changes(deck_with_a_duplicate, tmp_path):
    out = tmp_path / "fixed.pptx"
    unique_shape_ids(deck_with_a_duplicate, out)

    before, after = deck.probe(deck_with_a_duplicate), deck.probe(out)
    assert len(before["slides"]) == len(after["slides"])
    text = lambda ix: sorted(p["text"] for s in ix["slides"]
                             for sh in s["shapes"] for p in sh.get("paras", []))
    geom = lambda ix: [sh.get("geom") for s in ix["slides"] for sh in s["shapes"]]
    assert text(before) == text(after)
    assert geom(before) == geom(after)


def test_embedded_parts_are_byte_identical(deck_with_a_duplicate, tmp_path):
    """The duplicates live inside embedded objects; their payloads must not be disturbed."""
    out = tmp_path / "fixed.pptx"
    unique_shape_ids(deck_with_a_duplicate, out)

    before, after = zipfile.ZipFile(deck_with_a_duplicate), zipfile.ZipFile(out)
    media = [n for n in before.namelist()
             if n.startswith(("ppt/embeddings/", "ppt/media/"))]
    for name in media:
        assert name in after.namelist(), f"{name} was dropped"
        assert before.read(name) == after.read(name), f"{name} was rewritten"


# --------------------------------------------------------------------------- prepare()

def test_preparing_a_clean_template_is_a_byte_copy(sample_deck, tmp_path):
    """The cache key is the template's content hash.

    Round-tripping a clean template through python-pptx changes the zip container even when
    every part survives, so its hash would change and it would be learned again every single
    time. A template that needs nothing must come through untouched.
    """
    from deck_agent.engine.normalise import prepare

    out = tmp_path / "prepared.pptx"
    report = prepare(sample_deck, out)
    assert report["changed"] is False
    assert out.read_bytes() == sample_deck.read_bytes()


def test_preparing_is_idempotent(deck_with_a_duplicate, tmp_path):
    """Preparing an already-prepared template must be a no-op, hash included."""
    from deck_agent.engine.normalise import prepare

    once, twice = tmp_path / "1.pptx", tmp_path / "2.pptx"
    first = prepare(deck_with_a_duplicate, once)
    second = prepare(once, twice)

    assert first["changed"] is True and first["ids_renumbered"] >= 1
    assert second["changed"] is False
    assert once.read_bytes() == twice.read_bytes()


def test_prepare_reports_what_it_found(deck_with_a_duplicate, tmp_path):
    from deck_agent.engine.normalise import prepare

    report = prepare(deck_with_a_duplicate, tmp_path / "out.pptx")
    assert report["duplicate_ids_before"], "a caller should be able to see what was wrong"
    assert report["slides_touched"]
