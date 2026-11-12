"""Build a deck from a plan, and say what is wrong with the result.

One function, `build_from_kit`, is the whole deterministic loop: compile the plan into ops,
apply them, re-probe the result, check it against the template's own baseline, and scan for
leaked example content. It is used by

* the create flow (a plan from the model),
* kit building (plans made of placeholder text, to accept patterns and measure capacity),
* the model-written-code flow (to check whatever the code produced, with the same checks).

`repair_feedback` turns findings into sentences a model can act on. The important detail,
learned the hard way by the engine's author: text wraps by whole lines, so "make it 10%
shorter" does nothing - the model needs a character budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..errors import PlanInvalid
from . import bootstrap
from .deck import Baseline, check, preserved_count, probe


@dataclass
class BuildResult:
    """What building a deck from a plan produced: the file, the operations, and what the checks
    found.

    Returned by: build_from_kit; read by assemble and the edit route's rebuild.
    """
    out_path: Path
    ops: list[dict]
    exec: dict
    findings: list[dict]
    structural: list[str]
    leaked: list[str]
    out_index: dict
    pre_existing: dict = field(default_factory=dict)
    tidied: dict = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """True when the build has no findings, no structural problems and no leaked template text.
        """
        return not (self.findings or self.structural or self.leaked)

    def summary(self) -> dict:
        """A short dict for logs and the trace: slides, ops, findings, parts preserved, what was
        tidied.

        Called by: assemble, the rebuild in edit._rebuild_with_layouts.
        """
        return {"slides": len(self.out_index["slides"]), "ops": len(self.ops),
                "findings": len(self.findings), "structural": self.structural,
                "leaked_example_text": self.leaked,
                "parts_preserved": preserved_count(self.exec),
                **{k: v for k, v in self.tidied.items() if v}}


# --------------------------------------------------------------------------- compile

def compile_plan(plan: dict, index: dict, kit: dict, content: dict | None = None) -> list[dict]:
    """Plan -> ops. Pure. Raises `PlanInvalid` with messages written to be fed back to a model.

    Called by: build_from_kit, engine.kitbuild.accept, engine.kitbuild.capacity,
        nodes.edit._change_layouts, nodes.edit._insert_slides.
    """
    bootstrap()
    from deckops.compiler import CompileError
    from deckops.patterns import compile_from_kit

    try:
        return compile_from_kit(plan, index, kit, content or {})
    except CompileError as e:
        raise PlanInvalid("this plan does not fit the template", list(e.args[0])) from None


def example_slide_of(plan: dict, kit: dict):
    """A new deck's slide N was copied from some example slide of the template. Findings must be
    compared against *that* slide, or every quirk of the template is blamed on the new deck.

    Called by: build_from_kit.
    """
    def resolve(finding):
        """Map one finding on the new deck to the template slide that slide was copied from."""
        i = finding["slide_index"]
        slides = plan["slides"]
        if 0 <= i < len(slides):
            pat = kit["patterns"].get(slides[i].get("pattern"))
            if pat:
                return pat["slide"] - 1
        return i
    return resolve


# --------------------------------------------------------------------------- build

def build_from_kit(src: str | Path, index: dict, kit: dict, plan: dict, content: dict | None,
                   out_path: str | Path, baseline: Baseline | None = None) -> BuildResult:
    """Compile the plan, apply it to the template, then check what came out.

    Called by: engine.kitbuild.accept, engine.kitbuild.capacity, nodes.create.assemble,
        nodes.edit._rebuild_with_layouts; tests/validate_real_model.py.
    """
    from .deck import apply_ops  # local: keeps the import graph of this module flat
    from .tidy import clean_plan, remove_orphans

    # Both of these are for things the checks cannot see; `tidy.py` explains each one.
    plan, citations = clean_plan(plan)
    ops = compile_plan(plan, index, kit, content)
    ops, orphans = remove_orphans(ops, plan, index, kit)
    ex = apply_ops(src, ops, out_path)
    out_index = probe(out_path)
    report = check(out_path)
    if baseline is not None:
        report = baseline.filter(report, example_slide_of(plan, kit))
    bootstrap()
    from deckops.patterns import leaked_text

    return BuildResult(
        out_path=Path(out_path), ops=ops, exec=ex,
        findings=report["findings"], structural=report["structural"],
        leaked=leaked_text(index, kit, plan, out_index), out_index=out_index,
        pre_existing=report.get("pre_existing", {}),
        tidied={"citation_markers_removed": citations, "orphaned_decorations_removed": orphans},
    )


def baseline_for_kit(src: str | Path, kit: dict) -> Baseline:
    """The checks, run on the template's own example slides. The yardstick for a new deck.

    Called by: engine.kitbuild.accept, engine.kitbuild.capacity, nodes.create.assemble,
        nodes.edit._rebuild_with_layouts; tests/validate_real_model.py.
    """
    return Baseline.of(src, {p["slide"] - 1 for p in kit["patterns"].values()})


# --------------------------------------------------------------------------- explain

def repair_feedback(findings: list[dict], out_index: dict) -> dict[int, list[str]]:
    """Findings -> {slide index: [sentences for the model]}.

    An overflow becomes a character budget. The box holds `available_pt` of height; one line
    costs about 1.2x the font size; so it holds `fits` lines while the text needs `needs`.
    Scaling the current text by fits/needs (and shaving 10% for safety) gives a length that
    will actually fit, which a percentage never does.

    Called by: nodes.create.repair, nodes.edit._change_layouts, nodes.edit._insert_slides.
    """
    texts = {sh["ref"]: " ".join(p["text"] for p in sh.get("paras", []))
             for sl in out_index["slides"] for sh in sl["shapes"]}
    out: dict[int, list[str]] = {}
    for f in findings:
        if f["kind"] == "overflow":
            text = texts.get(f["ref"], "")
            line = 1.2 * (f.get("max_font_pt") or 12)
            fits = max(1, int(f["available_pt"] // line))
            needs = max(1.0, f["needed_pt"] / line)
            limit = max(8, int(len(text) * fits / needs * 0.9))
            msg = (f'text too long for its box: "{text[:160]}"; '
                   f"rewrite it in at most {limit} characters")
        elif f["kind"] == "overlap":
            msg = "two text boxes overlap: use shorter text or fewer items"
        elif f["kind"] == "appearance":
            # Someone looked at the rendered slide and said what was wrong with it. Pass it on
            # in their words: it is already a sentence about something visible.
            where = f.get("where")
            msg = (f"looking at this slide: {f['what']}" + (f" ({where})" if where else "")
                   + ". Fix it with shorter text or fewer items.")
        else:
            msg = f"{f['kind']}: {f.get('ref', '')}"
        out.setdefault(f["slide_index"], []).append(msg)
    return out


def font_fallback_ops(findings: list[dict]) -> list[dict]:
    """The deterministic last resort for overflow: shrink the text. No model involved.

    Called by: nodes.translate.translate_repair.
    """
    return [{"op": "set_font_size", "target": f["ref"], "size_pt": f["suggest_size_pt"]}
            for f in findings if f["kind"] == "overflow" and f.get("suggest_size_pt")]
