"""Helpers every node needs: ids, workspaces, publishing a version, previews, style.

A node's job is one step of the work plus a state update. These take care of the parts that
would otherwise be copied into each of them, and they hold two rules in one place:

* a new version of the deck is **appended**, never overwritten - that is what makes undo and
  "compare with what you had" possible at all;
* a node that touches bytes does so in a temporary workspace and publishes what matters, so
  the state holds object URIs and nothing else.
"""
from __future__ import annotations

import re

import time
import uuid
from pathlib import Path

from ..infra.storage import sha256_file
from ..infra.workspace import Workspace, open_workspace
from ..settings import settings
from ..state import DeckState


def new_run_id() -> str:
    """A new run id, `YYYYmmdd-HHMMSS-xxxxxx`.

    Called by: intake and workspace(), when the state has none yet.
    """
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def workspace(state: DeckState, keep: bool | None = None):
    """A scratch directory for this node, with the store attached.

    Called by: nodes.create.assemble, nodes.create.outline, nodes.create.render_slides,
        nodes.create.repair, nodes.custom_code.custom_code, nodes.deliver.deliver,
        nodes.edit.edit_apply, and 8 more.
    """
    s = settings()
    return open_workspace(state["thread_id"], state.get("run_id") or new_run_id(), s,
                          keep=bool(keep))




def version_number(uri: str | None) -> int | None:
    """The number in a version's file name: `.../v003.pptx` -> 3; None for anything else."""
    m = re.search(r"/v(\d+)\.pptx$", uri or "")
    return int(m.group(1)) if m else None


def next_version_name(state: DeckState) -> str:
    """The file name the next deck version gets: one more than the newest, v001.pptx, v002.pptx ...

    Numbered from the newest version, not by counting them: with keep_versions, older versions
    are deleted, and counting would hand out a name that is still in use.

    Called by: publish_version.
    """
    versions = state.get("deck_versions") or []
    newest = version_number(versions[-1]) if versions else 0
    return f"v{(newest if newest is not None else len(versions)) + 1:03d}.pptx"


def publish_version(state: DeckState, ws: Workspace, local_name: str) -> tuple[str, list[str]]:
    """Publish a built deck as the next version. -> (uri, the new deck_versions list)

    Called by: nodes.create.assemble, nodes.custom_code.custom_code, nodes.edit.edit_apply,
        nodes.translate.translate_apply.
    """
    uri = ws.publish(local_name, next_version_name(state), versioned=True)
    return uri, [*(state.get("deck_versions") or []), uri]


def template_fingerprint(path: str | Path) -> str:
    """A template's content hash. The kit cache key: the same template never pays twice.

    Called by: nodes.intake.intake, nodes.kit.ensure_kit.
    """
    return sha256_file(path)


def make_previews(state: DeckState, ws: Workspace, deck: Path,
                  pages: list[int] | None = None) -> tuple[list[str], str | None]:
    """Render previews and publish them. -> (uris, why there are none)

    Called by: nodes.create.assemble, nodes.custom_code.custom_code, nodes.edit.edit_apply,
        nodes.translate.translate_apply.
    """
    from ..sandbox.render import render

    result = render(deck, ws.dir("previews"), pages)
    if not result.ok:
        return [], result.skipped
    return ws.publish_dir("previews", "*.png"), None


def style_brief(kit: dict, index: dict) -> dict:
    """What model-written code is allowed to know about the template's look.

    Deliberately small: a palette, a font, a text colour and two type sizes. Enough to draw a
    slide that belongs with the others, and not enough to reproduce the template's layout,
    which is the deterministic path's job.

    Called by: nodes.custom_code.custom_code; tests/validate_real_model.py.
    """
    chart = kit.get("chart_style", {}) or {}
    # Type sizes: the median is body text. The *largest* size in a template is usually the
    # cover's 60pt headline, which would be absurd on a content slide - so a title is the
    # median of the upper half, and capped. Getting this wrong is not cosmetic: an oversized
    # title overflows its box and the checks bounce the slide straight back.
    title_pt, body_pt = 28.0, 14.0
    sizes = sorted(sh["font_pt"] for sl in index["slides"] for sh in sl["shapes"] if sh.get("font_pt"))
    if sizes:
        body_pt = float(sizes[len(sizes) // 2])
        upper = sizes[len(sizes) // 2:]
        title_pt = min(44.0, max(body_pt * 1.4, float(upper[len(upper) // 2])))
    return {
        "palette": chart.get("palette") or ["1F4E79", "2E75B6", "9DC3E6"],
        "font": chart.get("font") or "Calibri",
        "text_colour": chart.get("text_colour") or "374151",
        "grid_colour": chart.get("grid_colour") or "E5E7EB",
        "title_pt": round(float(title_pt), 1),
        "body_pt": round(float(body_pt), 1),
    }


def design_frame(kit: dict, template_index: dict) -> dict:
    """Where a designed slide is drawn: the template's own content-slide frame.

    A slide drawn on a blank layout looks pasted in from another deck - no header band, no
    footer, no logo. So it is drawn on the layout the template's content slides actually use
    (their header, title box and footer come with it), and inside the area those slides keep
    for content: the union of every content box on the kit's example slides, minus the title
    and the page number.

    -> {"chrome_layout": {"master": m, "layout": l, "name": ...} | None,
        "body_box": {"x", "y", "w", "h"} in EMU}

    Called by: nodes.custom_code.custom_code.
    """
    from collections import Counter

    slides = template_index["slides"]
    content_patterns = [p for name, p in (kit.get("patterns") or {}).items()
                        if name.split("@")[0] not in ("cover", "closing")]
    names = Counter(slides[p["slide"] - 1].get("layout") for p in content_patterns
                    if 0 < p["slide"] <= len(slides))
    chrome = None
    if names:
        wanted = names.most_common(1)[0][0]
        for lay in template_index.get("layouts") or []:
            if lay.get("name") == wanted:
                m = re.match(r"M(\d+)L(\d+)", str(lay.get("id", "")))
                if m:
                    chrome = {"master": int(m.group(1)), "layout": int(m.group(2)), "name": wanted}
                break

    from ..engine.tidy import _all_shapes

    size = template_index["slide_size"]
    xs, ys, x2s, y2s = [], [], [], []
    for p in content_patterns:
        if not 0 < p["slide"] <= len(slides):
            continue
        shapes = _all_shapes(slides[p["slide"] - 1])
        ids = [sid for name, sid in p["fields"].items() if name not in ("title", "page", "footer")]
        if "items" in p:
            ids += [sid for group in p["items"]["groups"] for sid in group]
        for sid in ids:
            g = (shapes.get(sid) or {}).get("geom")
            if g and g["w"] and g["h"]:
                xs.append(g["x"]); ys.append(g["y"])
                x2s.append(g["x"] + g["w"]); y2s.append(g["y"] + g["h"])
    if xs:
        # Inside the margins the template's own title keeps, and clear of its footer line: a
        # kit's boxes can run nearer the edge than a drawn diagram should.
        margin = int(size["w"] * 0.035)
        foot = int(size["h"] * 0.9)
        left, right = max(min(xs), margin), min(max(x2s), size["w"] - margin)
        top, bottom = min(ys), min(max(y2s), foot)
        box = {"x": left, "y": top, "w": right - left, "h": bottom - top}
    else:                                         # no kit to learn from: a sensible default
        box = {"x": int(size["w"] * 0.06), "y": int(size["h"] * 0.2),
               "w": int(size["w"] * 0.88), "h": int(size["h"] * 0.68)}
    return {"chrome_layout": chrome, "body_box": box}


def blank_layout_index(index: dict) -> int:
    """A layout with no placeholders, for code-written slides to draw on.

    Called by: nodes.custom_code.custom_code; tests/validate_real_model.py.
    """
    for i, lay in enumerate(index.get("layouts") or []):
        if not lay.get("placeholders"):
            return i
    return 6                                    # python-pptx's default template: 6 is "Blank"


def warnings_of(state: DeckState) -> list[str]:
    """The honest list of what is not right about the deck we are about to hand over.

    Called by: nodes.create.after_assemble, nodes.deliver.deliver,
        nodes.translate.after_translate.
    """
    out = []
    for f in state.get("findings") or []:
        if f["kind"] == "overflow":
            out.append(f"slide {f['slide_index'] + 1}: text may not fit its box "
                       f"(needs {f['ratio']:.2f}x the space)")
        elif f["kind"] == "overlap":
            out.append(f"slide {f['slide_index'] + 1}: two text boxes overlap")
        elif f["kind"] == "appearance":
            out.append(f"slide {f['slide_index'] + 1}: {f['what']}"
                       + (f" ({f['where']})" if f.get("where") else ""))
        else:
            out.append(f"slide {f['slide_index'] + 1}: {f['kind']}")
    out += [f"structural: {p}" for p in state.get("structural") or []]
    out += [f"template content left in the deck: {t}" for t in state.get("leaked") or []]
    # Advisory: what the vision model noticed but was not asked to act on. Worth telling the
    # person, not worth rewriting a slide over.
    out += [f"noticed: {n}" for n in state.get("vision_notes") or []]
    return out
