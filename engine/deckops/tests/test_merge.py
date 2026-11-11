"""Rules + vision: vision decides meaning, the rules decide structure, physics vetoes both."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from controlplane.kit_merge import merge  # noqa: E402
from deckops.autokit import draft_kit  # noqa: E402
from deckops.probe import probe  # noqa: E402


@pytest.fixture(scope="module")
def tpl(tmp_path_factory):
    d = tmp_path_factory.mktemp("kit")
    subprocess.run([sys.executable, os.path.join(ROOT, "samples/make_style_template.py"), str(d)], check=True,
                   capture_output=True)
    path = str(d / "example_style.pptx")
    return draft_kit(path, "t"), probe(path)


def _get(tpl, kind):
    kit, index = tpl
    p = kit["patterns"][kind]
    return p, index["slides"][p["slide"] - 1], len(index["slides"])


def test_no_vision_changes_nothing(tpl):
    p, slide, total = _get(tpl, "chart")
    name, out, dec = merge("chart", p, slide, None, p["slide"], total)
    assert (name, out) == ("chart", p) and not dec["vision"]


def test_vision_renames_but_physics_vetoes(tpl):
    p, slide, total = _get(tpl, "chart")
    f = p["fields"]
    vision = {"boxes": {str(f["side_text"]): "points",            # fine: a roomy box
                        str(f["footer"]): "body",                   # one-line box: vetoed
                        str(f["title"]): "page"}}                   # not a page number: vetoed
    name, out, dec = merge("chart", p, slide, vision, p["slide"], total)
    assert out["fields"]["points"] == f["side_text"]
    assert out["fields"]["footer"] == f["footer"] and out["fields"]["title"] == f["title"]
    assert len(dec["vetoed"]) == 2


def test_number_and_value_need_the_right_text(tpl):
    p, slide, total = _get(tpl, "columns")
    g0, fields = p["items"]["groups"][0], p["items"]["fields"]
    heading = g0[fields["heading"]]
    for wrong in ("number", "value"):                               # headings aren't 1,2,3 nor digits
        name, out, dec = merge("columns", p, slide, {"boxes": {str(heading): wrong}}, p["slide"], total)
        assert "heading" in out["items"]["fields"] and dec["vetoed"]


def test_kind_must_fit_the_content(tpl):
    p, slide, total = _get(tpl, "columns")
    name, _, dec = merge("columns", p, slide, {"kind": "steps"}, p["slide"], total)   # no numbers on it
    assert name == "columns" and any("kind" in v for v in dec["vetoed"])
    name, _, _ = merge("columns", p, slide, {"kind": "cards"}, p["slide"], total)      # cards fits
    assert name == "cards"


def test_vision_can_drop_a_slide_and_keep_decoration(tpl):
    p, slide, total = _get(tpl, "chart")
    assert merge("chart", p, slide, {"offer": False}, p["slide"], total)[0] is None
    page = p["fields"]["page"]                                       # short text: may stay as decoration
    _, out, _ = merge("chart", p, slide, {"boxes": {str(page): "fixed"}}, p["slide"], total)
    assert page in out["keep"] and "page" not in out["fields"]
    long_box = p["fields"]["side_text"]                             # long text: would leak template content
    _, out, dec = merge("chart", p, slide, {"boxes": {str(long_box): "fixed"}}, p["slide"], total)
    assert long_box not in out.get("keep", []) and dec["vetoed"]
