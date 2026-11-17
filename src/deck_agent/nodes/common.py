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

    Called by: most graph nodes - design.nodes, revise.nodes, nodes.gather, nodes.intake,
        nodes.translate, nodes.look, nodes.deliver.
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

    Called by: design.nodes.build, revise.nodes.join, nodes.translate.translate_apply.
    """
    uri = ws.publish(local_name, next_version_name(state), versioned=True)
    return uri, [*(state.get("deck_versions") or []), uri]


def template_fingerprint(path: str | Path) -> str:
    """A template's content hash, recorded with every run so a deck says what it was built on.

    Called by: nodes.intake.intake.
    """
    return sha256_file(path)


def make_previews(state: DeckState, ws: Workspace, deck: Path,
                  pages: list[int] | None = None) -> tuple[list[str], str | None]:
    """Render previews and publish them. -> (uris, why there are none)

    Called by: design.nodes.build, revise.nodes.join, nodes.translate.translate_apply.
    """
    from ..sandbox.render import render

    result = render(deck, ws.dir("previews"), pages)
    if not result.ok:
        return [], result.skipped
    return ws.publish_dir("previews", "*.png"), None


def warnings_of(state: DeckState) -> list[str]:
    """The honest list of what is not right about the deck we are about to hand over.

    Called by: nodes.deliver.deliver, nodes.translate.after_translate.
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
