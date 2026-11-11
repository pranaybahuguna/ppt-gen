"""The automatic style kit: it must label the example template the same way a hand-written kit does,
catch a wrongly labelled kit, and never let a picture from an example slide into a new deck."""
import io
import json
import os
import subprocess
import sys

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from compare_kits import compare  # noqa: E402
from controlplane.kit_check import role_problems  # noqa: E402
from deckops.autokit import draft_kit  # noqa: E402
from deckops.executor import execute  # noqa: E402
from deckops.patterns import compile_from_kit, leaked_text  # noqa: E402
from deckops.probe import probe  # noqa: E402


@pytest.fixture(scope="module")
def kit_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("kit")
    subprocess.run([sys.executable, os.path.join(ROOT, "samples/make_style_template.py"), str(d)], check=True,
                   capture_output=True)
    return d


def _png(colour):
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), colour).save(buf, "PNG")
    buf.seek(0)
    return buf


def test_auto_kit_matches_hand_kit(kit_dir):
    ref = json.load(open(kit_dir / "example_style_kit.json"))
    _, rows = compare(str(kit_dir / "example_style.pptx"), ref)
    assert all(r[5] for r in rows), [r for r in rows if not r[5]]


def test_mislabelled_long_field_is_caught(kit_dir):
    index = probe(str(kit_dir / "example_style.pptx"))
    kit = json.load(open(kit_dir / "example_style_kit.json"))
    pat = dict(kit["patterns"]["chart"])
    # swap: call the one-line footer box "commentary"
    pat["fields"] = {**pat["fields"], "commentary": pat["fields"]["footer"], "footer": pat["fields"]["commentary"]}
    shapes = {int(s["ref"].rsplit("shape:", 1)[1]): s for s in index["slides"][pat["slide"] - 1]["shapes"]}
    assert role_problems(kit["patterns"]["chart"], shapes) == []
    assert any("commentary" in p for p in role_problems(pat, shapes))


def test_logo_kept_content_picture_removed(kit_dir, tmp_path):
    prs = Presentation(str(kit_dir / "example_style.pptx"))
    for slide in prs.slides:                                   # same small logo on every slide
        slide.shapes.add_picture(_png("red"), Inches(12), Inches(0.2), Inches(1))
    prs.slides[1].shapes.add_picture(_png("blue"), Inches(1), Inches(2), Inches(6))   # a "diagram" on one slide
    src = str(tmp_path / "with_pictures.pptx")
    prs.save(src)

    kit = draft_kit(src, "with_pictures.pptx")
    index = probe(src)
    pat_name, pat = next((n, p) for n, p in kit["patterns"].items() if p["slide"] == 2)
    assert len(pat.get("keep", [])) == 1                       # the logo, not the diagram

    n = len(pat["items"]["groups"]) if "items" in pat else 0
    plan = {"slides": [{"pattern": pat_name,
                        "fields": {f: "New text" for f in pat["fields"] if f not in ("page", "footer")},
                        **({"items": [{f: "Item" for f in pat["items"]["fields"]}] * n} if n else {})}]}
    out = str(tmp_path / "out.pptx")
    execute(src, compile_from_kit(plan, index, kit), out)
    out_index = probe(out)
    pics = [s for s in out_index["slides"][0]["shapes"] if s.get("kind") == "picture"]
    assert len(pics) == 1                                      # logo survives
    assert leaked_text(index, kit, plan, out_index) == []      # diagram did not
