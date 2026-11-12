"""The escape hatch: run python-pptx code a model wrote.

When the deterministic loop cannot express a slide - no layout in the template holds that
shape of content, or the repair rounds run out - the model is allowed to write code instead of
filling a menu. That is a real departure from the engine's first hard rule ("the model never
writes positions, sizes, colours, fonts or XML"), so it is fenced in on four sides:

1. **It is off by default** (`allow_custom_code`), and the graph only reaches it after the
   deterministic path has actually failed. It is a fallback, not a shortcut.
2. **A static gate** rejects obviously wrong code in milliseconds and hands the model a reason
   (`guard.py`). Explicitly not the security boundary; see that module's note.
3. **A throwaway container** is the boundary: no network, read-only root filesystem, no
   capabilities, half a gigabyte of memory, seconds of CPU, destroyed after one call.
4. **The output is checked like any other build** - overflow, overlap, structural problems,
   leaked template content - and then *resealed*, so slides the code did not touch stay
   byte-identical to the deck we started from. The escape hatch does not get to quietly
   rewrite the rest of the deck.

If the code fails, the error goes back to the model and we try again; if it keeps failing, the
previous version of the deck is still there and nothing has been lost.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..settings import Settings, settings
from . import guard
from .docker import SandboxUnavailable, run_container


@dataclass
class CodeResult:
    """The outcome of running model-written code: the deck it produced, or the error and the
    problems phrased for the model; whether the static check refused it; slide counts.

    Returned by: run_builder; read by custom_code and the code-slide replay.
    """
    ok: bool
    deck: Path | None = None
    error: str | None = None
    problems: list[str] = field(default_factory=list)   # phrased for the model
    stdout: str = ""
    slides_before: int | None = None
    slides_after: int | None = None
    rejected_by_guard: bool = False
    seconds: float = 0.0
    container: dict = field(default_factory=dict)

    def feedback(self) -> list[str]:
        """What to tell the model so its next attempt is better."""
        return self.problems or ([self.error] if self.error else [])


#: What the model is told it will be given. Kept here, next to the harness that provides it,
#: so the prompt and the runtime cannot drift apart.
CONTRACT = """\
Write a Python module with exactly one top-level function:

    def build(prs, ctx):
        ...

`prs` is an open python-pptx Presentation of the current deck. `ctx` is a dict:

    ctx["slide_size"]   {"w": <EMU>, "h": <EMU>}          the slide's size in EMU
    ctx["style"]        {"palette": ["RRGGBB", ...],      colours and fonts taken from the
                         "font": "...",                   template, so your slide matches it
                         "text_colour": "RRGGBB",
                         "title_pt": <float>, "body_pt": <float>}
    ctx["content"]      the words and numbers to place. Use these; invent nothing.
    ctx["insert_after"] <int> | null                      where this slide will end up, for
                                                          context only - see the rule below
    ctx["target_slide"] <int> | null                      a slide ALREADY in prs to rework in
                                                          place: prs.slides[ctx["target_slide"]]
    ctx["shapes"]       [{"id","role","name","kind","x","y","w","h","text"}] | null
                                                          what is on that slide now; role is
                                                          title / page / frame (leave these) or
                                                          content (yours to move, restyle,
                                                          reuse or delete)
    ctx["chrome_layout"] {"master": m, "layout": l} | null  the template's content-slide
                                                          layout: header band, title box and
                                                          footer come with it. Use
                                                          prs.slide_masters[m].slide_layouts[l]
    ctx["body_box"]     {"x","y","w","h"} in EMU          the area below the title and above
                                                          the footer: draw only inside it
    ctx["form"]         "<diagram name>"                  what to draw this slide as
    ctx["form_hint"]    "<one line>"                      what that diagram looks like
    ctx["avoid_forms"]  ["..."]                           diagrams other slides already use
    ctx["blank_layout"] <int>                             a blank layout, if chrome_layout is null

Rules:
  * add your slide with `prs.slides.add_slide(...)` (unless ctx["target_slide"] is given);
    do not save `prs`, and **do not move or
    reorder any slide**. The harness puts your slide in the right place afterwards. (It finds
    your slide by identity, so moving it yourself cannot corrupt the deck - it simply wastes
    an attempt.)
  * add or modify slides on `prs`; do not save it - the harness does that
  * when ctx["target_slide"] is given, rework that slide and add no slide: leave every shape
    whose role is title, page or frame exactly as it is
  * otherwise add the slide on ctx["chrome_layout"] and put the title in slide.shapes.title
  * EMU everywhere (914400 per inch); keep every shape inside ctx["body_box"]
  * use only ctx["style"] colours and fonts, so the slide looks like the rest of the deck
  * put only words a reader should see on the slide: never an identifier, a key or an id
  * no network, no filesystem, no subprocesses - none of them exist where this runs
  * import only from: pptx, math, copy, json, re, textwrap, itertools, functools, dataclasses,
    typing, collections, colorsys, statistics, decimal, fractions, datetime, enum, random,
    string, unicodedata
"""


def run_builder(code: str, ctx: dict, deck: str | Path | None = None,
                s: Settings | None = None) -> CodeResult:
    """Run `code`'s `build(prs, ctx)` against `deck` (or a blank deck). -> CodeResult.

    Called by: nodes.custom_code.custom_code, nodes.edit._replay_code_slides;
        tests/validate_real_model.py.
    """
    s = s or settings()

    verdict = guard.inspect(code)
    if not verdict.ok:
        return CodeResult(ok=False, rejected_by_guard=True, problems=verdict.feedback(),
                          error="the code was rejected before it was run")

    if s.code_runner == "off":
        return CodeResult(ok=False, error="running model-written code is turned off "
                                          "(code_runner=off)")

    workdir = Path(tempfile.mkdtemp(dir=s.workspace_root, prefix="code-"))
    try:
        (workdir / "in").mkdir()
        (workdir / "out").mkdir()
        (workdir / "in" / "code.py").write_text(code)
        (workdir / "in" / "ctx.json").write_text(json.dumps(ctx, ensure_ascii=False))
        # one source of truth for what may be imported: the guard checked it statically, the
        # harness enforces the same list at runtime
        (workdir / "in" / "allowed_imports.json").write_text(
            json.dumps(sorted(guard.ALLOWED_IMPORTS)))
        if deck is not None:
            shutil.copy(deck, workdir / "in" / "deck.pptx")
        try:
            res = run_container(
                ["python3", "/opt/runner/code_harness.py", "in", "out", str(s.code_timeout_s)],
                workdir,
                timeout_s=s.code_timeout_s + 20,     # the harness kills itself first; this is the backstop
                memory=s.code_memory,                # much tighter than a render
                cpus="1", pids=64, network="none", tmpfs_mb=64, s=s,
            )
        except SandboxUnavailable as e:
            return CodeResult(ok=False, error=str(e))

        rf = workdir / "out" / "result.json"
        info = json.loads(rf.read_text()) if rf.exists() else {}
        if res.timed_out or res.killed_for_memory or (not rf.exists() and res.exit_code == 137):
            if res.killed_for_memory:
                why = f"the code used more than {s.code_memory} of memory"
            elif res.timed_out:
                why = "the code did not finish in time"
            else:
                # Killed, and Docker does not say it was the memory cap: almost always the CPU
                # limit. Say both rather than pick one and send the model the wrong way.
                why = (f"the code was stopped after {res.seconds}s - it ran past its time limit "
                       f"of {s.code_timeout_s}s")
            return CodeResult(ok=False, error=why,
                              problems=[f"{why}; do less work and keep it simple"],
                              seconds=res.seconds, container=res.detail())
        if not info.get("ok"):
            problems = []
            if info.get("error"):
                problems.append(info["error"])
            if info.get("traceback"):
                problems.append("traceback:\n" + info["traceback"][-1200:])
            if not problems:
                problems = [f"the sandbox exited with code {res.exit_code}", res.stderr[-600:]]
            return CodeResult(ok=False, error=info.get("error") or "the code failed",
                              problems=problems, stdout=info.get("stdout", ""),
                              seconds=res.seconds, container=res.detail())

        produced = workdir / "out" / "deck.pptx"
        kept = Path(tempfile.mkdtemp(dir=s.workspace_root, prefix="codeout-")) / "produced.pptx"
        shutil.copy(produced, kept)
        return CodeResult(ok=True, deck=kept, stdout=info.get("stdout", ""),
                          slides_before=info.get("slides_before"),
                          slides_after=info.get("slides_after"),
                          seconds=res.seconds, container=res.detail())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
