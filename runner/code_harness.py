"""Run code a model wrote, against one deck. Runs inside the sandbox container.

    python3 /opt/runner/code_harness.py in out 60

Inputs (in `in/`):
    code.py     the module the model wrote; must define `build(prs, ctx)`
    ctx.json    everything the code is allowed to know: slide size, style, the content to place
    deck.pptx   optional: the deck to add to. Absent means start from a blank presentation.

Outputs (in `out/`):
    deck.pptx   the saved result
    result.json {"ok", "error", "traceback", "stdout", "slides_before", "slides_after"}

The container is the security boundary: no network, read-only root filesystem, no capabilities,
a hard memory cap and a few seconds of CPU. The restrictions in *this* file - a trimmed
`__builtins__`, rlimits, an alarm - are defence in depth, so that honest mistakes fail fast and
loudly. They are not a sandbox, and nothing here should be read as one.
"""
from __future__ import annotations

import io
import json
import os
import resource
import signal
import sys
import traceback

BANNED_BUILTINS = {
    "eval", "exec", "compile", "open", "input", "breakpoint", "help",
    "memoryview", "globals", "vars", "exit", "quit", "license", "credits",
}

#: `__import__` cannot simply be removed: the `import` statement itself looks it up in the
#: module's builtins, so taking it away stops `import pptx` from working. It is replaced with a
#: version that honours the same allowlist the host checked statically - which is better than
#: removing it, because it also closes the gap where a module name is assembled at runtime.
#: The list arrives as a file next to the code, so there is one source of truth for it
#: (`deck_agent.sandbox.guard.ALLOWED_IMPORTS`) rather than a copy that drifts.
DEFAULT_ALLOWED = {"pptx", "math", "copy", "json", "re", "textwrap", "itertools", "functools",
                   "dataclasses", "typing", "collections", "colorsys", "statistics", "decimal",
                   "fractions", "datetime", "enum", "random", "string", "unicodedata"}


class Timeout(Exception):
    pass


def _limits(cpu_seconds: int) -> None:
    # Headroom on purpose. The wall-clock alarm below is what *reports* a timeout; if the CPU
    # rlimit fired first the process would be killed outright, result.json would never be
    # written, and the caller would see an unexplained exit code instead of a sentence it can
    # hand back to the model. The rlimit is the backstop, not the mechanism.
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds + 5, cpu_seconds + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))


def _guarded_import(allowed: set):
    real = __import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if level or root not in allowed:
            raise ImportError(
                f"{name!r} is not available here. This code runs with no network and no "
                f"filesystem; you may import only: {', '.join(sorted(allowed))}")
        return real(name, globals, locals, fromlist, level)

    return guarded


def _safe_builtins(allowed: set) -> dict:
    import builtins

    out = {k: v for k, v in vars(builtins).items() if k not in BANNED_BUILTINS}
    out["__import__"] = _guarded_import(allowed)
    return out


def run(indir: str, outdir: str, seconds: int) -> dict:
    from pptx import Presentation
    from pptx.util import Emu

    code = open(os.path.join(indir, "code.py")).read()
    ctx = json.load(open(os.path.join(indir, "ctx.json")))
    allow_file = os.path.join(indir, "allowed_imports.json")
    allowed = set(json.load(open(allow_file))) if os.path.exists(allow_file) else DEFAULT_ALLOWED
    deck = os.path.join(indir, "deck.pptx")
    prs = Presentation(deck) if os.path.exists(deck) else Presentation()
    before = len(prs.slides._sldIdLst)

    module: dict = {"__name__": "model_code", "__builtins__": _safe_builtins(allowed),
                    "Emu": Emu}
    stdout = io.StringIO()
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(
        Timeout(f"the code ran for longer than its {seconds}s time limit and was stopped")))
    signal.alarm(seconds)
    real_stdout, sys.stdout = sys.stdout, stdout
    try:
        exec(compile(code, "model_code.py", "exec"), module)          # noqa: S102 - the point of the file
        build = module.get("build")
        if not callable(build):
            return {"ok": False, "error": "code.py must define a function build(prs, ctx)"}
        build(prs, ctx)
    except Timeout as e:
        return {"ok": False, "error": str(e), "stdout": stdout.getvalue()[-4000:]}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}",
                "traceback": traceback.format_exc(limit=6), "stdout": stdout.getvalue()[-4000:]}
    finally:
        signal.alarm(0)
        sys.stdout = real_stdout

    out = os.path.join(outdir, "deck.pptx")
    try:
        prs.save(out)
    except Exception as e:
        return {"ok": False, "error": f"the code produced a deck that could not be saved: {e}",
                "traceback": traceback.format_exc(limit=4)}
    return {"ok": True, "deck": "deck.pptx", "slides_before": before,
            "slides_after": len(prs.slides._sldIdLst), "stdout": stdout.getvalue()[-4000:]}


def main(argv: list[str]) -> int:
    indir, outdir, seconds = argv[0], argv[1], int(argv[2])
    os.makedirs(outdir, exist_ok=True)
    try:
        _limits(max(5, seconds))
        result = run(indir, outdir, seconds)
    except Exception:
        result = {"ok": False, "error": "the harness itself failed",
                  "traceback": traceback.format_exc(limit=5)}
    with open(os.path.join(outdir, "result.json"), "w") as f:
        json.dump(result, f, indent=1)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
