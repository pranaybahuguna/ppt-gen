"""Build a style kit for a template, in this process. Nobody labels anything by hand.

A *kit* is the map from a template's own example slides to named, fillable boxes: which shape
is the title, which shapes repeat as items, how many items really fit. It is what lets a model
write a deck in a corporate look by choosing from a menu instead of writing geometry.

The engine already does all the hard parts. What this module changes is only *where* they run:
the engine's `prepare_template` drives them through a sandboxed subprocess, which the single
process design removes. So the five steps below call the engine's functions directly, and the
only thing that still needs a container is rendering the example slides as pictures - and only
when a vision model is going to look at them.

    1  draft      rules read the template and propose a pattern per usable slide  (autokit)
    2  id check   every shape id in the draft really exists                       (validate_kit)
    3  pictures   render each candidate slide, and a copy with every fillable box
                  outlined and numbered                                          (sandbox + marks)
    4  label      a vision model names each box and the slide's kind; `merge` keeps structure
                  from the rules, meaning from the model, and vetoes the model on physics
    5  accept     build real test slides from each pattern and check them; a pattern that
                  fails is dropped, never guessed at
    6  capacity   measure how many items each pattern actually holds

Steps 5 and 6 are re-driven here rather than imported, because the engine's versions build
their test decks through the sandbox hop. They use the engine's own pure helpers - the test
slide generator, the role check, the placeholder text, the headroom constant - so the
*judgement* stays the engine's and only the plumbing is ours.
"""
from __future__ import annotations

import copy
from pathlib import Path

from ..errors import Boundary, PlanInvalid, Refused
from . import bootstrap
from .build import baseline_for_kit, build_from_kit, compile_plan
from .deck import probe

bootstrap()
from controlplane.kit_check import (  # noqa: E402  (the engine's pure helpers)
    CAPACITY_CAP,
    HEADROOM,
    TEST_DATA,
    role_problems,
    test_slides,
)
from controlplane.kit_merge import boxes_for_vision, merge  # noqa: E402
from deckops.autokit import draft_kit  # noqa: E402
from deckops.marks import mark  # noqa: E402
from deckops.patterns import _shapes_by_id, validate_kit  # noqa: E402

TEST_CONTENT = {"datasets": TEST_DATA}
FOOTER = "Sample footer text"


# --------------------------------------------------------------------------- 1-2

def draft(template: str | Path) -> tuple[dict, dict, dict]:
    """Rules only: a candidate pattern for every slide that can be reused. -> (kit, index, notes)"""
    raw = draft_kit(str(template), Path(template).name, dedupe=False)
    notes = {"draft_notes": raw.get("notes"), "slides_total": raw.get("slides_total")}
    kit = {k: v for k, v in raw.items() if k not in ("notes", "slides_total")}
    index = probe(template)
    problems = validate_kit(kit, index)
    for name in {e.split("'")[1] for e in problems if "'" in e}:
        kit["patterns"].pop(name, None)
    notes["id_problems"] = problems
    return kit, index, notes


# --------------------------------------------------------------------------- 3-4

def marked_pictures(template: str | Path, kit: dict, index: dict, outdir: Path,
                    render) -> dict[int, bytes]:
    """Render each candidate slide and outline every fillable box, numbered, for a vision model.

    `render` is the renderer to use - `deck_agent.sandbox.render.render`, injected so that this
    module never starts a process itself. Returns {slide number: PNG bytes of the marked copy}.
    """
    slides = sorted(p["slide"] - 1 for p in kit["patterns"].values())
    result = render(template, outdir, slides)
    if not result.ok:
        return {}
    out: dict[int, bytes] = {}
    for p in kit["patterns"].values():
        n = p["slide"]
        src = outdir / f"slide-{n:02d}.png"
        if not src.exists():
            continue
        loose = list(p["fields"].values())
        first = ([p["items"]["groups"][0][pos] for pos in p["items"]["fields"].values()]
                 if "items" in p else [])
        dst = outdir / f"slide-{n:02d}-marks.png"
        mark(str(src), str(dst), index["slides"][n - 1], index["slide_size"], loose + first, first)
        out[n] = dst.read_bytes()
    return out


def label_and_merge(kit: dict, index: dict, planner, marked: dict[int, bytes],
                    total: int, log=print) -> tuple[dict, dict, dict]:
    """Rules + (optional) vision -> one pattern per slide kind. -> (patterns, decisions, answers)"""
    merged: dict[str, dict] = {}
    decisions: dict[str, dict] = {}
    answers: dict[str, dict] = {}
    use_vision = bool(getattr(planner, "vision", False)) and bool(marked)
    for key, pat in sorted(kit["patterns"].items(), key=lambda kv: kv[1]["slide"]):
        n = pat["slide"]
        label = None
        if use_vision and n in marked:
            try:
                label = planner.label_slide(marked[n], boxes_for_vision(pat, index["slides"][n - 1]),
                                           key.split("@")[0], slide=n)
            except Exception as e:
                log(f"  vision failed on slide {n}: {e.__class__.__name__}; using the rules alone")
        if label:
            answers[str(n)] = label
        kind, mapped, why = merge(key, pat, index["slides"][n - 1], label, n, total)
        decisions[str(n)] = {"rules": key.split("@")[0], "final": kind,
                             "vision_used": bool(label), **why}
        if kind is None:
            continue
        if kind in merged:
            decisions[str(n)]["dropped"] = f"same kind as slide {merged[kind]['slide']} ({kind})"
            continue
        mapped.pop("big_text", None)
        merged[kind] = mapped
    return merged, decisions, answers


# --------------------------------------------------------------------------- 5

def accept(template: str | Path, index: dict, kit: dict, workdir: Path,
           planner=None, example_pngs: dict | None = None, log=print) -> tuple[dict, dict]:
    """Build test slides from every pattern and check them. A failing pattern is dropped.

    Two separate questions, both of which the engine's author found matter:
      1. does the pattern hold content as long as its example's?   -> test text at that length
      2. does each label make sense for its box?                   -> `role_problems`, which is
         physical: a field meant for prose must sit in a box that holds at least two lines.
    """
    report: dict = {}
    plan_slides: list[dict] = []
    owner: list[str] = []
    for name, pat in kit["patterns"].items():
        problems = role_problems(pat, _shapes_by_id(index["slides"][pat["slide"] - 1]))
        if problems:
            report[name] = {"passed": False, "reasons": problems}
            continue
        slides = test_slides(name, pat, index)
        solo = copy.deepcopy(kit)
        solo["patterns"] = {name: pat}
        try:
            compile_plan({"footer": FOOTER, "slides": slides}, index, solo, TEST_CONTENT)
        except Exception as e:
            report[name] = {"passed": False,
                            "reasons": [f"compile: {m}" for m in (getattr(e, "detail", None) or [str(e)])][:5]}
            continue
        plan_slides += slides
        owner += [name, name]
        report[name] = {"passed": True, "reasons": []}

    if plan_slides:
        plan = {"footer": FOOTER, "slides": plan_slides}
        try:
            result = build_from_kit(template, index, kit, plan, TEST_CONTENT,
                                    workdir / "acceptance.pptx", baseline_for_kit(template, kit))
        except (Refused, Boundary, PlanInvalid) as e:
            # The whole point of the acceptance test is that a pattern which cannot be built
            # gets dropped with a reason. A refusal from the engine is that answer arriving in
            # the form of an exception, and it must not take the kit build down with it: a
            # template where every pattern fails should end with an empty kit and an
            # explanation, not a traceback.
            reasons = [str(e)] + [str(d) for d in (e.detail or [])][:3]
            for name in set(owner):
                report[name]["passed"] = False
                report[name]["reasons"] += reasons
            final = copy.deepcopy(kit)
            final["patterns"] = {}
            final.pop("draft", None)
            report["_test_deck"] = None
            report["_vision_check"] = "not run (no test deck was built)"
            return final, report
        for f in result.findings:
            r = report[owner[f["slide_index"]]]
            r["passed"] = False
            r["reasons"].append(f"{f['kind']} on test slide {f['slide_index'] + 1}")
        if result.structural or result.leaked:
            for name in set(owner):
                report[name]["passed"] = False
                report[name]["reasons"].append("structural problem or leaked example text in the test deck")
        report["_test_deck"] = str(result.out_path)
        report["_vision_check"] = _vision_compare(result, owner, report, planner, example_pngs,
                                                  workdir, log)
    final = copy.deepcopy(kit)
    final["patterns"] = {n: p for n, p in kit["patterns"].items() if report.get(n, {}).get("passed")}
    final.pop("draft", None)
    return final, report


def _vision_compare(result, owner, report, planner, example_pngs, workdir, log) -> str:
    """Optional last check: does a test slide still *look* like its example? Needs a renderer."""
    if not (getattr(planner, "vision", False) and getattr(planner, "can_compare", True) and example_pngs):
        return "not run (no vision model configured)"
    from ..sandbox.render import render as render_fn

    shots = render_fn(result.out_path, workdir / "acceptance_png")
    if not shots.ok:
        return f"not run ({shots.skipped})"
    by_index = {int(p.stem.split("-")[1]) - 1: p for p in shots.previews}
    for i, name in enumerate(owner):
        # A pattern with no picture of its example slide cannot be compared with one. That is a
        # missing input, not a failure of the pattern, and dropping it on those grounds would
        # be wrong - it showed up as "vision check failed: KeyError".
        if i % 2 == 0 or not report[name]["passed"] or i not in by_index \
                or name not in example_pngs:
            continue
        try:
            verdict = planner.compare_slides(example_pngs[name], by_index[i].read_bytes())
        except Exception as e:
            verdict = {"same_design": False,
                       "problems": [f"vision check failed: {e.__class__.__name__}"]}
        if not verdict.get("same_design"):
            report[name]["passed"] = False
            report[name]["reasons"] += ([f"vision: {p}" for p in verdict.get("problems", [])][:3]
                                        or ["vision: the design changed"])
    return "run"


# --------------------------------------------------------------------------- 6

def capacity(template: str | Path, index: dict, kit: dict, workdir: Path,
             cap: int = CAPACITY_CAP) -> tuple[dict, dict]:
    """How many items does each pattern really hold? Measured, not taken from the example.

    The example shows k items; that number is an accident of whoever drew the slide. So build a
    test slide with k+1, k+2 ... cap items, with text 15% longer than the example's, and take
    the largest count that still passes the checks.
    """
    kit = copy.deepcopy(kit)
    trial = copy.deepcopy(kit)
    slides: list[dict] = []
    owner: list[tuple[str, int]] = []
    report: dict = {}
    for name, pat in kit["patterns"].items():
        if "items" not in pat:
            continue
        k = len(pat["items"]["groups"])
        report[name] = {"example": k, "max": k, "first_failure": None}
        counts = list(range(k + 1, cap + 1))
        if not counts:
            continue
        trial["patterns"][name]["items"]["max"] = cap
        solo = copy.deepcopy(trial)
        solo["patterns"] = {name: trial["patterns"][name]}
        for n, s in zip(counts, test_slides(name, pat, index, counts, stretch=HEADROOM)):
            try:
                compile_plan({"footer": FOOTER, "slides": [s]}, index, solo, TEST_CONTENT)
            except Exception as e:
                report[name]["_compile_fail"] = (n, (getattr(e, "detail", None) or [str(e)])[0])
                break
            slides.append(s)
            owner.append((name, n))

    if slides:
        try:
            result = build_from_kit(template, index, trial, {"footer": FOOTER, "slides": slides},
                                    TEST_CONTENT, workdir / "capacity.pptx",
                                    baseline_for_kit(template, trial))
        except (Refused, Boundary, PlanInvalid) as e:
            # Measuring failed, so nothing is learned and nothing is claimed: every pattern
            # keeps the count its example slide shows, which is the conservative direction.
            for name, r in report.items():
                r["first_failure"] = f"could not be measured: {e}"
            return kit, report
        bad: dict[str, set[int]] = {}
        for f in result.findings:
            name, n = owner[f["slide_index"]]
            bad.setdefault(name, set()).add(n)
        broken = bool(result.structural or result.leaked)
        for name, r in report.items():
            for n in sorted(n for nm, n in owner if nm == name):
                if broken or n in bad.get(name, ()):
                    r["first_failure"] = f"{n} items: text no longer fits"
                    break
                r["max"] = n
    for name, r in report.items():
        cf = r.pop("_compile_fail", None)
        if cf and not r["first_failure"] and r["max"] == cf[0] - 1:
            r["first_failure"] = f"{cf[0]} items: {cf[1]}"
        kit["patterns"][name]["items"]["max"] = r["max"]
    return kit, report


# --------------------------------------------------------------------------- all of it

def build_kit(template: str | Path, workdir: Path, planner=None, render=None,
              log=print) -> tuple[dict, dict]:
    """Template in, tested kit out. -> (kit, report). No person in the loop."""
    template = Path(template)
    workdir.mkdir(parents=True, exist_ok=True)
    kit, index, notes = draft(template)
    log(f"  draft: {len(kit['patterns'])} candidate slides")

    marked: dict[int, bytes] = {}
    if render is not None and getattr(planner, "vision", False):
        marked = marked_pictures(template, kit, index, workdir / "examples", render)
        log(f"  pictures: {len(marked)} example slides marked up for the vision model")

    total = notes.get("slides_total") or len(index["slides"])
    merged, decisions, answers = label_and_merge(kit, index, planner, marked, total, log)
    kit["patterns"] = merged
    kit.setdefault("template", template.name)
    log(f"  labels: {len(merged)} distinct slide kinds: {sorted(merged)}")

    example_pngs = {name: (workdir / "examples" / f"slide-{p['slide']:02d}.png").read_bytes()
                    for name, p in merged.items()
                    if (workdir / "examples" / f"slide-{p['slide']:02d}.png").exists()}
    kit, accepted = accept(template, index, kit, workdir, planner, example_pngs or None, log)
    passed = sorted(kit["patterns"])
    log(f"  accepted: {len(passed)} of {len(merged)} patterns: {passed}")

    kit, measured = capacity(template, index, kit, workdir)
    if measured:
        log("  capacity: " + ", ".join(f"{n} {r['example']}->{r['max']}" for n, r in measured.items()))
    report = {**notes, "labels": decisions, "vision_labels": answers,
              "patterns": accepted, "capacity": measured}
    return kit, report


#: Roles that carry *words*. A box holding one of these has to look like a line of text.
WORD_ROLES = {"heading", "label", "title", "subtitle", "body", "text", "points", "caption",
              "intro", "takeaway", "commentary", "detail", "side_text", "note"}
#: Roles that are legitimately short and square - a figure, a step number, a page number.
BADGE_ROLES = {"value", "number", "page"}
MIN_ASPECT = 1.6          # wider than tall; a 1:1 box is a badge, not a line of text
MIN_LINES = 3.0           # ... unless it is tall enough to be a paragraph


def badge_problems(pattern: dict, shapes: dict) -> list[str]:
    """Fields pointed at a badge rather than at a line of text.

    This is the check that stands in for the acceptance test when there is no time to build
    one, and it exists because of a real failure. On a deck whose steps are drawn as numbered
    circles, the drafting rules offered the *circle* as the item's `heading` and the actual
    heading box as `detail_1`. Nothing caught it: `role_problems` only guards long-prose roles,
    and the overflow predictor is happy because two short lines do fit inside a 50-point
    circle. The slide that came out had "Rates" wrapped over two lines inside a badge.

    So: a field whose role carries words must sit in a box that is meaningfully wider than it
    is tall, or tall enough to be a paragraph. `value`, `number` and `page` are exempt, because
    a short figure in a square tile is exactly right.

    In the spirit of the engine's own convention - prefer a rule that *drops* a doubtful
    pattern over one that guesses - a pattern with such a field is dropped, not repaired.
    """
    out = []
    named = list(pattern["fields"].items())
    if "items" in pattern:
        first = pattern["items"]["groups"][0]
        named += [(f, first[pos]) for f, pos in pattern["items"]["fields"].items()]
    for field, sid in named:
        role = field.split("_")[0]
        if role in BADGE_ROLES or role not in WORD_ROLES or sid not in shapes:
            continue
        shape = shapes[sid]
        geom = shape.get("geom")
        if not geom or not geom.get("h"):
            continue
        aspect = geom["w"] / geom["h"]
        lines = (geom["h"] / 12700) / ((shape.get("font_pt") or 12) * 1.2)
        if aspect < MIN_ASPECT and lines < MIN_LINES:
            out.append(f"field {field!r} carries words but its box is {aspect:.2f}:1 and holds "
                       f"{lines:.1f} line(s) - that is a badge, not a text box")
    return out


def light_kit(deck: str | Path, planner=None, log=print) -> tuple[dict, dict]:
    """A kit for a deck that is being *edited*, where the deck is its own template.

    Deliberately not the full six steps: inserting one slide does not justify building test
    decks and measuring capacities, and the answer would be thrown away the moment the deck
    changes again. So this is steps 1, 2, 4 - draft, check the ids, name the kinds - **plus the
    role check**, which is the part that must not be skipped.

    Why that one stays. `role_problems` is pure and costs nothing: it asks whether a field
    meant for prose actually sits in a box that holds at least two lines at its own font size.
    Leaving it out produced a real, ugly failure - on a deck whose slides use numbered circles,
    the drafting rules offered the *circle* as the heading box, and an inserted slide put
    "Volatilité des taux" inside a 30-point circle, wrapped over five lines and overlapping
    everything near it. The overflow checks caught it afterwards, but by then a bad slide had
    been built. This check drops the pattern before it is ever offered.

    Item counts still stay at whatever the example slide shows, which is the conservative
    direction, and the finished slide is still checked like any other.
    """
    kit, index, notes = draft(deck)
    merged, decisions, _ = label_and_merge(kit, index, planner, {}, len(index["slides"]), log)

    usable, dropped = {}, {}
    for name, pattern in merged.items():
        shapes = _shapes_by_id(index["slides"][pattern["slide"] - 1])
        problems = role_problems(pattern, shapes) + badge_problems(pattern, shapes)
        if problems:
            dropped[name] = problems
        else:
            usable[name] = pattern
    if dropped:
        log(f"  light kit: dropped {sorted(dropped)} "
            f"({'; '.join(p for ps in dropped.values() for p in ps)[:120]})")

    kit["patterns"] = usable
    kit.setdefault("template", Path(deck).name)
    return kit, {**notes, "labels": decisions, "dropped_by_role_check": dropped,
                 "mode": "light (role check only; no acceptance test, no capacity)"}
