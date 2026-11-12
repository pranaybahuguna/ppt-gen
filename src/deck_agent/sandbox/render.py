"""Render a deck to PNG previews.

Three backends, chosen by `render_backend`:

* `docker` (default) - LibreOffice and poppler live in the sandbox image, so the agent needs
  no native dependency at all. One throwaway container per render.
* `local` - a `soffice` that is already installed on this machine. Faster (no container start,
  warm profile) and useful on a developer box. The engine's own `render()` does the work.
* `off` - skip rendering. Everything else still works: overflow, overlap and structural checks
  are predicted from font metrics and geometry, so a deck can be built and checked with no
  renderer present. What you lose is pictures - previews for a human, and the vision model's
  input when building a style kit.

Rendering is never load-bearing for correctness. It is load-bearing for *trust*, which is why
`off` says so loudly in the result rather than quietly returning nothing.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import Boundary
from ..settings import Settings, settings
from .docker import ContainerResult, SandboxUnavailable, run_container


@dataclass
class RenderResult:
    """What rendering produced: which backend, the PNG previews and PDF, or why it was skipped.

    Returned by: render(); read by make_previews, the kit builder and the vision checks.
    """
    backend: str
    previews: list[Path] = field(default_factory=list)
    pdf: Path | None = None
    skipped: str | None = None          # why there are no pictures, if there are none
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """True when previews were produced."""
        return self.skipped is None


def render(pptx: str | Path, outdir: str | Path, pages: list[int] | None = None,
           dpi: int = 60, s: Settings | None = None) -> RenderResult:
    """Render `pages` (0-based slide indices; None = all) as PNGs into `outdir`.

    Called by: cli.cmd_kit, engine.kitbuild._vision_compare, nodes.common.make_previews,
        nodes.edit._look_at_one, nodes.kit.ensure_kit; tests/validate_real_model.py.
    """
    s = s or settings()
    pptx, outdir = Path(pptx).resolve(), Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    if pages is None:
        from ..engine.deck import probe
        pages = list(range(len(probe(pptx)["slides"])))
    if not pages:
        return RenderResult(backend=s.render_backend, skipped="no slides asked for")

    if s.render_backend == "off":
        return RenderResult(backend="off",
                            skipped="rendering is turned off (render_backend=off): no previews, "
                                    "and no vision check when building a style kit")
    if s.render_backend == "local":
        return _render_local(pptx, outdir, pages, dpi)
    if s.render_backend == "docker":
        return _render_docker(pptx, outdir, pages, dpi, s)
    raise Boundary(f"unknown render_backend {s.render_backend!r}: use docker, local or off")


def _render_local(pptx: Path, outdir: Path, pages: list[int], dpi: int) -> RenderResult:
    """Render with a LibreOffice installed on this machine, through the engine's own renderer.

    Called by: render(), when render_backend is 'local'.
    """
    from ..engine import bootstrap

    bootstrap()
    from deckops.validate import render as engine_render

    soffice = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if not Path(soffice).exists():
        return RenderResult(backend="local",
                            skipped="render_backend=local but no soffice on this machine; "
                                    "install LibreOffice or use render_backend=docker")
    import time
    t0 = time.time()
    try:
        pdf, pngs = engine_render(str(pptx), str(outdir), pages, dpi)
    except Exception as e:
        return RenderResult(backend="local", skipped=f"local LibreOffice failed: {e.__class__.__name__}: {e}")
    return RenderResult(backend="local", previews=[Path(p) for p in pngs], pdf=Path(pdf),
                        seconds=round(time.time() - t0, 2))


def _render_docker(pptx: Path, outdir: Path, pages: list[int], dpi: int, s: Settings) -> RenderResult:
    """One container, `--network none`, the deck copied in and the PNGs copied out.

    Called by: render.
    """
    import tempfile

    with tempfile.TemporaryDirectory(dir=s.workspace_root, prefix="render-") as tmp:
        work = Path(tmp)
        (work / "in").mkdir()
        (work / "out").mkdir()
        shutil.copy(pptx, work / "in" / "deck.pptx")
        try:
            res: ContainerResult = run_container(
                ["python3", "/opt/runner/render.py", "in/deck.pptx", "out",
                 ",".join(map(str, pages)), str(dpi)],
                work, timeout_s=s.render_timeout_s, memory=s.sandbox_memory,
                cpus=s.sandbox_cpus, pids=s.sandbox_pids, network="none", s=s,
            )
        except SandboxUnavailable as e:
            return RenderResult(backend="docker", skipped=str(e))
        result_file = work / "out" / "result.json"
        info = json.loads(result_file.read_text()) if result_file.exists() else {}
        if not res.ok or not info.get("ok"):
            why = info.get("error") or ("timed out" if res.timed_out else f"exit {res.exit_code}")
            detail = info.get("stderr") or res.stderr[-600:]
            return RenderResult(backend="docker", seconds=res.seconds,
                                skipped=f"rendering failed in the sandbox: {why} {detail}".strip())
        out: list[Path] = []
        for name in info["previews"]:
            shutil.copy(work / "out" / name, outdir / name)
            out.append(outdir / name)
        pdf = None
        if info.get("pdf") and (work / "out" / info["pdf"]).exists():
            shutil.copy(work / "out" / info["pdf"], outdir / info["pdf"])
            pdf = outdir / info["pdf"]
        return RenderResult(backend="docker", previews=out, pdf=pdf, seconds=res.seconds)
