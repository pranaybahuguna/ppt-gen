"""Run python-pptx code a model wrote, against a deck, in a throwaway container.

The edit route uses it for two kinds of task: a *tweak* (a small direct change to one slide) and
a *draw* (a custom visual the standard pieces cannot make). Model-written code is fenced in:

1. **A static gate** rejects obviously wrong code in milliseconds and hands the model a reason
   (`guard.py`). Explicitly not the security boundary; see that module's note.
2. **A throwaway container** is the boundary: no network, read-only root filesystem, no
   capabilities, little memory, seconds of CPU, destroyed after one call.
3. **The output is checked** for text that the change made overflow or collide (revise.work),
   and **resealed**, so slides the code did not touch stay byte-identical to the deck we
   started from.

If the code fails, the error goes back to the model and it tries again; if it keeps failing,
that slide is left as it was and the person is told.
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

    Returned by: run_builder; read by revise.work.tweak and revise.nodes.join.
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




def run_builder(code: str, ctx: dict, deck: str | Path | None = None,
                s: Settings | None = None) -> CodeResult:
    """Run `code`'s `build(prs, ctx)` against `deck` (or a blank deck). -> CodeResult.

    Called by: revise.work.tweak (trying the code), revise.nodes.join (applying it);
        tests/integration/test_sandbox.py.
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
