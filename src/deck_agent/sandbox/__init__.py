"""The execution environment: throwaway containers.

This package is the *only* place in the project that starts an external process. It exists
for two jobs, and it is worth being precise about why each one is here, because they are
different kinds of reason:

1. **Code a model wrote** (`code.py`) - the escape hatch. When the deterministic loop cannot
   express a slide, the model writes python-pptx code and we run it. This is a genuine
   **trust boundary**: the code is not reviewed by anyone before it runs. No network, read-only
   root filesystem, no capabilities, a small memory cap, a few seconds of CPU, one container
   per call, destroyed afterwards.

2. **Rendering previews** (`render.py`) - LibreOffice and poppler. This is a **dependency
   boundary**, not a trust boundary: a ~1 GB native toolchain plus metric-compatible fonts
   that we would otherwise make a prerequisite for running the agent at all. It is also true
   that LibreOffice is the one component that will happily fetch remote content out of a
   hostile deck, so keeping it off the network is a bonus rather than the point. Set
   `render_backend=local` to use a locally installed `soffice` instead and the container is
   then used only for job 1.

Everything else - probing, compiling, applying edits, the byte-preserving save, overflow and
overlap checks - is pure Python and runs in the agent process. See docs/02-architecture.md.
"""
from __future__ import annotations

from .docker import ContainerResult, SandboxUnavailable, available, run_container  # noqa: F401
