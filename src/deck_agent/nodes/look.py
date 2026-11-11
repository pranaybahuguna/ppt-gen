"""Look at the rendered slides, with a vision model, and turn what it sees into findings.

Everything else that checks a deck reasons about geometry: does this text fit its box, do these
two boxes overlap, is the file structurally sound. That catches a great deal and is completely
blind to a third category - slides that are *valid and wrong*. Both real defects in this
project were in that category:

* a decorative panel left empty after the text that belonged on it was dropped;
* a heading wedged into a 50-point circle meant for a step number, wrapping over five lines.

Neither is an overflow. Neither is an overlap. Both are obvious the moment you look, which is
why the engine's working notes say to look at the rendered PNGs before every commit. This node
does that automatically: it hands each rendered slide to a vision model and asks what a person
in the room would notice.

Three rules keep it from doing harm:

1. **It judges appearance only.** The prompt forbids comments on wording, tone, content choice
   or taste - a false alarm costs a rewrite of a slide that was fine.
2. **Only `high` severity acts.** Those become findings that the repair loop will try to fix
   once. Everything else is recorded as a warning for the person, and changes nothing.
3. **It never blocks delivery.** A model that cannot see straight must not be able to stop a
   deck going out. If the call fails, that is said plainly and the deck is delivered.

It reuses the previews the build already rendered, so it costs model calls and no extra
rendering. It is off by default (`vision_review`), because it is one call per slide.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..settings import settings
from ..state import step


def review_previews(previews: list[Path], planner, context: dict | None = None,
                    log=print) -> tuple[list[dict], list[str]]:
    """Look at each rendered slide. -> (findings to act on, notes for the person)

    A preview is named `slide-NN.png`, 1-based, so the slide index is recoverable from the
    filename - the same convention the engine's renderer uses.
    """
    s = settings()
    if not previews:
        return [], ["there were no rendered slides to look at"]
    if not getattr(planner, "review_slide", None):
        return [], ["this planner cannot look at pictures"]

    def one(path: Path):
        try:
            index = int(path.stem.split("-")[-1]) - 1
        except (ValueError, IndexError):
            index = 0
        try:
            answer = planner.review_slide(path.read_bytes(), {**(context or {}),
                                                              "slide": index + 1})
        except Exception as e:
            return index, None, f"{e.__class__.__name__}: {e}"
        return index, answer, None

    with ThreadPoolExecutor(max_workers=s.vision_workers) as pool:
        results = list(pool.map(one, previews))

    findings: list[dict] = []
    notes: list[str] = []
    for index, answer, error in sorted(results, key=lambda r: r[0]):
        if error:
            notes.append(f"slide {index + 1}: could not be looked at ({error})")
            continue
        if answer.get("note"):
            notes.append(answer["note"])
        for problem in answer.get("problems", []) or []:
            what = str(problem.get("what", "")).strip()
            if not what:
                continue
            entry = {"kind": "appearance", "slide_index": index, "what": what,
                     "where": str(problem.get("where", "")).strip(),
                     "severity": str(problem.get("severity", "low")).lower()}
            if entry["severity"] == "high":
                findings.append(entry)
            else:
                notes.append(f"slide {index + 1}: {what}"
                             + (f" ({entry['where']})" if entry["where"] else ""))
    if findings or notes:
        log(f"  looked at {len(previews)} slide(s): {len(findings)} to fix, {len(notes)} noted")
    return findings, notes


def _slide_number(uri: str) -> int:
    import re

    m = re.search(r"slide-(\d+)\.png$", str(uri))
    return int(m.group(1)) if m else 0


def look(state, planner=None, log=print) -> dict:
    """The graph node. Off unless `vision_review` is on; never blocks delivery."""
    s = settings()
    if not s.vision_review:
        return {"trace": [step("look", outcome="not asked for")]}

    from .common import workspace

    previews = state.get("previews") or []
    # Slides about to be reworked, or already reworked, are not looked at here: the first are
    # about to change, and the second were looked at, one by one, when they were reworked.
    designed = ({p["position"] for p in state.get("pending_code") or [] if p.get("rework")}
                | {c["position"] for c in state.get("code_slides") or [] if c.get("rework")})
    if designed:
        previews = [u for u in previews if _slide_number(u) - 1 not in designed]
    if not previews:
        return {"trace": [step("look", outcome="nothing was rendered")],
                "errors": [{"kind": "not_looked_at", "message":
                            "the deck was not looked at: no previews were rendered "
                            "(is render_backend off?)"}]}

    with workspace(state) as ws:
        local = [ws.fetch(uri, f"look/{Path(uri).name}") for uri in previews]
        findings, notes = review_previews(
            local, planner,
            {"deck_title": (state.get("plan") or {}).get("title", "")}, log)

    # The previews are of the whole deck, designed slides included; repair works on the plan,
    # which has no designed slides in it. Number the findings the way repair will read them,
    # and keep what was seen on a designed slide as a note - it was looked at when it was drawn.
    code_slides = state.get("code_slides") or []
    drawn = sorted({c["position"] for c in code_slides if "position" in c})
    # A reworked slide is a plan slide in its own position; one added by code (no layout could
    # hold it) is not in the plan, and the slides after it are one further on.
    inserted = sorted({c["position"] for c in code_slides
                       if "position" in c and not c.get("rework")})
    if drawn:
        kept = []
        for f in findings:
            i = f["slide_index"]
            if i in drawn:
                notes.append(f"slide {i + 1}: {f.get('what', f['kind'])}")
                continue
            kept.append({**f, "slide_index": i - sum(1 for d in inserted if d < i)})
        findings = kept

    # Appearance findings join the geometric ones, so the existing decision and repair loop
    # handle them without knowing where they came from.
    return {
        "findings": [*(state.get("findings") or []), *findings],
        "vision_notes": notes,
        "trace": [step("look", slides=len(local), to_fix=len(findings), noted=len(notes))],
    }
