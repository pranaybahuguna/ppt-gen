"""The bridge to the deckops engine. The only module that knows where the engine lives.

The engine is vendored unmodified at `engine/deckops` (see engine/README.md). We import its
modules and call their functions **in this process**: probe, compile, execute, byte-preserving
save and the geometry checks are all pure Python over `python-pptx` / `lxml`, so there is
nothing to gain from marshalling them through a subprocess.

Two small, deliberate acts of composition happen here, both documented:

1. `use_local_fonts()` prepends our own font directory to the engine's `FONT_DIRS`, so the
   overflow predictor can use metric-compatible fonts (Carlito, Liberation) that we extract
   from the sandbox image instead of requiring them to be installed on the host.
2. `guard_deck()` is the engine's own `zipguard` check, lifted to intake. In deckops it ran
   inside the sandbox because that was the trust boundary; here it runs the moment a deck
   arrives, before any parser sees it.

Nothing in this package opens a network connection, and nothing shells out: the only external
processes this project starts are sandbox containers (see `deck_agent.sandbox`).
"""
from __future__ import annotations

import functools
import subprocess
import sys
from pathlib import Path

from ..errors import Boundary
from ..settings import REPO, settings

ENGINE_ROOT = REPO / "engine" / "deckops"


@functools.lru_cache(maxsize=1)
def bootstrap() -> Path:
    """Put the engine on the import path. Idempotent; safe to call from anywhere."""
    if not (ENGINE_ROOT / "deckops" / "probe.py").exists():
        raise Boundary(
            f"the deckops engine is not at {ENGINE_ROOT}. Run:\n"
            "    git clone https://github.com/pranaybahuguna/deckops engine/deckops",
            {"expected": str(ENGINE_ROOT)},
        )
    if str(ENGINE_ROOT) not in sys.path:
        sys.path.insert(0, str(ENGINE_ROOT))
    use_local_fonts()
    return ENGINE_ROOT


def use_local_fonts() -> list[str]:
    """Prepend our font directory to the engine's search path.

    The overflow predictor measures text with real font metrics. Corporate templates ask for
    Calibri/Arial; the engine substitutes the metric-compatible Carlito/Liberation families.
    Rather than make installing those a prerequisite, `make fonts` extracts them from the
    sandbox image into `local/fonts`, and this points the engine at them.

    Returns the engine's font directories, for `doctor` to report.
    """
    from deckops import validate as _validate  # noqa: PLC0415  (needs sys.path set first)

    d = str(settings().font_path)
    if d not in _validate.FONT_DIRS:
        _validate.FONT_DIRS.insert(0, d)
    return _validate.FONT_DIRS


def fonts_available() -> dict:
    """Which of the metric-compatible families we can actually measure with."""
    bootstrap()
    from deckops.validate import FONT_SUBST, _font_file

    out = {}
    for asked, substitute in sorted(FONT_SUBST.items()):
        try:
            out[asked] = Path(_font_file(asked, False)).name
        except FileNotFoundError:
            out[asked] = None
    out["_exact"] = all(
        v and v.lower().startswith(FONT_SUBST[k].lower()) for k, v in out.items() if not k.startswith("_")
    )
    return out


def guard_deck(path: str | Path) -> None:
    """The engine's cheap archive checks, run before any parser touches an incoming deck.

    Raises `Boundary` with the engine's reason: too large, too many entries, an encrypted or
    path-traversing entry, a suspicious compression ratio, or not a .pptx at all.
    """
    bootstrap()
    from deckops.zipguard import UnsafeArchive, check

    try:
        check(str(path))
    except UnsafeArchive as e:
        raise Boundary(f"refused this file: {e}", {"file": Path(path).name}) from None


@functools.lru_cache(maxsize=1)
def engine_version() -> dict:
    """What built this deck. Recorded in every manifest so a deck can be traced to a commit."""
    bootstrap()
    try:
        sha = subprocess.run(
            ["git", "-C", str(ENGINE_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        sha = ""
    return {"engine": "deckops", "commit": sha or "unknown", "path": str(ENGINE_ROOT)}
