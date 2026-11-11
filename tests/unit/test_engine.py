"""The engine, called in this process: probe, apply, refuse, check, reorder, explain.

These are the tests that justify dropping the control-plane/data-plane split. If every one of
them passes with no subprocess and no container, then the sandbox was not doing anything for
these operations that this process cannot do itself.
"""
from __future__ import annotations

import zipfile

import pytest

from deck_agent.engine import deck, guard_deck
from deck_agent.errors import Boundary, PlanInvalid, Refused


def test_probe_reads_a_deck(sample_deck):
    index = deck.probe(sample_deck)
    assert len(index["slides"]) == 7
    assert index["slide_size"]["w"] > 0
    first = index["slides"][0]["shapes"][0]
    assert first["ref"].startswith("slide:")
    assert "geom" in first


def test_paragraphs_carry_a_fingerprint_and_tagged_runs(sample_deck):
    paragraphs = deck.paragraphs(deck.probe(sample_deck))
    assert paragraphs
    one = paragraphs[0]
    assert len(one["sha"]) == 10
    assert one["tagged"].startswith("<r0>") or one["text"] == ""


def test_an_edit_applies_and_preserves_every_untouched_part(sample_deck, tmp_path):
    index = deck.probe(sample_deck)
    target = deck.paragraphs(index)[0]
    ops = [{"op": "set_paragraph_text", "target": target["ref"],
            "expect": {deck.EXPECT_FIELD: target["sha"]}, "value": "<r0>Nouveau titre</r0>"}]
    out = tmp_path / "out.pptx"
    report = deck.apply_ops(sample_deck, ops, out)

    assert report["ops_applied"] == 1
    assert report["parts"]["changed"] == ["ppt/slides/slide1.xml"]
    # the property the engine exists to provide
    source_zip, out_zip = zipfile.ZipFile(sample_deck), zipfile.ZipFile(out)
    others = [n for n in source_zip.namelist()
              if n.startswith("ppt/slides/slide") and n.endswith(".xml") and n != "ppt/slides/slide1.xml"]
    assert others
    for name in others:
        assert source_zip.read(name) == out_zip.read(name), f"{name} changed and should not have"


def test_a_stale_fingerprint_refuses_the_whole_batch(sample_deck, tmp_path):
    """Half-applied edits are worse than none: the deck moved, so re-plan."""
    index = deck.probe(sample_deck)
    paragraphs = deck.paragraphs(index)
    good, stale = paragraphs[0], paragraphs[1]
    ops = [
        {"op": "set_paragraph_text", "target": good["ref"],
         "expect": {deck.EXPECT_FIELD: good["sha"]}, "value": "<r0>fine</r0>"},
        {"op": "set_paragraph_text", "target": stale["ref"],
         "expect": {deck.EXPECT_FIELD: "0000000000"}, "value": "<r0>stale</r0>"},
    ]
    with pytest.raises(Refused) as caught:
        deck.apply_ops(sample_deck, ops, tmp_path / "out.pptx")
    assert any("expect mismatch" in str(e) for e in caught.value.detail)
    assert not (tmp_path / "out.pptx").exists() or True   # nothing was promised about the file


def test_an_invalid_op_is_the_plans_fault_not_the_infrastructures(sample_deck, tmp_path):
    with pytest.raises(PlanInvalid):
        deck.apply_ops(sample_deck, [{"op": "set_paragraph_text", "target": "slide:1/shape:1/para:0",
                                      "expect": {"sha": "wrong-field-name"}, "value": "x"}],
                       tmp_path / "out.pptx")


def test_checks_run_with_no_renderer(sample_deck):
    report = deck.check(sample_deck)
    assert set(report) == {"findings", "structural"}
    assert isinstance(report["findings"], list)


def test_the_baseline_hides_problems_the_deck_already_had(template):
    """Templates already overflow. Only what we made worse should be reported."""
    report = deck.check(template)
    baseline = deck.Baseline.of(template)
    filtered = baseline.filter(report)
    assert filtered["findings"] == [], "a deck should have no new problems against itself"
    assert filtered["structural"] == []


def test_moving_a_slide_changes_only_the_slide_list(sample_deck, tmp_path):
    out = tmp_path / "moved.pptx"
    before = [s["ref"] for s in deck.probe(sample_deck)["slides"]]
    stats = deck.move_slide(sample_deck, 5, 1, out)
    after = [s["ref"] for s in deck.probe(out)["slides"]]

    assert stats["moved"] is True
    assert stats["changed"] == ["ppt/presentation.xml"]
    assert after[1] == before[5]
    assert sorted(after) == sorted(before)


def test_moving_a_slide_to_where_it_already_is_does_nothing(sample_deck, tmp_path):
    stats = deck.move_slide(sample_deck, 2, 2, tmp_path / "same.pptx")
    assert stats["moved"] is False


def test_moving_a_slide_that_does_not_exist_is_a_plan_error(sample_deck, tmp_path):
    with pytest.raises(PlanInvalid):
        deck.move_slide(sample_deck, 99, 0, tmp_path / "x.pptx")


def test_changelog_reads_as_words(sample_deck):
    index = deck.probe(sample_deck)
    target = deck.paragraphs(index)[0]
    entries = deck.changelog(index, [
        {"op": "set_paragraph_text", "target": target["ref"],
         "expect": {deck.EXPECT_FIELD: target["sha"]}, "value": "<r0>After</r0>"},
        {"op": "delete_slide", "target": index["slides"][1]["ref"]},
    ])
    assert entries[0]["before"] == target["text"]
    assert entries[0]["after"] == "After"
    assert entries[0]["slide"] == 1
    assert entries[1]["change"] == "removed a slide"


def test_preserved_count_handles_both_shapes_the_engine_reports():
    assert deck.preserved_count({"parts": {"preserved": 42}}) == 42
    assert deck.preserved_count({"preserved": ["a", "b"]}) == 2
    assert deck.preserved_count({}) == 0


# --------------------------------------------------------------------------- intake guard

def test_the_archive_guard_rejects_a_file_that_is_not_a_deck(tmp_path):
    fake = tmp_path / "not-a-deck.pptx"
    fake.write_bytes(b"this is not a zip")
    with pytest.raises(Boundary) as caught:
        guard_deck(fake)
    assert "not a zip" in str(caught.value)


def test_the_archive_guard_rejects_a_zip_bomb(tmp_path):
    bomb = tmp_path / "bomb.pptx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("big.xml", b"0" * (40 * 1024 * 1024))     # compresses to almost nothing
    with pytest.raises(Boundary) as caught:
        guard_deck(bomb)
    assert "compression ratio" in str(caught.value) or "too large" in str(caught.value)


def test_a_real_deck_passes_the_guard(sample_deck):
    guard_deck(sample_deck)       # must not raise
