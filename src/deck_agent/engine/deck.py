"""Deterministic deck operations, in this process.

Everything here is a thin, typed wrapper over an engine function, plus the bits of
bookkeeping the engine's own orchestrator did inline and that all three flows need:

* `probe`      - read a deck into JSON (shapes, geometry, text, fingerprints)
* `apply_ops`  - bind-then-apply a batch of ops and save byte-preservingly
* `check`      - predicted text overflow, text overlap, structural problems (no rendering)
* `Baseline`   - differential filtering: only report what *this* edit made worse
* `changelog`  - what changed, in words, for the caller

None of it opens a socket or starts a process. Rendering, which does start a process
(LibreOffice), lives in `deck_agent.sandbox.render`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..errors import Boundary, PlanInvalid, Refused
from . import bootstrap

#: The engine's fingerprint field inside an op's `expect` block. (Note: the handoff
#: document's §7 example writes this as `sha`; the engine's `deckops/ops.py` requires
#: `text_sha`, and that is what the executor checks.)
EXPECT_FIELD = "text_sha"


# --------------------------------------------------------------------------- read

def probe(path: str | Path) -> dict:
    """Deck -> index.json. Every box with its id, geometry, font size, text and fingerprint."""
    bootstrap()
    from deckops.probe import probe as _probe

    return _probe(str(path))


def paragraphs(index: dict, slide_indices: set[int] | None = None) -> list[dict]:
    """The paragraphs of some slides, each with its ref, text, fingerprint and tagged runs."""
    bootstrap()
    from deckops.probe import iter_paragraphs

    return list(iter_paragraphs(index, slide_indices))


def slide_refs(index: dict) -> list[str]:
    return [s["ref"] for s in index["slides"]]


# --------------------------------------------------------------------------- write

def apply_ops(src: str | Path, ops: list[dict], dst: str | Path) -> dict:
    """Run a batch of ops. All-or-nothing: if any target is stale or missing, nothing changes.

    Raises `Refused` with the engine's reasons - that is the engine protecting the deck
    (usually a fingerprint that no longer matches, i.e. the deck moved under us), so the
    answer is to re-probe and re-plan, never to force it.
    """
    bootstrap()
    from pydantic import ValidationError

    from deckops.executor import ExecError, execute

    try:
        return {"ok": True, **execute(str(src), ops, str(dst))}
    except ExecError as e:
        raise Refused("the engine refused this batch of edits", e.errors) from None
    except ValidationError as e:
        # The op list did not match the engine's typed op language. That is a bad *plan*,
        # not broken infrastructure - it is feedable back to whoever produced the ops.
        raise PlanInvalid("these edits are not valid operations",
                          [f"{'.'.join(str(p) for p in d['loc'])}: {d['msg']}" for d in e.errors()[:8]]) from None
    except Exception as e:  # a crash in the engine is ours to report, not the model's
        raise Boundary(f"the engine failed while applying edits: {e.__class__.__name__}: {e}") from None


# --------------------------------------------------------------------------- check

def check(path: str | Path, slide_indices: set[int] | None = None) -> dict:
    """Predicted overflow, text overlap and structural problems. No rendering, no subprocess.

    -> {"findings": [...], "structural": [...]}
    """
    bootstrap()
    from deckops.validate import validate

    report = validate(str(path), slide_indices, "-", do_render=False)
    return {"findings": report["findings"], "structural": report["structural"]}


@dataclass
class Baseline:
    """The same checks, run on the deck (or the template's example slides) we started from.

    Real corporate templates already overflow and already overlap; absolute thresholds give
    false repairs, which is why the engine's design notes call differential validation out
    explicitly. `keep()` answers one question: did *we* make this worse?

    `slide_of` maps a finding's slide index in the *output* to the slide index in the
    baseline it should be compared against - identity for an edit or a translation, and
    "which example slide was this built from" when creating from a style kit.
    """

    overflow: dict = field(default_factory=dict)       # (slide_index, shape_id) -> ratio
    overlaps: set = field(default_factory=set)         # (slide_index, (id_a, id_b))
    structural: list = field(default_factory=list)
    tolerance: float = 1.03                            # the predictor is ~4% pessimistic

    @classmethod
    def of(cls, path: str | Path, slide_indices: set[int] | None = None) -> "Baseline":
        rep = check(path, slide_indices)
        return cls(
            overflow={(f["slide_index"], _shape_id(f["ref"])): f["ratio"]
                      for f in rep["findings"] if f["kind"] == "overflow"},
            overlaps={(f["slide_index"], tuple(f["shapes"]))
                      for f in rep["findings"] if f["kind"] == "overlap"},
            structural=rep["structural"],
        )

    def keep(self, finding: dict, slide_of=lambda f: f["slide_index"]) -> bool:
        base_slide = slide_of(finding)
        if finding["kind"] == "overlap":
            return (base_slide, tuple(finding["shapes"])) not in self.overlaps
        was = self.overflow.get((base_slide, _shape_id(finding["ref"])), 0)
        return finding["ratio"] > max(1.0, was) * self.tolerance

    def filter(self, report: dict, slide_of=lambda f: f["slide_index"]) -> dict:
        """-> the report with pre-existing problems removed."""
        return {
            "findings": [f for f in report["findings"] if self.keep(f, slide_of)],
            "structural": [p for p in report["structural"] if p not in self.structural],
            "pre_existing": {
                "findings": len(report["findings"]) - len([f for f in report["findings"] if self.keep(f, slide_of)]),
                "structural": self.structural,
            },
        }


def preserved_count(stats: dict) -> int:
    """How many parts kept their original bytes.

    The engine reports this two ways - `execute()` gives a count, `save_preserving()` gives the
    list of part names - so callers go through here rather than guessing.
    """
    value = (stats or {}).get("parts", stats or {}).get("preserved")
    if isinstance(value, int):
        return value
    return len(value or [])


def _shape_id(ref: str) -> str:
    """'slide:256/shape:9/para:0' -> '9'."""
    return ref.rsplit("shape:", 1)[-1].split("/")[0]


# --------------------------------------------------------------------------- explain

_SUMMARY = {
    "add_slide_from_pattern": "added a slide",
    "add_slide": "added a slide",
    "delete_slide": "removed a slide",
    "delete_shape": "removed a shape",
    "set_font_size": "changed a font size",
    "set_geometry": "moved or resized a shape",
    "set_doc_properties": "reset the file properties",
}


def changelog(index: dict, ops: list[dict]) -> list[dict]:
    """What a batch of ops does, in words, for the caller to read or approve.

    Text edits are shown before/after; everything else gets a one-line summary. The engine
    stores text as tagged runs (`<r0>Bonjour</r0>`), so tags are stripped for display.
    """
    bootstrap()
    from deckops.refs import parse_tagged

    before = {p["ref"]: (s["index"], p["text"]) for s in index["slides"]
              for p in paragraphs({"slides": [s]})}
    out = []
    for op in ops:
        kind = op.get("op")
        if kind in ("set_paragraph_text", "set_run_text"):
            slide_index, old = before.get(op["target"], (None, ""))
            out.append({"slide": (slide_index + 1) if slide_index is not None else None,
                        "ref": op["target"], "change": "text",
                        "before": old, "after": _plain(parse_tagged(op.get("value", "")))})
        else:
            entry = {"change": _SUMMARY.get(kind, kind), "op": kind}
            if op.get("target"):
                entry["ref"] = op["target"]
            if op.get("source"):
                entry["from"] = op["source"]
            if op.get("steps"):
                entry["steps"] = len(op["steps"])
            out.append(entry)
    return out


def _plain(tokens) -> str:
    """Tagged runs -> the text a reader sees."""
    parts = []
    for t in tokens:
        if t[0] == "r":
            parts.append(t[2])
        elif t[0] == "plain":
            parts.append(t[1])
        elif t[0] == "br":
            parts.append("\n")
    return "".join(parts)


# --------------------------------------------------------------------------- reseal

def reseal(src: str | Path, produced: str | Path, dst: str | Path) -> dict:
    """Re-apply byte preservation to a deck that something else wrote.

    Code written by a model goes through python-pptx, which re-serialises every part it opens.
    That would quietly change parts of the deck nobody asked to change, and the engine's
    seventh hard rule is that untouched slides stay byte-identical.

    So the deck the sandbox produced is opened here and saved through the engine's
    `save_preserving`, which compares every part with the original *by meaning* - canonical
    XML, relationships with resolved targets, effective content types - and keeps the original
    bytes wherever the meaning is unchanged. The result: the code's slide is new, and the rest
    of the deck is the file we started with, byte for byte.

    -> {"preserved": n, "changed": [...], "added": [...], "removed": [...]}
    """
    bootstrap()
    from pptx import Presentation

    from deckops.executor import save_preserving

    from . import guard_deck

    guard_deck(produced)                      # it came from untrusted code: check it like any upload
    prs = Presentation(str(produced))
    return save_preserving(prs, str(src), str(dst))


def new_slide_index(before: dict, after: dict) -> int | None:
    """Which slide in `after` was not in `before`, by slide id rather than by position.

    Needed because code written by a model may put its slide wherever it likes. Assuming it
    appended produced a genuinely scrambled deck once: the code had inserted its slide in the
    middle, and the host then moved a different slide - an existing one - to the target
    position. Identity is the only safe way to find it.
    """
    known = {s["ref"] for s in before["slides"]}
    for i, slide in enumerate(after["slides"]):
        if slide["ref"] not in known:
            return i
    return None


def move_slide(src: str | Path, frm: int, to: int, dst: str | Path) -> dict:
    """Move a slide to another position, byte-preservingly.

    Why this is here: the engine's op language can position a slide built from a *layout*
    (`add_slide` takes `after`), but a slide copied from an example slide
    (`add_slide_from_pattern`) is always appended. So "add a risks slide after slide 4" needs
    one more deterministic step, and this is it - a reorder of `p:sldIdLst`, nothing else,
    saved through the engine's part-by-part comparison so every slide's own XML is untouched.

    Both indices are 0-based positions in the current order.
    """
    bootstrap()
    from pptx import Presentation

    from deckops.executor import save_preserving

    prs = Presentation(str(src))
    lst = prs.slides._sldIdLst
    entries = list(lst)
    if not 0 <= frm < len(entries):
        raise PlanInvalid(f"cannot move slide {frm + 1}: the deck has {len(entries)} slides")
    to = max(0, min(to, len(entries) - 1))
    if to == frm:
        import shutil

        shutil.copy(src, dst)
        return {"moved": False, "preserved": None}
    element = entries[frm]
    lst.remove(element)
    lst.insert(to, element)
    stats = save_preserving(prs, str(src), str(dst))
    return {"moved": True, "from": frm, "to": to, **stats}
