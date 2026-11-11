"""Helpers every node needs: ids, workspaces, publishing a version, previews, style.

A node's job is one step of the work plus a state update. These take care of the parts that
would otherwise be copied into each of them, and they hold two rules in one place:

* a new version of the deck is **appended**, never overwritten - that is what makes undo and
  "compare with what you had" possible at all;
* a node that touches bytes does so in a temporary workspace and publishes what matters, so
  the state holds object URIs and nothing else.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from ..infra.storage import sha256_file, store
from ..infra.workspace import Workspace, open_workspace
from ..settings import settings
from ..state import DeckState


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def workspace(state: DeckState, keep: bool | None = None):
    """A scratch directory for this node, with the store attached."""
    s = settings()
    return open_workspace(state["thread_id"], state.get("run_id") or new_run_id(), s,
                          keep=bool(keep))


def fetch_current(state: DeckState, ws: Workspace, name: str = "source.pptx") -> Path:
    """The deck we are working on, on local disk."""
    from ..state import current_deck

    uri = current_deck(state) or state.get("source_deck")
    if not uri:
        raise ValueError("there is no deck in this session to work on")
    return ws.fetch(uri, name)


def next_version_name(state: DeckState) -> str:
    return f"v{len(state.get('deck_versions') or []) + 1:03d}.pptx"


def publish_version(state: DeckState, ws: Workspace, local_name: str) -> tuple[str, list[str]]:
    """Publish a built deck as the next version. -> (uri, the new deck_versions list)"""
    uri = ws.publish(local_name, next_version_name(state), versioned=True)
    return uri, [*(state.get("deck_versions") or []), uri]


def template_fingerprint(path: str | Path) -> str:
    """A template's content hash. The kit cache key: the same template never pays twice."""
    return sha256_file(path)


def make_previews(state: DeckState, ws: Workspace, deck: Path,
                  pages: list[int] | None = None) -> tuple[list[str], str | None]:
    """Render previews and publish them. -> (uris, why there are none)"""
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


def blank_layout_index(index: dict) -> int:
    """A layout with no placeholders, for code-written slides to draw on."""
    for i, lay in enumerate(index.get("layouts") or []):
        if not lay.get("placeholders"):
            return i
    return 6                                    # python-pptx's default template: 6 is "Blank"


def warnings_of(state: DeckState) -> list[str]:
    """The honest list of what is not right about the deck we are about to hand over."""
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
